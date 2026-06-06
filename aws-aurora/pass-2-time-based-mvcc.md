# Pass 2 — Time-based MVCC: snapshots, visibility, write-conflicts, commit-wait

> **Goal:** after reading, you can explain *exactly* how Aurora decides what a transaction sees — using two scalar timestamps instead of stock Postgres's in-flight xid set — derive Property 1 from first principles, walk a write-conflict abort, and explain why a committer *waits* before telling the client "done."
> **Reading time:** ~22 minutes.
> **Method:** the usual ladder. Each rung is the simplest design that **fails**; the next rung fixes the named pain.
> **Scope guard:** **single-shard / non-distributed view only.** We build the MVCC engine that runs *inside one shard*. We deliberately do **not** cover the cross-shard 2PC commit protocol, nor how a reader assembles a consistent cut *across* shards, nor how a reader copes with a *writer on another shard* whose clock is skewed or whose transaction is half-prepared. Those are **Pass 3**. We *do* cover **commit-wait** here, because it is about a single committing transaction establishing real-time order — it needs no second shard to explain.

Vocabulary carried from Pass 0/1: **shard**, **router**, **single-shard sweet spot**, **Time Sync** (Amazon's hardware clock service), **CEB** (ClockErrorBound — the bound on how far a local clock may differ from true time), **Property 1** (the visibility invariant, previewed in Pass 0 Rung 5).

New terms this pass defines inline: **xid**, **xmin/xmax**, **xip_list**, **MVCC**, **commit-log (xid→commitTs map)**, **Snapshot Isolation (SI)**, **first-committer-wins**, **commit-wait**, **Strong SI**.

---

## Rung 1 — How stock PostgreSQL decides visibility, and the two ways it fails at scale

Before we can appreciate Aurora's timestamp trick, we need the stock-Postgres mechanism it *replaces*, in mechanical detail. This is the rung Pass 0 sketched; here we make it concrete enough to break.

**MVCC** = Multi-Version Concurrency Control: instead of overwriting a row in place, the database keeps multiple *versions* of each row, and each reader is shown the version that was current "as of" its transaction. Readers never block writers and writers never block readers — they just see different versions.

In stock Postgres, the bookkeeping for "as of" is built on integers called **xids**.

```
A modifying transaction grabs an xid from ONE global counter:

         global xid counter (lives on the single primary)
                 │ increments by 1 per writing txn
                 ▼
   txn P → xid 100      txn Q → xid 101      txn R → xid 102   ...
```

- An **xid** (transaction id) is a monotonically increasing integer handed out by a single global counter on the primary. Only *writing* transactions consume one. (Read-only transactions don't need an xid — keep that in mind, it echoes the "read-only skips work" theme.)
- Every stored row version carries two of these xids:
  - **xmin** — the xid of the transaction that *created* this version.
  - **xmax** — the xid of the transaction that *superseded* it (updated or deleted it). If the version is still current, xmax is empty.

So a row that was inserted by txn 100 and later updated by txn 102 leaves **two** versions on disk:

```
   row "balance for cust 7"
   ┌──────────────────────────────────────────────┐
   │ version A:  value=50   xmin=100   xmax=102     │  ← created by 100, replaced by 102
   │ version B:  value=80   xmin=102   xmax=(none)  │  ← created by 102, still current
   └──────────────────────────────────────────────┘
```

### The snapshot: a *set* of in-flight xids

When a transaction needs to read, it takes a **snapshot** — its definition of "what counts as already-committed for me." Disambiguation (same as Pass 0): *snapshot* here means a **visibility set**, not a storage backup.

A stock-Postgres snapshot is the triple `(xmin, xmax, xip_list)`:

```
snapshot taken by a reader:
   xmin     = lowest xid still running   (anything below this is definitely settled)
   xmax     = next xid to be assigned    (anything >= this hasn't started; treat as invisible)
   xip_list = the explicit set of xids that are IN-PROGRESS right now
              ("xip" = eXtant/In-Progress)
```

Visibility rule, in words: **a row version is visible if the transaction that created it (xmin) is committed *and* is not in my `xip_list`.** A version whose creator is still in-flight (in the list), or hasn't started yet (>= snapshot's xmax), is invisible.

Toy example. Reader takes a snapshot at the moment xids 100 and 101 have committed, 102 is still running, and 103 hasn't started:

```
   snapshot = ( xmin=102, xmax=103, xip_list={102} )

   version A   xmin=100   → 100 committed, not in {102}  → VISIBLE
   version B   xmin=102   → 102 is in xip_list           → NOT visible (still in-flight)
```

The reader sees value=50 (version A), *not* the 80 written by the still-running 102. Correct MVCC behavior.

### Pain (a): building that set is a contention point

Every time a transaction takes a snapshot it must compute the current set of in-flight xids. That means walking the shared list of active transactions under a lock. At low concurrency this is cheap. At thousands of transactions per second across many cores, that shared structure becomes a measured hot spot — Postgres's `ProcArrayLock` is a well-known scalability bottleneck precisely because snapshotting contends on it. The cost grows with the *number of concurrent transactions*, which is exactly the regime an OLTP scale-out database lives in.

### Pain (b): the set doesn't even *exist* across machines

This is the fatal one for a distributed database. The `xip_list` is the set of transactions in-flight **on one primary**. There is no global counter handing out xids across four independent shards, and no single node that knows the union of all shards' in-flight transactions. You could *build* one — a central xid oracle that every shard consults — but that reintroduces the exact bottleneck and single point of failure the whole architecture exists to escape (Pass 0 Rung 4 named this).

```
   shard 0 in-flight: {100, 102}      shard 2 in-flight: {77, 90}
   shard 0 in-flight: {100, 102}      shard 3 in-flight: {201}
              ↑ no node knows the UNION, and there is no shared counter ↑
```

**The pain, stated plainly:** the snapshot is a *set*, and a set is (a) expensive to compute under contention and (b) impossible to assemble globally without a central coordinator. We need a notion of "what's visible" that every shard can evaluate **alone**, cheaply, with no shared state.

> **Real-world hook:** the `ProcArrayLock` snapshot bottleneck is real enough that stock Postgres has had multiple patches over the years to reduce snapshot cost (e.g. the "snapshot scalability" work in PG 14). Aurora sidesteps the whole structure instead of optimizing it.

---

## Rung 2 — The fix: throw away the set, keep a single scalar timestamp

Here is the move the entire paper pivots on. Replace the three-part `(xmin, xmax, xip_list)` snapshot with **one number**: a timestamp drawn from a physical clock (Amazon Time Sync).

Each transaction gets two timestamps:

```
   startTs   — assigned at the transaction's FIRST query.
               Value = now().latest  (the high end of the Time Sync interval; why "latest" is a Pass 3 detail).
               Defines WHAT THE TXN CAN SEE.

   commitTs  — assigned at commit time.
               STAMPS WHAT THE TXN WROTE.
```

> Toy numbers first (as CLAUDE.md demands): we'll use small integers like `startTs=100`, `commitTs=60`, `CEB=5`. Real figures — CEB under 1 millisecond, microsecond-scale in some regions — are quoted only at the end, and flagged "verify against §5.1." The point is never the magnitude; it's that CEB is **small, bounded, and non-zero.**

### Property 1 — the load-bearing invariant

> **Property 1:** a transaction `T` sees the writes of a transaction `T'` **if and only if** `T'.commitTs <= T.startTs`.

That is the whole visibility rule. One comparison of two scalars. We will re-state it every time we use it (CLAUDE.md rule: don't assume the learner carries it across rungs).

```
toy:  reader T has startTs = 100

   writer T' committed at commitTs = 60   →  60 <= 100  →  T sees T''s writes
   writer T''  committed at commitTs = 140 → 140 <= 100 →  T does NOT see them (committed in T's future)
```

### Why a scalar lets each shard decide alone

Re-derive the unlock, because it's the reason the set had to go. A set membership test (`is xid 102 in xip_list?`) needs the *whole set*, which needs global knowledge. A scalar comparison (`60 <= 100?`) needs only the two numbers already in hand:

- The reader **carries its `startTs`** to every shard it touches. That's one integer in the request.
- Each row version on a shard already knows the `commitTs` of its creator (next rung shows how).
- So each shard answers "visible?" **locally**, with no shared in-flight list, no central counter, no lock on a global structure.

```
   reader carries  startTs=100  ──┬──► shard 0:  compare locally
                                   ├──► shard 2:  compare locally
                                   └──► shard 3:  compare locally
   no shard needs to know what any OTHER shard or txn is doing.
```

This simultaneously kills **both** pains from Rung 1: no expensive set to build (Pain a), and nothing global to assemble (Pain b). The snapshot shrank from "a contended set" to "a number you put in your pocket."

> **Pre-empt the obvious alternative:** *why not a central timestamp oracle that hands out a global ordering* (as Greenplum / some MPP systems do)? Because that's the same shape as the central xid counter — one node every transaction must consult, a throughput ceiling and a single point of failure. Aurora's bet is that a *physical clock at each node*, bounded by Time Sync's CEB, removes the need to consult anyone. The cost of that bet (clock skew) is what Rung 5 and Pass 3 pay down.

> **Real-world hook:** this scalar-timestamp idea is the same family as Google Spanner's TrueTime commit timestamps. Aurora applies it inside a PostgreSQL-compatible engine rather than a from-scratch database.

---

## Rung 3 — Time-based version visibility: same row format, timestamp decision (§5.2)

We just said "each row version knows its creator's `commitTs`." But Rung 1 showed Postgres rows carry `xmin`/`xmax`, which are **xids, not timestamps**. Did Aurora rewrite the on-disk row format? **No** — and seeing why not is this rung.

### The row format is unchanged

A row version still stores `xmin` (creator xid) and `xmax` (superseder xid), exactly as stock Postgres. What changes is the *question asked of those fields at read time*. Aurora needs to turn an xid into a `commitTs`. It does that with a structure Postgres already has.

```
   xid → commitTs lookup lives in the COMMIT LOG
   (Postgres already keeps a per-xid commit record; Aurora stores the
    transaction's commitTs in/alongside it. No row-format change.)

        xid 100  →  commitTs 60
        xid 102  →  commitTs 140
```

The **commit-log** here means Postgres's existing per-transaction commit metadata (the structure stock Postgres uses to record "xid 100 committed"). Aurora records the `commitTs` there too. So a row version still says "I was made by xid 100"; the engine looks up "xid 100 committed at 60" and applies Property 1.

> **Pre-empt:** *"so did they change the row layout?"* No. The row header carrying xmin/xmax (each a 4-byte xid in Postgres) is untouched. Only the *interpretation* changes (xid → commitTs via the commit log) and the commit log gains a timestamp. This is a deliberate choice: keeping the row format identical means existing Postgres storage, vacuum, and page logic largely carry over.

### The visibility test, restated for versions

A version is visible to reader `T` iff:

```
   xmin.commitTs <= T.startTs  <  xmax.commitTs
   └─ creator committed at/before I started ─┘ └─ superseder committed AFTER I started ─┘
```

The right-hand `xmax` check is **skipped if there is no next version** (xmax empty = still current). The left side *is* Property 1 applied to the creator; the right side is Property 1 applied to the superseder, negated ("the version that replaced this one is NOT yet visible to me, so this one still is").

### Toy multi-version row, three readers

```
   row "balance for cust 7"
   ┌───────────────────────────────────────────────────────────────┐
   │ version A: value=50  xmin=100(commitTs 60)  xmax=102(commitTs 140) │
   │ version B: value=80  xmin=102(commitTs 140) xmax=(none)            │
   └───────────────────────────────────────────────────────────────┘

   Reader R1  startTs = 50:
      A:  60 <= 50 ?  NO  → A not visible (its creator committed after R1 started)
      B: 140 <= 50 ?  NO  → B not visible
      → R1 sees NEITHER version → the row did not exist for R1 yet. Correct.

   Reader R2  startTs = 100:
      A:  60 <= 100 < 140 ?  YES and YES → A VISIBLE
      B: 140 <= 100 ?        NO          → B not visible
      → R2 sees value=50. Correct (the update at 140 is in R2's future).

   Reader R3  startTs = 200:
      A:  60 <= 200 < 140 ?  60<=200 YES, but 200<140 NO → A NOT visible (superseded)
      B: 140 <= 200 ?        YES, no xmax → B VISIBLE
      → R3 sees value=80. Correct.
```

Notice each reader needed only its own `startTs` and the row's two commit timestamps — no in-flight set, no other shard. That is the Rung 2 promise made mechanical.

> **Real-world hook:** because visibility is now "find the version whose commitTs window straddles my startTs," a long-running reporting query with a fixed `startTs` automatically sees a stable, consistent view of every table for its whole duration — without holding any lock — which is exactly what you want for an analytics scan over an OLTP store.

---

## Rung 4 — Write-conflict detection: Snapshot Isolation, first-committer-wins (§5.3)

Visibility (Rungs 2–3) governs *reads*. It says nothing about two transactions trying to write the *same* row concurrently. If both just stamped a new version, you'd get a **lost update** — one write silently clobbering the other. We need a rule for write conflicts. Here Aurora does **exactly what stock Postgres does** — this rung is mostly "good news, nothing new to learn," but you must see *why* the timestamp scheme doesn't change it.

**Snapshot Isolation (SI)** is the isolation level: every transaction reads from its own consistent snapshot (Property 1), and write conflicts are resolved by **first-committer-wins** — if two transactions modify the same row, the one that commits first wins; the other must abort.

The mechanism, step by step, when transaction `T` modifies a row:

```
   1. T takes an EXCLUSIVE ROW LOCK on the row.  Held until T commits/aborts.
      (This serializes WRITERS to the same row — only one at a time past this point.)

   2. After locking, T RE-CHECKS: is there a newer committed version of this row
      that is OUTSIDE my snapshot (i.e. a version whose creator committed
      AFTER my startTs)?

   3a. If NO such newer version  → T proceeds, writes its new version.
   3b. If YES (someone committed a change to this row since I started)
                                  → T ABORTS.  First-committer-wins.
```

The row lock in step 1 means two writers to the same row can't *both* be in step 2 at once; the loser waits for the winner to release the lock at commit, then its step-2 re-check finds the winner's freshly-committed version and aborts it.

### Toy example: two concurrent writers

```
   Both want to update "balance for cust 7", current version has commitTs 60.

   T1  startTs=100      T2  startTs=100
   ─────────────────────────────────────────────────────
   T1: lock row 7  ✓ (gets it first)
   T2: lock row 7  … BLOCKS (T1 holds it)
   T1: re-check — newest committed version commitTs=60, 60<=100, inside snapshot → OK
   T1: write new version, COMMIT at commitTs=130, release lock
   T2: lock row 7  ✓ (now free)
   T2: re-check — newest committed version is now commitTs=130, and 130 > 100 (T2.startTs)
                  → a newer version committed after T2 started → T2 ABORTS.
```

Had T1 *aborted* instead of committing, T2 would acquire the lock, find no newer committed version (60 is still newest, and `60 <= 100`), and proceed — the lock loser only aborts when the lock winner actually committed a conflicting version.

T2 must retry with a fresh `startTs` (e.g. 140), at which point it would *see* T1's write and can update on top of it. This is precisely stock-Postgres SI behavior (`ERROR: could not serialize access due to concurrent update`), and it is unchanged because **first-committer-wins only needs to compare commit timestamps to the reader's startTs** — which the scalar scheme already gives us for free.

> **Pre-empt:** *why row locks at all if timestamps order everything?* Because timestamps order *committed* writes after the fact, but two in-flight writers haven't committed yet — without a lock both would pass their re-check simultaneously and one update would be lost. The lock is what forces the serialization point so first-committer-wins has a "first" to point at.

> **Scope flag:** this is the **single-shard** conflict story. When the two writers live on *different* shards, or the conflicting write is part of a half-prepared distributed transaction, the re-check needs more — that's **Pass 3**.

> **Real-world hook:** an inventory-decrement under flash-sale contention is exactly this: many transactions racing to update one stock row. SI/first-committer-wins means losers get a clean serialization error and retry, rather than silently overselling.

---

## Rung 5 — The remaining gap: Property 1 alone doesn't guarantee real-time order (§5.6)

We have a working single-shard MVCC engine. Now the subtle pain that motivates **commit-wait**. This rung needs no second shard — it's about one committing transaction and one later transaction, and the fact that **clocks are intervals, not points.**

### The gap

Property 1 says `T` sees `T'` iff `T'.commitTs <= T.startTs`. That's a statement about *timestamps*. But timestamps come from physical clocks bounded by CEB, so a timestamp and the *real-world moment* it was taken don't line up exactly. This lets a nasty anomaly through:

```
   T1 commits, picks commitTs = 100, and immediately returns "OK" to the client.
   The client, now knowing T1 is done, starts T2.
   T2 reads its startTs from the local clock — but that clock, within its CEB,
      could hand back startTs = 98.

   Property 1:  is T1 visible to T2?   100 <= 98 ?  NO.
   → T2 does NOT see T1, even though T2 started AFTER T1 finished in real time.
```

That violates the intuition any application relies on: *if I committed a write, then started a new transaction, the new transaction sees my write.* This is the difference between plain SI and **Strong SI**.

> **Strong Snapshot Isolation** = SI **plus** the real-time guarantee: if `T2` begins after `T1` has *returned to the client*, then `T2` must see `T1` (`T2.startTs > T1.commitTs`). Strong SI is what makes the database behave "as if" there were one global clock, despite there being many skewed ones.

### The mechanism: commit-wait (Spanner-style)

The fix is to make `commitTs` *true*: don't let a transaction claim it committed at time `commitTs` until the **real time has provably passed `commitTs` everywhere**. Concretely, after picking `commitTs`, the committer does **not acknowledge the client** until:

```
   now().earliest  >  commitTs
   └─ the LOW end of the local Time Sync interval ─┘
```

`now().earliest = local_clock - CEB`. Waiting until even the *pessimistic* low end of the interval exceeds `commitTs` guarantees real time is past `commitTs`. This deliberate delay is **commit-wait**.

### The 3-step interval argument (from §5.6), with toy numbers

Let CEB = 5 (toy). T1 commits at `commitTs = 100`.

```
   Step 1 — T1 doesn't ack until now().earliest > 100.
            now().earliest = clock1 - CEB.  T1 waits until clock1 - 5 > 100, i.e. clock1 > 105.
            Let t1 = the real moment T1 finally returns to the client.
            Because we waited for the pessimistic low end:   t1 >= now().earliest > T1.commitTs = 100.
            So:  t1 > 100.                                              ... (i)

   Step 2 — T2 starts AFTER T1 returns, at real moment t2.
            "after" means:  t2 > t1.                                    ... (ii)

   Step 3 — T2's startTs is read as now().latest = clock2 + CEB at moment t2.
            now().latest is an UPPER bound on real time, so:  T2.startTs = now().latest >= t2.
            So:  T2.startTs >= t2.                                      ... (iii)

   Chain them:  T2.startTs >= t2  (iii)
                          > t1    (ii)
                          > 100   (i)
                = T1.commitTs.

   Therefore  T2.startTs > T1.commitTs  →  by Property 1, T2 SEES T1.   ✓ Strong SI.
```

The two halves are deliberately asymmetric and that's the point: the **committer** waits on the *pessimistic low* end (`earliest`) so its commitTs is provably in the real past; the **reader** takes the *pessimistic high* end (`latest`) so its startTs provably covers real "now." Squeezing from both ends closes the CEB gap that let the anomaly through.

```
   timeline (toy, CEB=5):

   T1.commitTs=100
   ├──────── commit-wait until now().earliest > 100 ────────┤
                                                            t1  (ack to client)
                                                              │
                                                  client starts T2 at t2 > t1
                                                                  │
                                              T2.startTs = now().latest >= t2 > t1 > 100
```

### Why this is usually free

Commit-wait sounds like "add CEB latency to every commit." It mostly doesn't, for one structural reason: **a committing transaction must already flush its log to Aurora storage durably (6-way, 2-per-AZ — Pass 1 Rung 4), and that storage write takes longer than CEB.** The commit-wait runs **in parallel** with the storage flush. Since the storage round-trip (network + quorum acknowledge) typically exceeds the small CEB, the wait is already over by the time durability lands. Net added latency: usually ~0.

```
   commit:  pick commitTs ──┬── flush log to Aurora storage  (the SLOW part) ─────┐
                            └── commit-wait until now().earliest>commitTs (fast) ─┘
            ack client only when BOTH done  →  dominated by the storage flush, not the wait.
```

> Real numbers (verify against §5.1/§5.6): CEB is reported **under 1 ms**, microsecond-scale in some regions, while a durable storage quorum write is typically larger. So commit-wait is hidden under the flush in the common case. Treat exact magnitudes as "from the paper; verify."

> **Pre-empt the rejected alternative:** *why not delay the READ until the shard's clock catches up* (Clock-SI style) instead of making the writer wait? Because that taxes the common path — reads are the bulk of OLTP traffic, and read-only transactions are supposed to be the cheap path (Pass 0 Rung 7). Aurora pushes the wait onto the *committer*, where it hides under the flush, rather than onto every reader.

> **Real-world hook:** this is the same guarantee Spanner buys with TrueTime "commit wait." It's what lets an app do "POST /order → 200 OK → GET /order" and *always* see the order it just created, even though the two requests may land on different nodes with independently-drifting clocks.

---

## What this pass nailed down

```
stock PG snapshot   = (xmin, xmax, xip_list)   ← a SET; contended to build, impossible across machines
        ↓ replace with
Aurora snapshot     = startTs  (a scalar from Time Sync, taken at first query)
                      commitTs (a scalar, taken at commit)

Property 1          : T sees T'  ⇔  T'.commitTs <= T.startTs      (one comparison, evaluated per-shard)

version visibility  : xmin.commitTs <= T.startTs < xmax.commitTs  (xmax skipped if current)
                      row format UNCHANGED; xid→commitTs via the existing commit log

write conflicts     : SI, exclusive row lock to commit + re-check, first-committer-wins (= stock PG)

commit-wait (§5.6)  : committer holds the client ack until now().earliest > commitTs
                      → upgrades plain SI to STRONG SI (real-time order)
                      → runs parallel to the storage flush, so ~free (flush > CEB)
```

What we deliberately did **not** touch — and where it lives:
- How `startTs` and `commitTs` are agreed **across shards**, and the **lead-shard 2PC** that makes a multi-shard write atomic → **Pass 3** (§5.4).
- How a **reader on a shard** handles a *writer whose clock is skewed*, or a transaction that is **half-prepared** (in 2PC's prepared-but-not-committed limbo) → **Pass 3** (§5.5). This is the natural next question: "Property 1 is fine if every commitTs is settled — but what does a reader do when it lands on a version whose committer is still in-flight or hasn't finished commit-wait?" That's exactly the cross-shard read problem.

---

## The 3 checkpoint questions

Answer in your own words. They tell me what to reinforce in Pass 3.

1. **Stock Postgres takes a snapshot as `(xmin, xmax, xip_list)`; Aurora takes it as a single `startTs`.** Name the *two distinct* reasons the set-based snapshot fails for Aurora (one is about a single busy node, one is about multiple machines), and explain precisely why a scalar fixes *both*.

2. **Walk this multi-version row for a reader with `startTs = 120`:** version A `xmin commitTs=40, xmax commitTs=120`; version B `xmin commitTs=120, xmax none`. Which version is visible, and what does the strict-vs-non-strict inequality in `xmin.commitTs <= startTs < xmax.commitTs` decide at the boundary value 120?

3. **Commit-wait makes the committer pause before acking the client.** Reconstruct the 3-step interval argument (using `now().earliest` for the writer and `now().latest` for the reader) showing that a transaction starting *after* a commit returns must see that commit. Then explain why this pause usually adds ~0 latency in Aurora specifically.

**Also flag:**
- Any rung where the **pain** didn't feel concrete — especially Rung 5: did the "T2 starts after T1 finishes but doesn't see it" anomaly feel like a *real* bug before commit-wait fixed it, or did it feel manufactured?
- Any term you'd struggle to define unaided: **xid, xmin/xmax, xip_list, MVCC, commit-log (xid→commitTs map), Property 1, Snapshot Isolation, first-committer-wins, now().earliest / now().latest, CEB, commit-wait, Strong SI.**
- Whether the **asymmetry** in commit-wait (writer waits on `earliest`, reader reads `latest`) landed as "obviously why it closes the gap" or as a formula to memorize — that asymmetry recurs all through Pass 3.
- Whether keeping the **row format unchanged** (xid stays on the row, commitTs lives in the commit log) was convincing, or whether you still suspect Aurora must have rewritten storage.
