# Pass 5 — Query processing: pushdown, foreign tables, joins

> **Goal:** after reading, you can explain why a router (which holds **no data** — Pass 1) must *push compute down* to shards instead of pulling rows up; how the router reuses two stock-Postgres mechanisms (**partitioned tables** + a **Foreign Data Wrapper**) so the unmodified planner "just works"; read a distributed `EXPLAIN` plan tree and say which fragment runs where; classify a `WHERE` predicate as **foreign** (push it) vs **local** (pull data up); say which **joins** push down and — the subtle one — exactly which **outer/anti-joins do NOT**; and restate the single-shard sweet spot as the whole performance thesis it has been all along.
> **Reading time:** ~25 minutes.
> **Method:** the usual ladder. Each rung is the simplest design that **fails**; the next rung names the pain and fixes it.
> **Scope guard:** this is the **query-processing** pass — §7. We cover how a router turns one SQL statement into shard work while moving as little data as possible. We do **not** re-open concurrency control: Pass 2–3 already gave us snapshots and `commitTs`. Assume every shard read in this pass simply uses the transaction's `startTs` to pick its visible snapshot (Pass 2's Property 1: a write is visible iff `write.commitTs <= reader.startTs`). We never re-derive that here; we just lean on "each shard reads as of `startTs`."

**Carried from Pass 1 (restated when first used, not re-derived):**
- **router** — front door: connections + planning + authoritative placement map, **holds no user data**. This pass is *entirely* about the consequence of that one fact.
- **shard** — Postgres compute node owning hash-partitioned data + execution; runs the plan fragments the router hands it.
- **sharded / reference / standard tables** — sharded = hash-split across all shards; reference = full copy on every shard; standard = lives whole on one shard.
- **co-location** — `collocate_with` makes identical key values land on the same shard (customer + their orders together). This pass is where co-location finally *pays off* in joins.
- **single-shard sweet spot** — the cheap path; the thesis we land again at the end.

**Carried from Pass 4 (referenced, not re-derived):**
- **table slice** — fine-grained placement unit, ~512 per sharded table, stored on a shard as a **native Postgres partition**. Note this carefully: there are **two layers of partitioning** in this system, and Rung 2 separates them.

**New terms this pass defines inline:** **Foreign Data Wrapper (FDW)**, **foreign table**, **foreign scan / async foreign scan**, **partial aggregation**, **partition pruning**, **foreign vs local predicate**, **partition-wise join**, **null-padded side**, **distributed function**.

---

## Rung 1 — The pain: the router holds no data, so a naive distributed query drowns it

Pass 1's defining decision: the router holds connections, plans, and the placement map — but **zero user rows**. Every byte of `customers`, `orders`, `tax_rates` lives on shards. That decision is wonderful for scaling connections. It is a disaster for the *obvious* way to run a query.

Watch the naive design. The app sends `SELECT count(*) FROM orders;` to a router. The router has no orders. So the simplest possible thing it can do:

```
   NAIVE ("pull up, compute at the router"):
      router → shard 0: "send me ALL your orders rows"
      router → shard 1: "send me ALL your orders rows"
      ...
      router → shard 7: "send me ALL your orders rows"
      router: receive 8 shards' worth of rows over the network,
              concatenate them, then count(*) locally.
```

The pain, made concrete. Suppose `orders` holds 100M rows per shard at the common 8-shard config (Pass 4) — a plausible mid-size OLTP table; the exact figure doesn't matter, only that it dwarfs the single-integer result — so 800 million rows total, spread evenly. To answer a query whose *result is a single integer*, the router would drag **800 million rows across the network** into a node that was deliberately built to hold no data. Its network card saturates, its (small, data-free) memory can't buffer the flood, and the one node every connection funnels through becomes the bottleneck for the entire shard group.

```
   the router was DESIGNED data-free (Pass 1) — so "pull all rows up to it"
   is the single worst thing you can ask it to do.
```

The fix names itself. The shards already *hold* the rows and already *are* Postgres compute. So **push the compute down to the shards** — let each shard count its own rows in parallel — and move only the small *result* up:

```
   PUSHDOWN ("compute at the shards, move only results up"):
      router → each shard: "count YOUR orders rows, send me just the number"
      shard 0 → 100,000,000     shard 1 → 100,000,000   ...   (8 small integers)
      router: sum the 8 partial counts → 800,000,000. Done.
```

