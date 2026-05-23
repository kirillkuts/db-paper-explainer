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

A query like `SELECT AVG(latency_ms) WHERE country_id = 7` has to **scan all 4 GB** off disk (or RAM) just to compute one number. Disk reads + memory bandwidth = the slowest things in a CPU's life.

**The pain:** scanning raw columns is bandwidth-bound. Smaller column = faster query.
**The fix:** compress.

---

## Rung 2 — The simplest compression: bit-packing

`latency_ms < 2000` means **every value fits in 11 bits** (because 2¹¹ = 2048). We're storing 32 bits and wasting 21 of them on leading zeros.

Bit-packing: use only the bits you need.

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

Two instructions: shift, mask. Per value. **One value at a time.**

### Pain

1 billion values × 2-3 instructions × ~0.3 ns per instruction ≈ **1 second per column**. Still too slow when a query touches multiple columns.

**The fix:** stop doing one value at a time. Do many in parallel.

---

## Rung 3 — SIMD: many values per instruction

Modern CPUs have **SIMD** ("Single Instruction Multiple Data") registers — wide registers split into independent **slots** ("lanes"). One instruction operates on all slots at once.

```
AVX2 register = 256 bits = 8 slots × 32 bits each:

┌──────┬──────┬──────┬──────┬──────┬──────┬──────┬──────┐
│slot 0│slot 1│slot 2│slot 3│slot 4│slot 5│slot 6│slot 7│
│ 32 b │ 32 b │ 32 b │ 32 b │ 32 b │ 32 b │ 32 b │ 32 b │
└──────┴──────┴──────┴──────┴──────┴──────┴──────┴──────┘

One SIMD-add instruction adds 8 pairs of 32-bit numbers simultaneously.
```

**The rule of SIMD:** each slot does its **own** work using only its **own** bits. The instant a slot needs to reach into another slot's bits, the CPU stalls (cross-lane shuffle = slow).

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

Look at v2. The bottom 10 bits are in **slot 0**. The top 1 bit is in **slot 1**. To assemble v2, slot 1 must read into slot 0. **Cross-lane shuffle.** SIMD's promise dies.

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

**Works for SIMD.** Breaks compression: 21 of 32 bits wasted = **66% waste**. We're back to 4 GB. Compression ratio is gone.

For 5-bit values you'd waste 27/32 = 84%. For 17-bit values, you'd waste 47%. Padded packing breaks badly for values close to slot size.

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

Inside slot 0, the stream looks like normal scalar bit-packing — values straddle word boundaries, but **the straddle is between two consecutive words of the SAME slot**. Recombining them is a `shift + OR` **inside the slot**. No cross-lane access.

**Zero waste + full SIMD parallelism + lane-independent.** This is **FastLanes**.

---

## Rung 5 — Why "every 4th value" is the magic

You might ask: why does slot 0 own `v0, v4, v8, v12, …` instead of `v0, v1, v2, v3, …`?

Because of **how SIMD writes its output back to memory**. When the decoder finishes one iteration, all 4 slots emit one value each:

```
iteration 0:  slot 0 → v0    slot 1 → v1    slot 2 → v2    slot 3 → v3
iteration 1:  slot 0 → v4    slot 1 → v5    slot 2 → v6    slot 3 → v7
iteration 2:  slot 0 → v8    slot 1 → v9    slot 2 → v10   slot 3 → v11
```

Stored to output, the array comes out **in natural order**: `v0, v1, v2, v3, v4, v5, v6, v7, …` — no post-decode reshuffle needed.

If instead slot 0 had `v0..v255` contiguous, output would be `v0, v256, v512, v768, v1, v257, …` — scrambled. You'd need a permute pass to fix it. The every-Nth assignment sidesteps that.

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
