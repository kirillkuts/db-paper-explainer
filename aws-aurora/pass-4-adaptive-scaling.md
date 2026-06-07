# Pass 4 — Adaptive scaling: vertical, horizontal, and shard splits

> **Goal:** after reading, you can explain the two independent axes a shard group grows along (bigger nodes vs more nodes); define an **ACU** and read off how a customer's capacity budget turns into router/shard *counts*; show how the budget is re-sliced toward hot nodes; define a **table slice** and say why there are ~512 of them; and — the crown jewel — walk the 4-phase **shard split** that moves slices off a hot shard *without piling more load on it*, naming the pain each phase solves.
> **Reading time:** ~25 minutes.
> **Method:** the usual ladder. Each rung is the simplest design that **fails**; the next rung names the pain and fixes it.
> **Scope guard:** this is the **elasticity** pass — §4, §4.1, §4.2. We cover how the shard group resizes itself. We do **not** introduce new concurrency-control mechanics: the switchover phase takes locks and may abort transactions, but we lean entirely on **Pass 3's** locking *mechanic* (terminating in-flight txns to force lock release). The only new wrinkle is *granularity* — switchover locks at **table level** (DDL granularity) rather than Pass 3's row level — which we name explicitly rather than smuggle in. Failover/recovery durability is still **Pass 6**.

**Carried from Pass 1 (restated when first used, not re-derived):**
- **shard group** — the whole router + shard fleet behind one DNS endpoint.
- **router** — front door: connections + planning + the authoritative **placement map**, no user data.
- **shard** — a Postgres compute node owning hash-partitioned data + execution, sitting on its own Aurora storage volume.
- **placement map: slice → shard** — the lookup the router uses instead of the app's `hash(key) % N`. Pass 1 promised the word **slice** would be *defined* here. It is (Rung 4).
- **co-location** — `collocate_with` makes identical key values land on the same shard, so a customer's orders sit with the customer → single-shard transaction, skips 2PC.
- **Aurora storage** — every volume replicated 6 ways, 2 per AZ across 3 AZs; **copy-on-write** clones are cheap (we lean hard on this in Rung 5).
- **standbys** — a shard runs **0–2** standbys (customer-chosen); a router has none (fungible, DNS reroutes).

**Carried from Pass 3 (referenced, not re-derived):**
- **row locks / terminating in-flight txns** — a writer holds an exclusive lock; to force a lock release you abort the transaction holding it. Switchover (Rung 5, Phase 3) reuses this *release mechanic* (abort the holder) but on a coarser lock *object*: a **table-level** lock (DDL granularity), not a row lock.

**New terms this pass defines inline:** **ACU** (Aurora Capacity Unit), **Serverless V2**, **min/max ACU band**, **dynamic min/max ACU**, **table slice**, **shard split**, **switchover window**, **heat management**.

---

## Rung 1 — The pain: a fixed-size shard group is wrong almost all the time

Pass 1 built the *structure* of a shard group but froze its size. Freezing the size is the pain. Watch both ways it bites.

```
   You provision a shard group for "expected" load.

   CASE under-provisioned:   Black Friday hits. Shards saturate — CPU pinned,
                             buffer cache thrashing, p99 latency explodes.
                             You can't add capacity fast enough; the site degrades.

   CASE over-provisioned:    To be safe from the above, you size for the PEAK
                             year-round. 51 weeks out of 52 you pay for capacity
                             that sits idle. Pure waste.
```

A single fixed size forces you to choose which way to be wrong. The fix is **elasticity**: let the shard group grow when load rises and shrink when it falls, automatically.

But "grow" is not one thing. There are **two independent axes**, and conflating them is the second half of the pain:

```
   AXIS 1 — VERTICAL  ("make each node bigger")
       give an existing router or shard more memory / CPU / network.
       Helps when a node is hot but the DATA is fine where it is.

   AXIS 2 — HORIZONTAL ("add more nodes")
       add another shard (and move some data to it) or another router.
       Helps when a single node — even at max size — can't hold the load,
       OR when you have more data than one node should own.
```

And a third independence, easy to miss: **routers and shards scale separately.** A connection-heavy app (lots of clients, modest data) needs more *routers*; a write-heavy app (modest connections, huge throughput) needs bigger or more *shards*. Pass 1 Rung 3 already split their roles — here that split pays off, because each role grows on its own schedule.

```
   four independent levers the system (and you) can pull:
       routers: bigger   |   routers: more
       shards:  bigger   |   shards:  more
```

> **The pain, stated plainly:** a fixed shard group is either wasteful or saturating, and even "just scale it" hides two orthogonal axes (bigger vs more) across two independent node types (routers vs shards). The rest of this pass is the machinery that pulls each lever automatically.

