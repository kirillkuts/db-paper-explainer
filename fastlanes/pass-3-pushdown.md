# Pass 3 — Filtering Without Decoding

> **Goal:** see why FastLanes' real win in practice isn't "decode is faster" — it's "you often don't decode at all." Walk through predicate pushdown for plain bit-packing, FOR, DICT, and DELTA on the same 20-byte block from Pass 2.
> **Reading time:** ~15 minutes.
> **Method:** same ladder. Each rung names a new pain; the next rung removes it.
> **Scope:** filter scans (WHERE clauses). Aggregation pushdown (GROUP BY over compressed data) lives in a later pass.

---

## Where we left off

Pass 2 proved bit-packing decodes 3.74× faster with FastLanes than buffered scalar. But that number assumes you actually decode all the values — and in a real analytical query, **you rarely do**. A typical `SELECT … WHERE …` rejects 99% of rows. So decoding all 1B rows just to throw away 990M is wasted work. Going 3.74× faster only means you waste it 3.74× faster.

This pass is about evaluating predicates as close to the compressed bytes as you can get, so the rejected rows pay almost nothing.

---

## Vocabulary

- **Predicate** — the Boolean expression in a `WHERE` clause. `latency > 100`, `country = 'Canada'`, `ts BETWEEN A AND B`.
- **Selection bitmap** — one bit per row, set to 1 where the predicate is true. The output of a filter scan, consumed by the next operator.
- **Pushdown** — evaluating the predicate against the compressed form, not the decoded values. The rewrite that makes this possible is codec-specific.
- **Selectivity** — fraction of rows that pass. Production analytical queries are typically 0.01–1% selective. Low selectivity is what makes pushdown win.
- **Materialization** — writing decoded values to memory. Pushdown lets you skip it for rejected rows.

---

## Rung 1 — Naive: decode everything, then filter

The obvious approach.

```
   compressed bytes
         │
         ▼  decode (full)
   1B × 32-bit values in RAM        ← 4 GB intermediate buffer
         │
         ▼  SIMD CMP-GT > 100
   selection bitmap (125 MB)
         │
         ▼  next operator
```

For 1B rows at FastLanes' 3.74× decode speed:

- decode: ~1 second
- compare: ~0.1 second
- **memory pressure: 4 GB of decoded values, written then immediately read by the comparator**

### The pain

If selectivity is 1%, we materialized 1B values just to keep 10M of them. That's 990M values decoded, written to memory, read back, and discarded. And the 4 GB intermediate buffer blows out L3 cache, so the comparator can't read from cache the way a tighter pipeline would — every value comes back from DRAM. We're paying twice: once to write the junk, once to read it.

**The fix:** never let the decoded values live in memory. Either fuse decode + compare in registers, or skip decode entirely for rejected rows.

---

## Rung 2 — Fused bit-packed scan: decode in registers, emit bitmap

Use Pass 2's block. 32 values, 5 bits each, 4-stream lane-stripe layout, 20 bytes total. Query: `WHERE v > 20`.

The decoded values (from Pass 2):

```
pos: 0   1   2   3   4   5   6   7   8   9  10  11  12  13  14  15
v:   3  10  17  24  31   6  13  20  27   2   9  16  23  30   5  12

pos:16  17  18  19  20  21  22  23  24  25  26  27  28  29  30  31
v:  19  26   1   8  15  22  29   4  11  18  25   0   7  14  21  28
```

The trick is small. Take Pass 2's decode loop and slip **one SIMD-compare** in right after the unpack-and-mask step. The unpacked values are already sitting in a register — so compare them there, record which lanes passed, and never store the values at all. The decoded numbers exist for one instruction and then evaporate.

```
   reg ← bit-unpack 4 values  (one per slot)
   mask ← SIMD-CMP-GT(reg, splat(20))     ← NEW: 4-bit lane mask
   pack mask into the running bitmap byte
   (no store of `reg` itself)
```

### Walk through every iteration