Eight integers up the wire instead of 800 million rows. That is the entire game of this pass: **push compute down, move small results up.** Every rung below is a different flavor of "what can we safely push down, and how does the router represent the remote data well enough to plan it?"

> **Real-world hook:** this is the same instinct as MapReduce's "move computation to the data, not data to the computation," and the same reason analytics engines push aggregation into storage (e.g. predicate/aggregate pushdown in Parquet readers, or Citus's distributed planner). Limitless's twist is doing it inside an *unmodified* PostgreSQL planner — Rung 2 is how.

---

## Rung 2 — How the router represents data it doesn't have: partitioned tables + FDW

The router must *plan* a query over `orders` without holding a single order. The pain: a Postgres planner only knows how to plan over tables it can see locally. How do you make the stock planner reason about rows that live on eight other machines?

AWS's answer is to **not write a new planner**. Instead they describe the distributed data using two mechanisms PostgreSQL *already has*, so the stock planner and executor "just work."

### Mechanism (a): a sharded table is a Postgres *partitioned table*

On the router, `orders` is modeled as a **partitioned table** — Postgres's native feature for splitting one logical table into child partitions. The router defines **one partition per shard**, and those partitions carve up the hash space exactly the way the placement map (Pass 1) carves it:

```
   ROUTER's view of "orders" (a partitioned table):

        orders  (partitioned by hash of cust_id)
        ├── orders_p0   → hash range owned by shard 0
        ├── orders_p1   → hash range owned by shard 1
        ├── ...
        └── orders_p7   → hash range owned by shard 7
```

Now the planner sees a perfectly ordinary partitioned table and applies all its mature partitioned-table machinery (Rung 6's partition pruning, Rung 5's partition-wise joins) for free.

### Mechanism (b): each partition is a *foreign table* via a custom FDW

But a router partition `orders_p0` holds no rows — the rows are on shard 0. So each partition is declared as a **foreign table**.

> **Foreign Data Wrapper (FDW):** a Postgres extension mechanism that lets the planner and executor query an *external* data source as if it were a local table — the wrapper knows how to connect to the source, fetch rows, and (crucially) push work to it. Postgres ships `postgres_fdw` for talking to other Postgres servers; Limitless uses a **custom FDW** tuned for its shards.
>
> **Foreign table:** the local stand-in object the FDW exposes. It has the table's columns and types but no local storage — reads go through the wrapper to the remote source.

So each router-side partition is a foreign table whose FDW points at the real data on a shard:

```
   ROUTER                                   SHARDS (real data)
   ──────                                   ──────────────────
   orders (partitioned table)
   ├── orders_p0  [foreign table] ──FDW──►  shard 0 : real orders rows
   ├── orders_p1  [foreign table] ──FDW──►  shard 1 : real orders rows
   ├── ...                                  ...
   └── orders_p7  [foreign table] ──FDW──►  shard 7 : real orders rows
```

The planner plans over the partitioned table; when execution reaches a partition, the FDW reaches out to its shard. The router never stores a row — it just *describes* where rows live and how to push work there.

How the other two table types map:

```
   sharded table     → partitioned table; ONE foreign-table partition PER shard
   reference table   → a foreign table reachable on EVERY shard (full copy each — Pass 1)
   standard table    → a SINGLE foreign table pointing at the ONE shard that holds it
```

### The two layers of partitioning — disambiguate them now

This system uses the word "partition" at **two different levels**, and conflating them will wreck Rungs 5–6. Split them explicitly:

```
   ROUTER-SIDE partitioning  (this pass, Rung 2):
       one partition PER SHARD. Purpose: let the stock planner reason about
       which shard(s) a query touches. Each partition is a FOREIGN table.

   SHARD-SIDE partitioning   (Pass 4):
       one partition PER SLICE (~512). Purpose: fine-grained data placement
       and migration. Each partition is a REAL local Postgres partition holding rows.
```

Same Postgres feature (partitioning), used at two scales for two jobs. Router-side: "which machine?" Shard-side: "which slice, for migration?" When this pass says "partition," it means **router-side, one-per-shard**, unless it says otherwise.