> **Real-world hook:** this is the elasticity promise behind "serverless" databases generally — pay for what you use, absorb spikes without a capacity-planning project. Aurora Limitless has to deliver it across a *fleet* of nodes, not one box.

---

## Rung 2 — ACUs and initial sizing (§4): turning a capacity budget into node counts

Before anything can scale, we need a *unit* to scale in, and a starting size. Both come from one concept.

### The unit: the ACU

> **ACU (Aurora Capacity Unit):** the granular unit of capacity Serverless bills and scales in. **One ACU ≈ 2 GB of memory**, plus a *proportional* slice of CPU and network bandwidth.

Why anchor on memory, of all things? Because for a database engine, memory is what runs out first. It holds the buffer cache (the data pages kept in RAM) and the per-connection working memory — starve it and everything stalls. So Aurora pegs the unit to the binding resource and sizes CPU and network to match. An ACU is a *balanced* bundle, not a lopsided one. "10 ACUs" then means roughly 20 GB of memory with CPU/network to match. (The 2 GB figure is the published Aurora Serverless V2 anchor; treat the CPU/network ratios as "proportional, exact ratio not load-bearing here.")

> **Serverless V2** is the Aurora feature that scales a *single* Postgres node's capacity up and down in fine ACU steps, in place, without a restart. Limitless uses it as the per-node vertical-scaling primitive (Rung 3). For now: a node can smoothly become bigger or smaller, measured in ACUs.

### The customer's only knob: total min/max ACU

The customer does **not** hand-pick node counts. They set **one** budget for the whole shard group:

```
   customer sets:   shardGroupMinACU   (floor — never shrink below this)
                    shardGroupMaxACU   (ceiling — never grow above this; the spend cap)
```

That's the entire capacity contract. Everything else — how many routers, how many shards, how big each gets — the system derives. This is the "transparent" promise from Pass 1 applied to *sizing*: you declare a budget, not a topology.

### From max budget to initial node counts: the Table-1 lookup

Here's the pain. A brand-new shard group has zero load history — yet it still needs *some* starting number of routers and shards on day one. What can you size off when you know nothing about the traffic yet? The one thing the customer did tell you: their budget ceiling. So Aurora derives the starting topology from **shardGroupMaxACU** via a fixed lookup (the paper's Table 1). Four verified rows from that table — read them top-to-bottom and watch counts grow monotonically:

```
   total      initial      initial     default      max-ACU
   nodes      routers      shards      min-ACU      range
   ─────      ───────      ──────      ───────      ────────────
     4    =      2     +      2          16          16 – 400
    12    =      4     +      8          48          1101 – 1200
    18    =      6     +     12          72          1701 – 1800
    24    =      8     +     16          96          2301 – 6144
```

Three things to read off the table. (1) **`total nodes = routers + shards`** — the 4-node row is 2+2, the 24-node row is 8+16. (2) The **router:shard ratio is ~1:2** at every row past the smallest (4r/8s, 6r/12s, 8r/16s) — shards grow about twice as fast as routers, the data-vs-connection asymmetry made literal. (3) The **`max-ACU range` is the budget band that selects the row**: a `shardGroupMaxACU` of, say, 1150 lands in the `1101–1200` band → you start at 4 routers + 8 shards. The `default min-ACU` column is the floor the system picks if you don't set one — note it climbs with the row (16, 48, 72, 96), because a bigger topology has more nodes that each need a non-zero floor.

Why drive counts off the **max**, not the min or the current load? Because the max is the *most* the group could ever need to spread, so it dictates how many nodes you must have *room* to grow into. Sizing the initial topology off the ceiling means the group is already shaped to absorb growth up to its budget without an immediate horizontal reshape on day one.

Why does a bigger budget map to **more** nodes (not just bigger ones)? Because a single node has a practical maximum ACU — there's a biggest box. Past that ceiling, the only way to spend more budget is to add nodes. So the lookup grows counts roughly with budget: more money → more parallel capacity → more nodes to hold it.

Notice the rows give **more shards than routers** (e.g. 8 shards vs 4 routers at ~1200 ACU). That asymmetry reflects the typical workload: data + write throughput (shards) usually scales faster than connection + planning load (routers).

### Standbys don't count against the budget

A subtlety that trips people up: a shard's **standbys** (Pass 1: 0–2 warm replicas) each get capacity **matching their primary** — but that capacity is **not** drawn from `shardGroupMaxACU`. The budget governs the *serving* capacity (primaries + routers). Standbys are an availability cost layered on top, billed separately. So a 1200-ACU budget with 2 standbys per shard is *not* secretly only 400 ACU of serving capacity — it's the full 1200 for primaries, plus the standby overhead.