Each iteration produces 4 lane results, which correspond to 4 consecutive row positions (lane-stripe property — see Pass 2 Rung 4).

| iter | values in slots 0..3 | row pos | mask bits 0..3 | bitmap byte built |
|------|----------------------|---------|----------------|-------------------|
| 0    | 3, 10, 17, 24        | 0..3    | 0, 0, 0, 1     | byte0 bits 0..3   |
| 1    | 31, 6, 13, 20        | 4..7    | 1, 0, 0, 0     | byte0 bits 4..7   |
| 2    | 27, 2, 9, 16         | 8..11   | 1, 0, 0, 0     | byte1 bits 0..3   |
| 3    | 23, 30, 5, 12        | 12..15  | 1, 1, 0, 0     | byte1 bits 4..7   |
| 4    | 19, 26, 1, 8         | 16..19  | 0, 1, 0, 0     | byte2 bits 0..3   |
| 5    | 15, 22, 29, 4        | 20..23  | 0, 1, 1, 0     | byte2 bits 4..7   |
| 6    | 11, 18, 25, 0        | 24..27  | 0, 0, 1, 0     | byte3 bits 0..3   |
| 7    | 7, 14, 21, 28        | 28..31  | 0, 0, 1, 1     | byte3 bits 4..7   |

Each byte's bits, LSB = lowest position:

```
byte 0 (rows 0..7):  0 0 0 1 1 0 0 0   → MSB-first binary 0001 1000 = 0x18
byte 1 (rows 8..15): 1 0 0 0 1 1 0 0   → MSB-first binary 0011 0001 = 0x31
byte 2 (rows 16..23): 0 1 0 0 0 1 1 0  → MSB-first binary 0110 0010 = 0x62
byte 3 (rows 24..31): 0 0 1 0 0 0 1 1  → MSB-first binary 1100 0100 = 0xC4
```

**Final bitmap: `0x18 0x31 0x62 0xC4`.** 11 set bits → 11 matching rows. Verified positions: 3, 4, 8, 12, 13, 17, 21, 22, 26, 30, 31. ✓

### What did we save?

Compared to Rung 1: **no 4 GB intermediate buffer**. Values flow through registers, and the only thing that lands in memory is the bitmap — 125 MB at 1B rows, 32× less than the decoded values would have been. We still touch every row, so for 1% selectivity it's still wasted work *per byte*. But now the waste is cache-friendly and bandwidth-cheap instead of a 4 GB DRAM round-trip.

Cost per value: Pass 2 measured ~2.25 SIMD ops/value for FastLanes decode. Adding one compare + a mask-packing op amortized over 4 values lands roughly at **~2.5 ops/value for the full scan**. Versus Rung 1's "decode (2.25) + write (1) + read (1) + compare (1)" ≈ 5+ ops/value.

### Pain

We still did the full decode work. When a predicate rejects 99% of rows, that's galling — we'd love to skip the decode entirely for the rows we're about to throw away. But to skip the decode, you have to ask your question of the *encoded* bytes directly, without reconstructing the value first. Whether you can do that depends on the codec. The next three rungs work through one codec at a time.

---

## Rung 3 — FOR pushdown: rewrite the constant, skip the base-add

`latency_ms` is FOR-encoded with `base = 50` and bit-packed 11-bit offsets. Encoded: `value = base + offset`.

The predicate `WHERE latency_ms > 200` rewrites:

```
base + offset > 200
offset > 200 - base
offset > 150            ← rewrite the constant ONCE, before the scan starts
```

Here's the move: we shifted the constant once, up front, so the inner loop can compare the raw bit-packed offsets without ever adding `base` back on. The `+ base` SIMD-add that decode would have done per iteration **vanishes from the inner loop entirely**. The actual `latency_ms` value is never reconstructed — we answered the question without ever building the number.

```
inner loop per iteration of 4 values:
   bit-unpack 4 offsets        ~2 ops/value
   SIMD-CMP-GT vs splat(150)   ~0.25 op/value (amortized)
   pack to bitmap              ~0.25 op/value
   ───
   ~2.5 ops per value
```

