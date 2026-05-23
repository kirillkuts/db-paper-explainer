# Pass 5 — The Encoder: How a Cascade Gets Picked

> **Goal:** flip from read-side to write-side. Passes 0–4 traced the decoder. The decoder is mechanical — given the bytes, the algorithm is fixed. The encoder is an *optimization problem* — given the values, *which codecs* should produce the bytes? The ladder here is about search, sampling, and cost models, not bit shifts.
> **Reading time:** ~15 minutes.
> **Method:** same ladder. Each rung removes the previous rung's wasted work.
> **Scope:** how codec choice and bit-width get made. Not how the bits are written — that's the inverse of Passes 2/4 and adds no new mechanics.

---

## Vocabulary

- **Encoder / compressor** — the write-side program. Reads raw values, decides on a codec chain, emits compressed bytes plus metadata.
- **Block** — the unit of independent encoding decision. Typically 1024 values (the same T from Pass 2). One block's metadata can choose DELTA → FOR → bit-pack while the next block uses plain bit-pack — fully independent.
- **Cascade** — the codec chain applied to one block. E.g. `DICT → RLE → bit-pack`. Always ends in bit-pack (or no compression).
- **Cost model** — a function from `(cascade, sample data)` → `(predicted compressed size, predicted decode cost)`. What lets the encoder rank candidates.
- **Sample block** — a small random subset of blocks the encoder trials cascades on. Used so encoder cost is independent of column size.

---

## Rung 1 — Naive: try every cascade on every block

The simplest correct encoder:

```
for each block in column:
    for each candidate cascade in CANDIDATE_SET:
        trial-encode this block with this cascade, measure size
    pick the cascade with smallest size, emit
```