Why exclude them? Because counting standbys in the serving budget would mean buying a standby *shrinks the capacity you actually serve from* — a perverse incentive against availability. Keeping them separate means availability and serving-capacity are independent decisions (the same separation-of-concerns Pass 1 Rung 5 drew between durability and compute-availability).

### Real anchors (so the numbers feel like a real system)

```
   most frequently used config (paper §4):   4 routers,  8 shards
   biggest observed PRODUCTION group  (§4):  32 routers, 64 shards
```

Two things to read off these — but first **reconcile them with Table 1**, because they look like they contradict it. Table 1 above lists only **initial-sizing** rows (the rows shown top out at 8 routers / 16 shards). The **32/64** figure is *not* a Table-1 row: the paper's §4 prose states it separately as the **largest shard group observed in production so far**, reached over time via horizontal scaling and shard splits (Rungs 5–6) — a group that *grew* well past its initial Table-1 topology. So Table 1 = "where a group starts"; 32/64 = "how big the biggest one has gotten after scaling." Two different questions, two different numbers.

With that settled: first, **4/8 is the common case** — the paper calls 4 routers + 8 shards the most frequently used configuration, so that's the topology to hold in your head. Second, the biggest observed group keeps the same ~1:2 router:shard ratio (32:64), so the data-vs-connection asymmetry from Table 1 holds as workloads scale up, not just at the small end.

> **Real-world hook:** the HammerDB / TPC-C evaluation in §8 runs on configurations in exactly this range; when the paper reports near-linear scaling with added shards, "added shards" means walking *up* this lookup as the budget rises.

---

## Rung 3 — Vertical scaling (§4.1): re-slicing the budget toward hot nodes

Initial counts are set (Rung 2). Now the *bigger* axis. Think of the budget as a fixed pie you have to cut among the nodes. If you cut even slices, you're betting every node works equally hard — but they never do. One shard is on fire while another idles. The even split hands the idle node a slice it'll never finish and leaves the busy one starving. That mismatch is the pain this rung fixes.

### Each node has its own min/max ACU band

Serverless V2 scales a node within a **band**:

```
   node i:   dynamicMinACU_i  ≤  consumedACU_i  ≤  dynamicMaxACU_i
             (floor for node i)                    (ceiling for node i)
```

The node's *actual* size (`consumedACU_i`) floats inside its band as load changes, in fine steps, no restart. The interesting question is: **who sets each node's band?** Because the per-node ceilings must sum to something the customer's `shardGroupMaxACU` permits.

### Start even, then re-slice in proportion to consumption

```
   START:   split shardGroupMaxACU EVENLY across all nodes.
            (no load history yet → no reason to favor anyone.)

   PERIODICALLY:   re-slice each node's band in proportion to what it actually
                   consumed, giving hot nodes more headroom and cold nodes less.
```

The formula for the re-sliced ceiling of node `i`:

```
                            consumedACU_i
   dynamicMaxACU_i  =  shardGroupMaxACU  ×  ─────────────────────
                                            Σ_j consumedACU_j
```

Read it plainly: node `i`'s share of the total ceiling equals node `i`'s share of the total *current consumption*. The min is re-sliced the same proportional way. A toy walk-through with 4 shards and a 1200-ACU group ceiling:

```
   consumed:   shard0 = 300   shard1 = 100   shard2 = 100   shard3 = 100
   sum        = 600

   dynamicMax_shard0 = 1200 × 300/600 = 600     ← hot shard gets HALF the ceiling
   dynamicMax_shard1 = 1200 × 100/600 = 200
   dynamicMax_shard2 = 1200 × 100/600 = 200
   dynamicMax_shard3 = 1200 × 100/600 = 200
                                        ─────
   (ceilings still sum to 1200 = the budget; nothing overspent)
```

### Why proportional, and why this is the *right* bet

Why bet the headroom on the busy node? Because a hot node's demand grows fastest — it's the one most likely to slam into its ceiling before the next re-slice comes around. Give it the largest band and its Serverless V2 still has room to climb. A cold node, meanwhile, will never touch a big band, so handing it one just locks the headroom away from the node that actually needs it. Proportional allocation does one thing, continuously: it aims the spare budget at wherever the heat is.

> **Pre-empt:** *"Why not just give every node the full `shardGroupMaxACU` as its ceiling?"* Because then the per-node ceilings would sum to `N × shardGroupMaxACU` — every node could independently scale to the full budget and the group would blow past the customer's spend cap by N×. The bands must *partition* the budget. Proportional re-slicing is how you partition it intelligently instead of evenly.