Same cost as Rung 2 — but notice what's different. In Rung 2 the value existed in a register for one instruction. Here the *value itself never exists* at all. For the 99% of rows we're going to reject, that's the whole win: we never assemble them in the first place.

### Block-level early-out (the free win)

Think of `min` and `max` as a label on the outside of a sealed box. If the box is labeled "everything inside is between 50 and 90" and you're looking for values above 200, you don't open it — you walk past. If the encoder stored `min` and `max` for each FOR block (cheap — one pass at encode time), the planner can decide the *whole block's fate* from the label, without running the inner loop at all:

```
block_min = base + min(offsets)
block_max = base + max(offsets)

if  C ≥ block_max  →  no row passes,   emit 32 zero bits, skip block
if  C < block_min  →  all rows pass,   emit 32 one bits,  skip block
otherwise          →  run the inner loop with rewritten constant
```

For a `WHERE ts > '2024-01-01'` over a historical column, **99% of blocks short-circuit at this level**. The SIMD inner loop never runs for them.

### Edge case — unsigned underflow

The rewrite `C - base` is computed in unsigned arithmetic over packed offsets, so there's a trap: if `C < base`, the subtraction wraps around to a huge number instead of going negative. But step back — if the constant is below `base`, then it's below *every* value in the block, so every row passes anyway. The planner catches `C < base` *before* generating the scan and just emits the all-ones early-out. Same logic at the top end: `C ≥ base + max_offset` means no row can reach the constant, so emit all-zeros. The inner loop only runs in the middle, when `base ≤ C < base + max_offset` — exactly the case where the answer actually varies row to row.

### Pre-empt: "what about range predicates?"

`WHERE value BETWEEN 100 AND 200` rewrites to `offset BETWEEN 100-base AND 200-base`. Two SIMD-compares per iteration instead of one (one >= lower, one <= upper, ANDed). Still no base-add. Still no value materialization. Cost rises to ~3 ops/value.

If either bound underflows, the block early-outs as before.

---

## Rung 4 — DICT pushdown: integer compare on bit-packed IDs

Column `country_name`. DICT-encoded:

```
dictionary:   [ "United States", "Canada", "Mexico", "Brazil", ..., 200 entries ]
              each entry: ~12 bytes string

codes column: bit-packed 8-bit IDs, one per row
              1B rows × 8 bits = 125 MB
```

Query: `WHERE country_name = 'Canada'`.

### The wrong way

Decode every code, jump into the dictionary to fetch the string it points at, then `strcmp` that string against `'Canada'`. Do that a billion times. Each lookup is a random jump into the dictionary — a cache miss — followed by a byte-by-byte string compare. That's 1B gathers and 1B strcmps for a question that has the same answer for every row holding the same code. Disaster.

### The right way — DICT pushdown

```
step 1 (once, planner-time):
   scan the 200-entry dictionary for 'Canada' → id = 1

step 2 (inner scan):
   SIMD-CMP-EQ on bit-packed IDs vs splat(1)
   emit bitmap
```

The insight: `'Canada'` has exactly one ID. Find it once, and the billion-row question collapses to "which codes equal 1?" — pure integer comparison. **The dictionary is never touched during the inner scan.** strcmp runs 200 times, on the dictionary entries, not 1B times on rows. That's a **5,000,000× reduction in string-comparison work.** And what's left in the inner loop is just an 8-bit-packed integer compare — ~1.5 SIMD ops per value, the cheapest scan in the whole pipeline.

### Pre-empt: `IN ('Canada', 'Mexico', 'Brazil')`

Build the integer set `{1, 2, 3}` at planner time. Two options for the inner loop:

- **Small set (≤4 elements):** unroll into N parallel `CMP-EQ`s and OR the masks. 1 op per element.
- **Larger sets:** build a 256-bit lookup table (one bit per possible ID) and do `gather + bitwise-AND` per iteration. One op regardless of set size.

Both keep the dictionary out of the inner loop.

