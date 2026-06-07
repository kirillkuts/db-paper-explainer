# Pass 0 — The Map

> **Goal:** after reading, you can explain Aurora PostgreSQL Limitless in 3 sentences, and you know what each later pass will deepen.
> **Reading time:** ~12 minutes.
> **Method:** we build the design one rung at a time. Each rung is the simplest thing that **fails**. The next rung exists to fix that failure. This is a map, not a deep-dive — when something deserves its own pass, we flag it and move on.

---

## Rung 1 — One Postgres box, and the wall it hits

Stock PostgreSQL is **single-primary**. Exactly one node accepts writes. Read replicas can fan out reads, but every `INSERT`/`UPDATE`/`DELETE` funnels through that one primary.

```
                writes + reads
                      │
                      ▼
              ┌───────────────┐
              │   PRIMARY     │   ← the only writer
              └───────────────┘
               │     │     │
               ▼     ▼     ▼
            replica replica replica   ← read-only copies
```

For most apps this is fine. It stops being fine the day you outgrow **one machine** — and there are three different ways to outgrow it:

- **Write throughput** — the primary's CPU/IO is a single ceiling. You can buy a bigger box (vertical scaling), but the biggest box is still one box.
- **Storage** — one volume, one filesystem. Tens of terabytes is a stretch; hundreds is painful.
- **Connections** — each Postgres connection costs a backend process and memory. A primary chokes well before tens of thousands of live connections.

**The pain:** every axis bottoms out at "the size of one machine." OLTP workloads that grow past that have nowhere to go.

> **OLTP** = Online Transaction Processing: many small, fast, concurrent read+write transactions (think checkout, payments, inventory). Contrast with OLAP/analytics, which runs few huge scans. Aurora Limitless targets OLTP.

**The fix everyone reaches for first:** split the data across many machines — *shard* it.

---

## Rung 2 — Naive app-level sharding, and what it quietly breaks

The obvious move: pick a key (say `customer_id`), run N independent Postgres databases, and route each row to a shard by hashing the key.

```
app code computes:  shard = hash(customer_id) % 4

   shard 0        shard 1        shard 2        shard 3
 ┌─────────┐    ┌─────────┐    ┌─────────┐    ┌─────────┐
 │ Postgres│    │ Postgres│    │ Postgres│    │ Postgres│
 └─────────┘    └─────────┘    └─────────┘    └─────────┘
```

Write throughput scales. Storage scales. Connections spread out. So what's wrong?

Here's the catch. **The application now owns the hard parts of a database.** Picture three concrete failures:

1. **Cross-shard transactions lose atomicity.** Move money from a customer on shard 0 to one on shard 2. That's two separate Postgres transactions on two separate machines. If shard 0 commits and shard 2 crashes, money vanishes. Stock Postgres `BEGIN/COMMIT` only spans one node.

2. **Cross-shard reads lose a consistent snapshot.** A report summing balances across all four shards reads shard 0, then shard 1, then 2, then 3. While you're walking that line, other transactions commit behind you. The total you hand back **never existed at any single instant** — it's a photo stitched from four different moments. That's a torn read.

3. **The schema is frozen.** Adding a shard means re-hashing and physically moving rows, by hand, while the app keeps running. Re-sharding live is a project, not a config change.

**The pain:** naive sharding gives you scale by **throwing away transactions and consistency** — the two things you bought a relational database for. And it shoves all the routing, rebalancing, and failure handling into application code.

**The fix:** push sharding *below* the SQL surface, so the app still sees **one** PostgreSQL database with real ACID transactions, while the system handles distribution. That's the whole ambition of Aurora Limitless.