### The shard-vs-router asymmetry, again

Vertical scaling means *adding ACUs*, but an ACU buys memory + CPU + network as a bundle, and routers and shards spend that bundle differently:

```
                  bound by...               so extra ACU mostly buys...
   SHARD          compute + buffer cache    BUFFER CACHE (cache more data pages,
                  (it runs query fragments   fewer storage round-trips) + CPU
                  over real data)
   ROUTER         memory/heap (it holds      HEAP (more connections, bigger plan
                  connections + plans, no     state) — no buffer cache to grow,
                  user data to cache)         it caches no data
```

A bigger shard mostly means *more buffer cache* (the hot working set fits in memory → fewer trips to Aurora storage). A bigger router mostly means *more heap* (room for more client connections and larger plan/working state). Same ACU, different payoff, because Pass 1's role split (shards hold data, routers hold connections+plans) determines what each one is starved for.

### Scale-up fast, scale-down conservative

One last asymmetry, in *time* rather than resource:

```
   SCALE-UP:    fast / aggressive   — a spike is an emergency; respond NOW or
                                       saturate and drop requests.
   SCALE-DOWN:  slow / conservative  — shrinking too eagerly causes THRASHING:
                                       shrink → load returns → scramble to grow
                                       again → shrink → ... a costly oscillation.
```

The cost of being slow to grow is dropped requests (bad and immediate). The cost of being slow to shrink is a little extra spend for a while (mild and bounded). So the policy is asymmetric: leap up, ease down. This avoids the classic autoscaler pathology of flapping around a threshold.

> **Real-world hook:** this is the same up-fast/down-slow heuristic you'll recognize from Kubernetes HPA stabilization windows or EC2 Auto Scaling cooldowns — the universal autoscaler lesson that thrash is worse than a few minutes of over-provisioning.

---

## Rung 4 — Horizontal scaling: defining the table slice (§4.2)

Vertical scaling hits a wall eventually: there's a biggest box, and once a node is it, you can't go bigger. The only move left is **horizontal** — add a shard and move data onto it. But "move data" begs a question: move it in chunks of *what size*? Move a whole shard's worth at a time and you can't rebalance with any finesse — it's all or nothing. Track and move every row on its own and the bookkeeping buries you. You want something in between. Pass 1 kept saying "slice" and promising to define it later. Here it is.

### The slice, defined

Recall from Pass 1 that a sharded table is hash-partitioned: each shard owns a **hash range** for that table. We now subdivide that range.

> **Table slice:** the fine-grained unit of data placement and migration. Each sharded table's full hash space is cut into a fixed-ish number of slices — **typically 512 per table** — and those slices are distributed across the shards. A shard *owns* some subset of the 512 slices for each sharded table.

```
   sharded table "customers", hash space cut into 512 slices:

        slice 0   slice 1   slice 2  ...  slice 510  slice 511
          │         │         │              │          │
          ▼         ▼         ▼              ▼          ▼
   distributed across shards, e.g. with 8 shards each owns ~64 slices:

        shard 0:  slices { 0..63 }     shard 4: slices { 256..319 }
        shard 1:  slices { 64..127 }   ...
```

The placement map from Pass 1 is now precisely **slice → shard**: the router hashes the key, the hash picks a slice (one of 512), and the map says which shard owns that slice.

### Why ~512 — fine enough but not too fine

```
   TOO COARSE (e.g. 1 slice = the whole shard's range):
       to rebalance you'd move an entire shard's data at once — huge, all-or-nothing,
       no way to peel off "just the hot half."

   TOO FINE (e.g. millions of slices):
       the slice→shard map becomes enormous; every router caches it; every
       re-shard touches a mountain of bookkeeping. Tracking cost explodes.

   512:  small enough that you can move slices to rebalance at a useful granularity
         (peel off half = move 256 slices), yet few enough that the map stays compact
         and cheap for every router to hold and update.
```