### Pre-empt: `LIKE 'United%'`

Same idea, one step bigger. At planner time, walk the 200-entry dictionary and find every ID whose string matches the LIKE pattern. Now the query is just `id IN {set_of_matching_ids}` — the case you already solved above. **Wildcard string matching turns into integer set membership**, evaluated once against 200 entries instead of a billion times against rows. This rewrite is the single most important reason production analytical engines DICT-encode every string column.

### Edge case — set is the whole dictionary

If the `IN` set contains every ID actually present in the block, emit all-ones early-out without scanning. (Detect this via the block's min/max ID stats.)

---

## Rung 5 — DELTA pushdown: when the chain forces a decode

DELTA is the codec that fights back. It stores `delta[i] = v[i] - v[i-T]` (per-lane in FastLanes; see Pass 2 Rung 6) — each value is described only as a hop from the previous one. To know whether `v[i] > C`, you need `v[i]` itself, and the only way to get it is to add up every delta before it in the stream. A single offset doesn't tell you the value; you have to walk the chain.

So unlike FOR and DICT, **there's no rewrite that dodges the sum.** The dependency is baked into the codec. The deltas don't carry the answer; only their running total does.

### Best you can do: fuse the compare into the prefix-sum loop

Pass 2 Rung 6 measured DELTA decode at ~2.25 SIMD ops/value (4 parallel running sums, no materialization between stages). Add one compare per value at the end of the prefix sum:

```
inner loop per iteration of 4 values:
   bit-unpack delta            ~2 ops/value
   running += delta            ~0.25 op/value (one SIMD-add per 4 values)
   SIMD-CMP-GT(running, C)     ~0.25 op/value
   pack to bitmap              ~0.25 op/value
   ───
   ~2.75 ops per value
```

Versus FOR scan's ~2.5. So forcing the chain costs us about 10% — a **modest rise**, not a wall. And we still win the big thing: the reconstructed values live in the running-sum register and never hit memory.

### The big win is at the block level

Here's the rescue. DELTA blocks store the same `min` / `max` label on the box. And DELTA is the codec you reach for precisely on monotonic columns — timestamps, sequential IDs — where values march steadily upward. On a column like that, a block's min and max bracket a tight, ordered range, so a range predicate almost always lands entirely inside or entirely outside it. The expensive chain we just dreaded? For most blocks it never runs.

```
WHERE ts > '2024-05-01'   on a block whose max ts is '2024-04-30'   → skip block, no decode
                          on a block whose min ts is '2024-05-02'   → all pass, no decode
                          on a block straddling the cutoff           → run the fused scan
```

For a query covering one month of historical data, **99.9% of timestamp blocks short-circuit** on min/max alone.

---

## Rung 6 — Putting it together: a 3-column query end-to-end

```sql
SELECT country_name, AVG(latency_ms)
FROM events
WHERE timestamp_ns > '2024-05-01'
  AND country_id IN (1, 2, 3)
  AND latency_ms > 100
GROUP BY country_name
```

Per block, in order:

1. **Block min/max check on `timestamp_ns`** — block's max < cutoff → skip. ~99% of historical blocks eliminated here, **no decode at all**.
2. **DICT pushdown on `country_id`** — rewrite to `id IN {1,2,3}`, SIMD-compare bit-packed IDs. ~1.5 ops/value. Bitmap A.
3. **FOR pushdown on `latency_ms`** — rewrite to `offset > 100 - base`, SIMD-compare bit-packed offsets. ~2.5 ops/value. Bitmap B.
4. **AND the two bitmaps** (cheap — one SIMD-AND per byte).
5. **For matching rows only** (~0.1% of remaining block) — gather `country_id`, look up `country_name` in dictionary (200 entries → fits in cache, fast), compute `base + offset` for `latency_ms`, accumulate into the GROUP BY hash table.

Look at where the money went. The expensive work — string lookup, full decode of latency, hash insertion — runs on **0.1% of rows**. The other 99.9% never got decoded; they paid only the bit-packed-compare cost on two columns, then got AND-ed away. That's the thesis of the whole pass made concrete: the rows you reject should barely cost anything, and here they don't.

### Order matters

The planner runs cheapest predicate first when it's also expected to be most selective. DICT equality on bit-packed IDs is cheaper than FOR range scan, and `country_id IN {1,2,3}` is more selective than `latency_ms > 100` (assuming most countries aren't in the set). So step 2 before step 3.

