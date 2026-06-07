# Pass 6 — Failover, backups, recovery, and evaluating the claims

> **Goal:** two jobs in one final pass. **Part A (§6)** closes the last mechanical gap — what happens when a shard instance is *suspected dead but might be alive* (split-brain), how Aurora keeps such a zombie from serving stale data (**read leases**), what a new instance must wait for before it serves, and how **backups/recovery** lean on the timestamps-everywhere design built across Passes 2–3. **Part B (§8–§10)** steps back and *evaluates the paper*: what the HammerDB numbers actually show (honestly, with the real figures), where Aurora Limitless sits against Citus / Greenplum / Spanner / CockroachDB / DSQL, and the limitations the authors themselves admit.
> **Reading time:** ~28 minutes.
> **Method:** Part A is the usual ladder (each rung is the simplest thing that fails). Part B is a ladder of *skepticism*, in the spirit of the FastLanes "evaluating claims" pass — ask whether the system delivers, not just how it works.
> **Number honesty:** every evaluation figure below is quoted **exactly as the paper gives it** (Table 2 configs, NOPM, latency percentages). Where I reason beyond the paper, I mark it as inference, not a paper claim.

**Carried from earlier passes (restated when first used, not re-derived):**
- **Aurora storage** (Pass 1): every shard's data sits on a volume replicated **6 ways across 3 AZs**. The load-bearing fact this pass needs: *a storage volume accepts writes from only ONE instance at a time.*
- **Property 1** (Pass 2): `T` sees `T'` **iff** `T'.commitTs <= T.startTs`. One scalar comparison.
- **now().earliest / now().latest** (Pass 2): the low/high ends of the Time Sync interval; `now().latest = clock + CEB`, `now().earliest = clock − CEB`. **CEB** = ClockErrorBound, small but non-zero.
- **commit-wait** (Pass 2): a committer holds the client ack until `now().earliest > commitTs`.
- **prepareTs / commitTs, lead shard, COMMITTING, Property-1-per-shard** (Pass 3): a multi-shard write commits all-or-nothing at one `commitTs` decided by a **lead shard**; `commitTs = max(all prepareTs, lead's own proposal)`, and every minted timestamp comes from `max(C, now().latest)`. The lead shard's durable COMMITTED+commitTs record is **authoritative** — that is what made router-failure recovery work.

**New terms this pass defines inline:** **split-brain**, **zombie instance**, **read lease**, **TTL (lease)**, **NOPM**, **NEWORD**, **ACU**, **saturation**, **serializability vs snapshot isolation** (recap), **OCC**.

---

# PART A — Failover, backups, recovery (§6)

## Rung 1 — The pain: a "dead" instance might be a live zombie, and split-brain writes corrupt data

Think of a night watchman who stops answering his radio. Maybe he collapsed. Maybe he's just in a dead spot. You can't tell from the other end — so you send a replacement. But if the first guy was only out of range and wanders back to his post, you now have two watchmen both certain they're on duty. That's the whole problem of this rung.

Every shard runs on a compute **instance** (a Postgres-derived process on a machine). Instances fail, hang, or get network-partitioned. The HA system (Pass 1 §3.4) watches them: when an instance stops responding to health checks, it is **suspected dead** and a **replacement instance** is launched to take over that shard.

Here's the trap. "Suspected dead" is a *guess*, made over an unreliable network. The original instance might be:

```
   genuinely dead         → replacement is correct, no problem
   alive but slow / GC-paused / network-partitioned   → it's a ZOMBIE
```

> **Zombie instance:** an instance the HA system gave up on and replaced, but which is actually still running and still thinks it owns the shard. It missed the memo about its own replacement.

Now both the replacement and the zombie believe they own shard 0. If **both accept writes**, you get **split-brain**:

> **Split-brain:** two instances independently accept writes for the same data, producing two divergent histories that can never be reconciled. The classic distributed-systems data-corruption failure.

```
   client A → zombie       : UPDATE balance = 40
   client B → replacement  : UPDATE balance = 90
   → two conflicting "truths" for the same row, on two instances. Corruption.
```

### The fix Aurora gets almost for free: one-writer-per-volume

Recall the Pass-1 storage fact: **an Aurora storage volume accepts writes from only ONE instance at a time.** The shard's data lives on that volume. So when the replacement instance comes up and attaches to the volume, it becomes the single allowed writer. The zombie's next write to the volume is **rejected by storage** — it no longer holds the write token.