> **Pre-empt:** *"Why reuse partitioned-tables + FDW instead of writing a purpose-built distributed planner?"* Because the stock PostgreSQL planner is decades-mature: cost-based, with partition pruning, partition-wise joins, and aggregate pushdown already implemented and battle-tested. Re-describing the distributed layout in terms the planner already understands means Limitless inherits all of that for free and stays PostgreSQL-compatible — the same "reuse the engine" philosophy as building slices on Postgres partitions (Pass 4) and 2PC on existing shard durability (Pass 3). The cost — a few places where the stock cost model doesn't know data is remote — is exactly what AWS had to patch (Rung 5).

> **Real-world hook:** `postgres_fdw` is what people already use to federate a few Postgres servers by hand. Limitless turns that artisanal trick into the automatic backbone of a sharded engine — every sharded table is silently a partitioned table of foreign tables.

---

## Rung 3 — Aggregation pushdown: split the work into "partial" and "finalize"

Rung 1 showed *why* we push `count(*)` down; Rung 2 gave the router a way to *plan* over remote data. Now watch the planner actually do it, because the shape of the plan is reused everywhere below.

The pain a naive count still has even with FDW: if the FDW just fetched all rows and the router counted, we're back to Rung 1's flood. The planner must *split* the aggregate.

> **Partial aggregation:** Postgres can break an aggregate into two stages — a **partial** stage that each data source computes locally over its own rows, and a **finalize** stage that combines the partials. For `count(*)`, the partial is "count my rows" and the finalize is "sum the partial counts." (For `avg`, the partial is `(sum, count)` and finalize divides — same idea, slightly richer partial.)

Here is the plan tree for `SELECT count(*) FROM orders;` (read it bottom-up, the way execution flows):

```
   Finalize Aggregate                        ← router: SUM the 8 partial counts → final answer
     └── Append                              ← router: concatenate the per-shard results
           ├── Async Foreign Scan  on orders_p0   → shard 0: PARTIAL count(*)  (counts its rows)
           ├── Async Foreign Scan  on orders_p1   → shard 1: PARTIAL count(*)
           ├── ...
           └── Async Foreign Scan  on orders_p7   → shard 7: PARTIAL count(*)
```

Three nodes to read:

- **Async Foreign Scan** (one per shard, the leaves). A **foreign scan** is the executor node that pulls from a foreign table via its FDW. **Async** means the router fires all eight off *concurrently* rather than waiting for shard 0 before asking shard 1 — so all shards count *in parallel*, and total latency is roughly the slowest single shard, not the sum of all eight. The pushed-down fragment each shard runs is `PARTIAL count(*)` — it counts its own rows and returns **one integer**.
- **Append** (router). Stitches the eight per-shard results into one stream. This is the standard Postgres node for "union the partitions of a partitioned table."
- **Finalize Aggregate** (router). Sums the eight partial counts into the final `800,000,000`. The only arithmetic the router does, over eight tiny inputs.

Compare the two designs by what crosses the network:

```
   NAIVE (Rung 1):    800,000,000 rows  ───────►  router    (saturates it)
   PUSHDOWN:          8 partial integers ───────► router     (trivial)
```

**Generalize.** The same partial/finalize split, plus pushing **sorting** down, cuts transferred data for a whole class of queries:
- `GROUP BY region` → each shard computes partial aggregates *per group*; router merges groups. The wire carries one row per (shard, group), not every base row.
- `ORDER BY ... LIMIT 10` → each shard sorts locally and returns its **own top 10** — note it must send 10, not 1, because the global top 10 could all live on a single shard, so one row per shard would miss them. The router merges the eight pre-sorted 10-row streams and keeps the overall top 10. The wire carries 10 rows per shard (80 total), not the whole table.

The principle: **do as much reduction (count, sum, group, sort, limit) at the shard as possible, so the bytes climbing to the router shrink to the size of the answer, not the size of the data.**

> **Real-world hook:** this partial/finalize split is exactly how distributed SQL engines (Citus, Presto/Trino, Spark SQL) run aggregates — "pre-aggregate per partition, combine at the coordinator." Limitless gets it from stock Postgres's two-phase aggregation, wired to fire across shards asynchronously.

---

## Rung 4 — Predicate pushdown: which `WHERE` clauses are safe to run on a shard?