512 is the deliberate middle: ~64 slices per shard at the common 8-shard config — enough granularity to split a hot shard roughly in half, few enough that the map is trivial to keep correct. (The paper says "typically 512"; it's a tuned default, not a hard law.)

How does a shard *represent* its slices internally? As **Postgres partitions** — each slice is a native partition of the table on that shard. This is the elegant reuse: Postgres already knows how to attach and detach partitions atomically, and the shard split (Rung 5) exploits exactly that with `DETACH PARTITION`.

### Co-located slices migrate together

Co-location (Pass 1) only works if it *survives* migration. So the rule:

```
   "customers" slice 7  and  "orders" slice 7  (co-located on cust_id)
        → ALWAYS live on the same shard
        → ALWAYS migrate TOGETHER

   moving slice 7 of customers WITHOUT slice 7 of orders would split a
   customer from their orders → the single-shard join (Pass 1) breaks → 2PC tax.
```

Corresponding slices of co-located tables move as one bundle, so the single-shard sweet spot is preserved across every rebalance. The migration unit is really "slice number N across all co-located tables," not one table's slice in isolation.

### What never splits

- **Reference tables** (full copy per shard) aren't sliced — they're replicated whole; adding a shard just gives it another full copy.
- **Standard tables** (one shard, Pass 1) **never split**. They live entirely on a single shard by definition; horizontal scaling doesn't touch them. (That's the trade-off Pass 1 named: a standard table is capped at one shard's capacity.)

> **Real-world hook:** representing slices as Postgres partitions means Limitless inherits Postgres's mature partition machinery (constraint exclusion, partition-wise joins, atomic attach/detach) rather than inventing a custom data-placement layer — the same "reuse the engine" philosophy as building 2PC on top of existing shard durability (Pass 3).

---

## Rung 5 — The shard-split workflow (§4.2): the crown jewel

Now the hard one. Don't be put off — it looks gnarly, but each phase exists to clean up the mess the previous one left, so if you follow the chain it tells its own story. The setup: a shard is hot, max vertical scaling can't save it, and we have to move some of its slices to a *new* shard. The obvious way to do that is a disaster. The real way is four phases. Let's earn each one.

### The naive approach, and why it makes things worse

```
   NAIVE:  spin up new shard → READ the migrating slices FROM the hot source shard
           → WRITE them to the new shard → update the map.

   PROBLEM: the source shard is ALREADY at capacity — that's WHY we're splitting it.
            A giant read job over its data piles MORE load onto the exact node
            that's drowning. The cure makes the disease worse. You might not even
            be ABLE to do it: at max capacity there's no spare CPU/IO to serve the copy.
```

The whole design challenge is: **split a shard that has no spare capacity to give.** Each phase below exists to keep load *off* the hot source.

### Phase 1 — Storage-level cloning (copy-on-write)

```
   PAIN:  copying data through the source's compute saturates the source.

   FIX:   don't copy through compute. Use Aurora's COPY-ON-WRITE volume clone
          (Pass 1: Aurora storage). The new shard's volume starts as a CLONE of
          the source's volume — sharing the same physical pages, copying a page
          only when one side later MODIFIES it.
```

```
   source volume ──clone──► new shard volume
        (initially share ALL physical pages; zero bulk copy)
        a page is physically duplicated only WHEN one side writes it (copy-on-write)
```

Here's why this is the whole ballgame. The clone happens **at the storage layer** — the Aurora storage service does it, *not* the source's Postgres compute. Think of it like handing someone a copy of a shared document by pointing them at the same file rather than photocopying every page: nothing moves until somebody edits. So the source's CPU and buffer cache barely notice. That's what lets you split a shard **even at max capacity** — the heavy lifting happens below the tier that's drowning. The new shard has (a clone of) the source's data almost instantly, no bulk read.

> **Pre-empt:** *"The source already runs 0–2 standbys (Pass 1). Why not just promote/fork one of those as the new shard — or stream the data over a logical replication slot — instead of cloning the volume?"* Two reasons. (1) A standby is a full warm copy of the **whole shard**, not the **subset of migrating slices**. Promoting it gives you a second copy of *everything*, then you'd *still* have to detach the slices you don't want — and worse, the fork still flows through **compute** (replay, promotion), which is the exact resource the hot source has none of. (2) A logical-replication slot streams row changes *through the source's compute and WAL decoder* — again loading the drowning node. The copy-on-write storage clone is **sub-compute** (it happens below the Postgres engine, in the storage service) and **near-instant** (it copies no pages up front). That combination — off-compute and immediate — is what neither the standby-promotion path nor the logical-slot path can offer.

### Phase 2 — Redo-log replay (catch up)

```
   PAIN:  cloning takes some (small) time, and the source DIDN'T STOP. While the
          clone was being set up, new writes kept landing on the source. The new
          shard's clone is now slightly STALE.

   FIX:   the new shard REPLAYS the source's REDO LOG from the clone point forward,
          applying everything the source committed since the clone — catching up.
```

```
   clone taken at log position P.
   source kept writing → log advanced to P+Δ.
   new shard replays  [P → P+Δ]  to converge with the source.
```

A **redo log** is just the write-ahead record of every change — the same WAL that Postgres and Aurora already keep for durability, nothing new. Replaying it is cheap and well-trodden. So the new shard chases the source's tail, like a runner trying to lap up to someone still moving. And there's the catch: while the source keeps accepting writes, there's always a little more tail to chase. You can get *close* but never quite touch. Freezing that last gap is exactly Phase 3's job.

### Phase 3 — Switchover (the brief, the only impactful, window)

```
   PAIN:  replay can get NEARLY caught up but never fully, because the source keeps
          writing the migrating slices. To finish, we must briefly FREEZE writes to
          those slices and flip ownership atomically — without corrupting in-flight work.
```

This is where we lean on **Pass 3's locking *mechanic*** — but the lock *object* is new, so name it precisely. Pass 3 used exclusive **row** locks (a writer locks the individual rows it touches). Switchover instead takes a **table-level** lock on the migrating tables — the same coarse-grained lock that **DDL** takes (Pass 3 Rung 6 previewed: a schema change locks the whole table, not rows). Why coarser? Because switchover is freezing *all* writes to those tables' migrating slices at once, not protecting a handful of rows — a table-level lock blocks them in one acquisition instead of millions of row locks.

The lock *object* is new (table-level, like DDL). The lock *release* mechanic is **identical to Pass 3**: to take a lock something else holds, you **abort the holder**. Nothing new there.

```
   STEP 1  Take a TABLE-LEVEL lock on the migrating tables at ALL routers + ALL
           shards (the same lock granularity DDL uses — NOT the row locks of Pass 3).
           Conflicting in-flight transactions are TERMINATED to force their
           lock release (Pass 3 mechanic, unchanged: abort the txn holding the lock).
           Some customer transactions get aborted here — they retry.

   STEP 2  With writes to the migrating slices frozen, the source's redo for those
           slices STOPS advancing → the new shard's replay finishes the last sliver
           and is now fully caught up.

   STEP 3  Routers UPDATE the placement map:  the migrated slices → new shard.
           (This is the authoritative slice→shard flip from Pass 1.)

   STEP 4  Source DETACHES the migrated slices  (Postgres DETACH PARTITION).
           New shard DETACHES the slices it WON'T host (it cloned the WHOLE volume,
           so it holds copies of slices that stay on the source — drop ownership of those).

   STEP 5  Release the locks.

   AFTER:  new traffic for the migrated slices routes to the NEW shard.
```

Two `DETACH`es because the clone was of the *entire* source volume — so right after Phase 1 *both* shards physically have *all* the slices. The split is finalized by each side **detaching the partitions it should no longer own**: source drops the migrated ones, new shard drops the kept-behind ones. `DETACH PARTITION` makes that an atomic metadata flip, not a data copy.

> **Stress — customer impact is confined to this switchover window.** Everything before Phase 3 (clone, replay) is invisible to the customer — no locks, no aborts, runs in the background even at max capacity. Only during switchover are **DDL and writes to the *migrating* slices blocked**, and some conflicting transactions **aborted** (they retry). Reads and all traffic to *non*-migrating slices keep flowing. The window is brief by design — replay is already nearly caught up before locks are taken, so the frozen interval is just the final sliver plus the map flip.

### Phase 4 — Clean-up

```
   PAIN:  after Phase 3 both shards still physically hold pages for slices they
          no longer OWN (a leftover of the whole-volume clone).

   FIX:   each shard BACKGROUND-DELETES the slices no longer mapped to it.
          Source reclaims space for migrated slices; new shard reclaims space for
          kept-behind slices. Lazy, off the hot path, no customer impact.
```

This is pure housekeeping — reclaiming the storage that copy-on-write left duplicated — and it happens lazily in the background, so it never competes with serving traffic.

### Who picks the split point

```
   AUTO-SPLIT (system-initiated):   HEAT MANAGEMENT picks the split point by
                                    HOT SLICES — it moves the slices carrying the
                                    most load, so the new shard actually relieves
                                    the source's pressure.

   CUSTOMER-INITIATED:              moves HALF the slices (e.g. 256 of 512 for a
                                    sharded table) — a simple, predictable rebalance.
```

> **Heat management** is the subsystem that tracks per-slice load ("heat") and decides *which* slices to peel off. Auto-split is heat-driven (target the hot slices); a customer asking for a split just gets the even-half move. Either way the *mechanism* (the 4 phases) is identical — only the choice of which slices to move differs.

> **Real-world hook:** copy-on-write cloning is the same trick behind Aurora's "fast clone" feature for dev/test database copies and behind LVM/ZFS/btrfs snapshots — share pages, diverge on write. Limitless repurposes it so a *production split of a saturated shard* costs the source almost nothing, which is the property that makes auto-scaling a hot cluster actually safe.

---

## Rung 6 — Adding a router, and the capacity trade-off (§4.2)

Shards split is the hard direction. Adding a **router** is the easy one, because routers hold no user data (Pass 1) — there are no slices to migrate.

```
   ADD A ROUTER:
     1. CLONE an existing router (it carries only metadata: topology, schema,
        placement map — copy that, not gigabytes of user data).
     2. ADD it to the topology via cluster management (the new router is registered
        as part of the shard group).
     3. REGISTER it in DNS — now the shared DNS endpoint (Pass 1 Rung 5) starts
        handing NEW connections to it too.
```

Because routers are fungible and behind one DNS endpoint, adding one is "clone metadata + announce in DNS." No data movement, no switchover window, no aborted transactions. This is the connection-scaling lever from Pass 1 Rung 3 made concrete: a connection-heavy app grows by adding routers, cheaply.

### Where the new node's capacity comes from

Whether you add a shard or a router, the new node needs ACUs — and the budget is fixed at `shardGroupMaxACU`:

```
   new node's budget  =  UNCONSUMED capacity within shardGroupMaxACU.

   if there's spare headroom under the ceiling  → the new node is allocated from it.
   if the budget is fully consumed              → the add is REJECTED (no spare to give).
```

You can't conjure capacity out of nowhere. A horizontal add succeeds only if the budget has room to spare; if it doesn't, you raise `shardGroupMaxACU` first — that's the only knob (Rung 2). Notice what this enforces: horizontal and vertical scaling both drink from the *same* budget, so neither can quietly cheat the spend cap. One pie, two ways to cut it.

> **The edge case — heat vs the spend cap.** This bites even **auto-splits** (Rung 5, system-initiated to relieve a hot shard). An auto-split needs a *new* shard, which needs ACUs from unconsumed budget. If the budget is **fully consumed**, there is no headroom — so the split **cannot proceed**, and the hot shard simply **stays hot**. The system does not silently overspend to cool it. `shardGroupMaxACU` is a **hard wall, by design — even against heat management**: the customer's spend cap outranks the system's own desire to relieve pressure. The only fix is for the customer to raise `shardGroupMaxACU`, which frees headroom and lets the pending split (and the relief it brings) finally run.

### The customer's real lever: vertical vs horizontal

```
   VERTICAL (bigger nodes):     fast (Serverless V2, in place, no data move),
                                bounded by the biggest single node. Good for
                                spikes and for fitting a hot working set in cache.

   HORIZONTAL (more nodes):     unbounded scale, but a shard add costs a SPLIT
                                (the 4 phases, with a brief switchover window).
                                Good for sustained growth past one node's ceiling.
```

The system uses vertical scaling for fast response and horizontal scaling for structural growth — but **the customer's ultimate lever is just `min/max ACU`**. Set the band, and the system decides moment-to-moment how much is bigger-nodes vs more-nodes, re-slicing budgets (Rung 3) and splitting shards (Rung 5) underneath. That single knob, back where we started in Rung 2, is the whole control surface.

> **Real-world hook:** this mirrors how you'd reason about scaling any service — vertical first because it's instant and disruption-free, horizontal when you hit the single-box wall — except Limitless makes both automatic under one budget, so the customer never files a capacity ticket.

---

## What this pass nailed down

```
THE PAIN (Rung 1):  fixed size = waste OR saturation. Two axes (bigger vs more),
                    two independent node types (routers vs shards) → 4 levers.

ACUs + SIZING (Rung 2):
   1 ACU ≈ 2 GB memory (binding resource) + proportional CPU/network.
   customer sets shardGroupMin/MaxACU ONLY. initial router/shard COUNTS come from
   MAX via Table 1 (1101–1200 band → 4 routers + 8 shards; total=routers+shards;
   ~1:2 router:shard ratio). bigger budget → more nodes (single node has a ceiling).
   standbys match their primary, NOT in the budget.
   anchors: common = 4/8 (a Table-1 row); biggest OBSERVED in production = 32/64
   (§4 prose, NOT a Table-1 row — a group grown via splits past its initial size).

VERTICAL (Rung 3, §4.1):
   each node has a min/max ACU band; Serverless V2 floats it inside, no restart.
   start EVEN, then re-slice:  dynamicMaxACU_i = shardGroupMaxACU × consumed_i / Σconsumed.
   hot nodes grow demand fastest → give them the headroom.
   shard extra ACU → BUFFER CACHE; router extra ACU → HEAP.
   scale-up FAST (spikes), scale-down CONSERVATIVE (avoid thrash).

HORIZONTAL — SLICES (Rung 4, §4.2):
   slice = fine-grained migration unit; ~512 per sharded table (fine enough to
   rebalance, few enough to track); a shard stores its slices as Postgres PARTITIONS.
   co-located tables' matching slices migrate TOGETHER (preserve single-shard join).
   reference tables replicate whole; STANDARD tables never split.

SHARD SPLIT (Rung 5, §4.2) — 4 phases:
   1 CLONE  copy-on-write volume clone at the STORAGE layer (not via source compute)
            → split even at max capacity, source barely touched.
   2 REPLAY new shard replays source redo to catch up (source kept writing).
   3 SWITCHOVER  take a TABLE-LEVEL lock (DDL granularity, NOT Pass 3's row locks)
            on migrating tables at all routers+shards (release mechanic IS Pass 3:
            abort the holder); replay finishes; update slice→shard map;
            source DETACHes migrated slices, new shard DETACHes kept-behind ones;
            release locks. ← the ONLY customer-impact window (brief; DDL+writes to
            migrating slices blocked, some txns aborted/retried).
   4 CLEANUP both shards background-delete slices they no longer own.
   auto-split picks HOT slices (heat management); customer split moves HALF.

ADD ROUTER + TRADE-OFF (Rung 6, §4.2):
   clone metadata → add via cluster mgmt → register in DNS. no data move.
   new node's budget = UNCONSUMED capacity; rejected if none spare. even an
   AUTO-SPLIT stalls if budget is exhausted → hot shard stays hot until the
   customer raises shardGroupMaxACU (spend cap is a hard wall vs heat management).
   vertical = fast/bounded; horizontal = unbounded/costs a split.
   customer's only lever stays min/max ACU.
```

Deferred and where it lives:
- **Failover / recovery durability** (§6) → Pass 6 — *how* the cloned volume and lead-shard state survive a crash mid-split.
- **DDL coordination** (§5.7) → later pass — switchover blocks DDL on migrating tables; the full multi-node DDL protocol is its own pass (previewed in Pass 3 Rung 6).

---

## The 3 checkpoint questions

Answer in your own words. They tell me what to reinforce next.

1. **From budget to topology.** A customer sets `shardGroupMaxACU = 1200` and runs **1 standby per shard**. (a) Roughly how many routers and shards does the group start with, and where does that come from? (b) Is the standbys' capacity drawn from the 1200? Explain why that choice is the right incentive. (c) Why does a *bigger* budget map to *more* nodes rather than just bigger ones?

2. **Re-slice the budget.** A 1200-ACU group has 4 shards consuming `400, 200, 100, 100`. (a) Compute each shard's `dynamicMaxACU` with the proportional formula, and confirm the ceilings still sum to the budget. (b) Explain why proportional-to-consumption beats both an even split *and* "give everyone the full 1200." (c) Same extra ACU on a shard vs a router buys different things — what, and why does Pass 1's role split explain it?

3. **Walk the shard split.** A saturated shard at max capacity must shed half its slices. (a) Why is the naive "read the source, write the new shard" approach self-defeating, and what specifically lets Phase 1 avoid it *even at max capacity*? (b) Why is Phase 2 (redo replay) necessary at all after the clone? (c) In Phase 3, name exactly what gets locked *and at what granularity* (and how that differs from Pass 3's lock object), which Pass-3 mechanic forces the lock release, and what the customer observes during the window — and why that window is *brief*. (d) Why are there *two* `DETACH PARTITION`s (one per shard)? (e) An auto-split fires to relieve a hot shard, but `shardGroupMaxACU` is fully consumed. What happens, and what is the only fix?

**Also flag:**
- **The 4-phase split (Rung 5):** did the *per-phase pain* land — i.e. is it clear that each phase exists to fix the previous phase's leftover problem (clone is stale → replay; replay can't finish → switchover freeze; clone duplicated everything → cleanup)? Or did it read as four steps without the connective tissue?
- **Copy-on-write cloning (Phase 1):** did "the clone is built at the storage layer, not via source compute" click as *the* reason a max-capacity shard can be split, or still feel hand-wavy?
- **The slice (Rung 4):** did **~512** land as a justified middle (fine enough / few enough), or as memorized trivia? Did "co-located slices migrate together → preserves the single-shard join" connect back to Pass 1, or feel like a new unrelated rule?
- **Vertical re-slicing (Rung 3):** did the proportional formula feel motivated ("aim spare budget at the heat"), or arbitrary? Did up-fast/down-slow feel principled?
- Any term you'd struggle to define unaided: **ACU, Serverless V2, min/max ACU band, dynamic min/max ACU, table slice, shard split, switchover window, heat management.**