```
   replacement attaches to volume  →  becomes sole writer
   zombie tries to write           →  storage REJECTS (not the current writer)
   zombie sees its writes fail     →  concludes it lost ownership → exits
```

The zombie can no longer corrupt anything because it physically cannot write. Split-brain *writes* are impossible — not by a clever consensus protocol, but because the storage layer was already built to enforce a single writer (the same "reuse an existing durable substrate" pattern from Pass 3).

> **Pre-empt:** *"Why not just fence the zombie with a lease/heartbeat on the compute side, like ZooKeeper-style leadership?"* Aurora doesn't need a separate fencing service for *writes* because the volume itself is the fence — there is exactly one write slot, and storage owns it. The compute layer doesn't have to agree on who's leader; it only has to *try to write*, and the loser finds out immediately.

> **Real-world hook:** this is the same idea as a "fencing token" or STONITH ("shoot the other node in the head") in HA clusters — but instead of a fragile out-of-band kill, the authoritative single-writer slot is the storage volume an Aurora shard already depends on for durability.

---

## Rung 2 — The subtler pain: a zombie can still serve stale READS — read leases fix it

Rung 1 stopped split-brain *writes*. But a zombie that can't write can still happily answer **reads** from its local state. And that local state is **frozen at the moment it was replaced** — it has no idea about any write the replacement accepted afterward.

Why this matters: Pass 2's §5.6 promised **external consistency / real-time order** — if transaction `W` committed before read `R` started in real time, `R` must see `W`. A zombie violates exactly this:

```
   t=0   zombie replaced; replacement now owns shard 0.
   t=1   client commits W on the REPLACEMENT: balance 50 → 90  (commitTs=200, durable).
   t=2   a read R (startTs=210) is routed to the ZOMBIE (HA hasn't fully updated routing yet).
         zombie's local state still says balance=50 (it never saw W).
         R reads 50.

   But W (commitTs=200) committed in real time BEFORE R (startTs=210) started,
   and 200 <= 210, so Property 1 says R MUST see W.  The zombie served a STALE read.
   → real-time order / external consistency VIOLATED.
```

So the write-fence isn't enough. We must stop a zombie from serving reads it can no longer guarantee are fresh.

### The fix: read leases

> **Read lease:** a time-bounded permission for an instance to serve reads up to a certain snapshot time. It is granted *as a side effect of a successful storage write*, and it expires. An instance that cannot renew its lease must assume it has been replaced and stop serving.

Before the mechanism, one setup step — and it's the load-bearing one, so it's worth pinning down carefully. Where does the lease end "live"? On the **same number line as a reader's `startTs` (Pass 2): the Amazon Time Sync timeline.** Here's why. The lease is granted at the granting write's commit/HLC timestamp, and that timestamp is minted by the *same* rule as every other timestamp in the system — `max(C, now().latest)` (Pass 3). So when we write "the write's timestamp is `t`", `t` isn't some private shard-clock counter that could be skewed off everyone else's. It's a Time-Sync-anchored value. And that's the payoff: comparing a reader's `startTs` against `t + TTL` is the **exact same scalar comparison as Property 1** — both operands sit on one timeline. No hidden skew to reconcile.

The mechanism, in two rules:

```
   GRANT  (on each successful storage write whose minted timestamp is t,
           where t = max(C, now().latest) — the SAME minting rule as every
           other timestamp, so t lives on the Time Sync timeline):
            instance gets a lease valid until  t + TTL.
            "I am allowed to serve reads at snapshot times <= t + TTL."

   USE / RENEW:
            the instance may serve a read with snapshot time S  only if  S <= (its lease end).
            Every successful write pushes the lease end forward (renews it).
            If it CANNOT get a successful write through (because it's a zombie and the
            volume rejects it) after retries → it can't renew → lease EXPIRES →
            instance concludes "I've been replaced" → self-terminates.
```

> **Open question — the idle-but-alive primary.** As stated, a lease renews *only* on a successful write. So what about a perfectly healthy instance that simply isn't doing any writes for a while — does its lease lapse and force it to self-terminate? The paper §6 only says *"Every shard holds a lease, which is established and renewed upon a successful write to the storage"* — it describes renewal **through successful writes** and does **not** spell out the no-writes case. A plausible resolution is a lightweight lease-renewal/heartbeat write pushed through the same single-writer volume slot (so renewal stays backed by the one authority), but **the paper doesn't detail this** — treat it as inference, not a stated fact.

