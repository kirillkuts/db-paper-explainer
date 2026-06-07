# Pass 0 — The Map

> **Goal:** after reading, you can explain FastLanes in 3 sentences.
> **Reading time:** ~10 minutes.
> **Method:** we build the idea one rung at a time. Each rung is the simplest thing that **fails**. The next rung exists to fix that failure.

---

## Rung 1 — Why compress at all?

Analytical databases (ClickHouse, DuckDB, Snowflake) store **columns** of numbers. A typical table:

```
events table
─────────────
timestamp     bigint
user_id       int
country_id    int    ← only ~200 countries
latency_ms    int    ← almost always < 2000
```

The table has **1 billion rows**. Stored as raw 4-byte integers:

```
1,000,000,000 rows × 4 bytes = 4 GB per column
```

A query like `SELECT AVG(latency_ms) WHERE country_id = 7` has to **scan all 4 GB** off disk (or RAM) just to compute one number. And moving those bytes is the slow part — disk reads and memory bandwidth are the slowest things in a CPU's life. The arithmetic is free by comparison; the CPU sits idle waiting for data to arrive.

**The pain:** scanning raw columns is bandwidth-bound. Smaller column = faster query.
**The fix:** compress.

---

## Rung 2 — The simplest compression: bit-packing

Think of it like writing the number 5 on a form with thirty blank boxes — you fill one box and leave twenty-nine empty. That's what a 32-bit integer does to a small value. `latency_ms < 2000` means **every value fits in 11 bits** (because 2¹¹ = 2048). So we're storing 32 bits and wasting 21 of them on leading zeros, every single value, a billion times over.

Bit-packing fixes that: use only the bits you need, and stop there.

```
value 5     in 32 bits: 00000000 00000000 00000000 00000101    ← 27 bits wasted
value 5     in 11 bits:                          00000000101   ← tight

value 1999  in 11 bits:                          11111001111   ← still fits

Memory savings: 11/32 ≈ 65% smaller column. 4 GB → 1.4 GB.
```

### Decoding one value (scalar CPU)

To get value `i` out:
```
bit_position = i * 11
shift        = bit_position % 8   ← where in the byte it starts
read 16 bits starting at byte (bit_position / 8)
shift right by `shift`, mask with 0b11111111111 (11 ones)
```

Two instructions: shift, then mask. Per value. **One value at a time.** That last part is the catch.

### Pain

1 billion values × 2-3 instructions × ~0.3 ns per instruction ≈ **1 second per column**. Still too slow when a query touches multiple columns.

**The fix:** stop doing one value at a time. Do many in parallel.

---

## Rung 3 — SIMD: many values per instruction

Doing one value at a time leaves most of the CPU idle. Modern CPUs have **SIMD** ("Single Instruction Multiple Data") registers — wide registers split into independent **slots** ("lanes"), like an egg carton with eight cups. One instruction operates on all cups at once instead of one at a time.

```
AVX2 register = 256 bits = 8 slots × 32 bits each:

┌──────┬──────┬──────┬──────┬──────┬──────┬──────┬──────┐
│slot 0│slot 1│slot 2│slot 3│slot 4│slot 5│slot 6│slot 7│
│ 32 b │ 32 b │ 32 b │ 32 b │ 32 b │ 32 b │ 32 b │ 32 b │
└──────┴──────┴──────┴──────┴──────┴──────┴──────┴──────┘

One SIMD-add instruction adds 8 pairs of 32-bit numbers simultaneously.
```

There's one catch, and it's the whole story of this paper. **The rule of SIMD:** each slot does its **own** work using only its **own** bits. The cups don't talk to each other. The instant a slot needs to reach into another slot's bits, the CPU has to stall and shuffle the data across — and a cross-lane shuffle is slow. Keep that rule in your head; everything below is about not breaking it.

### Trying SIMD on bit-packed data — and watching it fail

11-bit `latency_ms` values, packed back-to-back:

```
bit pos:   0       11      22      33      44      55
           │       │       │       │       │       │
values:    v0      v1      v2      v3      v4      v5
           [─11──] [─11──] [─11──] [─11──] [─11──] [─11──]
```

SIMD load reads 256 bits in one shot. The CPU splits it by **physical bit position**, 32 bits per slot:

```
slot 0 ← bits   0–31    contains: [v0 = 11b] [v1 = 11b] [v2 low 10b]
slot 1 ← bits  32–63    contains: [v2 high 1b] [v3 = 11b] [v4 = 11b] [v5 low 9b]
slot 2 ← bits  64–95    ...
slot 3 ← ...
```

Now look at v2. The bottom 10 bits landed in **slot 0**. The top 1 bit landed in **slot 1**. To put v2 back together, slot 0 has to reach into slot 1 — exactly the move the rule forbids. **Cross-lane shuffle.** And it's not one bad value: because 11 doesn't divide evenly into 32, the boundaries drift and almost every value ends up split. SIMD's promise dies.

**The pain:** straight bit-packing places value boundaries wherever they fall, which means they straddle slot boundaries.
**The fix:** rearrange the data so straddles never happen between slots.

---

## Rung 4 — Three fixes you might try, only one is good

### Fix A — pad every value to slot size

"Just pad 11-bit values to 32 bits each. One value per slot. No straddles."

```
slot 0: [v0 11b][21 bits of zero]
slot 1: [v1 11b][21 bits of zero]
...
```

**Works for SIMD** — every value sits alone in its slot, no straddles. But look what it costs: 21 of 32 bits wasted = **66% waste**. We're back to 4 GB. We bought speed by throwing away the entire reason we compressed.

And it gets worse for smaller values. For 5-bit values you'd waste 27/32 = 84%. For 17-bit values, 47%. Padding only breaks even when values happen to fill the slot — the rest of the time it's just the original problem wearing a disguise.

### Fix B — bit-slice: lane *i* holds bit-*i* of many values

"Slot 0 has bit-0 of v0..v31, slot 1 has bit-1 of v0..v31, …"

```
slot 0:  bit0 of v0, v1, v2, ..., v31
slot 1:  bit1 of v0, v1, v2, ..., v31
...
slot 10: bit10 of v0, v1, v2, ..., v31
```

**Works for predicates** ("is value > 100?" — can be answered bit-by-bit). **Useless for reconstructing actual values** — to rebuild v0 you'd have to gather bit 0 from slot 0, bit 1 from slot 1, etc. Cross-lane shuffles everywhere. (This is **BitWeaving/V** — a real technique, but only for filter pushdown, not general decode.)

### Fix C — lane-stripe: each slot owns its own private stream

"Slot 0 decodes v0, v4, v8, v12, …  Slot 1 decodes v1, v5, v9, v13, …"

Each slot has its **own bit-packed stream** of values. Memory interleaves the streams at slot-word granularity so one SIMD load fills all slots with their respective words:

```
memory:  [slot0 word0][slot1 word0][slot2 word0][slot3 word0][slot0 word1]...

SIMD load (128 bits):
  slot 0 ← slot0 word0  (next chunk of slot 0's private stream)
  slot 1 ← slot1 word0  (next chunk of slot 1's private stream)
  slot 2 ← slot2 word0
  slot 3 ← slot3 word0
```

Here's the trick. Straddles don't disappear — inside slot 0 the stream still looks like ordinary scalar bit-packing, and values still cross word boundaries. But now **the straddle is between two consecutive words of the SAME slot**, not between two different slots. Slot 0 only ever reaches into its own next word, which it already holds. Recombining is a `shift + OR` that never leaves the lane. The rule stays unbroken.

**Zero waste + full SIMD parallelism + lane-independent.** This is **FastLanes**.

---

## Rung 5 — Why "every 4th value" is the magic

You might ask: why does slot 0 own `v0, v4, v8, v12, …`? Wouldn't it be simpler to give slot 0 the first chunk, `v0, v1, v2, v3, …`?

