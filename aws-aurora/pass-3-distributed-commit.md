# Pass 3 — Distributed commit: time-aware 2PC and reading from shards

> **Goal:** after reading, you can walk a multi-shard write committing atomically on **one** agreed `commitTs`; explain why the coordinator lives at a **lead shard** instead of the router; recover the protocol when the router dies; and — the hard part — explain how a reader on a shard stays consistent despite clock skew, a half-prepared writer on another shard, and a transaction caught mid-flush.
> **Reading time:** ~30 minutes. This is the hardest pass.
> **Method:** the usual ladder. Each rung is the simplest design that **fails**; the next rung names the pain and fixes it.
> **Scope guard:** this is the **cross-shard** pass. We cover multi-shard *commit* (§5.4) and the three tricky cases of *reading from shards* (§5.5), closing on a distributed-deadlock hook (§5.8). We **defer**: DDL's multi-node locking (§5.7), Read Committed (§5.9), and failover/recovery durability (§6). We will say *that* the lead shard's durable state is what makes recovery possible — Pass 6 details *how* that durable state survives a crash.

**Carried from Pass 2 (restated when first used, not re-derived):**
- **Property 1** — `T` sees `T'` **iff** `T'.commitTs <= T.startTs`. The whole visibility rule, one scalar comparison.
- **startTs** = `now().latest` (the high end of the Time Sync interval) taken at the txn's first query.
- **commitTs** — taken at commit, stamps what the txn wrote.
- **commit-wait** — a committer holds the client ack until `now().earliest > commitTs`, upgrading SI to Strong SI.
- **CEB** (ClockErrorBound) — the bound on how far a local clock may differ from true time; `now().earliest = clock - CEB`, `now().latest = clock + CEB`. Small, bounded, non-zero.
- **row locks / first-committer-wins** — a writer takes an exclusive row lock and re-checks for a newer committed version; the lock loser aborts.

**New terms this pass defines inline:** **2PC** (two-phase commit), **prepare / commit-prepared**, **coordinator**, **lead shard**, **prepare timestamp**, **PREPARED / COMMITTING state**, **HLC** (hybrid logical clock), **waits-for graph**, **distributed deadlock**.

---

## Rung 1 — The pain: timestamps make each shard decide alone, but a multi-shard write must be atomic AND share ONE commitTs

Pass 2 closed on exactly this cliff. Let's make the pain concrete before reaching for the tool.

Time-based MVCC is *gloriously local*. A reader carries one `startTs`; each shard answers "visible?" with `commitTs <= startTs`, knowing nothing about any other shard. That independence is the entire point — no central coordinator, no shared in-flight set.

But now consider a **multi-shard write**: a transfer that debits a customer on **shard 0** and credits a customer on **shard 2**.

```
   transfer $10:  shard 0 row  balance 50 → 40
                  shard 2 row  balance 70 → 80
```

Two independent problems appear at once:

**Problem A — atomicity.** These are two separate Postgres-derived engines on two machines. If shard 0 commits its debit and shard 2 crashes before crediting, $10 vanishes. Stock `BEGIN/COMMIT` only spans one node (Pass 0 Rung 2 named this). We need all-or-nothing across machines.

**Problem B — one shared commitTs.** Suppose both shards commit, but each picks its *own* `commitTs` from its *own* clock — shard 0 stamps the debit at `commitTs=100`, shard 2 stamps the credit at `commitTs=130`. Now watch a reader with `startTs=115` apply Property 1 independently on each shard:

```
   reader T  startTs=115
   shard 0 debit:  commitTs=100   100 <= 115  → VISIBLE   (sees balance 40)
   shard 2 credit: commitTs=130   130 <= 115  → NOT visible (still sees balance 70)

   T sees 40 + 70 = 110.   The $10 is in mid-air.  A torn read of an ATOMIC transfer.
```

This is the cross-shard torn read from Pass 0 Rung 2, now *inside* the system. Atomicity of the *write* is not enough — if the two halves carry different commit timestamps, Property 1 (which we love precisely because each shard evaluates it alone) will happily reveal one half without the other. **Both halves must commit at the same single commitTs**, so that any reader's `startTs` is either `>=` it (sees both) or `<` it (sees neither).

> **The pain, stated plainly:** a write spanning shard 0 and shard 2 must (A) commit all-or-nothing and (B) commit at **one** commitTs every participant honors. Per-shard independence — the thing that made reads cheap — is exactly what makes a multi-shard *write* dangerous.

### The classic tool: two-phase commit (2PC)

Think of a wedding officiant. Before declaring the couple married, they ask each party "do you?" and wait for both yesses — only then do they pronounce it. Nobody is half-married. That two-question-then-pronounce shape is exactly **2PC** = two-phase commit, the textbook protocol for atomic commit across machines. A **coordinator** (the officiant) drives **participants** through two phases:

```
   PHASE 1 — PREPARE
     coordinator → each participant: "can you commit? lock and durably stage your changes."
     each participant: takes locks, flushes its changes to durable storage, replies "PREPARED (yes)".
     A PREPARED participant has PROMISED it can commit if told to — it cannot
     unilaterally abort anymore.

   PHASE 2 — COMMIT
     coordinator (once ALL replied PREPARED) → each participant: "commit now."
     each participant: makes its staged changes live, releases locks.
```

The promise is the crux: after a participant says PREPARED, it has surrendered the right to abort on its own. That's what lets the coordinator guarantee all-or-nothing — once everyone is PREPARED, the coordinator *knows* the commit can complete.

### 2PC's notorious flaw: a dead coordinator blocks prepared participants

```
   participant A: PREPARED ✓ (locks held, changes staged, waiting for phase 2)
   participant B: PREPARED ✓ (locks held, waiting)
                              coordinator  ✗ CRASHES here, before sending COMMIT
   A and B are now STUCK:
     - they promised not to abort
     - nobody is left to tell them whether to commit
     → they hold their LOCKS, blocking every other txn touching those rows,
       until the coordinator comes back.
```

This is the **blocking problem** of 2PC. Picture the officiant fainting right after both yesses but before pronouncing the couple married — everyone stands frozen at the altar, nobody willing to leave. That's the prepared participants: holding locks, unable to move, for as long as the coordinator is down. For an OLTP system where the coordinator might be down for **minutes**, that's unacceptable. One coordinator death freezes rows across the cluster.

So the question that drives the rest of this commit story is: **how do we get 2PC's atomicity without 2PC's blocking?**

> **Real-world hook:** XA transactions (the JTA/XA two-phase protocol many Java app servers use across databases) hit this exact wall — a crashed transaction manager leaves resource managers holding "in-doubt" prepared transactions that a human DBA often has to resolve by hand. Aurora has to make that recovery automatic and fast.

---

## Rung 2 — Pre-empting the obvious fixes: not 3PC, not a replicated coordinator — move the coordinator to a lead shard

Before the real design, name the fixes the learner reaches for and why the paper rejects them. (CLAUDE.md rule: name the simpler distributed design and why it loses.)

### "Why not 3PC?"

**Three-phase commit (3PC)** inserts an extra "pre-commit" phase so that no single coordinator failure leaves participants unable to decide. In principle it is non-blocking under a single crash. In practice 3PC is **rejected** because it (a) adds a whole extra round-trip of latency to *every* distributed commit, and (b) stops being safe under network partitions (the failure mode that actually happens in a datacenter), where a partitioned group can decide differently from the rest. You pay a constant latency tax for a guarantee that fractures exactly when you need it. Not worth it.

### "Why not a Raft/Paxos-replicated coordinator, Spanner-style?"

Google Spanner avoids coordinator blocking by making the coordinator a **replicated state machine**: the coordinator's decision lives in a Paxos group, so if one coordinator replica dies, another already has the state and continues. This works, but it assumes you are *willing to run a dedicated, replicated coordinator service*. Aurora's reasoning is architectural:

```
   Who is the natural coordinator in Aurora?  The ROUTER —
   it already terminates the client connection, plans the query, and knows
   which shards the transaction touched (Pass 0 Rung 3).

   BUT: a router has NO dedicated standby.   Routers are cost-optimized
   front doors. Giving every router a hot Paxos-replicated twin (just to hold
   in-flight transaction state) would roughly double router cost.

   → If the coordinating router dies mid-2PC, recovery would take MINUTES
     (spin up a replacement, rebuild state) — the blocking flaw, in full.
```

So putting the coordinator at the router *and* making it crash-safe would mean paying for router standbys the architecture deliberately avoids.

### The key architectural move: persist the authoritative outcome at a lead shard, not at the router