> **TTL (lease):** Time-To-Live — how long after the granting write the lease stays valid. It is a *small* duration. The single most important tuning fact (Rung 3): **TTL is set shorter than the typical failover time**, so by the time a replacement is serving, the zombie's lease has almost always already expired on its own.

### Toy walk-through of the TTL

Let TTL = 5 (toy units). (The real TTL's magnitude isn't quoted here — the paper §6 describes the lease but I don't have the exact duration; **verify against §6** before citing a number, the way earlier passes flagged CEB magnitudes.)

Both `t` and the reader's `startTs` below are on the **Time Sync timeline** (just pinned above), so every `<=` here is the same kind of comparison as Property 1.

```
   zombie's last successful write minted t = 100 (a Time Sync timestamp).
   → its lease ends at 100 + 5 = 105.  It may serve reads with snapshot S <= 105.

   The volume gets handed to the replacement around real-time ≈ 103.
   From then the zombie's writes are REJECTED → it can't renew → lease frozen at 105.

   A read R with startTs = 210 (also a Time Sync timestamp) arrives at real-time ≈ 108.
   zombie checks:  210 <= 105 ?  NO.   (same number line — Property-1-style comparison)
   → zombie REFUSES to serve R (its lease doesn't cover that snapshot).
   And since it can't renew past 105, it soon concludes it's a zombie and exits.
```

The lease ties "am I allowed to answer this read?" to "have I recently proven I still own the volume?" A zombie, by definition, can no longer prove it — so its lease decays and it goes quiet instead of lying.

> **Pre-empt:** *"Why grant the lease off a WRITE rather than a heartbeat?"* Because the write is the thing that already proves single ownership (Rung 1). A separate heartbeat would be a second source of truth that could disagree with the volume's write slot. Pinning the lease to a successful write means "I can serve reads" is backed by the same fact as "I can write" — one authority, not two.

> **Real-world hook:** this is a lease in the Chubby/Spanner sense — bounded-time permission that must be renewed against an authority — but the authority here is the storage volume's single-writer slot, not a separate lock service.

---

## Rung 3 — Failover wait: why a new instance must pause before serving

Rung 2 stopped the zombie from serving reads outside its lease. So are we done? Not quite — the same danger lurks on the **new** side. When a replacement (or a recovering instance) comes up, it mustn't rush in and commit a transaction at a `commitTs` that overlaps a lease window the old instance was still allowed to honor. Picture it from the zombie's seat: it's still inside its not-yet-expired lease, happily serving a read, and the new instance just committed a write at a timestamp that read *should* have seen — but the zombie never got the memo. Same Rung-2 anomaly, approached from the other direction.

The fix is a short, principled **failover wait**:

```
   NEW instance recovery sequence:
     1. read from the volume t_last = the GRANTING-WRITE timestamp the previous
        instance's lease was based on (a Time Sync timestamp; Rung 2's grant rule
        says that lease was valid until t_last + TTL — its ceiling).
     2. WAIT until  now().latest > t_last + TTL  (i.e. wait past the old lease's ceiling).
     3. acquire its OWN fresh lease.
     4. ONLY THEN begin serving reads and accepting commits.
```

So `t_last + TTL` is exactly the old lease's ceiling from Rung 2, and the wait condition `now().latest > t_last + TTL` reads, in plain words, as **"don't serve until real time has passed the old lease's ceiling."**

### Why the wait makes everything consistent

The payoff uses the Pass-3 fact that every minted timestamp (prepareTs, commitTs) comes from `max(C, now().latest)`. After the wait, `now().latest > t_last + TTL`. Therefore **any transaction the new instance commits gets `commitTs > t_last + TTL`.**

Now look at what the old zombie could *still* legitimately serve: only reads with snapshot `S <= t_last + TTL` (its lease ceiling). So:

```
   any commit by the new instance:   commitTs  >  t_last + TTL
   any read the zombie may serve:     S        <=  t_last + TTL
   ⇒  commitTs  >  S  for every such pair.

   By Property 1, that new commit is INVISIBLE to any read the zombie is allowed
   to serve — so the zombie isn't hiding a write it was supposed to show.
   It only "misses" transactions it was never supposed to see anyway. Consistent.
```

In words: the wait guarantees the new instance's writes all land *strictly after* the entire window the old lease could cover. The two worlds (zombie's lingering reads, new instance's fresh commits) are cleanly separated on the timeline, so neither can show a torn or stale view. This is the same "push timestamps apart so Property 1 can't be fooled" move as the HLC and commit-wait in Passes 2–3 — applied across a failover boundary.

### Why it's usually free

```
   TTL is deliberately set SHORTER than typical failover time.
   typical:   failover itself takes longer than TTL
              → by the time the new instance is up, t_last + TTL is already in the PAST
              → now().latest > t_last + TTL is ALREADY true → no extra wait.
   worst case (very fast failover): a brief wait of at most ~TTL before serving.
```

So the failover wait is a correctness backstop that, in the common case, costs nothing — because choosing TTL < failover-time means the old lease has normally expired before the new instance even finishes coming up.

> **Pre-empt:** *"Why not skip the wait and just have the new instance pick a huge starting commitTs?"* Because commitTs must stay tied to real Time Sync time (Pass 2/3) for external consistency across the *whole* cluster — you can't unilaterally jump one shard's clock forward without breaking cross-shard ordering. Waiting for real time to pass `t_last + TTL` keeps the shard on the same physical timeline as everyone else.

> **Real-world hook:** this is the failover analogue of commit-wait: instead of waiting out clock uncertainty before *acking a commit*, the new instance waits out the previous *lease window* before *serving at all*, so the handoff never produces a read that violates real-time order.

---

## Rung 4 — Backups and recovery: the payoff of timestamps everywhere (§6)

Imagine photographing a relay race to prove who held the baton at the gun. One camera is easy. But if you have one camera per runner and they don't fire at the exact same instant, your photos can show the baton in two hands at once — or in none. A single-node Postgres backup is the one-camera case: "a consistent snapshot of one database." A *distributed* system is the relay race, and this is the place the whole timestamp design finally cashes out: a backup must be a **consistent cut across all shards** — every shard frozen at the *same logical instant*, so it never captures half of a cross-shard transfer.

Picture doing it naively. Back up shard 0 at 12:00:00.000 and shard 2 at 12:00:00.050, and a transfer that committed at 12:00:00.030 lands in shard 0's backup but vanishes from shard 2's. That's a **torn backup** — the Pass-3 torn-read, reborn in cold storage.

### The mechanism: a backup is defined by a single timestamp `t`

Because **every log record is tagged with its transaction's commit-protocol timestamp** (a commit record carries the txn's `commitTs`, Pass 3), Aurora can define a backup purely by a timestamp:

```
   A backup tied to Time Sync timestamp t  =  EXACTLY the transactions with commitTs <= t,
   cluster-wide.

   Mechanism: on each volume, include every log entry whose timestamp <= t; drop the rest.
   Do this on ALL volumes with the SAME t.
```

This is the direct payoff of **Property 1 / the scalar-timestamp design** (CLAUDE.md's spine). Because "committed-as-of-t" is a single scalar comparison that every shard evaluates *independently* and identically, a consistent cluster-wide cut is just "every volume, keep log entries with timestamp <= t." No coordination, no global pause — the timestamps that made *reads* consistent (Pass 2) now make *backups* consistent for free.

```
   recovery:  load ALL volumes (each truncated to entries with timestamp <= t), restart.
              every shard is now consistent as of the same t → the cluster restarts
              at one coherent instant.
```

### The edge case: a distributed txn that's prepared on some shards but committed on others at backup time

Here's the subtle part, and it closes a loop from Pass 3. Consider a distributed transaction `T'` with `commitTs = t' <= t` (so it *belongs* in the backup). At the instant the backup is cut, `T'` might be **half-resolved**:

```
   at backup time t:
     shard 0 (lead) : T' durably COMMITTED, commitTs = t'      ← decision recorded
     shard 2        : T' still only PREPARED (COMMIT PREPARED hadn't arrived yet)
```

If we recover this backup, shard 2 comes up holding a PREPARED-but-undecided `T'`. What outcome should it apply? **Exactly the router-failure case from Pass 3 Rung 4:** a prepared participant unsure of the outcome **asks the lead shard**.