Aggregates push down (Rung 3). The next obvious win is **filters**: don't ship rows the query will throw away — apply the `WHERE` at the shard and ship only survivors. But here's the catch §7 forces us to confront: **not every predicate is safe to run on a shard.** The router must *classify* each predicate first.

> **Foreign predicate:** a predicate the router judges safe to execute *at the shard* (push it down). **Local predicate:** a predicate that must be evaluated *at the router*, which forces the matching rows to be pulled up first.

The classification turns on one question: **will this expression produce the same result on the shard as it would at the router?** A predicate is *foreign* (pushable) only if it's built from things whose meaning is stable and identical everywhere:

```
   FOREIGN (push to shard) — safe because the result is identical wherever it runs:
       • built-in operators / comparisons:   email = 'abc@xyz.com'
       • IMMUTABLE functions:                upper(name) = 'ACME'
         (IMMUTABLE = given the same inputs, ALWAYS returns the same output,
          no dependence on time, session, config, or who is asking.)

   LOCAL (keep at router) — UNSAFE to trust to the shard:
       • VOLATILE functions:                 WHERE random() < 0.01
         (a different value every call → a different result on every shard)
       • STABLE functions:                   anything depending on session/txn context
         (paper §7 groups STABLE under LOCAL too — see the edge note below)
       • SECURITY DEFINER / definer-context functions:
         run with the DEFINER's privileges & context, not the caller's
```

### Why mutable and definer functions can't be trusted to the shard

Walk the reasoning, because this is the rung most worth getting right:

- **VOLATILE** (e.g. `random()`, `clock_timestamp()`): the function may return a *different value every call*. Take `WHERE random() < 0.01` and push it to eight shards: you've called `random()` eight times in eight separate processes — eight independent random streams. Which rows survive would depend on *where* the filter ran, which is incoherent. It must run **once, at the router**, over rows pulled up.
- **STABLE** (anything depending on session settings like `timezone` or the current transaction): the value is fixed *within one statement* but depends on **session/transaction context** that lives on the router, not the shard. A shard executing the fragment doesn't share the router session's settings, so it could compute a different answer. The paper §7 classifies STABLE as **local** for exactly this reason.
- **SECURITY DEFINER / definer-context functions:** these run with the *function definer's* privileges and context rather than the calling session's. The shard isn't the right place to reproduce that privilege/context boundary, so the router won't push them.

Toy examples making the split concrete on `orders`:

```
   SELECT * FROM orders WHERE email = 'abc@xyz.com';
       email = 'abc@xyz.com'  is a built-in equality on a constant → FOREIGN.
       Plan: each shard scans, applies the filter, returns ONLY matches.
       Few rows cross the wire.

   SELECT * FROM orders WHERE random() < 0.01;
       random() is VOLATILE → LOCAL. Pushed to 8 shards it would run 8 independent
       random streams; the surviving rows would depend on WHERE it ran. The router
       must apply it once, over rows pulled up. (This sampling filter is genuinely
       unshippable — the categorical LOCAL verdict is plainly correct.)

   SELECT * FROM orders WHERE is_visible_to(current_user, order_id);  -- SECURITY DEFINER
       definer function → LOCAL. It runs with the definer's privileges/context, which
       the shard can't reproduce, so the router evaluates it.
```

> **The confusing edge — `now()`.** `now()` is STABLE, and by the paper's rule
> (STABLE → local) a `now()`-based bound is local. This trips people because stock
> `postgres_fdw` *does* fold `now()` to a constant and push that constant down. Both
> are true: this paper groups STABLE under local as a category, while in practice a
> planner may first resolve a STABLE call like `now()` to a constant before deciding.
> Don't generalize from `now()`; the load-bearing cases (VOLATILE, definer) are the
> ones that are categorically unshippable.

The payoff is the same shape as Rung 3: a *foreign* predicate shrinks what crosses the wire to just the matching rows; a *local* predicate, in the worst case, forces a pull-up and is exactly the Rung-1 pain we're trying to avoid — which is why "write predicates the shard can evaluate" is real performance advice, not pedantry.

> **Real-world hook:** the IMMUTABLE/STABLE/VOLATILE labels are stock PostgreSQL function *volatility categories* — the same ones that govern whether Postgres can use an expression index or inline a function. Limitless repurposes that existing safety classification to decide pushdown, rather than inventing a new trust model.

---

