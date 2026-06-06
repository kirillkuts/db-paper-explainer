# Pass 1 — Data Model & Architecture

> **Goal:** after reading, you can take a stock-Postgres schema, decide which table becomes **sharded / reference / standard**, write the actual `SET` syntax, explain *why a customer's orders land on the same shard as the customer*, and draw the router/shard split sitting on top of Aurora's 6-way storage.
> **Reading time:** ~18 minutes.
> **Method:** same ladder as Pass 0. Each rung is the simplest thing that **fails**; the next rung fixes that pain. We name the pain *before* the fix.
> **Scope guard:** this pass is structural only. *No* timestamp / MVCC / 2PC mechanics — Pass 0 Rung 5–7 already previewed those, and Pass 2+ derives them. When a consistency question comes up here, we point at it and move on.

Vocabulary carried from Pass 0: **router** (front door, no data), **shard** (hash-partitioned data + execution), **shard group** (the whole router+shard fleet), **single-shard sweet spot** (the cheap path that skips 2PC).

---

## Rung 1 — App-level sharding, and the two specific things it forces on *you*

Pass 0 Rung 2 said naive sharding "shoves the hard parts into application code." Let's make that concrete, because Aurora's whole data model is the answer to exactly these two pains.

You run 4 Postgres databases and shard `customers` by `hash(cust_id) % 4`.

```
app code, on every single query, must compute:
        shard = hash(cust_id) % 4
        open connection to that shard
        run the SQL there
```

**Pain 1 — the app is now the query router.** Every read and write must first answer "which box?" That logic leaks into ORMs, into reporting jobs, into migrations. Change the shard count from 4 to 8 and `% 4` becomes `% 8` *everywhere*, and the rows must physically move to match. The routing rule is hard-coded into application logic instead of living in the database.

**Pain 2 — there is no cross-shard transaction.** Customer 7 (shard 3) places an order, and the order row must also live somewhere. If `orders` is sharded by its own `order_id`, that order might hash to shard 1 — a *different box* from its customer. Now "insert order + decrement customer credit" spans two machines, and stock Postgres `BEGIN/COMMIT` cannot wrap two machines. You're back to the atomicity loss from Pass 0.

**Aurora's goal, stated plainly:** *transparent sharding*. The app connects to **one** endpoint, writes ordinary SQL with no `% 4` anywhere, and the system decides placement and keeps transactions ACID. To deliver that, Aurora needs the app to declare **what kind of table** each table is — so it can place rows intelligently. That declaration is Rung 2.

> **Real-world hook:** this is the same itch that Citus, Vitess, and CockroachDB scratch. Aurora Limitless's distinctive bet is doing it *inside* a PostgreSQL-compatible engine on Aurora storage, so existing Postgres apps move with minimal rewrite.

---

## Rung 2 — Three table types: telling the system what data goes where

The app can't route transparently unless the system knows the *shape* of each table's access pattern. Aurora gives you exactly three table types. We'll build them on the paper's Figure 1 toy schema:

```
customers (cust_id PK, name, region)          ← big, per-customer, the natural shard axis
orders    (order_id PK, cust_id FK, amount)   ← big, always queried per-customer
tax_rates (region PK, rate)                    ← tiny, rarely changes, joined constantly
```

### Type A — Sharded table: hash-partitioned on a key *you* choose

A **sharded table** is split across all shards by hashing a column you nominate, the **shard key**. This is the type that actually buys you horizontal scale (big tables spread their rows + write load across shards).

```sql
SET limitless_create_table_mode = 'sharded';
SET limitless_create_table_shard_key = '{cust_id}';
CREATE TABLE customers (cust_id int PRIMARY KEY, name text, region text);
```

```
hash(cust_id) decides the home shard. To make this checkable, use a TOY
hash for these 4 shards: toy_hash(k) = k mod 4 (the real one is a
production hash, not mod — this is just so you can verify the placement):

   cust_id=7  → 7 mod 4 = 3 → shard 3      cust_id=42 → 42 mod 4 = 2 → shard 2
   cust_id=9  → 9 mod 4 = 1 → shard 1      cust_id=88 → 88 mod 4 = 0 → shard 0
```