```
   on recovery, shard 2's PREPARED T' is in limbo →
     shard 2 asks the LEAD shard (shard 0, whose ID it persisted at prepare time):
       lead's durable record says COMMITTED at t' (and t' <= t) → COMMIT T' at t'.
       (if the lead had NOT committed → ABORT T' everywhere — same rule.)
```

So recovery reuses the *same* lead-shard inquiry that handled a dead router. The lead shard's durable COMMITTED+commitTs record is authoritative whether the thing that interrupted the protocol was a crashed router (Pass 3) or a backup snapshot taken mid-2PC (here). One recovery rule, two triggers — that's the elegance the timestamps-everywhere + authoritative-lead-shard design buys.

> **Pre-empt:** *"Could the backup include T' on the lead but the lead's commitTs t' be > t while a participant already made it live?"* No — a participant only makes its half live at `commitTs = t'` (the single agreed value), and the backup keeps log entries with timestamp <= t uniformly. If `t' > t`, T' is excluded everywhere; if `t' <= t`, it's included everywhere (after the inquiry resolves the prepared halves). The single shared commitTs (Pass 3 Rung 1's whole point) is exactly what makes "<= t" an all-or-nothing test across shards.

> **Real-world hook:** this is point-in-time restore (PITR) for a *sharded* database. Systems that lack a global timestamp (sharded MySQL setups, hand-rolled Citus backups) struggle to get a transactionally-consistent cross-shard restore; Aurora gets it because every commit already carries a globally-meaningful timestamp.

---

# PART B — Evaluating the claims (§8 evaluation, §9 related work, §10 limitations)

Passes 0–5 built the model of *how* Aurora Limitless works. Part B asks the reviewer's question: **does it actually deliver, and what does it carefully not claim?** Same discipline as the FastLanes "reading experiments critically" pass — track the baseline, the workload shape, and the load-bearing assumption.

---

## Rung 5 — What the §8 evaluation actually shows (and the honest caveat)

### The setup

The paper benchmarks with **HammerDB**, an open-source load tester running a **TPC-C-derived** OLTP workload (the classic warehouse/order-entry benchmark). Configuration:

```
   workload:        HammerDB (TPC-C-derived)
   scale:           12,000 warehouses
   clients:         1,000 concurrent
   distributed txns: ~10% of the workload   ← hold onto this number; it's load-bearing
```

> **NOPM** = **N**ew **O**rders **P**er **M**inute — TPC-C's primary throughput metric (how many "new order" business transactions complete per minute). Higher is better.
>
> **NEWORD** = the **New-Order transaction** latency — how long a single new-order transaction takes. Lower is better. (TPC-C's signature transaction; NOPM counts these, NEWORD times one.)
>
> **ACU** = **A**urora **C**apacity **U**nit — Aurora's unit of provisioned compute+memory (Pass 4's scaling currency). More ACU = a bigger node.

Five configurations are tested (Table 2), described as `<routers>r/<shards>s` with a max ACU budget. From the paper:

```
   config   shape        max ACU
   ──────   ──────────   ───────
   r1       2r / 4s      1536
   r2       4r / 8s      1536
   r3       8r / 16s     1536
   r4       4r / 8s      3072
   r5       8r / 16s     3072
```

These let the authors separate **two kinds of scaling** by holding one thing fixed and changing another.

### Result 1 — Vertical scaling (more ACU, same shape)

Compare **r3 → r5**: both are `8r/16s`, ACU goes `1536 → 3072` (double the per-component compute):

```
   throughput:  2.04M → 2.89M NOPM      = +41.6%
   NEWORD latency: 16.42 → 9.72 ms      = −40.8%
```

Doubling ACU gave +41.6% throughput and cut latency by 40.8% — a solid vertical win, though *not* linear (doubling compute gave ~1.4×, not 2×, throughput — expected, since not all cost is CPU-bound).

Now the **saturation lesson** — compare **r2 → r4**: both `4r/8s`, ACU `1536 → 3072`:

```
   throughput:  only +4.7%
   NEWORD latency:    −37.7%
