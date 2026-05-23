# Pass 4 — The Other Decoders: Per-Lane DELTA, DICT Gather, and the RLE Question

> **Goal:** complete the decode-mechanics picture. Pass 2 deferred RLE and DICT; Pass 2 Rung 6 introduced "per-lane DELTA" in one paragraph but never traced what the encoded deltas actually look like or how decode becomes a single SIMD-add. Fix all three here.
> **Reading time:** ~15 minutes.
> **Method:** same ladder. Start from the version of DELTA that needs a prefix-sum scan, then *kill the scan* by changing the delta definition. Then DICT gather (easy). Then RLE (honestly hard).

---

## Vocabulary recap

From Pass 2 we still use **slot** (hardware SIMD lane), **stream** (one slot's assigned values), **T** (number of streams the layout commits to), and **W** (the CPU's SIMD width). The toy stays at T=4 throughout.

One new term shows up in Rung 1:
- **Prefix sum (scan)** — computing `out[i] = sum(in[0..=i])` for all `i`. The classical DELTA decoder needs this because each reconstructed value depends on every prior delta. Pure scalar is `N` serial adds; the SIMD trick (Hillis-Steele) does it in `log₂(N)` shift-and-add steps.

---

## Rung 1 — Classical DELTA: the Hillis-Steele scan, traced bit-by-bit

Classical DELTA encodes `delta[i] = v[i] - v[i-1]`. To reconstruct, each value depends on the running total of every delta before it.

Concrete numbers (from playground Example 3):

```
original  v: [ 100, 103, 107, 108, 115, 120, 122, 125 ]
first       =  100
deltas      = [    3,   4,   1,   7,   5,   2,   3 ]   (7 deltas for 8 values)
```

Take the first 4 deltas: `[3, 4, 1, 7]`. Load into a 4-lane SIMD register.

### The two-step Hillis-Steele scan (N=4 → log₂(4) = 2 steps)

```
initial (step 0):    [ 3,  4,  1,  7 ]

step 1 — shift right by 1 lane (zero-fill), add to self:
   shifted:          [ 0,  3,  4,  1 ]
   sum:              [ 3,  7,  5,  8 ]

   What each lane now holds:
     lane 0: d[0]                  = 3
     lane 1: d[0] + d[1]           = 3 + 4 = 7
     lane 2: d[1] + d[2]           = 4 + 1 = 5
     lane 3: d[2] + d[3]           = 1 + 7 = 8

step 2 — shift right by 2 lanes (zero-fill), add to self:
   shifted:          [ 0,  0,  3,  7 ]
   sum:              [ 3,  7,  8, 15 ]

   What each lane now holds (full prefix sum):
     lane 0: d[0]                       = 3
     lane 1: d[0] + d[1]                = 7
     lane 2: d[0] + d[1] + d[2]         = 8
     lane 3: d[0] + d[1] + d[2] + d[3]  = 15

add first_value (100) to every lane:
                      [103, 107, 108, 115]

these are v[1..5].  ✓  matches original.
```

Each step **doubles the reach** of every lane. After `log₂(N)` steps, the highest lane holds the full prefix sum.

### Cost per value

- 2 SIMD shifts + 2 SIMD adds = **4 SIMD ops to decode 4 values** = **1 op per value** for the scan part.
- Plus the bit-unpack of deltas (~2 ops/value from Pass 2).
- Plus chunk-stitching: the last lane of each chunk is the carry-in to the next chunk's scan. One scalar add per chunk; amortized to ~0.25 op/value.

Total ≈ 3.25 ops per value. This is the **classical** DELTA inner loop.

### The pain

The scan stage is correct and fast — but it exists *only because the encoder chose to make each value depend on its immediate predecessor*. That cross-lane data dependency is what forces the log₂(N) shifts. **The dependency is a choice, not a law.**

**The fix:** change which value each delta refers to so the dependency vanishes from inside a chunk.

---

## Rung 2 — Per-lane DELTA: kill the scan with a single SIMD-add

The trick is one tiny change in the encoder:

```
classical:    delta[i] = v[i]  -  v[i - 1]      ← previous value
per-lane:     delta[i] = v[i]  -  v[i - T]      ← value T positions back, T = number of lanes
```

