# Pass 1 — The Cascading Codecs

> **Goal:** understand the 4 codecs the paper composes (FOR, DELTA, RLE, DICT), and how they cascade into one decode pipeline.
> **Reading time:** ~15 minutes.
> **Method:** same ladder as Pass 0 — each codec is the simplest fix for the previous codec's pain.

---

## Where we left off

Pass 0 established: **FastLanes bit-packs values across SIMD lane-stripes**, so straddles stay inside one slot and decode runs in lockstep. But there's a hidden assumption: **values must be small enough to be worth bit-packing**.

If your column has 60-bit values, packing them in 60 bits saves only 4 bits per value (vs 64). That's not nothing, but it's not the 4× win the paper sells. Bit-packing on its own is **only useful when values are small**.

This pass: how to make values small enough that bit-packing pays off, **for any kind of column**.

---

## Rung 1 — When bit-packing isn't enough

Look at three real-world columns from our `events` table:

```
country_id     :  values like  7, 7, 7, 14, 7, 14, 7  (~200 distinct values, lots of repeats)
timestamp_ns   :  values like  1715000001234567890, 1715000001234567891, …  (huge, monotonic)
order_id       :  values like  1_000_000, 1_000_001, 1_000_002, …  (huge, but tightly clustered)
```

Trying naive bit-packing on each:

| Column | Max value | Bits needed | Bit-packing alone |
|---|---|---|---|
| country_id | 200 | 8 bits | ✅ 4× savings |
| timestamp_ns | ~10¹⁸ | 60 bits | ❌ barely 1.07× savings |
| order_id | ~10⁶ | 20 bits | ⚠️ 1.6× savings — meh |

**The pain:** bit-packing exploits only one property — that values are small in absolute size. Real data has other forms of redundancy (clustering, monotonicity, repetition, low cardinality) that bit-packing alone can't touch.

**The fix:** apply a *transformation* that turns the column's redundancy into "small numbers", then bit-pack the result.

That transformation is what a **codec** does. Four of them follow.

---

## Rung 2 — FOR (Frame Of Reference): big but clustered

**Observation:** `order_id` values aren't *small*, but they're all *close to each other*.

```
order_id values:  1_000_000, 1_000_001, 1_000_002, ..., 1_000_499
                  └────────── range = 500 ──────────┘
                  └──── all > 1 million ────┘
```

Think of a street where every house number starts with the same postal code — printing it on every envelope is wasted ink. The first million here is that postal code: every value shares it, so it carries no information. Subtract a **base value** (the minimum) and store just the **offsets** — the house numbers.

```
base = 1_000_000

original:  1_000_000   1_000_001   1_000_002   ...   1_000_499
offsets:           0           1           2   ...         499

  offsets max = 499  →  9 bits each instead of 20 bits.   2.2× extra savings on top of nothing.
```

### Decoding

```
decoded[i] = base + offset[i]
```

One SIMD-add. The base sits in a register, the offsets are bit-unpacked into the slots, one add reconstructs all of them in parallel. **Decoder cost: ~1 SIMD-add per batch.**

### Pain of FOR

FOR only helps if values **cluster** around some center. If `order_id` had a full 1B range, the offsets would still need 30 bits — FOR would do nothing.

**The fix:** what if values don't cluster, but *grow* — like timestamps?

---

## Rung 3 — DELTA: monotonic values

**Observation:** `timestamp_ns` values grow by tiny amounts at a time.

```
timestamp_ns:        1_715_000_001_234_567_890
                     1_715_000_001_234_567_891    +1
                     1_715_000_001_234_567_893    +2
                     1_715_000_001_234_567_898    +5
                     1_715_000_001_234_567_900    +2
                     ...
```

The values are huge. The **gaps** are tiny.

DELTA stores: the first value, then **the differences** between consecutive values.

```
first_value = 1_715_000_001_234_567_890

deltas:  +1, +2, +5, +2, +3, +1, +4, ...
                 └───── max delta ≈ 10 → 4 bits each
```

Original column: 60 bits per value. Delta column: 4 bits per value. **15× savings** before bit-packing even runs.

### Decoding — and why this is harder than FOR

FOR was easy: every value stood on its own, so you could reconstruct them in any order. DELTA isn't like that. To decode value `i`, you need value `i-1`. Which needs value `i-2`. Which needs… all the way back to `first_value`. **It's a chain.**

That sounds like bad news for SIMD — chains are serial, and SIMD wants independence. Naive scalar decode bears that out: `decoded[i] = decoded[i-1] + delta[i]`. Serial. Slow.

But don't panic — this exact chain has a well-known parallel form. SIMD decode uses a trick called **prefix sum** (also called scan): the running total at each position, computed not one-at-a-time but in `log₂(N)` doubling steps instead of `N`:

```
deltas:        [+1, +2, +5, +2, +3, +1, +4, +0]

Step 1: shift right by 1, add:
               [+1, +3, +7, +7, +5, +4, +5, +4]

Step 2: shift right by 2, add:
               [+1, +3, +8, +10, +12, +11, +10, +8]

Step 3: shift right by 4, add:
               [+1, +3, +8, +10, +13, +14, +18, +18]

Add first_value to all:
               final reconstructed timestamps
```

3 SIMD steps for 8 values. **log₂(N)** total. Each step is a SIMD shuffle + add — still SIMD-native, no scalar loop. The paper does this within each FastLanes vector (1024 values → 10 prefix-sum steps).

### Pain of DELTA

DELTA helps when values **change predictably and slowly**. It fails when:
- Values are random (deltas are as big as the original values).
- Values repeat in long runs (deltas become a stream of zeros — fine for bit-packing, but there's an even better codec for that).

---

## Rung 4 — RLE (Run-Length Encoding): long repeats

**Observation:** sorted or low-cardinality columns have **long runs** of identical values.

```
country_id (sorted):  7, 7, 7, 7, 7, 7, 14, 14, 14, 14, 14, 33, 33, 33, ...
                      └── run of 6 ──┘└──── run of 5 ────┘└─ run of 3 ─┘
```

DELTA on this would give: `0, 0, 0, 0, 0, +7, 0, 0, 0, 0, +19, 0, 0, …` — works, but still stores one value per row.

RLE stores: `(value, count)` pairs.

```
runs:  (7, 6), (14, 5), (33, 3), ...

For 6+5+3 = 14 rows, RLE stores 3 pairs.
```

Compression depends entirely on average run length. Run length 100 → 100× savings. Run length 2 → roughly break-even.

### Decoding

The output array is `expand each pair into `count` copies of `value``. Why is this **harder for SIMD** than FOR or DELTA? Because run lengths are irregular: one pair might fill 100 slots, the next just 2, so every slot wants to emit a different output size in the same iteration — and SIMD likes everyone doing the same thing at once. The FastLanes paper handles this with a specialized RLE-aware decoder; we'll see the mechanics in Pass 2.

### Pain of RLE

RLE fails when **values change frequently** with no repeats. Random-ish data → run length 1 → RLE actively *hurts* (one pair per value = 2× expansion).

### One more pain — what about strings?

RLE works on integers. What about a `country_name` column?

```
country_name:  "United States", "Canada", "Mexico", "United States", "United States", ...
```

Even with runs, you're still storing `"United States"` (13 bytes) over and over. We need a different transformation for high-cardinality categoricals — especially strings.

---

## Rung 5 — DICT: many duplicates of "big" values

**Observation:** a column has a **small set of distinct values** repeated many times. Strings are the canonical case.

```
Distinct values in country_name:  ["United States", "Canada", "Mexico", "Brazil", ..., 200 total]

Build a dictionary:
  ID    →   value
  0     →   "United States"
  1     →   "Canada"
  2     →   "Mexico"
  ...
  199   →   "Brazil"
```

Now store the column as **IDs** instead of values:

```
original:  "United States", "Canada", "Mexico", "United States", "Brazil", ...
encoded:               0,         1,        2,                0,         199, ...
```

200 distinct values fit in 8 bits per ID. A 13-byte string becomes 1 byte. **13× savings**, before bit-packing the IDs.

### Decoding

```
decoded[i] = dictionary[encoded[i]]
```

Picture eight people each handed a page number, all walking to the same book and flipping to their page at once. That's a **gather** in SIMD: each slot uses its decoded ID as an index into the dictionary array and pulls the corresponding value. AVX2 and AVX-512 have dedicated `vgather` instructions for exactly this. One SIMD-gather decodes 8 values in parallel.

### Pain of DICT

DICT fails when **cardinality is high relative to row count**. A column with 1B unique values doesn't compress — the dictionary itself becomes 1B entries.

DICT also leaves you with an **integer ID column** which still needs to be stored efficiently. Hand-off time.

---

## Rung 6 — Cascading: chain the codecs

Here's the payoff. Each codec exploits one kind of redundancy and hands back a simpler column than it got. So why stop at one? Feed that simpler column into the next codec, which finds redundancy the first one left behind. Chain them.

### A worked example: timestamp_ns column

```
Original:  1_715_000_001_234_567_890, 891, 893, 898, 900, 901, 901, 905, ...
           └──── 60-bit values, monotonic ────┘
```

#### Stage 1 — DELTA
```
first_value = 1_715_000_001_234_567_890
deltas:  1, 2, 5, 2, 1, 0, 4, ...

→ now we have small numbers, plus a few zeros
```

#### Stage 2 — FOR (on deltas)
The deltas are already small, but FOR squeezes the last drop:
```
min delta = 0
offsets:  1, 2, 5, 2, 1, 0, 4, ...   (same as deltas — min was 0, so no shift)

if min delta were, say, 1:
  offsets:  0, 1, 4, 1, 0, -1(error!), 3, ...

→ FOR helps when deltas don't include zero. With timestamps it often does, so this stage may be a no-op.
```

#### Stage 3 — Bit-pack
```
Max offset ≈ 10  →  4 bits per value.

Final stored bytes:  first_value (8 bytes) + base (8 bytes) + bit-packed offsets (4 bits × N).
```

#### Decode pipeline (reverse order, in one SIMD loop)

```
   ┌────────────────────────────────────────────────────┐
   │ Stage R3:  bit-unpack   →   raw offsets (slot 0..7)│
   ├────────────────────────────────────────────────────┤
   │ Stage R2:  + base       →   restore deltas         │
   ├────────────────────────────────────────────────────┤
   │ Stage R1:  prefix-sum   →   restore values         │
   │            + first_value                           │
   ├────────────────────────────────────────────────────┤
   │ Output: 8 reconstructed timestamps                 │
   └────────────────────────────────────────────────────┘

Total SIMD instructions per batch of 8 values:
  bit-unpack:   ~4 (shift, mask, OR-carry)
  FOR add:      ~1
  prefix-sum:   ~3-4 (log₂(8))
  ─────────────
  ≈ 10 instructions → 8 fully decoded 60-bit values
  ≈ 1.25 instructions per value (vs ~5 for serial scalar decode)
```

**The whole cascade runs as one SIMD loop, with no memory round-trips between stages.** Decoded values flow through registers, stage to stage.

### Picking the cascade per column

The encoder profiles each column and picks the best chain:

```
country_id (low cardinality, repeats)     →   DICT → RLE → bit-pack
country_name (strings, high redundancy)   →   DICT → bit-pack  
timestamp_ns (huge, monotonic)            →   DELTA → FOR → bit-pack
order_id (clustered)                      →   FOR → bit-pack
latency_ms (small, random)                →   bit-pack alone
```

Different cascade per column. The metadata at the start of each column tells the decoder which stages to apply (and in what order).

---

## Real-world hook

For a query like:
```sql
SELECT country_name, AVG(latency_ms)
FROM events
WHERE timestamp_ns > '2024-05-01'::ns
GROUP BY country_name
```

Each column uses its own cascade:
- `timestamp_ns` decodes via DELTA → FOR → bit-unpack, fed into the WHERE filter
- `latency_ms` decodes via bit-unpack alone, fed into AVG accumulator
- `country_name` decodes via DICT → bit-unpack, fed into GROUP BY hash table

**All three column decoders are tight SIMD loops.** None of them ever materializes intermediate plaintext arrays in memory — values flow from compressed bytes → SIMD registers → query operator → output, in one pass.

This is why analytical engines using FastLanes-style layouts (DuckDB, Velox, Apache Arrow's incoming format) can hit **multi-GB/s decompression bandwidths** on commodity CPUs.

---

## Glossary additions (Pass 1)

| Term | Meaning |
|---|---|
| **Codec** | A transformation that exploits one specific redundancy pattern. FOR / DELTA / RLE / DICT. |
| **FOR (Frame Of Reference)** | Subtract a per-column base. Turns clustered big values into small offsets. Decode = SIMD-add. |
| **DELTA** | Store differences between consecutive values. Helps monotonic columns. Decode = SIMD prefix-sum. |
| **Prefix sum (scan)** | Compute running totals across an array in log₂(N) SIMD steps. The SIMD-native DELTA decoder. |
| **RLE (Run-Length Encoding)** | Store `(value, count)` pairs. Wins on long runs, loses on random data. |
| **DICT (Dictionary)** | Replace each value with a small integer ID; store distinct values once. Wins on low-cardinality columns, especially strings. |
| **Gather** | A SIMD instruction that uses N indices to fetch N values from memory in one op. The SIMD-native DICT decoder. |
| **Cascade / pipeline** | Applying codecs in stages. Each stage simplifies the column for the next. Decoder reverses the order. |

---

## The 3 checkpoint questions

1. **You have a column called `event_type` with 12 distinct string values ("click", "view", "purchase", …) appearing in random order across 1 billion rows. What cascade would you pick, and why?** (Walk through which codecs you'd apply and in what order, with one-line reasoning per stage.)
2. **DELTA decode requires a prefix sum, which is "log₂(N) SIMD steps." Why is log₂(N) acceptable when scalar serial decode would also be N steps? What does the log factor actually buy you?** (Hint: think about *what kind of work* happens at each step.)
3. **A column has values that are clustered tightly AND monotonically increasing — e.g., `1_000_000, 1_000_005, 1_000_007, 1_000_012, …`. Two codecs both apply: FOR (clustered) and DELTA (monotonic). Which one do you do *first*, and why?** (Hint: think about what each stage produces and what the next stage prefers.)

Also flag:
- Any codec whose **pain** before the next codec didn't feel sharp (i.e. you couldn't picture exactly when it fails).
- Any decode operation you'd like worked out in concrete bits (Pass 2 material).
- Anything in the worked timestamp_ns example that felt hand-wavy.

Your answers shape Pass 2 — the **bit-level mechanics** with a full worked example (32 values, real bytes, real shifts, real SIMD pseudocode).