```

> **Saturation:** the regime where a system is bottlenecked on something other than the resource you're adding. At `4r/8s`, doubling ACU barely moved throughput (+4.7%) — the configuration was already throughput-saturated (likely bottlenecked elsewhere, e.g. routers or coordination, not shard CPU). But latency still dropped sharply (−37.7%) because the extra compute drained queues. **Lesson: more compute on a saturated config buys you latency, not throughput.** That's a genuinely honest result to report — it shows the system isn't a magic linear scaler.

### Result 2 — Horizontal scaling (more components, same total ACU)

Compare **r1 → r2**: both capped at **1536 ACU total**, but shape goes `2r/4s → 4r/8s` (twice as many routers and shards, each therefore getting *less* of the fixed ACU budget):

```
   throughput:  +58.7%
   NEWORD latency:  −29.1%
```

This is the more interesting win: **same total compute budget, but +58.7% throughput just from spreading it across more, better-balanced shards.** The paper's explanation is **better-balanced per-shard ACU** — splitting the budget into more shards puts each shard's working set in a sweeter operating range (less per-shard contention, better cache/lock behavior) than a few fat shards. Horizontal beats vertical here on the same budget, which is the whole pitch of a sharded system.

### The honest caveat — the load-bearing assumption

Now the reviewer's eye. The numbers are real and good, but notice **what makes them good**:

```
   • ONLY ~10% of transactions are distributed (cross-shard).
   • The workload is TPC-C-shaped and WELL-SHARDABLE: TPC-C partitions cleanly
     on the warehouse key, so ~90% of transactions are SINGLE-SHARD.
```

Recall the whole performance thesis from Passes 3 and 5: **single-shard transactions skip 2PC and collapse to one round trip.** This benchmark is ~90% single-shard. So the single-shard sweet spot is doing the heavy lifting — the system scales well *because the workload rarely exercises the expensive cross-shard path* (2PC, the 5(b)/5(c) inquiry/wait machinery from Pass 3).

```
   what the paper SHOWS:   excellent scaling on a well-shardable, ~10%-distributed workload.
   what the paper does NOT SHOW:  a workload with MANY cross-shard transactions.