With 4 codecs (FOR / DELTA / RLE / DICT) plus the no-op, and chains of up to 3 stages, the candidate set is roughly **60–80 cascades** (most combinations don't make sense — e.g. DICT after FOR — but a clean implementation enumerates them).

Cost: 1 GB column × 80 cascades = 80 full-column passes. At 1 GB/s scan, that's **80 seconds CPU per GB to encode**.

For a 1 TB warehouse ingest, **22 hours just for codec selection**. The actual compression work is on top of that.

### The pain

Encoder cost scales with column-size × cascade-count. For a write-once-read-many workload that's not catastrophic — you only pay it once. But it makes ingest the bottleneck, not the queries. And for any column the data is *homogeneous within itself*: 1B timestamps are timestamps throughout. Trying DICT on a slice of them and trying DICT on a different slice will give nearly the same answer.

**The fix:** don't try every cascade on every byte. Sample.

---

## Rung 2 — Sampling: decide cascade once per column

Insight: the **best cascade is a property of the column's distribution**, not of any specific 1024-value block. A timestamp column wants DELTA → FOR everywhere. A country-name column wants DICT everywhere.

So: take a random sample of K blocks (say K = 64), trial-encode each with every candidate cascade, average the compression ratios, pick the winner. Apply that one cascade to all blocks in the column.

```
SAMPLE_SIZE = 64                 # blocks
sample = random_blocks(column, SAMPLE_SIZE)

for each candidate cascade:
    total_size = sum( trial_encode(blk, cascade).size for blk in sample )
    record (cascade, total_size)

best_cascade = argmin over candidates
apply best_cascade to every block in the column
```

Cost: `64 × 80 = 5120` trial encodings. **Constant in column size.** A 1 GB column and a 1 TB column take the same encoder time for cascade selection — milliseconds.

### What if the sample picks wrong?

Two failure modes:

1. **Sample bias.** Random selection makes this rare for K = 64; the law of large numbers covers ~95% of real columns.
2. **The chosen cascade *can't encode* some block.** Example: encoder picked "bit-pack with 7-bit width" based on the sample's max value of 120, but block 482910 has a value of 5000. The 5000 doesn't fit in 7 bits.

The fix for case 2: **per-block fallback**. If the chosen cascade fails on a block, fall back to plain bit-packing for that block. Set a 1-bit flag in the block header. The decoder reads the flag and picks the right path.

Per-block fallback also handles the case where the chosen cascade is *valid but suboptimal* for some block — the encoder can choose to override for that specific block if measured compression is much worse than the column average. This costs an extra trial encoding per block, but only on the suspicious ones.

### The pain that opens rung 3

Trying 80 cascades on 64 samples is still ~5000 trial encodings. Most of those cascades are *obviously wrong* before trial-encoding. A timestamp column doesn't need to try DICT. A high-cardinality column doesn't need to try RLE. We're paying compute to verify the obvious.

**The fix:** prune the candidate set using cheap statistics.

---

## Rung 3 — Cheap statistics pre-filter the cascade space

Walk the sample once and compute per-block:

| Statistic | One-pass cost | What it tells you |
|---|---|---|
| `min`, `max` | trivial | Bit-width for plain pack and FOR offset width |
| Distinct count (HyperLogLog approx) | ~constant memory | DICT viability |
| Monotonicity violations (`#i : v[i] < v[i-1]`) | trivial | DELTA viability |
| Average run length | trivial | RLE viability |
| Sortedness (Spearman or simpler ordered-pairs count) | trivial | Whether the column was pre-sorted (changes RLE/DELTA priorities) |

From these, prune:

```
distinct_count / block_size > 0.1  →  drop DICT cascades  (cardinality too high)
monotonicity_violations > 5%       →  drop DELTA cascades (not monotonic enough)
avg_run_length < 4                 →  drop RLE cascades   (RLE would expand)
bit_width(max) <= 8 already        →  drop FOR cascades   (savings too small)
```

After pruning, candidate set is typically **3–10 cascades** per column, not 80. The trial-encode pass becomes a ~10× cheaper.

### Why this is fine

The pruning rules are conservative — they only drop cascades that the statistics *prove* are bad. Dropping DICT when distinct count is 50% of block size is not a guess; it's arithmetic. So pruning never costs compression.

### Real-world hook

For typical Parquet/columnar workloads (logs, metrics, transactional events), most columns are easy: `timestamp_ns → DELTA → FOR`, `user_id → bit-pack` (already small), `country_name → DICT`, `latency_ms → FOR`. The encoder does the easy classification from statistics alone and only trial-encodes 1–2 candidates per column. Encoder throughput hits **100–500 MB/s** in practice.

---

## Rung 4 — Bit-width fitting: the per-block decision that survives sampling

Even after cascade selection, each block needs its own **bit-width**. The bit-width is the only encoder decision the sample *can't* make for the whole column — it depends on per-block min/max.

Naive: `bit_width = ⌈log₂(max - min + 1)⌉` for FOR offsets.

### The outlier problem

One block of latency values:

```
99 values:  [10, 12, 15, 11, 13, 14, ..., 22, 18, 20, 17]   ← <100 each, fit in 7 bits
1  value:   5000                                              ← timeout, needs 13 bits

naive bit-width: 13 bits for ALL 100 values.
```

The 1% outlier inflated the bit-width by 6 bits. Block size jumps from `100 × 7 = 700 bits` to `100 × 13 = 1300 bits` — 1.86× larger because of one row.

This is the **single most damaging effect** in real-world bit-packing. Web latency, financial transactions, sensor data — all have rare big values that ruin naive width-fitting.

### The fix — patched bit-packing (PFOR)

Pick a bit-width B that fits, say, the 99th percentile. Store the 1% of outliers as `(position_in_block, full_value)` exception pairs in a side stream.

```
B = 7 bits (fits 99% of values)

bit-packed stream:  99 normal values @ 7 bits each
                  + 1 placeholder slot for the outlier (any value, irrelevant)

exception stream:   (position=42, value=5000)
                    one entry: 2-byte position + 4-byte value = 6 bytes
```

Decoder: bit-unpack as usual (gives wrong value at position 42), then walk the exception stream and **overwrite** the wrong values with the correct ones. The exception stream is short (≤1% of block size), so the overwrite pass is negligible.

### Numbers

```
naive:    100 × 13 bits          = 1300 bits = 162 B
patched:  100 × 7 bits + 6 B     =  700 + 48 = 134 B
                                  → 1.2× smaller, with one outlier
```

For blocks with skew (heavy-tailed distributions), the win is 2–4×.

### Why this lives in FastLanes specifically

This idea pre-dates FastLanes — it's **PFOR** (Patched Frame Of Reference, Goldstein/Ramakrishnan/Shaft 1998) and modernized as **ALP** for floats (Afroozeh et al. 2023, same group as the FastLanes paper). FastLanes inherits the technique; the contribution of FastLanes is that **patched bit-packing fits cleanly into the lane-stripe layout** — the exception stream is a separate side-channel that doesn't disturb the SIMD inner loop. Pure decode stays at ~2.25 ops/value; the exception overwrite is a separate scalar pass over a small array.

### Finding B from the sample

The encoder runs through candidate widths `B = 1, 2, 3, ..., bit_width(max)` and picks the one minimizing:

```
total_bits(B) = block_size × B    +    num_outliers(B) × exception_entry_size
              └─ packed cost ─┘       └────── exception cost ──────┘
```

`num_outliers(B)` = count of values that exceed `2^B - 1` (or fall outside the FOR offset range). Walk the sample once, sort values, the curve has a clear minimum. Pick the bottom of the U.

### Edge case — bimodal distributions

If the distribution is 50% in `[0..100]` and 50% in `[10000..10100]`, no choice of B works well:
- `B = 7` → 50% outliers, exception stream dominates.
- `B = 14` → no outliers but 7 wasted bits on every "small" value.

The encoder detects this (the U-curve has no clear minimum) and **splits the cascade**: insert a DICT or FOR stage above the bit-pack to map both modes into a small range. Or, in the worst case, just store the values raw — bimodal data sometimes can't be compressed.

---

## Rung 5 — The cost model: it's not just bytes

So far the encoder picks the cascade with the smallest encoded size. But pure size minimization can choose a cascade that *decodes slowly*, which is usually a bad trade for analytical workloads.

The cost model:

```
total_query_cost(cascade)  =  α × bytes_on_disk(cascade)
                            + β × decode_cycles_per_value(cascade)
```

α (storage weight) and β (CPU weight) depend on the deployment:

| Workload | α | β | Cascade preferences |
|---|---|---|---|
| Cold archival storage, rare queries | high | low | Maximize compression, even if decode is heavy. RLE-heavy, DELTA-heavy. |
| Hot OLAP, many queries | low | high | Maximize decode speed. Plain bit-packing, FOR. Skip DELTA when not needed. |
| Default (balanced) | medium | medium | Mostly DELTA/FOR/DICT cascades + bit-pack. RLE only on highly-redundant columns. |
| Pushdown-dominated (Pass 3 queries skip most rows) | low | depends | Predicate-rewritable codecs (FOR, DICT) preferred over DELTA, because DELTA forces full decode |

The β value gets measured per CPU at install time: how many cycles does a DELTA-scan inner-loop iteration take on this hardware? The encoder uses that constant when ranking candidates.

### What "decode cycles" means concretely

Per Pass 2/4, the per-value cycles for each codec:

```
plain bit-pack:    ~2.25 ops/value
FOR + bit-pack:    ~2.5  ops/value     (one extra add)
DELTA + FOR:       ~2.75 ops/value     (per-lane DELTA, one more add)
DICT (L1):         ~2.5  ops/value     (gather over hot dictionary)
DICT (L2+):        ~7.5+ ops/value     (gather cache-miss)
RLE:               bandwidth-bound, decoder spends most time storing output
```

So a sample column that compresses 1.8× under FOR+bit-pack (2.5 ops/value) vs 2.2× under DELTA+FOR+bit-pack (2.75 ops/value): plain FOR is preferred unless the storage cost wildly outweighs the 10% extra decode cost.

### Pre-empt: "Doesn't this make the encoder hardware-specific?"

Slightly. The encoder's β values change per CPU generation. But the *encoded bytes are still interpretable* (Pass 2 Rung 5) — any decoder reads them correctly. The cost model only affects which cascade is *chosen*, not how the resulting bytes are interpreted. An AVX-512 server can read bytes written by an ARM phone encoder; the chosen cascades might be suboptimal for the reader, but they still decode.

---

## Rung 6 — Block metadata: the full ledger

What does the encoder actually write at the start of each block?

```
block header (compact, typically ~30 bytes):
   cascade_id          1 byte    ← index into a registry of cascade chains
   bit_width B         1 byte
   value_count         2 bytes   ← usually = T (1024), but tail blocks differ
   min, max            8 bytes each   ← for Pass 3 pushdown skip
   base (FOR)          8 bytes        ← if FOR stage present
   first_values        T_first × 8 bytes  ← for DELTA carry, T_first = T (1024)
                                         only present if DELTA in cascade
   dict_id             2 bytes        ← reference to shared dictionary, if DICT
   exception_count     2 bytes
   exception_offset    4 bytes        ← byte offset into block's exception stream

bit-packed data:        block_size × B bits

exception stream:       exception_count × (2-byte position + bit-width-fitting value)
```

For a typical block of 1024 8-bit values:
- header: ~30 B
- packed data: 1024 B
- exceptions: ~10 × 6 B = 60 B
- **total ~1114 B for 1024 values = ~8.7 bits/value** vs raw 4 bytes/value = **3.7× compression**, plus the metadata for pushdown.

### Block size: why 1024 specifically

T = 1024 is the FastLanes virtual lane count (Pass 2 Rung 5). Block size = T values × N codec instances = exactly one "decode unit" per SIMD pass. Smaller blocks mean more header overhead. Larger blocks mean coarser min/max stats (and worse pushdown skip rates).

1024 is the sweet spot for x86 SIMD widths 4/8/16 — all divide 1024 evenly, so any CPU decodes one block in an integer number of SIMD iterations.

### Per-column metadata (one-time, not per-block)

- Codec choice for the column (the sampled-and-chosen cascade)
- Shared dictionary (for DICT columns)
- Total block count and offsets (for random-block access)

---

## Rung 7 — The whole encoder pipeline

Putting Rungs 1–6 together. One column of N values:

```
1. SAMPLE                    pick K random blocks (K = 64)
2. STATISTICS                walk sample, compute min/max/distinct/monotonicity/run-len
3. PRUNE                     drop cascades disqualified by statistics
4. TRIAL ENCODE              trial-encode each surviving cascade on sample
                              measure (compressed size, decode-cycle estimate)
5. RANK                      cost = α × size + β × cycles; pick the winner
6. BUILD METADATA            shared dictionary if DICT, column header, etc.
7. ENCODE COLUMN             apply the chosen cascade to every block in the column
                              per-block: pick B, find exceptions, write block header + data
8. FALLBACK                  if a block can't be encoded with the chosen cascade,
                              emit it as plain bit-pack and set the fallback flag
```

Total encoder cost: one full pass over the column (step 7) plus a small constant for steps 1–6. **Encoder runs at memory-bandwidth speeds**, ~500 MB/s on modern x86.

### How does this compare to the decoder?

| Phase | Throughput | Per-value cost |
|---|---|---|
| Encoder | ~500 MB/s | ~50 cycles/value (one-time) |
| Decoder | ~2–5 GB/s | ~2.25 cycles/value (per query) |

Encoder is **~20× slower** than decoder. But: encoder runs *once*; decoder runs every query, every scan, forever.

### The total economic argument

If a column is queried 100 times over its lifetime, the encoder's 50 cycles/value is amortized to 0.5 cycle/value per query — far below the decoder's 2.25. Encoder time is essentially free in the limit.

Even better: any improvement to the encoder's cost model (better β estimate, smarter pruning) saves cycles on every future query. **Encoder optimization compounds.** This is why production columnar engines spend disproportionate effort on encoder quality.

---

## Glossary additions (Pass 5)

| Term | Meaning |
|---|---|
| **Block** | The unit of independent encoding decision. 1024 values in FastLanes. |
| **Cascade** | The codec chain applied to one block. |
| **Sample block** | A random block used to trial-encode candidate cascades. |
| **Cost model** | Function ranking cascades by `α × bytes + β × decode_cycles`. |
| **Patched bit-packing (PFOR)** | Bit-pack to fit the 99th percentile; store the 1% outliers as `(position, value)` exception pairs. |
| **Per-block fallback** | A block flag that signals the encoder gave up on the chosen cascade and stored plain bit-pack. |
| **Cascade pruning** | Dropping cascades that statistics prove can't win, before any trial encoding. |

---

## The 3 checkpoint questions

1. **The encoder samples 64 blocks out of 1M to pick a cascade. Suppose 95% of blocks have "best cascade = A" and 5% have "best cascade = B." Without per-block fallback, what fraction of the column's bytes is sub-optimally encoded? *With* per-block fallback (where a block flagged for plain bit-pack is decoded as plain bit-pack, costing maybe 20% extra bytes), what fraction of bytes total are wasted? Compare to the cost of running per-block cascade selection instead of sampling.**

2. **Patched bit-packing fits 99% of values in B bits + exception stream for 1%. Walk through how the encoder finds B: enumerate the steps for a block of 1024 values, of which 1010 fit in `[0..100]` and 14 fit in `[0..5000]`. Concretely: which B values are tried? What's the cost function evaluated at each? What's the winner?**

3. **The cost model is `α × bytes + β × cycles`. For a "pushdown-dominated" workload where 99% of rows are filtered out before decode (Pass 3), how should the encoder reweight α and β? In particular: does DELTA become more or less attractive? Does DICT become more or less attractive? Why? (One sentence each.)**

Also flag:
- Any rung where the *pain* before the next rung didn't feel sharp.
- Whether the cost-model framing in Rung 5 felt like real engineering or like hand-waving — would a concrete worked example with two competing cascades and explicit α/β values help?
- Whether the patched-bit-packing example in Rung 4 felt mechanically clear, or whether you'd want a bit-by-bit trace like Pass 2 did for plain bit-packing.

Your answers shape Pass 6 (the paper's experimental results: what was measured, on what hardware, vs which baselines, and how to read the figures).