> **ACID** = Atomicity (all-or-nothing), Consistency, Isolation (concurrent txns don't corrupt each other), Durability. The naive shard design breaks A and I across shards. Keeping all four *across machines* is the entire challenge.

---

## Rung 3 — Split the roles: routers vs. shards

Think of a busy restaurant. The waiters take your order and run between tables; the cooks own the kitchen and do the actual work. Nobody asks a waiter to also fry the steak. Aurora Limitless's first structural idea is exactly that division of labor: separate the node that **talks to clients and plans queries** from the node that **owns data and executes**.

```
        clients (think they're talking to one Postgres)
          │        │        │
          ▼        ▼        ▼
     ┌─────────────────────────────┐
     │      ROUTERS                 │   own NO table data.
     │  - terminate connections     │   accept SQL, parse, plan,
     │  - parse + plan queries      │   decide which shards to hit,
     │  - coordinate transactions   │   stitch results back together.
     └─────────────────────────────┘
          │        │        │
          ▼        ▼        ▼
     ┌────────┐ ┌────────┐ ┌────────┐
     │ SHARD 0│ │ SHARD 1│ │ SHARD 2│   own a hash-partitioned
     │        │ │        │ │        │   slice of the data.
     │ runs   │ │ runs   │ │ runs   │   run the plan fragments
     │ plan   │ │ plan   │ │ plan   │   the router sends them.
     │ frags  │ │ frags  │ │ frags  │
     └────────┘ └────────┘ └────────┘
```

- **Routers** are stateless-ish front doors. They hold connections, plan, and coordinate. Because they hold no data, you can add routers to absorb more **connections and planning load** independently.
- **Shards** each own a **hash-partitioned** subset of every distributed table's rows, and execute the query fragments routed to them. Add shards to absorb more **data and execution load**.

This directly answers Rung 1's three axes: connections scale with routers, storage+throughput scale with shards. And it answers Rung 2's "app owns routing" complaint — the router does the hashing and stitching, not your code.

> Why two tiers instead of "every node does everything" (peer-to-peer, like some NoSQL systems)? Because connection-heavy OLTP and data-heavy execution scale at *different rates*. Splitting them lets you grow each independently. The exact data model (how tables are partitioned, co-location, reference tables) is **Pass 1**.

**The pain that's now exposed:** we have many independent Postgres-derived shards. The moment a transaction touches two of them, we're *right back* at Rung 2's atomicity and snapshot problems — only now they're hiding inside the system instead of in the app. Splitting roles didn't solve consistency. It just moved the problem somewhere we can't see it.

---

## Rung 4 — The real enemy: cross-shard consistency

So how does stock Postgres decide what a transaction can see, and why does that mechanism break across shards?

**Stock Postgres uses a set of transaction IDs.** Each transaction gets an integer **xid**. Every row stores `xmin` (the xid that created it) and `xmax` (the xid that deleted it). When a transaction starts, it takes a **snapshot**: essentially a list of which xids were still in-flight at that moment (the `xip_list`). Visibility = "was the row's `xmin` committed and *not* in my in-flight list?"

```
stock Postgres snapshot = { list of in-flight xids }   ← a SET, lives on one node
```

Now notice where that set lives. It's **local to one primary**. There's no shared, agreed-upon copy across four independent machines. Want to ask "is xid 5012 still in-flight?" across the whole cluster? You can't — not without one central node handing out every xid and tracking them all. And that node is exactly the bottleneck and single point of failure we set out to escape.

> Disambiguation: "snapshot" here means *the visibility set a transaction reads against*, not a storage backup. We'll keep these separate throughout.

**The pain:** xid-set snapshots don't compose across machines. We need a notion of "what's visible" that every shard can evaluate **independently**, with no shared in-flight set and no central oracle.

**The fix (the load-bearing idea of the whole paper):** replace the *set* of xids with a single *number* — a **timestamp**.

---

## Rung 5 — Time-based MVCC: a scalar replaces the set

Give every transaction two timestamps drawn from a physical clock:

- `startTs` — when the transaction began (defines what it can *see*).
- `commitTs` — when the transaction committed (stamps what it *wrote*).

Then visibility collapses to **one comparison**, the invariant the rest of the system is built on:

> **A write is visible to a reader if and only if `write.commitTs <= reader.startTs`.**
> (The paper calls this Property 1. We will re-derive and stress-test it in later passes — for now, just hold it.)

```
toy example (small integer timestamps):

  reader.startTs = 100

  row A written by txn with commitTs = 60   →  60 <= 100  →  VISIBLE
  row B written by txn with commitTs = 140  → 140 <= 100  →  NOT visible (committed in reader's future)
```

Why is this the unlock? Because a timestamp is a **scalar** — one number. Every shard can evaluate `commitTs <= startTs` **on its own**: no coordination, no shared in-flight list, no central xid dispenser. The snapshot is no longer a set you have to agree on; it's just a number you carry to each shard and hand over at the door.

But a number across machines raises the obvious objection — and you're probably already forming it. **Whose clock?** Four shards, four clocks, none of them perfectly in sync. If shard 2's clock runs 5 time-units fast, its commit timestamps look like they're "in the future," and the comparison quietly lies.

Aurora's answer is **Amazon Time Sync**, a hardware-assisted clock service that bounds how far any two nodes' clocks can drift apart. That bound has a name: the **CEB** (ClockErrorBound) — the most the local clock might be off from true time.

```
real clock isn't a point, it's an interval:

   true time somewhere in here
   ├──────────────●──────────────┤
   now - CEB    now (local)    now + CEB
```

> Toy numbers first: imagine `CEB = 5` (in our small-integer time units). Real numbers: the paper reports CEB **under 1 millisecond**, microseconds in some regions — but treat exact figures as "verify against §5.1." The point is CEB is *small and bounded*, not zero.

**Alternatives the paper explicitly rejects** (flagged here, examined later):
- *A central timestamp oracle* (one node hands out all timestamps, as in some MPP systems) — reintroduces a bottleneck and single point of failure. Rejected.
- *Just wait until clocks catch up before every read* (Clock-SI style) — adds latency to the common path. Aurora takes a more surgical approach.

The full derivation — how CEB makes the `commitTs <= startTs` comparison safe despite skew, write-conflict detection, and a thing called **commit-wait** for real-time ordering — is **Pass 2**.

**The pain that remains:** timestamps tell each shard *what to show*. They don't yet make a multi-shard *write* atomic. If a transaction writes shard 0 and shard 2, both must commit or neither — and they must agree on a single `commitTs`.

---

## Rung 6 — Atomic multi-shard commit: time-aware 2PC

The classic tool for "all-or-nothing across machines" is **two-phase commit (2PC)**:

```
PHASE 1 (prepare):  coordinator → all shards: "can you commit?"
                    each shard:  flush, lock, reply "yes, prepared"
PHASE 2 (commit):   coordinator → all shards: "commit now"
```

Classic 2PC has one notorious flaw. If the coordinator dies between the two phases, the prepared shards are stuck **blocked** — holding their locks, unable to commit, unable to abort — until the coordinator wakes back up. For OLTP, that kind of stall is a non-starter.

Aurora's twist is **time-aware, lead-shard 2PC**, which folds the timestamp scheme into 2PC to make the protocol **non-blocking**. Two ideas do the work. First, one of the participating shards plays **lead shard** — so the coordinator role lives *with the data*, not in a separate, fragile box that can vanish. Second, the shared `commitTs` from Time Sync gives every participant the same unambiguous commit point. Put those together and a shard that loses contact doesn't have to block: it can read its own durable state plus the agreed timestamp and work out the outcome by itself. (*Why not 3PC, or a Raft/Paxos-replicated coordinator like Spanner? — flagged here, contrasted in Pass 3.*)

```
multi-shard write:
   router → lead shard 0  (also a participant)
   lead shard 0 ⇄ shard 2   prepare
   pick a single commitTs (clock-derived, agreed)
   commit both at that commitTs  →  atomic + visible together per Property 1
```

The mechanics of *reading* from shards under this scheme — how a reader picks `startTs`, how it sees a consistent cut across all shards — is the other half of the same pass. **Pass 3.**

---

## Rung 7 — The sweet spot: most transactions skip 2PC entirely

Here is the performance thesis, and it is worth tattooing on the inside of your eyelids:

> **Read-only transactions and single-shard transactions skip 2PC completely.**

- **Read-only**: it only needs a `startTs`. It reads each shard with `commitTs <= startTs`. No prepare, no commit phase, no coordinator. Just a number and a comparison.
- **Single-shard write**: everything lives on one shard, so that one shard commits locally — exactly like stock Postgres. No distributed protocol at all.

Only **multi-shard writes** pay the 2PC cost. So the whole system is tuned around one bet: the *common* OLTP transaction touches one customer on one shard, so let it run at near-single-node speed. Make only the genuinely-distributed transaction pay for distribution.

```
transaction type            distributed protocol?    cost
─────────────────────────   ──────────────────────   ──────────────
read-only (any # shards)    none (just startTs)      cheap
single-shard write          local commit only        cheap (≈ stock PG)
multi-shard write           time-aware 2PC           the only expensive path
```

This is why the data model (Pass 1) cares so much about **co-locating** related rows on the same shard: keep transactions single-shard and you stay on the cheap path.

**Real-world hook:** the paper's evaluation runs HammerDB (a TPC-C-style OLTP benchmark) and reports throughput in **NOPM** (New Orders Per Minute). The headline result is near-linear scaling as shards are added — *because* the workload is mostly single-shard once data is co-located well. Exact NOPM figures: **verify against §8** when we get there.

---

## The Aurora Limitless thesis, in three sentences

> 1. Aurora Limitless turns single-primary PostgreSQL into a horizontally scaled, strongly consistent OLTP database by splitting **routers** (connections + planning, no data) from **shards** (hash-partitioned data + execution), all on Aurora's distributed storage.
> 2. It replaces stock Postgres's xid-set snapshots with **time-based MVCC** — a single scalar `commitTs`/`startTs` from Amazon Time Sync, where a write is visible iff `commitTs <= startTs` — so every shard can judge visibility independently with no central coordinator.
> 3. Multi-shard writes stay atomic via **non-blocking, lead-shard, time-aware 2PC**, while read-only and single-shard transactions skip 2PC entirely — which is the performance sweet spot the whole design optimizes for.

---

## One thing we skipped: Aurora's storage

Both routers and shards sit on top of **Aurora's storage layer**, which replicates data **6 ways across 3 Availability Zones** (a quorum design that survives losing an entire AZ plus one more copy). Treat this as a black box for now: "durable, fault-tolerant, log-structured storage shared under each node." Why 6 and 3, and how that enables fast failover, is **Pass 6**.

> **AZ** = Availability Zone: an isolated datacenter within an AWS region. 3 AZs means the system tolerates one datacenter dying.

---

## The rungs each later pass climbs

| Pass | Rung it deepens | Paper § |
|---|---|---|
| **Pass 1** | Data model & architecture — hash partitioning, co-location, reference tables, routers vs. shards in detail, Aurora storage | §2, §3 |
| **Pass 2** | Time-based MVCC — Amazon Time Sync, CEB, deriving Property 1, write-conflict detection, commit-wait for real-time order | §5.1, §5.2, §5.3, §5.6 |
| **Pass 3** | Time-aware 2PC + reading from shards — lead-shard non-blocking commit, how readers pick startTs and see a consistent cut | §5.4, §5.5 |
| **Pass 4** | Adaptive scaling — vertical (ACU), horizontal shard-splits, table-slices | §4 |
| **Pass 5** | Query processing & pushdown — FDW, plan fragments, distributed joins | §7 |
| **Pass 6** | Failover, backups, recovery — the 6-way/3-AZ storage and HA | §3.4, §6 |

> Numbers you'll see defined in later passes (flagged now so they don't ambush you): **CEB** (clock error bound), **ACU** (Aurora Capacity Unit — the vertical-scaling capacity metric), **table-slice** counts, **NOPM** (benchmark throughput). Each gets justified where it's introduced.

---

## The 3 checkpoint questions

Answer in your own words. They tell me what to reinforce in Pass 1.

1. **Why does stock Postgres's snapshot mechanism (a set of in-flight xids) fail across independent shards, and what single property does Aurora use instead?** State the visibility rule.

2. **A transaction reads balances from all 4 shards. Another transaction commits a transfer between two of them midway through. Walk through how time-based MVCC stops the reader from seeing a torn (never-existed) total — using `startTs` and `commitTs`.**

3. **Which transaction types skip 2PC, and why is that the central performance argument of the whole system? What does the data model have to do to keep transactions on that cheap path?**

Also flag:
- Any rung where the **pain** didn't feel concrete — i.e. you couldn't picture *why* the previous design hurt before the fix arrived.
- Any term you'd struggle to define without re-reading: **OLTP, ACID, xid/xmin/xmax, snapshot (visibility vs. backup), CEB, 2PC / lead shard, AZ.**
- Whether the clock-skew objection (Rung 5) felt like a real problem or hand-waved — that's the spine of Pass 2, so I want to know if it landed.