```

**Flag this as the load-bearing assumption.** A workload with, say, 50–80% distributed transactions (poorly chosen shard key, or genuinely cross-entity logic) would pay 2PC + cross-shard read coordination far more often, and the scaling curve could look very different. The paper doesn't measure that regime. This isn't dishonesty — TPC-C is the standard OLTP benchmark and ~10% distributed is a defensible "realistic" mix — but a careful reader should note that **the result is conditional on the workload being shardable**, which is exactly the thing the system asks the *customer* to get right (and, per Rung 6, the thing the authors admit is the hardest migration problem).

> **Real-world hook:** this is the same caveat as any sharded OLTP system's benchmark — Citus, Vitess, CockroachDB TPC-C numbers all assume a workload that partitions cleanly. The honest question to ask any such paper is "what's your cross-shard transaction percentage, and what happens when it's high?"

---

## Rung 6 — Where it sits among alternatives (§9), and the authors' own admitted limits (§10)

### The contrast table (§9)

Each alternative makes a different trade; Aurora Limitless's identity is clearest by what it *rejects*:

```
   System          Distributed-txn approach        What Aurora rejects / does differently
   ─────────────   ─────────────────────────────   ──────────────────────────────────────
   Citus           2PC, but NO snapshot isolation   Aurora KEEPS SI across distributed txns
                   for distributed txns → can show   (time-based MVCC, Property 1). Citus
                   cross-shard anomalies             trades consistency for simplicity here.

   Greenplum       SI, but via a CENTRAL             Aurora has NO central coordinator —
                   coordinator                       timestamps make each shard decide
                   → coordinator is a bottleneck     locally. (CLAUDE.md's "why not a
                                                     central oracle?" answered.)

   Spanner         TrueTime + Paxos/Raft groups,     Aurora is CROSS-AZ only (not geo), and
                   geo-distributed                   REUSES Aurora storage for durability
                                                     instead of running Paxos/Raft. Less
                                                     general (no global geo), cheaper to build.

   CockroachDB /   HLC; may WAIT OUT max clock skew  Aurora uses HLC TOO (Pass 3 5a), but
   YugabyteDB                                        Amazon Time Sync makes CEB tiny, so it
                                                     avoids the hundreds-of-ms uncertainty
                                                     waits these systems can suffer WITHOUT
                                                     a tight clock service.

   Aurora DSQL     OCC (optimistic concurrency),     A SIBLING AWS product, different point
                   multi-region                      in the space: OCC + multi-region vs
                                                     Limitless's locking SI + cross-AZ.
```

> **OCC (Optimistic Concurrency Control):** run the transaction without locks, then at commit *validate* that nothing it read changed; if it did, abort and retry. The opposite of Limitless's pessimistic row-locking (first-committer-wins, Pass 2).
>
> **Serializability vs Snapshot Isolation (recap):** SI gives every txn a consistent snapshot but allows write-skew anomalies; serializability forbids all anomalies by making the result equal to *some* serial order. Limitless provides SI (+ real-time order = Strong SI), **not** full serializability — see below.

The single sharpest contrast: **Aurora's bet is "reuse the existing Aurora storage substrate + a tight clock service (Time Sync) + HLC," instead of standing up new consensus groups (Spanner) or giving up distributed SI (Citus) or paying skew waits (vanilla HLC systems).** Every other design choice in Passes 1–5 follows from that bet.

### The authors' own stated limitations (§10) — the trust-earning part

A paper that lists its own limits earns credibility (the FastLanes "what it does NOT claim" rung). Aurora's authors admit, in §10:

```
   1. SHARD-KEY CHOICE IS A ONE-WAY DOOR.
      Pick the wrong shard key and fixing it requires DATA MIGRATION (rewriting
      placement). This is exactly the assumption Rung 5 flagged: the system's good
      numbers depend on a good shard key, and getting it wrong is expensive to undo.

   2. MIGRATING LEGACY NON-PARTITIONED APPS IS THE BIGGEST CUSTOMER PAIN.
      Taking an existing single-database Postgres app and choosing how to shard it
      (which tables sharded/reference/standard, which key) is the hardest real-world
      adoption problem the authors report. The transparency goal (Pass 1) reduces
      ongoing burden but not the initial sharding decision.

   3. THEY STOPPED SHORT OF SERIALIZABILITY.
      Two reasons given: (a) LIMITED CUSTOMER DEMAND for full serializability over
      Strong SI, and (b) a technical mismatch — Postgres's SSI (Serializable Snapshot
      Isolation) serialization order does NOT necessarily match COMMIT order, which
      conflicts with the commit-timestamp-ordered model Limitless is built on.
      So SI/Strong-SI is the deliberate ceiling, not an oversight.

   4. POSTGRES BEHAVIORS NEEDED MODERNIZING FOR THE DISTRIBUTED SETTING.
      e.g. SCHEMA EVOLUTION / VERSIONING. Single-node Postgres treats a DDL change
      as an instantaneous catalog flip; across many routers + shards it can't be.
      CONCRETE HOOK: an online schema change must VERSION THE CATALOG so old and new
      query plans coexist during rollout — while an ALTER propagates, some routers
      still plan against the old schema and some against the new, and both must stay
      valid until every component has flipped. (Full mechanism deferred to the DDL
      pass; previewed in Pass 3 Rung 6.)