Same column, T=4. Original values `[v0, v1, v2, v3, v4, v5, v6, v7]`. Per-lane deltas (store v0..v3 as-is, then take differences at stride 4):

```
stored:                v0,   v1,   v2,   v3,    d4,        d5,        d6,        d7
                       └─ first T values raw ─┘ └────── per-lane deltas ─────────┘

where  d4 = v4 - v0,   d5 = v5 - v1,   d6 = v6 - v2,   d7 = v7 - v3.
```

Now look at the lane-stripe assignment (Pass 2):

```
stream 0 (slot 0):  v0,  v4,  v8,  v12, ...      ← stride T = 4 within the stream
stream 1 (slot 1):  v1,  v5,  v9,  v13, ...
stream 2 (slot 2):  v2,  v6,  v10, v14, ...
stream 3 (slot 3):  v3,  v7,  v11, v15, ...
```

**Within each stream, the values are at exactly stride T from each other.** So `d4 = v4 - v0` is "delta within stream 0." Each stream is its own independent classical-DELTA chain. **The cross-lane dependency is gone.**

### The decode loop

After the first T values are loaded (raw `[v0, v1, v2, v3]`) into the running register, every subsequent chunk is reconstructed by **one SIMD-add**:

```
running ← initial values [v0, v1, v2, v3]      (loaded once at block start)

each iteration:
   delta_chunk ← bit-unpack 4 deltas   (one per stream, stride-T)
   running ← running + delta_chunk     (one SIMD-add)
   store running                        (one SIMD-store)
```

Concrete with the same column (timestamps spaced by 1, occasionally 2):

```
v        = [ 100, 103, 107, 108, 115, 120, 122, 125 ]
T        = 4

stored as:
   raw[0..4]  = [ 100, 103, 107, 108 ]              ← first T values
   delta[4..8]= [ 115-100, 120-103, 122-107, 125-108 ]
              = [   15,      17,     15,      17    ]   ← these get bit-packed

decode:
   running   = [ 100, 103, 107, 108 ]                    ← initial
   iter 0:
      delta ← [   15,  17,  15,  17 ]
      running ← running + delta = [ 115, 120, 122, 125 ] ← one SIMD-add
      store [ 115, 120, 122, 125 ]    ← v[4..8]
```

**No prefix-sum scan. No shifts. Just one SIMD-add per chunk.**

### Cost per value, per-lane DELTA

- Bit-unpack 4 deltas: ~2 ops/value (same as before).
- One SIMD-add per 4 values: ~0.25 op/value.
- Store: ~0.25 op/value.

Total ≈ **2.5 ops/value**. Versus classical DELTA's 3.25. ~25% cheaper inner loop, AND the inner loop is now a straight chain of independent operations (no shift latency on the critical path).

### The compression-ratio trade

Per-lane deltas span stride T instead of stride 1. Are they bigger?

For a smooth column (timestamps that grow by ~1 ns at a time), classical delta ≈ 1 per value. Per-lane delta at T=4 ≈ 4 per value. So 1-bit deltas become 3-bit deltas. **Compression ratio drops slightly** (e.g. 60-bit timestamps shrink to 3-bit instead of 1-bit deltas — still a 20× win versus 60×).

For a noisy column (deltas vary unpredictably), the *spread* of deltas matters more than the mean. Stride-T deltas have roughly T× wider spread → maybe 1 extra bit of width.

**The trade:** lose 1–2 bits per value in compression, gain ~25% in decode speed plus eliminate a `log₂(N)` latency chain. Worth it on every real workload the paper measured.

### Pre-empt: "Why didn't classical DELTA decoders just do this?"

Two reasons:

1. **Classical DELTA predates SIMD-aware columnar storage.** It was designed when reconstruction was scalar, and stride-1 deltas compress slightly better.
2. **Without lane-stripe storage, per-lane DELTA doesn't make sense.** You can't talk about "stride T" if T isn't a property of the layout. FastLanes' lane-stripe layout is what makes per-lane DELTA *natural* — the encoder and the SIMD width line up.

This is the kind of co-design the paper sells: layout + codec chosen together, not independently.

### Real-world hook

