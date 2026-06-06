# Topic

**Aurora PostgreSQL Limitless Database: Building a Highly Scalable OLTP Database** (Amazon Web Services, SIGMOD Companion '26).

How AWS extends single-primary Aurora PostgreSQL into a horizontally-scaled, strongly-consistent distributed OLTP database — without an application-level sharding burden and without a centralized transaction manager. The load-bearing idea is **time-based MVCC** (snapshots and commit order defined by physical timestamps from Amazon Time Sync) glued to a **non-blocking, lead-shard 2PC**.

# Source material

- `./Aurora PostgreSQL Limitless Database.pdf`
- `./Aurora PostgreSQL Limitless Database.epub`

Section map (paper §-numbers): §2 Data Model · §3 Architecture (3.1 Aurora storage, 3.2 data plane, 3.4 HA) · §4 Adaptive Scaling (4.1 vertical, 4.2 horizontal/shard-splits/table-slices) · §5 Concurrency Control + Commit (5.1 Time Sync, 5.2 time-based MVCC, 5.3 write-conflict, 5.4 time-aware 2PC, 5.5 reading from shards, 5.6 real-time order/commit-wait, 5.7 DDL, 5.8 deadlocks, 5.9 Read Committed) · §6 Failover/Backups/Recovery · §7 Query Processing (pushdown, FDW, joins) · §8 Evaluation (HammerDB).

# Learner profile

- Senior backend engineer (~10 years). Strong with SQL and the *application* view of Postgres transactions (isolation levels, commits, deadlocks).
- Weaker on Postgres MVCC internals (xid/xmin/xmax/snapshot xip_list), distributed-clock reasoning (clock skew, HLC, TrueTime-style bounds), and 2PC failure modes.
- Has not used Aurora's storage internals; treat "Aurora storage replicates 6 ways across 3 AZs" as something to explain, not assume.
- Prefers re-readable prose over compact prose. Flag every unexplained number (ACU, CEB, slice counts, NOPM figures).

# Teaching rules

(On top of the load-bearing rules baked into the pass-builder / pass-reviewer agents.)

- **Clocks are the spine.** Every concurrency-control pass must tie back to the single invariant: *a write is visible to a reader iff `write.commitTs <= reader.startTs`* (paper Property 1). Re-state it when it's used; don't assume the learner carries it across passes.
- **Always contrast with stock PostgreSQL.** Aurora Limitless replaces xid-set snapshots with scalar timestamps. Show the stock-Postgres mechanism that fails at distributed scale *before* introducing the Aurora replacement.
- **Name the simpler distributed design and why it's rejected** — e.g. "why not a central timestamp oracle (Greenplum)? why not just delay the read until the shard's clock catches up (Clock-SI)?" The paper makes these contrasts explicitly; use them as ladder rungs.
- **Toy timestamps first.** Use small integer timestamps (startTs=100, CEB=5) before quoting real figures (CEB <1ms, microseconds in some regions).
- **Single-shard is the sweet spot.** Keep returning to the fact that read-only and single-shard transactions skip 2PC — it's the performance thesis of the whole system.
- Max 1 pass per major mechanism. Prefer splitting §5 across multiple passes over cramming it.