```

Limitation 3 is the most intellectually honest: serializability isn't skipped because it's hard to *want*, but because Postgres's existing serializable mechanism (SSI) orders transactions in a way that doesn't line up with the commit-timestamp spine the whole system rests on. Bolting SSI on would fight the architecture. Choosing Strong SI is the design staying internally consistent.

> **Real-world hook:** notice limitation 1+2 are the *same* assumption that made the §8 numbers look good (well-shardable workload). The authors are honest about this: the benchmark's strength and the product's hardest adoption problem are two views of one fact — **everything hinges on choosing a good shard key**, and Aurora makes the *runtime* transparent but cannot make the *modeling decision* for you.

---

## What to remember in 5 sentences

1. Aurora prevents split-brain *writes* for free because an Aurora storage volume already admits only one writer — the replacement wins the volume and the zombie's writes are rejected, so it exits.
2. **Read leases** stop a zombie from serving *stale reads*: a successful write grants a lease to `t + TTL`, the instance may serve reads only at snapshots `<= t + TTL`, and a zombie that can't renew (because the volume rejects it) self-terminates; a recovering instance waits until `now().latest > t_last + TTL` so its commits land strictly above anything the old lease could show.
3. A **backup** is just a single Time Sync timestamp `t` — keep every log entry with `commitTs <= t` on every volume — which is a consistent cross-shard cut *for free* because the timestamp design makes "committed as of t" one scalar comparison; a txn caught prepared-on-some/committed-on-others is resolved on recovery by the same lead-shard inquiry that handled a dead router (Pass 3).
4. The §8 HammerDB numbers are real and strong (vertical r3→r5: +41.6% NOPM / −40.8% latency; horizontal r1→r2 at the same 1536 ACU: +58.7% NOPM from better-balanced per-shard ACU; and the saturation lesson that r2→r4 added latency relief but only +4.7% throughput) — but they rest on a ~10%-distributed, well-shardable TPC-C workload, so the **single-shard sweet spot is doing the heavy lifting** and the many-cross-shard-txn regime is unmeasured.
5. Aurora's identity is its bet — reuse Aurora storage + tight Time Sync clocks + HLC instead of Spanner's Paxos, Citus's give-up-on-SI, or vanilla HLC's skew waits — and the authors honestly admit the cost of that bet: shard-key choice is a one-way door, legacy migration is the biggest customer pain, and they deliberately stopped at Strong SI (not serializability) because Postgres SSI's order wouldn't match commit order.

---

## The 3 checkpoint questions

Answer in your own words. They tell me what landed.

1. **The two halves of zombie defense.** (a) Explain why the single-writer storage volume stops split-brain *writes* but does **not** stop a zombie from serving *stale reads*, using a concrete commitTs/startTs example that violates real-time order. (b) State the read-lease grant rule and the use rule, and walk (with TTL=5, last write at t=100) why the zombie refuses a read with startTs=210. (c) Why must a recovering instance wait until `now().latest > t_last + TTL` before serving — what would a too-eager new instance's commit do to a zombie still inside its lease?

2. **Backups as the timestamp payoff.** (a) Why is "keep every log entry with `commitTs <= t` on every volume" automatically a *consistent cross-shard cut*, and which earlier property makes that true? (b) A distributed txn T' has commitTs `t' <= t` but at backup time is COMMITTED on its lead shard and only PREPARED on a participant — describe exactly how recovery resolves it, and name the Pass-3 case it reuses. (c) Why does the *single shared commitTs* (not one per shard) make "<= t" an all-or-nothing test?

3. **Reading the evaluation critically.** (a) Quote the r1→r2 result and explain, in the paper's terms, why spending the *same* 1536 ACU on more shards beat fewer fat shards. (b) The r2→r4 comparison shows +4.7% throughput but −37.7% latency — what regime is this and what's the lesson? (c) State the load-bearing assumption behind *all* the good numbers, connect it to the authors' own §10 limitation about shard-key choice, and say what workload the paper does **not** evaluate.

**Also flag:**
- **Read leases (Rung 2–3):** did "the lease is granted by the same write that proves single ownership" land as *why* leases are pinned to writes, or did the lease feel like a separate bolted-on timer? Did the failover-wait inequality (`commitTs > t_last + TTL >= any zombie-servable S`) click as "push the new world strictly past the old lease window"?
- **The backup edge case (Rung 4):** did "recovery reuses the dead-router lead-shard inquiry" feel like a genuinely closed loop from Pass 3, or like a coincidence?
- **The honest caveat (Rung 5):** did "~90% single-shard is doing the heavy lifting" feel like fair skepticism, or like undercutting a legitimately good result? Did the connection between the *benchmark's strength* and the *§10 migration limitation* (both = "needs a good shard key") land?
- **The §9 contrast table:** which alternative's rejection clarified Aurora's identity most — Spanner (storage vs Paxos), Citus (keep SI vs drop it), or the HLC/Time-Sync skew point? Any row that felt like a name-drop rather than a real contrast?
- Any term you'd struggle to define unaided: **split-brain, zombie instance, read lease, lease TTL, NOPM, NEWORD, ACU, saturation, OCC, serializability vs snapshot isolation.**

---

*This is the final pass. Across Passes 0–6 the spine never moved: a write is visible to a reader iff `write.commitTs <= reader.startTs` (Property 1). Snapshots (Pass 2), cross-shard commit (Pass 3), scaling (Pass 4), query pushdown (Pass 5), and now failover and backups (Pass 6) are all just that one scalar comparison, defended against clock skew, half-prepared writers, dead routers, zombies, and point-in-time restore. The single-shard sweet spot — the path that skips all of it — is, from data model to benchmark, the entire performance argument.*