A 10-billion-row metrics-event timestamp column. Classical DELTA decode = bandwidth + log-step shifts; per-lane DELTA = bandwidth + one add. On a Xeon with AVX-512, this is the difference between **2 GB/s and 5 GB/s** decode throughput on the same compressed bytes.

---

## Rung 3 — DICT decode: the gather instruction, traced

DICT replaces each value with a small integer code; the decoder fetches the original from a dictionary table.

Toy example: a country column DICT-encoded with 4 distinct values.

```
dictionary:   index 0 → 10   (these stand for, say, country IDs after another mapping)
              index 1 → 20
              index 2 → 30
              index 3 → 40

codes column: [ 0, 2, 1, 3, 0, 2, 1, 3 ]   (8 codes, each 2 bits → bit-packed)
```

The SIMD decoder, one iteration (W=4):

```
codes_reg ← bit-unpack 4 codes        = [ 0, 2, 1, 3 ]
result    ← gather(base=&dict[0], indices=codes_reg)
                                       = [ dict[0], dict[2], dict[1], dict[3] ]
                                       = [   10,      30,      20,      40    ]
```

**One SIMD gather → 4 decoded values.** This is the entire decode path. Hardware names:
- AVX2: `vpgatherdd` (gather 4 or 8 doublewords from independent indices)
- AVX-512: `vpgatherdd` (16-wide)
- ARM NEON: no native gather wider than 8 bytes; emulated as 4 scalar loads or via `vtbl` for small tables.

### Cost per value: cycles, not just ops

Unlike bit-packed unpack (1 op = 1 cycle), a gather is **not one cycle**. It internally issues N independent loads and waits for the slowest one. Typical latency on x86:

| Dictionary size | Where it lives | Gather latency | Effective ops/value |
|---|---|---|---|
| ≤ 1 KB        | L1 cache | ~10 cycles | ~2.5 ops/value |
| 1 KB – 100 KB | L2 cache | ~30 cycles | ~7.5 ops/value |
| > 100 KB      | L3 / DRAM | 100+ cycles | 25+ ops/value |

**The decoder is a cache prefetch problem in disguise.** As long as the dictionary fits in L1, DICT is one of the cheapest codecs. If it spills, DICT becomes the slowest.

### Why the encoder chooses DICT only for low cardinality

The encoder caps DICT at a distinct-count threshold (typically ≤ 65,536 entries, so codes fit in 16 bits and the dictionary fits in a few MB). Above that, plain bit-packing or FOR is chosen instead — the encoder *would not* let DICT compete on a million-distinct-value column.

### Variable-length entries (strings)

Strings break the gather: each entry has its own length, so there's no fixed stride for the gather to use.

Standard fix — two-level indirection:

```
codes column:        [ 0, 2, 1, 3, ... ]       (bit-packed ints)
offset table:        [ 0, 13, 19, 25, 32 ]     (fixed-width: 4 bytes each)
                       └── one offset per distinct string + a terminator
string buffer:       "United StatesCanadaMexicoBrazil..."
                      └── all distinct strings concatenated
```

Decode:
1. Gather offsets `start = offset[code]`, `end = offset[code+1]` → 2 gathers, 4 indices each → 8 values total.
2. For each row, copy `string_buffer[start..end]` — variable-length memcpy, scalar.

If the query never materializes strings (Pass 3 pushdown), step 2 never runs in the inner loop. Only the offset table participates — and it's small. **This is why DICT pushdown is cheap even for huge string dictionaries.**

---

## Rung 4 — RLE: why SIMD-RLE is genuinely hard, and the pragmatic answer

The shape:

```
encoded:     [ (7, 6), (14, 5), (33, 3), ... ]      ← (value, count) pairs
decoded:     [  7, 7, 7, 7, 7, 7, 14, 14, 14, 14, 14, 33, 33, 33, ... ]
                └── 6 sevens ──┘└────── 5 fourteens ───────┘└─ 3 thirty-threes ─┘
```

### Why SIMD struggles

Pack 4 (value, count) pairs into a register. Each lane wants to emit a different number of output values:

```
lane 0: emit 6 copies of 7
lane 1: emit 5 copies of 14
lane 2: emit 3 copies of 33
lane 3: emit ... whatever
```