Why **hash** partitioning and not **range** (e.g. cust_id 1–1000 on shard 0)? Range partitioning creates hotspots: the newest customers (highest ids) all land on the last shard, so the shard taking all the fresh inserts becomes a bottleneck. Hashing spreads inserts evenly by construction. (Range has its uses for ordered scans; OLTP's many-small-writes profile prefers the even spread.)

### Type B — Reference table: one full copy on *every* shard

`tax_rates` is tiny, almost never changes, and gets joined to nearly every order. If we sharded it, a join would constantly have to reach across shards to fetch the matching rate. Instead, **replicate it everywhere**.

```sql
SET limitless_create_table_mode = 'reference';
CREATE TABLE tax_rates (region text PRIMARY KEY, rate numeric);
```

```
shard 0          shard 1          shard 2          shard 3
[tax_rates]      [tax_rates]      [tax_rates]      [tax_rates]   ← identical full copy on each
```

Now any shard can join orders→tax_rates **locally**, no cross-shard hop. The cost: a write to `tax_rates` must update every shard's copy, so this only pays off when reads vastly outnumber writes. That's why the rule of thumb is *small, rarely-changed, frequently-joined* — tax rates, currency codes, country lists, feature flags.

> **Disambiguation:** "reference table" here is a *placement choice* (replicate to all shards), not a foreign-key reference. The `orders.cust_id → customers.cust_id` foreign key is a different concept; don't conflate them.

### Type C — Standard table: lives on one shard, the easy on-ramp

A **standard table** isn't distributed at all — it sits whole on a single shard, behaving exactly like a stock-Postgres table.

```sql
SET limitless_create_table_mode = 'standard';
CREATE TABLE audit_log (id bigserial PRIMARY KEY, msg text);
```

Why have this at all, if the point is to scale out? Because it's the **import path**. When you migrate a stock Postgres database in, most tables start as `standard` — no shard-key decision required — and you promote the big ones to `sharded` later. It lets you adopt Limitless without redesigning every table on day one. The trade-off: a standard table gets none of the scale-out; it's capped at one shard's capacity, so you only leave low-traffic tables here.

### The key move: co-location

We still have Pain 2 from Rung 1 — an order on a *different* shard from its customer forces cross-shard transactions. Fix: tell `orders` to follow `customers`.

```sql
SET limitless_create_table_mode = 'sharded';
SET limitless_create_table_shard_key = '{cust_id}';
SET limitless_create_table_collocate_with = 'customers';
CREATE TABLE orders (order_id int PRIMARY KEY, cust_id int, amount numeric);
```

`collocate_with` says: *hash `orders.cust_id` with the same function used for `customers.cust_id`, so identical key values land on the same shard.* The mechanism is just determinism: placement is `hash(key) → slice → shard`, a fixed pipeline. If two rows feed the *same* key value through the *same* hash function, they hit the same slice, and a slice lives on exactly one shard — so they unavoidably co-reside. (Without `collocate_with`, `orders` could be hashed by a different function or key and miss.)

```
cust_id=7 → shard 3:
   shard 3 holds  customers row for 7
   shard 3 holds  ALL of customer 7's orders

→ "insert order + update customer 7" touches ONE shard → single-shard txn → skips 2PC
→ "list customer 7's orders, joined to their record" → single-shard join, no cross-shard hop
```

This is the data-model lever for the **single-shard sweet spot** from Pass 0 Rung 7. Co-locate the rows a transaction touches together, and that transaction stays on the cheap path. Pick a bad shard key (e.g. shard `orders` by `order_id` instead of co-locating on `cust_id`) and your common transactions go multi-shard and pay the 2PC tax. **The shard key is the single most consequential schema decision in the whole system.**

> **Real-world hook:** in a TPC-C-style workload (the HammerDB benchmark from §8), a "new order" transaction touches one warehouse's customer, orders, and order-lines. Co-locating all of those on the warehouse key keeps the hot path single-shard — which is exactly why the benchmark scales near-linearly with added shards.

---

## Rung 3 — Who actually holds what: the router/shard role split, in detail

Pass 0 sketched routers vs shards. Now the precise division of labor, because the *metadata* placement is what makes transparent routing work.

```
        app  ── one DNS endpoint ──┐
                                    ▼
   ┌──────────────────────────────────────────────┐
   │  ROUTERS  (each is itself a Postgres cluster)  │
   │   • terminate ALL client connections           │
   │   • parse + plan SQL                            │
   │   • hold AUTHORITATIVE metadata:                │
   │       - topology (which shards exist)           │
   │       - schema (table types, shard keys)        │
   │       - placement map: slice → shard            │
   │   • hold NO user table data                     │
   └──────────────────────────────────────────────┘
            │            │            │
            ▼            ▼            ▼
   ┌──────────┐  ┌──────────┐  ┌──────────┐
   │ SHARD 0  │  │ SHARD 1  │  │ SHARD 2  │   each a Postgres cluster
   │ hash-part│  │ hash-part│  │ hash-part│   • own hash-partitioned data
   │  data    │  │  data    │  │  data    │   • run the plan fragments
   │ + exec   │  │ + exec   │  │ + exec   │     the router hands them
   └──────────┘  └──────────┘  └──────────┘
```

**Shards** own the hash-partitioned data and execute plan fragments. That's it — they're workers that happen to hold a slice of every sharded table plus a full copy of every reference table.

**Routers** own everything *about* the data without owning the data:
- **Topology** — the current set of shards.
- **Schema** — every table's type and shard key, so the router knows how to route a given SQL statement.
- **Placement map** — which key ranges (we'll call the unit a *slice*, deepened in Pass 4) live on which shard. This is the lookup that replaces your app's `hash(cust_id) % 4`. The router computes the hash, consults the map, and sends the fragment to the right shard.

This is why routers are the authoritative metadata holders: routing transparently *is* knowing the placement map, and the app must hit a router that knows it. Shards don't need the global map — each just owns its own slice.

The whole set — routers + shards — is the **shard group** (vocabulary from Pass 0).

> Why keep metadata on routers and not on every shard (peer-to-peer)? Pass 0 Rung 3 answered the throughput half (connections vs data scale at different rates). The metadata half: a single authoritative copy of the placement map is far easier to keep correct during re-sharding than a copy gossiped across every shard. Routers are few and uniform; shards are many and busy. (How the map changes during shard-splits is Pass 4.)

> **Real-world hook:** because routers are stateless w.r.t. data, a connection-heavy app (tens of thousands of connections — Pass 0 Rung 1's third wall) scales by adding routers, with zero data movement.

---

## Rung 4 — What all of this stands on: Aurora distributed storage

Here's a claim that should bother you: a shard is "a Postgres cluster that owns data." If that shard's machine dies, is the data gone? It isn't — and the reason is that **neither routers nor shards store data on local disk in the normal sense.** Each one is a Postgres compute node sitting on top of an **Aurora storage volume**, and Aurora storage is a separate, replicated, fault-tolerant service. Treat it as a durability black box the rest of the system stands on. (Full failover/recovery mechanics are Pass 6; here we only need *why it's durable*.)

```
   shard 0 compute (Postgres)         ← runs queries, holds no durable data itself
        │  writes go DOWN as a log
        ▼
   ┌─────────────────────────────────────────────────────┐
   │  Aurora storage volume for shard 0                    │
   │  split into SEGMENTS; each write replicated 6 ways:   │
   │                                                       │
   │     AZ-a            AZ-b            AZ-c               │
   │   [copy][copy]    [copy][copy]    [copy][copy]        │
   │     2 copies        2 copies        2 copies          │
   └─────────────────────────────────────────────────────┘
```

Every write is replicated to **6 storage nodes**, placed as **2 per Availability Zone across 3 AZs**. (An **AZ** is an isolated datacenter within a region — Pass 0 defined it.) These specific numbers are not arbitrary; each one is a deliberate fault-tolerance budget:

- **3 AZs**, because surviving the loss of an *entire datacenter* requires copies in at least 3 independent failure domains. With only 2 AZs, losing one leaves a single AZ and no margin for a second fault.
- **6 copies total, 2 per AZ.** The design target this Limitless paper (§3.1) states is: **survive a full AZ failure *plus* one more node failure.** Walk it: lose one AZ → 2 copies gone, 4 remain. Then lose one more node → 3 remain. Six copies at 2-per-AZ is the smallest layout that still leaves copies standing after "whole AZ then one more"; fewer copies, or fewer-than-2-per-AZ, can't absorb that combined `AZ + 1` loss. More than 6 would cost write latency and storage for tolerance the SLA doesn't need.

> The exact quorum *sizes* — how many copies a write or read must touch — are **not stated in this Limitless paper's §3.1**, which gives only the layout above (6 copies / 2 per AZ / 3 AZs / survives AZ + 1). The original Aurora paper [36,37] sizes this as a 4-of-6 write / 3-of-6 read quorum; this Limitless paper doesn't restate that math, so we defer it (and *why* those numbers) to **Pass 6**. Do not read "survives AZ + 1" as something from which you can derive 4-of-6 here.

The key consequence for *this* pass: because durability lives in the storage layer, **compute and storage are decoupled.** A shard's Postgres process is, in a sense, disposable — kill it and a replacement can attach to the same durable volume. Routers and shards are "just compute"; the data survives them. That decoupling is exactly what makes Rung 5's high-availability story cheap.

> The quorum *sizes* and segment/protection-group structure (and why this also speeds recovery) are **Pass 6**. Here it's a black box with the shape this paper actually states: *6 copies, 2 per AZ, 3 AZs, surviving AZ + 1.*

> **Real-world hook:** this 6-way/3-AZ storage is the *same* foundation under ordinary single-node Aurora — Limitless inherits it rather than inventing new durability. Each router and each shard simply gets its own such volume.

---

## Rung 5 — Keeping compute alive: routers vs shards need different safety nets

Storage survives failures (Rung 4). But the **compute** nodes — routers and shards — can still crash. Here the design makes an asymmetric choice that's worth understanding, because it's a direct cost-vs-availability trade.

### Routers: identical, behind one DNS endpoint, no dedicated standby

All routers are interchangeable: they hold no data, just metadata copies plus connections. So Aurora puts them behind **one DNS endpoint** and treats them as a pool.

```
                 app
                  │
            DNS endpoint
          ┌───┬───┬───┐
          ▼   ▼   ▼   ▼
        rtr rtr rtr rtr     ← all identical; lose one, DNS reroutes to a survivor
```

If a router dies, there is **no special standby to promote** — DNS simply stops routing new connections to the dead one and sends them to a surviving router, which already has the metadata.

The obvious wrinkle: **DNS reroute only governs *new* connections.** Connections already open (or in-flight queries) on the dead router *drop* — the client gets an error and must reconnect, at which point DNS hands it a survivor. So "lose a router" is not invisible to clients holding a connection to that exact router; it's a reconnect, not a seamless handoff. The recovery window and exactly what the client observes is **Pass 6**; here just note the boundary: DNS heals new traffic, not connections mid-flight.

Why no dedicated standby? Because a standby router would sit idle holding nothing unique (no data to be the sole custodian of), so paying for it buys almost nothing. **Cost saving:** every router is doing real work; none is a parked spare.

### Shards: 0–2 standbys, customer-controlled

A shard is different. Yes, its *data* is safe in Aurora storage (Rung 4). But the **compute** that serves that data is a specific Postgres process owning a specific slice. If that process dies, that slice is **unavailable to serve queries** until a replacement is provisioned and attaches to the volume — which takes time. No other live shard can answer for it, because no other shard holds that slice's compute.

So Aurora lets you attach **standbys** to a shard — warm Postgres replicas in *other* AZs, ready to take over its slice immediately on failure:

```
   shard 0 primary  (AZ-a)
        ├── standby  (AZ-b)   ← promotable in seconds on primary failure
        └── standby  (AZ-c)
```

The count is **0 to 2**, and *you* choose per cost/availability appetite:
- **0 standbys** — cheapest; on failure you wait for a fresh shard to be provisioned and warmed (slice unavailable meanwhile). Fine for non-critical or easily-retried workloads.
- **1 standby** — one warm replica in another AZ; survives the primary's AZ failing.
- **2 standbys** — placed in the two *other* AZs (so all 3 AZs covered), the maximum because 3 AZs total means at most 2 *other* AZs to put a cross-AZ standby in. A third standby would have to share an AZ with an existing copy, adding cost without adding an independent failure domain.

### Why the asymmetry

```
                  holds unique state?      failure impact            safety net
   ROUTER         no (metadata is a copy)  reroute, no data lost     none needed → DNS reroute
   SHARD          yes (sole compute for    that slice can't be       0–2 standbys, customer-chosen
                  its data slice)          served until replaced
```

A router is fungible, so the cheapest safe design is "many identical, DNS picks a live one." A shard is *not* fungible — it is the only thing currently serving its slice — so it needs a promotable understudy if you want fast failover. The standby protects **availability of compute**, not durability of data (storage already handles durability). That clean separation — storage handles durability, standbys handle compute availability — is the payoff of the Rung 4 decoupling.

> **Real-world hook:** an app with a strict latency SLA runs 2 standbys per shard so a node failure is a sub-minute blip; a batch-analytics tenant might run 0 standbys to halve compute cost and tolerate a brief re-provision. Same architecture, dialed per workload.

---

## What this pass nailed down

```
data model:   sharded (hash on your shard key) ── scale-out, the big tables
              reference (full copy per shard)   ── small, hot-join, rarely-written
              standard  (one shard)             ── import on-ramp, low traffic
              + co-location → single-shard sweet spot

architecture: routers  = connections + planning + AUTHORITATIVE metadata, no data
              shards   = hash-partitioned data + execution
              both     = Postgres compute on Aurora storage (6 copies / 2-per-AZ / 3 AZs)
              HA       = routers: DNS reroute, no standby (fungible)
                         shards:  0–2 standbys (not fungible; protects compute availability)
```

What we deliberately did **not** touch (and where it lives): how a multi-shard transaction stays atomic (Pass 3, 2PC), how a reader sees a consistent cross-shard cut (Pass 2–3, time-based MVCC), how slices move when you add shards (Pass 4), the quorum math behind 6/3 (Pass 6).

---

## The 3 checkpoint questions

Answer in your own words. They tell me what to reinforce in Pass 2.

1. **You're migrating a stock-Postgres e-commerce DB.** For each of `customers`, `orders`, `line_items`, `country_codes`, `migration_audit_log`, pick a table type (sharded / reference / standard) and, for the sharded ones, the shard key + any `collocate_with`. Justify each choice by the access pattern, and say which choice keeps the checkout transaction single-shard.

2. **Routers hold no user data but are called the "authoritative" nodes.** Authoritative over *what*, and why does that specific state have to live on the router rather than on the shards — what would routing transparently be impossible without?

3. **A shard's data is replicated 6 ways in Aurora storage, yet the shard still gets 0–2 *standbys*.** If the data is already durable, what exactly does a standby protect that storage replication does *not*? Contrast with why a router needs no standby at all.

**Also flag:**
- Any rung where the **pain** didn't feel concrete — especially Rung 1 (did "no cross-shard transaction" feel real *before* co-location fixed it?).
- Any term you'd struggle to define unaided: **shard key, reference table (vs. FK reference), co-location, standard table, placement map / slice, shard group, AZ, standby (compute availability) vs. storage replication (durability).**
- Whether the **6 / 2-per-AZ / 3-AZ** justification (survive AZ + 1) landed as a real budget or felt like memorized trivia — it's the seed of Pass 6, so I want to know if the "why these numbers" clicked.