### Single-core throughput (rough numbers)

Production engines using FastLanes-style layouts (DuckDB, Vortex, Velox) achieve **1–5 GB/s scan throughput per core** on commodity x86. For a 1B-row table at ~125 MB per pushdown-able column (after compression), one core scans all three columns in well under a second.

---

## Rung 7 — The encode-time trade

These tricks aren't free at the encoder. The encoder writes, per block:

- `min`, `max` of the values (one pass — cheap).
- The dictionary, sorted (for fast `IN` lookups), for DICT columns.
- The `base` for FOR columns (typically `min(values)`, computed during the min/max pass).
- The codec choice itself.

It costs a few extra seconds per GB at write time. Is that worth it? You pay it once, and every future query that touches the column gets to skip blocks and dodge decodes for free. Amortized over thousands of reads, those seconds pay back almost immediately. This is exactly why FastLanes targets analytical storage — write once, read many. Flip the ratio to OLTP, where you write constantly and read little, and the math reverses: you'd be paying the encode tax over and over for queries that never come.

---

## Glossary additions (Pass 3)

| Term | Meaning |
|---|---|
| **Predicate** | A Boolean expression on column values (the WHERE clause). |
| **Selection bitmap** | One bit per row, set when the predicate is true. The standard scan output. |
| **Pushdown** | Evaluating a predicate against the encoded form, without reconstructing values. Codec-specific rewrite. |
| **Selectivity** | Fraction of rows that pass the predicate. Real analytical queries: 0.01–1%. |
| **Block min/max stats** | Per-block summary stored beside the data. Lets the planner skip whole blocks before any SIMD runs. |
| **Materialization** | Writing decoded values to memory. Pushdown skips it for rejected rows. |
| **Predicate rewrite** | Translating a predicate on `value` into a predicate on the encoded form. `value > C` ↔ `offset > C - base` for FOR; `string = "X"` ↔ `id = N` for DICT. |
| **Early-out** | Block-level decision (all-ones / all-zeros / scan-required) made from stats alone. |

---

## The 3 checkpoint questions

1. **Block min/max stats skip a block when `max ≤ C` (no match) or `min > C` (all match) for a `> C` predicate. For a DICT-encoded column with predicate `country_name = 'Canada'`, write the equivalent block-skip rule. Hint: think about what `min(id)` and `max(id)` tell you — *and* what extra per-block information would make the skip rule tighter.**

2. **FOR pushdown rewrites `value > C` to `offset > C - base`. Take the predicate `value BETWEEN 100 AND 200` with `base = 50`. Write the rewritten predicate over `offset`. Then redo it for `base = 300`. What does the planner do in each case before the inner loop runs?**

3. **Rung 5 says DELTA scan costs ~2.75 ops/value and FOR scan ~2.5. But the predicate `ts > '2024-05-01'` is usually answered by block min/max alone — both codecs skip the inner loop for most blocks. So when *does* the DELTA-vs-FOR inner-loop difference actually matter? Describe a query shape where DELTA blocks really must be scanned row-by-row, and contrast it with one where they don't.**

Also flag:
- Any rung where the pain didn't feel sharp — i.e. you couldn't picture *why* it hurt before the next rung was introduced.
- Any rewrite (FOR's `C - base`, DICT's string-to-ID, the `LIKE` pre-scan) you couldn't reproduce on paper without re-reading.
- Whether you believe the "0.1% of rows pay full decode" claim from Rung 6 — what experiment would you run on the playground to verify it?

Your answers shape Pass 4 (encoder internals: how a codec is chosen per column, plus the deferred RLE decode mechanics).