SIMD requires **identical per-lane behavior**. Different output sizes per lane breaks the contract. Three real approaches, ranked:

### Approach A — AVX-512 `VPEXPAND` (hardware-specific)

The `VPEXPANDD` instruction takes packed source data and spreads it according to a precomputed bitmask. With clever mask construction, you can implement RLE expansion. **Works on AVX-512 only.** Portability across ARM, AVX2 dies.

### Approach B — scalar outer loop, SIMD broadcast store (the production answer)

```c
out = output_pointer
for (value, count) in pairs:
    SIMD-broadcast value to a wide register
    while count >= 16:                     // 16 = SIMD width in elements
        store the broadcast register at `out`
        out  += 16
        count -= 16
    write remaining `count` scalar tail
```

Scalar control flow (one branch per pair), SIMD-wide stores. On any column with average run length ≥ 4, the stores hit the SIMD fast path, and throughput is **memory-bandwidth-bound on the output** (typically 30–50 GB/s).

This is what DuckDB, ClickHouse, and the FastLanes paper's reference implementation do.

### Approach C — Run-End Encoding (REE): change the format

Apache Arrow standardized this. Instead of `(value, count)` pairs, store `(values[], run_ends[])` where `run_ends[i]` is the cumulative count up to (and including) run `i`.

```
classical RLE:    [(7, 6), (14, 5), (33, 3)]
REE:              values   = [ 7, 14, 33 ]
                  run_ends = [ 6, 11, 14 ]   ← cumulative
```

**REE buys random access.** To find the value at row `i`:
- Binary search `run_ends` for the smallest index `j` with `run_ends[j] > i`.
- Return `values[j]`.

Cost: `O(log #runs)` per lookup vs `O(#runs)` for plain RLE. For point queries (`WHERE position = 50000`) or random-access scans, REE wins massively.

For full-column sequential scans, REE matches classical RLE — you just walk both arrays.

### Why the FastLanes paper does not chase a SIMD-RLE breakthrough

Honest framing: **RLE wins are bandwidth wins, not compute wins.** If a column compresses 100× under RLE, the decoded output is 100× larger than the input. Decoding a 40 MB compressed column produces 4 GB of output. On a 50 GB/s memory subsystem, *writing* the output takes ~80 ms regardless of whether the inner loop is SIMD or scalar. The decode is **bound by the store bandwidth**, not the unpack throughput.

So the FastLanes paper:
1. Uses classical RLE where it pays (low-cardinality categoricals, often after DICT).
2. Decodes with the Approach B pattern (scalar outer, SIMD broadcast store).
3. Pairs with REE for random-access workloads.
4. Spends its mechanical sophistication on bit-packing and per-lane DELTA, where SIMD actually changes the asymptotics.

This is a tasteful engineering trade — knowing where SIMD matters and where it doesn't.

### Edge case — short runs

When average run length is ≤ 4, RLE compresses poorly (one pair per value ≈ 2× expansion). The encoder rejects RLE in this regime and falls back to plain bit-packing of the same column. No decoder change needed; the metadata at the block header just says "no RLE here."

### Real-world hook

A sorted DICT-encoded `country_id` column at 1B rows, 200 distinct values. After sort, each country has long runs (~5M rows each). RLE compresses 1B 8-bit IDs (125 MB) down to 200 pairs (2.4 KB). **A 50,000× ratio.** Decode is bandwidth-bound on the 125 MB output, which streams through DRAM in ~3 ms. Whether the decode loop is SIMD or scalar matters not at all — the limit is RAM.

---

## Rung 5 — End-to-end inner loop: a cascade in concrete SIMD ops

Bringing it together. The `timestamp_ns` column with cascade **DELTA → FOR → bit-pack** (per Pass 1), now with per-lane DELTA so there's no scan.

```
encoding (write side):
  1. compute per-lane deltas: d[i] = v[i] - v[i-4]    (T=4)
  2. FOR on the deltas: offset[i] = d[i] - base
  3. bit-pack offsets to W bits each
  4. store: first 4 raw values, base, bit-packed offsets

decoding (read side, one iteration of 4 values, W=4):
```