## Rung 5 — Join pushdown: where co-location finally pays off (§7)

Filters and aggregates push down (Rungs 3–4). The hardest and most valuable case is the **join**. A naive distributed join is the Rung-1 nightmare squared: pull *both* tables up and join at the router, or — worse — *shuffle* rows between shards so matching keys meet. We want neither. We want each shard to do its slice of the join **locally and in parallel**, with the router doing only a cheap `Append`. Whether that's possible depends entirely on **where the matching rows live** — which is to say, on Pass 1's co-location.

> **Partition-wise join:** a Postgres optimization where, if two partitioned tables are partitioned the *same way* on the join key, the planner joins partition *i* of one to partition *i* of the other — never cross-partition. In Limitless, "partition *i*" = "shard *i*," so a partition-wise join becomes a **per-shard local join with no cross-shard data movement.**

### Case A — sharded ⋈ sharded, co-located on the join key

`customers ⋈ orders ON cust_id`. Both are sharded on `cust_id` and co-located (Pass 1), so **customer 7 and all of customer 7's orders sit on the same shard, always.** Therefore every matching pair is *already* on one shard — no row needs to move to find its partner.

```
   SELECT c.name, o.amount
   FROM customers c JOIN orders o ON c.cust_id = o.cust_id;

   Append                                   ← router: concatenate per-shard results
     ├── Async Foreign Scan → shard 0:  customers_p0 ⋈ orders_p0   (LOCAL join)
     ├── Async Foreign Scan → shard 1:  customers_p1 ⋈ orders_p1   (LOCAL join)
     ├── ...
     └── Async Foreign Scan → shard 7:  customers_p7 ⋈ orders_p7   (LOCAL join)
```

Each shard joins *its own* customers to *its own* orders — a complete, correct local join, because co-location guarantees no match lives on another shard. Eight joins run in parallel; the router just appends. **No cross-shard shuffle.** This is the co-location promise from Pass 1 cashed out: pick the shard key so your joins are on it, and joins become embarrassingly parallel.

> **The cost-model patch AWS had to make.** Stock PostgreSQL's planner cost model **doesn't natively know the data lives on separate shards** — to it, a partition-wise join is just one option among many, costed against alternatives like pulling data up and joining centrally. On a real distributed layout, the partition-wise (per-shard) path is almost always the right one because it avoids the network flood, but the unmodified cost model can mis-cost it and pick a worse plan. So AWS **modified the cost model** to recognize the distributed layout and prefer the push-down-the-join path. This is the concrete "few places we had to patch" cost the Rung-2 reuse strategy warned about.

### Case B — sharded ⋈ reference

`orders ⋈ tax_rates ON region`. `tax_rates` is a **reference** table (Pass 1): a *full copy on every shard*. So whatever order rows a shard holds, the matching tax rate is **already local on that same shard**. Same outcome as Case A — local parallel joins, router appends, no shuffle:

```
   SELECT o.amount, t.rate
   FROM orders o JOIN tax_rates t ON o.region = t.region;

   Append
     ├── shard 0:  orders_p0 ⋈ tax_rates(local full copy)
     ├── ...
     └── shard 7:  orders_p7 ⋈ tax_rates(local full copy)
```

But Case B carries a **restriction that Case A doesn't**, and it's the subtlest point in the pass. The local-join-then-append trick is correct only for join shapes where **a shard can decide every output row using only its own rows**. That holds for:

```
   PUSHES DOWN (sharded ⋈ reference):
       • Cartesian products
       • INNER joins
       • OUTER joins where the REFERENCE table is the NULL-PADDED side
```

> **Null-padded side:** in an outer join, the side whose columns get filled with NULLs when there's no match. In `orders LEFT JOIN tax_rates`, `orders` is preserved and `tax_rates` is the null-padded side (an order with no matching rate still appears, with NULL rate).

It does **NOT** push down for:

```
   DOES NOT PUSH DOWN:
       • OUTER joins where the SHARDED table is the NULL-PADDED side
       • ANTI-joins  (e.g. NOT EXISTS / "rows with NO match")
```

**Why the restriction — the one-sentence reason, then the unpacking.** A shard can produce an unmatched/null-padded row *only if it can be sure the row truly has no match anywhere* — but a shard only sees *its own* slice of the sharded table, so it **cannot rule out a match on another shard.**