It would — until you look at **how SIMD writes its output back to memory**. When the decoder finishes one iteration, all 4 slots emit one value each, side by side:

```
iteration 0:  slot 0 → v0    slot 1 → v1    slot 2 → v2    slot 3 → v3
iteration 1:  slot 0 → v4    slot 1 → v5    slot 2 → v6    slot 3 → v7
iteration 2:  slot 0 → v8    slot 1 → v9    slot 2 → v10   slot 3 → v11
```

Stored to output, the array comes out **in natural order**: `v0, v1, v2, v3, v4, v5, v6, v7, …` — no post-decode reshuffle needed.

Now try it the "simple" way. If slot 0 held `v0..v255` contiguous, the same side-by-side writes would spit out `v0, v256, v512, v768, v1, v257, …` — scrambled, and you'd need a whole permute pass just to unscramble it. So the every-Nth assignment isn't a quirk; it's the one choice that makes the output land already sorted, for free.

---

## The FastLanes thesis, in one sentence

> **FastLanes is a bit-packing memory layout where each SIMD lane owns its own private stream of values, so straddles between values stay inside one lane (cheap) instead of crossing lanes (catastrophically expensive).**

The measured result: **~4× faster decompression than the previous state of the art, same compression ratio.** Plus: the same bytes decode on scalar / SSE / AVX2 / AVX-512 / NEON — one layout, every CPU. The paper calls this property **interpretability**.

---

## Real-world hook (where this pays off)

For an analytical query like `SELECT AVG(latency_ms) WHERE country_id = 7`:
- Without FastLanes: decompression is **60–80% of query time**. Bandwidth-bound.
- With FastLanes: decompression drops 4×. Filtering / aggregation becomes the bottleneck instead — which is where you *want* to be optimizing.

DuckDB and similar engines use FastLanes-style layouts in production today (2024+).

---

## Glossary (minimum vocabulary for Pass 1)

| Term | Meaning |
|---|---|
| **Column scan** | Reading every value of one column in a table. The primary workload of analytical databases. |
| **Bit-packing** | Storing values using only the bits they need (e.g. 11 bits per value instead of 32). |
| **SIMD** | "Single Instruction Multiple Data" — one CPU instruction operates on multiple values in parallel. |
| **Slot (lane)** | One parallel sub-register inside a SIMD register. AVX2 has 8 slots of 32 bits. |
| **Cross-lane shuffle** | A SIMD operation that moves bits between slots. Slow. The thing FastLanes avoids. |
| **Straddle** | A value's bits crossing a word/slot boundary. FastLanes confines straddles to within-slot, never between slots. |
| **Carry-over** | The shift-and-OR trick that recombines a value split across two consecutive words *inside one slot*. |
| **Lane-stripe** | The FastLanes layout: every Nth value goes to slot N mod T. |
| **Interpretability** | Same compressed bytes decode on any SIMD width (SSE / AVX2 / AVX-512 / NEON) without re-encoding. |
| **Cascading codecs** | Applying compression in stages: FOR → DELTA → bit-pack. The topic of Pass 1. |

---

## The 3 checkpoint questions

Answer in your own words. They tell me what Pass 1 should reinforce.

1. **Why does straight bit-packing break SIMD?** (Hint: where do value boundaries fall?)
2. **The two natural-looking fixes are "pad each value" and "bit-slice (one bit per slot)". Why does FastLanes reject both?**
3. **In FastLanes' lane-stripe layout, slot 0 decodes v0, v4, v8, v12, … When a value straddles two consecutive words of slot 0's stream — say, the value sits across word 0 and word 1 — how is it reassembled, and crucially, which slot(s) are involved?**

Also flag:
- Any rung where the "pain" didn't feel concrete to you (i.e. you couldn't picture *why* it hurt before the next rung was introduced).
- Any term in the glossary you'd struggle to define without re-reading.

Your answers shape Pass 1 (FOR / DELTA / RLE / DICT — built the same ladder way).