```
   a = simd_load(reg[byte_idx])                       ~0.25 op/value
   b = simd_load(reg[byte_idx + 1])                   ~0.25 op/value
   window  = a | (b << 8)                             1 SIMD op
   shifted = window >> bit_in_byte                    1 SIMD op
   offset  = shifted & mask                           1 SIMD op
                          ← bit-unpack 4 offsets: ~3.5 ops total, ~1 op/value

   delta   = offset + splat(base)                     1 SIMD op    ← FOR: add base
   running = running + delta                          1 SIMD op    ← per-lane DELTA
   store(running)                                     1 SIMD op
                          ← reconstruction: 3 ops total, ~0.75 op/value

   ─────────────────────────────────────────────────
   total: ~7 SIMD ops produce 4 values  =  ~1.75 ops/value
```

Pass 2 measured DELTA+bit-pack at ~2.25 ops/value (Example 8). Adding FOR is ~0.25 op/value extra. The math lines up at ~1.75–2.25 depending on byte-load amortization assumptions.

**The point:** every codec in the cascade adds *one SIMD op per chunk of T values*. A 3-stage cascade is not 3× the cost of bit-packing alone — it's bit-packing + 2 SIMD ops/chunk + 0 memory round-trips. Cascades stack additively, not multiplicatively.

### Pre-empt: "What changed when DELTA went per-lane?"

In classical DELTA, the inner loop has ~3 ops for the scan (2 shifts, 1 add — log₂(4)) plus a serial chunk-carry add. Per-lane DELTA replaces all of that with ONE add. The cascade gets ~0.5 op/value cheaper, and the critical-path latency drops because there's no shift-add chain.

This is the FastLanes co-design payoff in one line: **lane-stripe layout lets codecs be re-stated as per-lane operations, which is what SIMD natively executes.**

---

## Glossary additions (Pass 4)

| Term | Meaning |
|---|---|
| **Prefix sum / scan** | `out[i] = sum(in[0..=i])`. The reconstruction operation for classical DELTA. |
| **Hillis-Steele scan** | The log₂(N) SIMD prefix-sum algorithm: at step k, shift right by 2^k lanes and add. |
| **Per-lane DELTA** | Encoding `delta[i] = v[i] - v[i - T]` so each stream is its own independent DELTA chain. Decode = one SIMD-add per chunk. |
| **Gather** | SIMD instruction that reads N independent memory positions into N lanes. The DICT decoder. |
| **Run-End Encoding (REE)** | RLE variant storing cumulative run-ends instead of counts. Supports O(log #runs) random access. |
| **Bandwidth-bound** | Performance limited by memory throughput, not by arithmetic throughput. RLE decode is the canonical case. |
| **Stride-T delta** | A delta computed at stride T instead of stride 1. Slightly worse compression, much faster decode. |

---

## The 3 checkpoint questions

1. **Take deltas `[1, 2, 5, 2, 3, 1, 4, 0]` (N=8). Walk through 3 Hillis-Steele steps. After step 3, what does each lane hold? In particular: lane 7 should equal the sum of all 8 deltas. Verify.**

2. **The per-lane DELTA encoder produces stride-T deltas (T=4). For a column whose stride-1 deltas are roughly N(μ=1, σ=0.5) (mean 1, stddev 0.5), what's the expected magnitude of stride-4 deltas? How many extra bits per value does this cost in bit-packing? (Hand-wavy answer fine — the point is to feel the trade.)**

3. **For a 1B-row column compressing 100× under RLE, decode produces 4 GB of output. The compressed bytes are 40 MB. List the limits in order: which one is the bottleneck (input-read bandwidth, inner-loop SIMD throughput, or output-store bandwidth)? Use that to argue whether a SIMD-RLE breakthrough would change the user-visible decode time.**

Also flag:
- Any rung where the pain didn't feel sharp.
- Whether the "stride-T deltas are slightly bigger" framing in Rung 2 felt hand-wavy — would a worked numerical example on real timestamps help?
- Whether RLE's "bandwidth-bound, not compute-bound" claim from Rung 4 surprised you. If yes, what experiment would convince you?

Your answers shape Pass 5 (the encoder algorithm: how a system actually *picks* a cascade per column, with sampling, cost models, and the bit-width-fitting decision).