Unpack it on an anti-join, `tax_rates WHERE NOT EXISTS (matching order)` — "tax rates that no order uses":

```
   tax_rates is replicated on EVERY shard (full copy).
   region 'EU' has orders on shard 3 but NOT on shard 0.

   shard 0, looking only at ITS orders:  "no 'EU' order here → emit 'EU' as unmatched"  ← WRONG
   shard 3, looking only at ITS orders:  "there IS an 'EU' order → 'EU' is matched"

   shard 0 emits a FALSE non-match, because the real match lives on shard 3,
   which shard 0 cannot see. The local-only decision is unsound.
```

The same trap hits an outer join with the **sharded** table null-padded (`tax_rates LEFT JOIN orders`, preserving every tax rate, null-padding orders): each shard would null-pad rates that *do* match an order on a different shard, double-counting and mis-padding. The safe rule is exactly the boundary stated above: push down only when a shard's *own* rows are sufficient to decide each output row (inner, Cartesian, or reference-side null-padding); otherwise the router must gather across shards. Inner joins and reference-null-padded outer joins are safe precisely because "matched" is a *positive* fact a shard can confirm locally; "no match anywhere" is a *global* fact it cannot.

### Case C — standard tables

```
   standard ⋈ standard   → both live on one (distinguished) shard already →
                            push the WHOLE join to that single shard.
   standard ⋈ anything-else  → currently just executed at the ROUTER
                            (pull up and join centrally — the Rung-1 path, accepted
                             here because standard tables are low-traffic by design, Pass 1).
```

A standard table lives whole on one shard, so two standard tables (placed on the same distinguished shard) join right there — a single-shard push. Mixing a standard table with a sharded/reference table has no clean per-shard alignment, so today the router just does it centrally; acceptable because standard tables are the low-traffic on-ramp (Pass 1), not the hot path.