Here is the pivot the whole protocol turns on. The router still *drives* the protocol (it's the natural place — it knows the participants). But the **authoritative record of the transaction's outcome is not kept at the router.** It is persisted at one of the **participants** — a designated **lead shard**.

```
   ROUTER  — drives the protocol (sends PREPARE, relays COMMIT), but is DISPOSABLE.
             Holds no authoritative outcome. If it dies, no in-doubt state is lost,
             because the truth lives elsewhere.

   LEAD SHARD — one of the shards the txn already updates. It is a SHARD, and
                shards DO have standbys on Aurora storage (6-way/3-AZ, Pass 1).
                It durably records the transaction's commit decision + commitTs.
                If it (or its primary) dies, a standby takes over in SECONDS, not minutes.
```

The trick is to put the *recoverable truth* on a node that is already replicated for durability — a shard — rather than on the cheap, standby-less router. The router becomes a relay you can lose. The lead shard becomes the single source of truth you can recover fast.

```
   Spanner:  coordinator state survives because the COORDINATOR is replicated (Paxos).
   Aurora:   coordinator state survives because it's stored at a PARTICIPANT (lead shard)
             that is ALREADY replicated for data durability — no new replicated service.
```

That's the answer to the blocking problem: not a fancier protocol (3PC), not a new replicated service (Spanner), but **relocating the authoritative state onto a node that's already fault-tolerant**, and making the driver (router) disposable.

> **Real-world hook:** this is a recurring AWS pattern — reuse an existing durable substrate (Aurora storage under every shard) instead of standing up a new highly-available service. The shard already had to be recoverable for its data; the protocol piggybacks its decision record on that same recovery path.

---

## Rung 3 — The no-failure 2PC flow (§5.4): one walk, toy timestamps

Now the happy path, step by step, with toy timestamps. Our transfer touches **shard 0** and **shard 2**. The router has already executed the writes (each shard holds an exclusive row lock on its row and has staged its new version, but neither has committed). Now the client says `COMMIT`.

We need: a single agreed `commitTs` that is `>=` every participant's view of "now," so that no participant has already handed out a `startTs` to some reader that would straddle this commit incorrectly.

```
   Cast:
     router          — driver (disposable)
     shard 0         — LEAD shard (router picks it; it's one of the updated shards)
     shard 2         — the other updated participant
   Toy: each shard reads now().latest from its own clock.
```

### Step-by-step (paper Figure 3)

```
   ── STEP 1: router picks the lead shard ──────────────────────────────
   Router picks shard 0 as LEAD among the updated shards {0, 2}.
   (Any updated shard can be lead; the paper picks one — e.g. the first updated.)

   ── STEP 2: router sends PREPARE to the OTHER updated shards ──────────
   Router → shard 2:  PREPARE.
   (The lead shard is NOT sent a separate PREPARE message in phase 1 here;
    the router will hand it the aggregated timestamp in step 4. Shard 2 is
    the "other" participant that must prepare and propose a timestamp.)

   ── STEP 3: shard 2 prepares ─────────────────────────────────────────
   shard 2:
     • computes a PREPARE TIMESTAMP = now().latest                  → say 130
       (the high end of its Time Sync interval; the HLC subtlety is Rung 5)
     • durably PERSISTS to its Aurora volume:
         - the prepared changes (so they survive a crash)
         - prepare info + the LEAD-SHARD ID (= shard 0)              ← so it knows whom to ask later
         - state = PREPARED   (promise: I will not abort on my own)
     • replies to router:  "PREPARED, prepareTs = 130"

   ── STEP 4: router aggregates and tells the lead shard ───────────────
   Router collects all prepare timestamps. Here just shard 2's: {130}.
   Router → shard 0 (LEAD):  "commit; max prepareTs received = 130"

   ── STEP 5: lead shard decides the single commitTs ───────────────────
   shard 0 (LEAD):
     • computes commitTs = max( received max prepareTs , its OWN proposal )
                         = max( 130 , now().latest_of_shard0 )
       Say shard 0's own now().latest = 110.  Then commitTs = max(130,110) = 130.
       (Toy simplification: the lead's own proposal is shown here as bare
        now().latest=110. The FULL truth — see Rung 5a — is that the lead's
        proposal is also max(C, now().latest), the HLC-governed value. Every
        minted timestamp goes through the HLC, the lead's own proposal included;
        nothing the lead mints escapes it. We use the bare form here because the
        HLC isn't introduced until Rung 5.)
     • durably PERSISTS:  state = COMMITTED, commitTs = 130            ← THE AUTHORITATIVE RECORD
     • commits LOCALLY at commitTs=130 (its staged debit becomes live), releases its row lock
     • acks router: "committed at 130"

   ── STEP 6: router tells the client, then finishes the others ────────
   Router → client: "OK".  (after commit-wait, see below)
   Router → shard 2:  COMMIT PREPARED, commitTs = 130
   shard 2: writes commitTs=130 into its commit log, makes its credit live, releases lock.
```

### Why `commitTs >= every prepare timestamp` — and why that's the load-bearing inequality

The lead shard computes `commitTs = max(all prepare timestamps, its own proposal)`. So by construction:

```
   commitTs  >=  shard 2's prepareTs (130)
   commitTs  >=  shard 0's own proposal (110)
   ⇒ commitTs = 130, which is >= BOTH.
```

Why must `commitTs` dominate every prepare timestamp? Because each participant's `prepareTs = now().latest` is an upper bound on real time *at that shard* when it prepared. A shard may have already handed a `startTs` to some local reader using that same clock. If the final `commitTs` were *below* a participant's prepareTs, that participant could have a reader whose `startTs` falls between (`prepareTs > startTs >= commitTs`) — a reader that should NOT see this write per its own clock but *would* under Property 1. Taking the **max** guarantees `commitTs` is at least as large as every shard's notion of "now at prepare," so the commit lands at or above every participant's clock — no participant is forced to commit "in its own past."

This is the same **asymmetry** from Pass 2's commit-wait: we squeeze from the pessimistic-high end (`now().latest`) so the agreed point provably covers real "now" everywhere. The single `commitTs=130` is what makes the two halves visible together — exactly the fix Rung 1 demanded for Problem B.

> **Closing Rung 1's loop — the torn read hasn't fully vanished yet.** Watch the gap *inside* this flow. After step 5, shard 0 (lead) has committed its debit locally at 130, but step 6 hasn't yet delivered `COMMIT PREPARED` to shard 2 — shard 2 still holds only a PREPARED credit. Now a reader with `startTs >= 130` lands on shard 2 and reads that row. It sees the *stale* value (70, not 80), because shard 2 doesn't yet know the credit's real commitTs is 130. That is **exactly the torn read from Rung 1** — one half (debit) visible, the other half (credit) not — except now it's a timing window rather than a clock-mismatch. We have *not* solved it with the single commitTs alone. Resolving this window — making shard 2's reader either learn commitTs=130 or treat the credit correctly — is precisely the **5(b)/5(c)** machinery below. Hold this picture; Rung 5 closes it.

### Commit-wait still applies — at the lead shard

The lead shard picked `commitTs=130`. Before the router acks the client (step 6), the lead shard does **commit-wait** (Pass 2): it does not consider the commit externally visible until `now().earliest > 130`. As in Pass 2, this runs in parallel with the durable flush, so it usually hides under the storage round-trip and adds ~0 latency.

> **Pre-empt:** *"Why does the lead shard, not the router, pick commitTs?"* Because the commitTs is part of the authoritative outcome, and the authoritative outcome must live on the recoverable node (Rung 2). If the router picked and then died before persisting it anywhere durable, the decision would be lost. Letting the lead shard compute and persist `commitTs` in the same durable write that records "COMMITTED" means the decision and its timestamp are recoverable as one atomic fact.

> **Real-world hook:** this is the bank-transfer shape — debit shard 0, credit shard 2, both at commitTs 130. A balance-summing report with `startTs >= 130` sees 40 + 80 = 110 *consistently*; one with `startTs < 130` sees 50 + 70 = 120 consistently. Never the torn 110 from Rung 1.

---

## Rung 4 — Router-failure recovery via the lead shard (§5.4)

We built the lead-shard design *for* this rung. Now spend it: walk what happens when the router dies mid-2PC. The recovery rule is short because the truth has exactly one home.

The governing principle: **the lead shard's durable state is authoritative.** Any participant unsure of the outcome asks the lead shard (it persisted the lead-shard ID back in step 3, so it knows whom to ask). The lead shard answers from its durable record. The router holds nothing anyone needs to recover.

### The recovery cases

```
   Router dies. For each in-flight distributed txn, classify by how far it got:

   CASE A — not yet PREPARED anywhere
     No participant ever promised. Nothing is staged durably as a promise.
     → ABORT. Safe: no one committed, no one is holding a promise it can't break.

   CASE B — PREPARED on participants, but outcome unknown to them
     Participants are in PREPARED limbo (the classic blocking case).
     Each asks the LEAD SHARD: "what happened to this txn?"
       • lead shard durably COMMITTED (has commitTs)  → COMMIT everywhere at that commitTs.
       • lead shard did NOT commit                    → ABORT everywhere.

   When does the ABORT sub-case of B happen?
     The router died AFTER prepares succeeded but BEFORE it told the lead shard
     to commit (before step 5 persisted COMMITTED). The lead shard never decided,
     so the only safe outcome is to abort — and because the lead shard never
     wrote COMMITTED, every inquiring participant gets the same "abort" answer.
     Consistent, all-or-nothing.
```

The key difference from classic 2PC: in classic 2PC the prepared participants in Case B have **no one to ask** (the coordinator was the only holder of the decision, and it's dead). In Aurora, the decision-holder is the **lead shard**, which has standbys — so a standby answers in **seconds**, and the participants unblock. The blocking flaw is bounded by shard-failover time, not by router-replacement time.

```
   classic 2PC:   prepared participant → asks dead coordinator → blocks for MINUTES
   Aurora:        prepared participant → asks lead shard (or its standby) → answer in SECONDS
```

> **Pre-empt:** *"What if the LEAD SHARD dies too?"* Its standby takes over from durable Aurora storage and replays the persisted state (COMMITTED+commitTs, or no decision). The authoritative record survives because it was written to the 6-way/3-AZ volume. *How* that durable state is reconstructed on failover is **Pass 6** — here we only rely on the fact that a shard, unlike a router, is recoverable.

### Restate the sweet spot: most transactions skip ALL of this

Everything above (Rungs 1–4) is the price of a **multi-shard write**. The performance thesis (Pass 0 Rung 7) is that the common transaction never pays it:

```
   transaction type        protocol                                       2PC?
   ─────────────────────   ────────────────────────────────────────────   ────
   read-only (any shards)  router just assigns startTs; reads each shard   NO
                           with commitTs <= startTs. Router can ack the
                           "commit" of a read-only txn IMMEDIATELY —
                           nothing was written, nothing to make durable.
   single-shard write      router forwards to the ONE shard; that shard    NO
                           commits LOCALLY and picks its OWN commitTs
                           (= max(C, now().latest), see Rung 5). Exactly
                           like stock Postgres — no lead shard, no prepare.
   multi-shard write       lead-shard time-aware 2PC (Rungs 1–4).          YES
```

A single-shard write is its own lead shard and its own — and only — participant, so the entire prepare/aggregate/commit-prepared dance collapses to "commit locally, pick a commitTs." That collapse *is* the sweet spot.

> **Real-world hook:** co-locating a customer's orders on the same shard as the customer (Pass 1's data model) keeps "place an order" single-shard — so it commits at near-stock-Postgres speed and never touches the recovery machinery above. Only genuinely cross-customer operations pay 2PC.

---

## Rung 5 — Reading from shards under skew (§5.5)

Commit is solved. Now the genuinely hard half. Don't tense up — it's three separate small problems wearing one scary label, and we take them one at a time. A **reader** lands on a shard and must apply Property 1 correctly, even though (a) the shard's clock is skewed relative to the reader's, (b) a writer to the very row may be **half-prepared** on another shard, and (c) a writer may have its `commitTs` but not yet be durable. Three sub-rungs, each its own pain.

Recall the reader's tool, unchanged: **Property 1 — `T` sees `T'` iff `T'.commitTs <= T.startTs`.** The trouble is making sure that comparison is *safe* against a shard that hasn't yet (or has only partially) decided the commitTs of a relevant writer.

### 5(a) — Clock skew: the missed-write anomaly, and the HLC fix

**The pain.** Reader `T` has `startTs = 100` (read from *its* clock, which is the router's). It reads **shard S**. But shard S's clock is *behind*: its current Time Sync interval is `[earliest=20, latest=40]`. So shard S, right now, thinks "now" is around 40. Watch what can go wrong if shard S later commits a *new* writer `T'` naively from its own slow clock:

```
   reader T arrives at shard S.  T.startTs = 100.
   shard S local time interval = [20, 40]  (S's clock lags).

   T reads the row, sees the current version (say commitTs=30, 30<=100 → visible). Fine so far.

   A moment later, a writer T' commits ON shard S, picking commitTs from S's slow clock:
       T'.commitTs = now().latest on S = 40.

   Now 40 <= 100 = T.startTs.   Per Property 1, T SHOULD have seen T'.
   But T already read and moved on — it MISSED a write that, by its own startTs,
   was supposed to be visible to it.    ← inconsistent / non-repeatable read.
```

The danger: shard S's lagging clock can mint a *new* commitTs (40) that still slips *under* `T.startTs` (100) **after** `T` has already read. Property 1 says T should see it; reality is T already left. A torn/non-repeatable read across the reader's own snapshot.

**The traditional fix (Clock-SI), and why Aurora rejects it.** Clock-SI's answer: make the **reader wait**. Before reading shard S, T waits until S's clock provably passes `T.startTs=100` (i.e. until S's `now().earliest > 100`). Then anything S commits afterward necessarily gets `commitTs > 100`, so T can't miss it. Correct — but it **taxes the reader**, the common OLTP path, and reads are the bulk of traffic (Pass 2 Rung 5 rejected reader-side waits for the same reason). We don't want every read to stall waiting for a slow shard's clock.

**Aurora's fix: a hybrid logical clock (HLC) per shard.** Define it inline, because it is the load-bearing replacement for the wait:

> **Hybrid logical clock (HLC):** a per-shard counter `C` that tracks the *maximum timestamp the shard has observed*, blending physical time with logical advancement. It is never allowed to go backward, and it can be **dragged forward** by any timestamp the shard sees — including a reader's `startTs`. It is "hybrid": grounded in physical Time Sync time, but bumpable by logical events so it never lags behind what it has already promised.

The mechanism is two small rules on every shard's `C`:

```
   RULE 1 (on each read by T):     C := max(C, T.startTs + 1)
   RULE 2 (when minting a write ts: prepare ts, or single-shard commit ts):
                                    ts := max(C, now().latest)
```

Rule 1 is the trick. When `T` (startTs=100) reads shard S, it *drags S's clock C up to at least 101* — `+1` so the next write is strictly above T.startTs, not equal to it. Now apply Rule 2 to the later writer `T'`:

```
   T reads shard S:   C := max(C, 100+1) = 101.       ← T dragged C up
   later, T' commits on S:  T'.commitTs = max(C, now().latest) = max(101, 40) = 101.

   Property 1:  101 <= 100 (T.startTs) ?  NO.
   → T' is committed STRICTLY ABOVE T.startTs → INVISIBLE to T.  ✓
   → T did NOT miss anything: T' was never supposed to be visible to it after all.
```

The skew is neutralized: once `T` has read shard S, **anything S commits afterward is forced above `T.startTs`** by the dragged-up `C`. T never has to wait — the *writer's* timestamp is pushed up instead. Aurora moved the cost from the reader (Clock-SI's wait) onto the clock-advancement of the shard, which is free.

```
   Clock-SI:   reader WAITS until shard clock > startTs.   (taxes every read)
   Aurora HLC: reader DRAGS shard clock to > startTs and reads immediately.
               later writes inherit the higher clock → can't sneak below startTs.
```

Note the prepare timestamp in Rung 3 was glossed as `now().latest` — the full truth is `max(C, now().latest)` (Rule 2). The HLC is what makes a prepare/commit timestamp respect every reader the shard has already served.

> **Pre-empt:** *"Why `+1` and not just `max(C, startTs)`?"* Because Property 1 uses `<=`. If a later write got `commitTs = 100 = T.startTs`, then `100 <= 100` is true and T *would* see it — re-introducing the missed-write race for a write that arrived after T read. The `+1` forces strict-greater, so post-read writes are unambiguously in T's future.

> **Real-world hook:** HLCs are the same idea CockroachDB uses (HLC timestamps that advance on observed messages) to get Spanner-like consistency *without* Spanner's specialized TrueTime hardware. Aurora has Time Sync *and* the HLC: Time Sync bounds skew, the HLC absorbs whatever skew remains so readers never wait.

### 5(b) — A prepared-but-undecided writer: inquire the lead shard

**The pain.** Reader `T` (startTs=100) reads a row on shard S. But a *distributed* writer `T'` has updated that very row and is **PREPARED** on shard S — with a prepare timestamp `prepareTs = 80` (which is `< T.startTs = 100`). T' is in 2PC limbo: its final `commitTs` will be decided by its **lead shard** (some other shard), and shard S **does not yet know it**.

```
   shard S holds:  row R, with a PREPARED version from T'.   T'.prepareTs = 80.
   reader T (startTs=100) wants to read R.

   Can T apply Property 1?   It needs T'.commitTs. But S only has prepareTs=80.
   The real commitTs = max(...) computed at T's LEAD shard, will be SOME value >= 80.

   If T'.commitTs turns out <= 100  → T must SEE T'.
   If T'.commitTs turns out >  100  → T must NOT see T'.
   S cannot decide locally. And T' is PREPARED (promised) — S can't just ignore it.
```

A prepared writer is a landmine. It *will* commit — it promised — at a commitTs S can't predict, that may or may not fall under T.startTs. So can S just guess? No. Either guess is a coin flip, and a wrong flip means a torn read.

**The fix: S inquires the lead shard, passing `T.startTs`.** Shard S knows the lead-shard ID (it persisted it at prepare time, Rung 3 step 3). So:

Before the mechanics, bound the cost so it stays consistent with the cost-asymmetry argument from 5(a)/5(c): this inquiry round-trip fires **only** when a reader actually lands on a PREPARED version of a row whose visibility its snapshot depends on — i.e. an in-flight cross-shard writer touched the exact row being read, mid-2PC. That is **rare**. It is *not* a per-read tax like Clock-SI's wait; the overwhelming majority of reads touch no prepared version and never inquire. The expensive path is paid only by the reads that genuinely collide with an undecided distributed write.

```
   shard S → lead shard of T':  "what is the outcome of T'?  (FYI, a reader with startTs=100 is waiting)"

   TWO outcomes at the lead shard:

   (i) T' already COMMITTED at the lead shard, with some commitTs (say 90):
        lead replies commitTs = 90.
        S applies Property 1 normally:  90 <= 100 → T SEES T'.   Decided.
        (If commitTs had been 150:  150 <= 100 → T does NOT see T'.)
        BUT: learning commitTs=90 only answers the TIMESTAMP question. If S's
        LOCAL copy of T' is now decided (commitTs known) but still COMMITTING —
        not yet durable on S — and 90 <= 100, then S applies the SAME wait from
        5(c) below: T blocks until S's copy of T' is durably COMMITTED (or ABORTED).
        5(b) resolves "what is T''s commitTs?"; 5(c) resolves "is it durable yet?".
        A cross-shard inquiry can land you straight in 5(c)'s window — the two
        sub-rungs compose.

   (ii) T' NOT yet committed at the lead shard (still undecided):
        the lead shard ADVANCES ITS OWN HLC:  C_lead := max(C_lead, T.startTs + 1) = max(C_lead, 101)
        and replies "not committed; proceed".
        Because T' will eventually commit at commitTs = max(C_lead, ...) >= 101,
        we now GUARANTEE  T'.commitTs >= 101 > 100 = T.startTs.
        → T'.commitTs > T.startTs → T' is INVISIBLE to T.   T reads as if T' isn't there.
```

Outcome (ii) is the same HLC trick from 5(a), applied *across shards*: by dragging the **lead shard's** clock above `T.startTs` *before* the lead shard has fixed `T'`'s commitTs, we force `T'` to land in `T`'s future. So whichever way the lead shard goes after the inquiry, T's read is consistent: either it learns a real commitTs and applies Property 1, or it guarantees T' commits above its startTs and treats T' as absent.

```
   Inquiry decision tree at lead shard:
                         ┌── COMMITTED → reply commitTs → S applies commitTs <= startTs
   S asks lead (startTs) ┤
                         └── UNDECIDED → bump C_lead to > startTs → T' forced into T's future → invisible
```

> **Pre-empt:** *"Why pass T.startTs to the lead shard at all?"* Because the lead shard needs it to *raise its HLC high enough*. Without it, the lead shard might later pick a commitTs that sneaks under T.startTs (the 5(a) race, cross-shard). Passing startTs lets the lead shard guarantee "I will commit T' strictly above this reader" — the cross-shard version of Rule 1.

> **Real-world hook:** this inquiry is why a reader scanning a shard during a busy cross-shard transfer doesn't block on the transfer *and* doesn't read half of it — it either resolves the transfer's commitTs or pushes the transfer cleanly into its future, in one round-trip to the lead shard.

### 5(c) — The COMMITTING window: a decided-but-not-durable writer

**The pain.** There's a sliver of time even after a commitTs is *chosen*. A transaction `T'` has its `commitTs` (say 90) but hasn't finished flushing durably to Aurora storage. The system marks it **COMMITTING**. It is *not done* — it could still **abort** if the durable flush fails. Now reader `T` (startTs=100) hits that row:

```
   T' state = COMMITTING,  commitTs = 90  (chosen, but not yet durable; might abort).
   reader T, startTs = 100.

   Property 1 says:  90 <= 100 → T should see T'.
   But T' is NOT durably committed yet — if T reads it now and T' later ABORTS,
   T saw a write that never happened (a dirty read of a doomed transaction).
   And if T ignored it and T' COMMITS, T missed a write it should have seen.
   Either guess can be wrong.
```

The COMMITTING window is the gap between "commitTs decided" and "durably committed (or aborted)." A reader whose `startTs >= commitTs` has a stake in the outcome but can't yet know it.

**The fix: the reader WAITS for the COMMITTING transaction to resolve.** Only in this narrow case — when `T'.commitTs <= T.startTs` (so T's visibility actually *depends* on T') and T' is COMMITTING — does the reader block:

```
   if  T' is COMMITTING  AND  T'.commitTs <= T.startTs:
        T WAITS until T' becomes COMMITTED (durable) or ABORTED.
        • T' COMMITTED → apply Property 1: 90 <= 100 → T sees T'.
        • T' ABORTED   → T' never happened → T reads the prior version.
```

This is a *short* wait — only as long as the durable flush takes, only for readers whose startTs straddles this specific commitTs, only against transactions in the COMMITTING sliver. It is the one place a reader genuinely blocks, and it's bounded by one storage flush.

```
   T' lifecycle (the reader-relevant states):
     PREPARED (5b) ──► COMMITTING (5c) ──► COMMITTED        ──► durable, Property 1 applies
                          │   commitTs chosen,    │
                          │   not yet durable     └─ or ──► ABORTED ──► version disappears
                          └─ reader with startTs >= commitTs WAITS through this box
```

> **Pre-empt:** *"Isn't this just Clock-SI's reader-wait that we rejected in 5(a)?"* No — 5(a)'s wait was on *every* read against a slow clock, on the common path. This wait is surgical: only for a reader whose startTs depends on a transaction that is *already in the act of committing* and might abort. It's unavoidable (the outcome is genuinely unknown) and rare (a tiny window), versus Clock-SI's pervasive wait.

> **Real-world hook:** this is the distributed analogue of the brief in-doubt moment any committing transaction has; Aurora bounds it to the storage-flush duration and only makes *dependent* readers wait, so it's invisible to the vast majority of reads.

---

## Rung 6 — Closing hook: distributed deadlocks (§5.8)

We've used row locks throughout (first-committer-wins from Pass 2; prepared transactions holding locks here). Locks now span **shards and routers**, which resurrects a hazard that was easy on one node: a deadlock whose cycle crosses machines.

```
   txn T1 holds row A on shard 0, waits for row B on shard 2
   txn T2 holds row B on shard 2, waits for row A on shard 0
   → a cycle that NO SINGLE shard can see — each shard only sees half the wait.
```

A **waits-for graph** is a directed graph where an edge `T1 → T2` means "T1 is blocked waiting on a lock T2 holds." A **distributed deadlock** is a cycle in this graph whose edges live on *different* nodes — so no single node's local graph contains the cycle.

Aurora's approach, sketched (full treatment is a later pass):

```
   • A designated node periodically GATHERS the local waits-for graphs from
     shards/routers and takes their UNION into one global graph.
   • It searches the union for a CYCLE.
   • On finding one, it ABORTS a (randomly chosen) victim transaction in the cycle,
     breaking the deadlock. The victim retries with a fresh startTs.
```

And a prevention trick for the operations that predictably grab many locks — **DDL** (schema changes), which must lock structures on *many* nodes at once:

```
   DDL acquires its multi-node locks in a FIXED ORDER — a "distinguished node" first,
   then the rest in a deterministic order. If everyone grabs locks in the same order,
   no cycle can form (the classic lock-ordering deadlock-avoidance rule).
```

Detection (gather-union-find-cycle-abort) handles the *unpredictable* deadlocks from ordinary transactions; ordering *prevents* the predictable ones from DDL. Full DDL — how a schema change coordinates across all shards and routers without freezing the cluster — is a **later pass (§5.7)**.

> **Real-world hook:** application deadlocks you already know from single-node Postgres (`ERROR: deadlock detected`) now have a cross-shard cousin; Aurora's global detector turns "two transactions on two machines stuck forever" into "one victim gets a retryable error," preserving the single-database illusion.

---

## What this pass nailed down

```
THE COMMIT PROBLEM (Rung 1):
   multi-shard write needs (A) atomicity AND (B) one shared commitTs,
   else Property 1 (evaluated per-shard) reveals half a transfer → torn read.

WHY NOT (Rung 2):  3PC (latency + unsafe under partition);
                   Spanner-style replicated coordinator (routers have no standby → minutes).
   KEY MOVE:       persist the authoritative outcome at a LEAD SHARD (a participant
                   that HAS standbys) — router only drives, and is disposable.

NO-FAILURE 2PC (Rung 3):
   router picks lead shard → PREPARE others (each: prepareTs=max(C,now().latest),
   persist prepare+lead-id) → router sends max(prepareTs) to lead →
   lead: commitTs = max(received, own proposal), persist COMMITTED+commitTs, commit, ack →
   router acks client (after commit-wait) → COMMIT PREPARED + commitTs to the rest.
   LOAD-BEARING: commitTs >= every prepareTs.

RECOVERY (Rung 4):  lead shard's durable state is authoritative.
   not-prepared → abort.   prepared-but-undecided → ask lead:
   lead COMMITTED → commit all at its commitTs; lead never decided → abort all.
   recovers in SECONDS (shard standby), not minutes (router replacement).
   SWEET SPOT: read-only (ack immediately) and single-shard (commit locally) SKIP 2PC.

READING UNDER SKEW (Rung 5):
   (a) clock skew → HLC per shard: read drags C := max(C, startTs+1);
       writes mint ts = max(C, now().latest) → post-read writes forced ABOVE startTs.
       (replaces Clock-SI's reader-wait; cost moves to the writer's timestamp.)
   (b) prepared-but-undecided writer → S inquires lead shard (passes startTs):
       committed → reply commitTs; undecided → lead bumps C to > startTs → writer invisible.
   (c) COMMITTING (commitTs chosen, not durable, might abort): a dependent reader
       (startTs >= commitTs) WAITS until COMMITTED or ABORTED — the one surgical wait.

DEADLOCKS (Rung 6):  cross-node waits-for graph → gather + union + find cycle + abort victim.
   DDL prevents its own deadlocks via fixed lock ordering (distinguished node first).
```

Deferred and where it lives:
- **DDL** multi-node coordination (§5.7) → later pass (only the lock-ordering trick previewed here).
- **Read Committed** isolation (§5.9) → later pass.
- **Failover / recovery durability** (§6) → Pass 6 — *how* the lead shard's COMMITTED+commitTs survives a crash and a standby replays it (we relied on the fact that it does).

---

## The 3 checkpoint questions

Answer in your own words. They tell me what to reinforce next.

1. **The lead-shard move.** Explain why Aurora persists a distributed transaction's authoritative outcome at a lead shard rather than at the router that drives the protocol. In your answer, say what specifically goes wrong if the *router* held it and then crashed, and why a *shard* can recover in seconds where a router would take minutes.

2. **Walk the no-failure 2PC for a transfer touching shard 0 (lead) and shard 2.** Shard 2 prepares with `prepareTs = 130`; shard 0's own proposal is `110`. Give the final `commitTs`, justify why it must be `max(...)` and not `min(...)` or shard 2's value alone, and then show what two different readers (`startTs = 120` and `startTs = 135`) each see — proving neither sees a torn transfer.

3. **The HLC read rule is `C := max(C, T.startTs + 1)`.** A reader with `startTs = 100` reads shard S whose clock interval is `[20, 40]`. (a) Why would a *naive* slow-clock shard let a later writer commit at `commitTs = 40` and create a missed-write anomaly? (b) Show how the HLC rule forces that later writer above `100` instead. (c) Why the `+1` — what breaks if it's `max(C, startTs)` given Property 1 uses `<=`?

**Also flag:**
- **HLC (Rung 5a/5b):** did the hybrid logical clock land as "a clock you can drag forward so later writes can't sneak below a reader," or still feel like a formula? Did the *cross-shard* reuse in 5(b) (bumping the **lead** shard's C through an inquiry) click as "the same trick, one shard further away," or as a separate mechanism?
- **The prepared-txn inquiry (Rung 5b):** was it clear *why* shard S can't decide locally (it lacks the commitTs, which only the lead shard computes), and why passing `startTs` in the inquiry is what makes the undecided branch safe?
- Did the difference between **5(a)'s rejected reader-wait** (pervasive, common path) and **5(c)'s accepted reader-wait** (surgical, only for dependent readers against a COMMITTING txn) feel principled, or arbitrary?
- Any term you'd struggle to define unaided: **2PC, prepare/commit-prepared, coordinator, lead shard, prepare timestamp, PREPARED/COMMITTING state, HLC, waits-for graph, distributed deadlock.**