> **Real-world hook:** "co-locate on the join key so joins are local" is the central tuning rule of every co-located distributed SQL system (Citus's distribution column, CockroachDB's interleaved/co-located tables, Cosmos DB's partition key). The anti-join/outer-join restriction is the same correctness boundary those systems hit: a shard can confirm a match locally but cannot confirm a *global* non-match.

---

## Rung 6 — The sweet spot, restated as the system's thesis (§7)

Every rung pushed *some* work down. The best outcome of all is when the router can push the **entire query** down to **one** shard — because then there's a single round trip, the lowest possible latency, and zero cross-shard coordination (and, recalling Pass 3, a single-shard transaction skips 2PC entirely). This is the **single-shard sweet spot** the whole paper has been building toward. Two mechanisms drive it.

### Mechanism 1 — partition pruning detects the single-shard case

> **Partition pruning:** the stock-Postgres optimization that, given the query's predicates, eliminates partitions that *cannot* contain matching rows — so the executor never touches them. Because router-side partitions map one-to-one to shards (Rung 2), pruning partitions = **pruning shards**.

When a query pins the shard key to a value (or a set landing in one shard's hash range), pruning collapses the plan to a single shard:

```
   SELECT * FROM orders WHERE cust_id = 7;

   hash(cust_id=7) lands only in shard 7's hash range (toy: 7 mod 8 = 7).
   Partition pruning eliminates orders_p0..p6.

   Plan:   Async Foreign Scan → shard 7 only.    ← ONE round trip, lowest latency.
```

Contrast the two extremes the same machinery produces:

```
   cust_id = 7         → prunes to ONE shard      → single-shard, 1 round trip   ★ sweet spot
   count(*) over all   → touches ALL shards       → fan-out, partial/finalize (Rung 3)
```

The more of your workload pins the shard key, the more of it lands on the sweet-spot path. That is *why* the shard key was called "the single most consequential schema decision" back in Pass 1.

### Mechanism 2 — function distribution pushes a whole SQL function to one shard

Pruning handles queries. SQL *functions* (stored procedures) get an analogous treatment. First the pain. Take a 5-statement `place_order(cust_id, ...)` that only ever touches one customer's rows. Run it the default way — body executes **at the router** — and each statement is its own round trip to the shard:

```
   place_order(7, ...) run at the ROUTER (default):
       stmt 1 → router→shard 7→router     (1 round trip)
       stmt 2 → router→shard 7→router     (1 round trip)
       stmt 3 → router→shard 7→router     (1 round trip)
       stmt 4 → router→shard 7→router     (1 round trip)
       stmt 5 → router→shard 7→router     (1 round trip)
       = 5 router↔shard round trips, all to the SAME shard 7.   ← chatty
```

Every statement hashes to the same shard 7, yet the router pays a round trip per statement. The fix: ship the **whole function body** to shard 7 once; all 5 statements run locally there:

```
   place_order(7, ...) SHIPPED whole to shard 7:
       router → shard 7: "run the whole function body"   (1 round trip)
       shard 7: runs stmt 1..5 LOCALLY, returns result
       = 1 round trip, no per-statement chatter.           ← the fix
```

You declare this with `limitless_distribute_function`, telling the system **which sharded table the function is tied to and which argument is the shard key**:

```
   -- a function that only ever touches ONE customer's data:
   limitless_distribute_function(
       function       => 'place_order(cust_id int, ...)',
       sharded_table  => 'customers',   -- the table whose layout it follows
       shard_key_arg  => 'cust_id'      -- which argument carries the shard key
   );

   call place_order(7, ...)  →  hash(7) → shard 7  →  ship the WHOLE function body
                                to shard 7 and run it THERE.
```

> **Distributed function:** a SQL function declared (via `limitless_distribute_function`) to touch only the shard identified by a designated shard-key argument, so the router pushes the entire function to that one shard instead of executing its body statement-by-statement with a round trip each. The body runs where the data is — one hop, no per-statement chatter.

### Close — the whole performance argument in one line

```
   good shard keys  +  co-location (Pass 1)  +  distributed functions
        ─────────────────────────────────────────────────────────────►
              MAXIMIZE the fraction of work that is SINGLE-SHARD
        ─────────────────────────────────────────────────────────────►
   single-shard  =  pushdown collapses to one shard (pruning / distributed fn)
                 =  one round trip, lowest latency
                 =  single-shard transaction skips 2PC (Pass 3)
```

This is the thesis the entire paper has been assembling. Pass 1 gave you the table types and co-location *so that* matching rows sit together. Pass 2–3 made single-shard transactions cheap by letting them skip 2PC. Pass 4 preserved co-location across rebalancing so the sweet spot survives scaling. And this pass shows the query processor *exploiting* all of it: the more your schema and access patterns keep work on one shard, the more the system runs on its fastest path. Choosing good shard keys, co-locating what you join, and distributing single-shard functions is, end to end, **the performance argument of Aurora Limitless.**

> **Real-world hook:** the HammerDB / TPC-C evaluation in §8 scales near-linearly precisely because a well-keyed TPC-C "new order" transaction is single-shard (everything co-located on the warehouse key) — so pruning collapses it to one shard, it skips 2PC, and adding shards adds throughput almost without coordination overhead. The sweet spot isn't a micro-optimization; it's why the benchmark scales.

---

## What this pass nailed down

```
THE PAIN (Rung 1):  router holds NO data (Pass 1). Naive "pull all rows up and
                    compute at the router" floods the one data-free bottleneck node
                    (800M rows up the wire to answer count(*)). Fix: push compute
                    DOWN to shards, move only small RESULTS up.

REPRESENT REMOTE DATA (Rung 2):  reuse stock Postgres so the planner "just works":
   (a) sharded table = PARTITIONED table, one partition PER SHARD.
   (b) each partition = a FOREIGN table via a custom FDW (queries an external
       source as if local). reference → foreign table on every shard;
       standard → foreign table to the one shard.
   TWO partition layers: router-side (per shard, this pass) vs shard-side
       (per slice ~512, Pass 4). Don't conflate.

AGGREGATION PUSHDOWN (Rung 3):  count(*) →
   Finalize Aggregate ← Append ← Async Foreign Scan(PARTIAL count) per shard.
   shards count in PARALLEL (async); router sums partials. 8 integers, not 800M rows.
   generalize: partial aggregation + pushed-down sort/limit shrink the wire.

PREDICATE PUSHDOWN (Rung 4):  classify each WHERE clause:
   FOREIGN (push) = built-in ops + IMMUTABLE fns (same result anywhere).
   LOCAL (pull up) = STABLE/VOLATILE/definer fns (depend on time/session/privilege
   context the shard can't reproduce → could differ per shard → run at router).

JOIN PUSHDOWN (Rung 5):
   sharded ⋈ sharded co-located on join key → per-shard LOCAL join + Append, NO shuffle.
       (AWS had to PATCH the cost model — it didn't natively know data is on shards.)
   sharded ⋈ reference (replicated everywhere) → per-shard local join + Append. BUT
       pushes down ONLY for Cartesian / inner / outer-with-REFERENCE-null-padded.
       NOT for outer-with-SHARDED-null-padded or ANTI-joins: a shard can confirm a
       match locally but CANNOT confirm a GLOBAL non-match (the match may live on
       another shard it can't see).
   standard ⋈ standard → push to the one shard; standard ⋈ other → router (central).

SWEET SPOT = THE THESIS (Rung 6):
   PARTITION PRUNING: predicate pins shard key → prune to ONE shard → whole query
       pushed to it → 1 round trip, lowest latency (+ skips 2PC, Pass 3).
   FUNCTION DISTRIBUTION (limitless_distribute_function): a fn touching one shard
       (declared with its sharded table + shard-key arg) ships WHOLE to that shard.
   good shard keys + co-location + distributed fns → maximize single-shard fraction
       = the entire performance argument of the paper.
```

Deferred and where it lives:
- **DDL coordination across routers/shards** (§5.7) → later pass.
- **The §8 evaluation numbers** (HammerDB NOPM, near-linear scaling) → Pass 8; here we only used the *shape* of the result (single-shard → linear scale) as the hook.

---

## The 3 checkpoint questions

Answer in your own words. They tell me what to reinforce next.

1. **Why pushdown, and how the planner even sees remote data.** (a) For `SELECT count(*) FROM orders;` over 8 shards × 100M rows, contrast exactly what crosses the network in the naive design vs the pushdown plan, and why the router specifically is the wrong place to compute. (b) Name the *two* stock-Postgres mechanisms the router reuses to represent a sharded table, what each one is for, and how reference and standard tables map onto them. (c) The word "partition" means two different things in this system — state both, and which one Rung 2 is about.

2. **Predicate classification.** For each predicate, say FOREIGN or LOCAL and *why*: (a) `WHERE status = 'shipped'`; (b) `WHERE upper(email) = 'A@B.COM'`; (c) `WHERE random() < 0.01`; (d) a `SECURITY DEFINER` function in the WHERE clause. Then state the single underlying test that decides all four, and what goes wrong if you push a VOLATILE function to eight shards. Bonus: `now()` is STABLE — which category does *this paper* put it in, and why does stock `postgres_fdw` behavior make it a confusing edge?

3. **Join pushdown and its boundary.** (a) Draw the plan for `customers ⋈ orders ON cust_id` (co-located) and explain why no row has to move between shards. (b) What did AWS have to modify in stock Postgres to make the planner *pick* that plan, and why was the unmodified planner not enough? (c) For `orders ⋈ tax_rates` (reference), give one join shape that pushes down and one that does **not**, and explain in one sentence the correctness reason the anti-join / sharded-null-padded case cannot be done per-shard.

**Also flag:**
- **The outer-join / anti-join restriction (Rung 5):** did "a shard can confirm a *match* locally but cannot confirm a *global non-match*" land as the crisp reason — i.e. is it clear *why* shard 0 emitting an unmatched 'EU' is wrong when the match is on shard 3? Or did the null-padded-side terminology blur it?
- **Foreign vs local predicate classification (Rung 4):** did IMMUTABLE-vs-STABLE/VOLATILE/definer feel like a principled "same result anywhere?" test, or like a memorized list? Did the *reason* mutable/definer functions can't be trusted to a shard (session/time/privilege context the shard doesn't share) click?
- **The two partition layers (Rung 2):** did router-side (per-shard) vs shard-side (per-slice, Pass 4) stay cleanly separated, or did they smear together?
- **The cost-model patch (Rung 5):** did "the stock planner didn't know data lives on shards, so AWS modified the cost model to prefer the per-shard join" feel like a concrete consequence of the Rung-2 reuse strategy, or like a throwaway aside?
- Any term you'd struggle to define unaided: **FDW, foreign table, async foreign scan, partial aggregation, foreign vs local predicate, partition pruning, partition-wise join, null-padded side, distributed function.**
```