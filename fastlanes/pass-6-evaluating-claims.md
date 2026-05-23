# Pass 6 — Reading the Paper's Experiments Critically

> **Goal:** pivot from *understanding the mechanism* to *evaluating the claim*. Passes 0–5 built the model of how FastLanes works. Pass 6 builds the discipline of asking *whether it actually delivers*, what baselines are honest, what numbers are gameable, and what the paper carefully does *not* claim.
> **Reading time:** ~15 minutes.
> **Method:** ladder of skepticism. Each rung is a question you should ask before accepting the next claim.
> **Note:** I'll mark every concrete number as either *from the paper / playground* (anchored) or *order-of-magnitude estimate* (use as a sanity bound, not a citation).

---

## Vocabulary

- **Headline claim** — the single number sold in the abstract. "4× faster decompression."
- **Baseline** — what the new system is compared against. *Choice of baseline determines the size of the win* — the most important detail nobody reads carefully.
- **Iso-compression** — comparing decode throughput at *equal compression ratio*. The fair comparison; otherwise you're trading axes.
- **Microbenchmark** — measures one tight loop in isolation. Easy to make look good. Decoder-only is the canonical kind here.
- **End-to-end benchmark** — measures a full query. Filter, aggregate, project. Harder to game; closer to user experience.
- **Hot cache / cold cache** — data already in L1/L2/L3 vs not. A 10–30× perf delta hides here. Microbenchmarks live in hot cache by accident.
- **Pareto frontier** — the set of points where you can't improve one axis (throughput) without sacrificing another (ratio). The honest way to compare codecs that trade differently.

---

## Rung 1 — The headline: "4× faster decompression"

The FastLanes paper's headline is roughly "**~4× faster decompression at equal or better compression ratio.**" Sounds great. Before accepting, four questions:

1. **vs what?** "State of the art" is a phrase, not a system. Is it BP128? Buffered scalar? Apache Parquet's V2 codec? Each gives a different ratio.
2. **on what hardware?** AVX-512 / AVX2 / NEON / scalar — different SIMD widths give different speedups. The same FastLanes implementation can be 1.5× on NEON and 8× on AVX-512.
3. **on what data?** Bit-width matters. Cascade complexity matters. Real distributions vs uniform random matters.
4. **measured how?** Throughput in GB/s of *compressed bytes* read? Of *decoded values* written? Latency for one block? End-to-end query time?

A 4× claim under one combination can be 1.2× under another. The pain: most readers see "4×", anchor on it, and never check the conditions.

**The fix:** track all four answers when reading any benchmark. The rest of this pass walks each one.

---

## Rung 2 — The baseline question

There are three honest baselines for a SIMD bit-packing codec. Each tells you a different thing:

### Baseline A — Raw scalar (one byte load per value)

The unbuffered loop. ~12 ops per value (Pass 2 Rung 6 naive scalar). **Not a real baseline** — nobody ships unbuffered scalar in production. The win against raw scalar is artificial: any SIMD codec looks 5–10× faster, regardless of cleverness.

When a paper compares against raw scalar without also showing buffered scalar — that's the tell. The real win is being hidden.

### Baseline B — Buffered scalar (shift-register decode)

Pass 2 Rung 6: ~8 ops/value. The honest scalar upper bound. **This is the right "no SIMD" baseline.** Speedup vs buffered scalar tells you what SIMD itself buys you, *correctly attributing the win to vectorization, not loop optimization*.

Pass 2's playground Example 8 measured **3.74× FastLanes-SIMD vs buffered scalar** on 50M 17-bit DELTA values. The "4×" headline is right at this number.

### Baseline C — BP128 (the previous SIMD SOTA)

[BP128](https://github.com/lemire/FastPFor) is Daniel Lemire's SIMD bit-packing library. It uses **padded-slot bit-packing** (Pass 0 Rung 4 Fix A) — each value fits in a fixed slot to avoid straddles, at the cost of compression ratio (33%+ waste at typical bit-widths).

vs BP128, FastLanes:
- decodes a bit faster (smaller margin — both are SIMD)
- compresses tighter (because no slot padding)

So the comparison is a **two-axis Pareto improvement**, not a single number. FastLanes sits upper-left in (throughput × compression-ratio) space — better on both axes than BP128.

### What to look for in the paper

Demand all three baselines. If the paper only shows vs raw scalar — they're hiding behind a weak comparison. If the paper only shows vs BP128 — they're hiding the absolute speed against the scalar floor. A trustworthy systems paper shows **buffered scalar AND BP128**, on the same hardware, same data, same SIMD width.

---

## Rung 3 — Hardware: where does the 4× actually come from?

Decompose the 4× into ingredients:

```
SIMD width multiplier:      AVX-512 = 16-wide u32 ops      → up to 16× theoretical
                            AVX2    =  8-wide              → up to  8×
                            NEON    =  4-wide              → up to  4×
                            scalar  =  1                   → 1×
×
Lane utilization:           FastLanes eliminates cross-lane     → ~1.0  fraction usable
                            naive bit-pack needs cross-lane    → ~0.3
                            BP128 with padding                  → ~0.7 (waste)
×
Memory-bandwidth ceiling:   bound by DRAM if column > L3        → 0.5–0.8
×
Loop overhead amortization: smaller blocks pay more overhead    → 0.8–0.95
                            
═══════════════════════════════════════════════════════════
combined typical (AVX-512):  16 × 1.0 × 0.6 × 0.9   ≈   8× theoretical
combined typical (AVX2):      8 × 1.0 × 0.6 × 0.9   ≈   4×
combined typical (NEON):      4 × 1.0 × 0.6 × 0.9   ≈   2×
```

These are *order-of-magnitude estimates*, not paper numbers. The point: **the 4× headline corresponds roughly to AVX2.** On NEON (ARM phones, Apple silicon) the win is more like 2×. On scalar fallback, it's 1× (FastLanes has no advantage over buffered scalar when SIMD is unavailable).

### What to look for

A good systems paper reports throughput for **multiple SIMD widths**, with the same data. FastLanes' interpretability property (Pass 2 Rung 5) is what makes this possible — the same bytes decode on every width — so the paper has no excuse to report only one width.

If the paper only reports AVX-512 numbers, ask: "what does this look like on the ARM Mac people actually own?" The answer is probably less impressive.

---

## Rung 4 — Synthetic data vs real data

Microbenchmarks default to **synthetic data**. Common varieties:

| Flavor | Description | How it games the benchmark |
|---|---|---|
| Uniform random in `[0, 2^B - 1]` | Every value fits in exactly B bits | No outliers → patched bit-packing (Pass 5 Rung 4) never triggers fallback. Best-case bit-packing. |
| Sequential `1, 2, 3, ...` | Perfectly monotonic | DELTA produces deltas of 1 → tiny bit-widths. Best-case DELTA. |
| Fixed value `7, 7, 7, ...` | Single distinct value | RLE compresses to one pair. Best-case RLE. |
| TPC-H lineitem | Semi-synthetic OLAP benchmark | Designed to mimic real OLAP shapes. Reasonable but not adversarial. |
| Real traces (Cloudflare logs, NYC taxi, GH archive) | Production data | The honest test. Skew, outliers, mixed cardinalities. |

A paper that reports only uniform random + TPC-H is *probably honest*, but the win on real data is unclear. A paper reporting **at least 5 real-world traces** has done the work.

### What hurts FastLanes on real data (and what the paper should disclose)

- Heavy-tailed distributions: trigger patched-bit-pack exception streams, raise decode cost.
- High-cardinality string columns: don't compress well under DICT, fall back to plain or bit-pack — modest wins.
- Mixed-distribution columns: per-block fallback flag triggers often, encoded data is heterogeneous.

These should appear in a "real-world distributions" section. If they don't, the paper hasn't honestly tested.

---

## Rung 5 — Hot vs cold cache: where does the data live?

A 1024-value block at 8 bits/value = **1 KB**. **Fits in L1 cache.** Microbenchmarks that decode the same block 1M times measure pure compute throughput, with the data permanently resident in L1 cache.

Real query workloads scan GB of data. The column doesn't fit in any CPU cache. The decoder is **bandwidth-bound**.

Three cache regimes:

| Regime | Column size | What dominates |
|---|---|---|
| L1-resident | < 32 KB | Pure compute. SIMD wins biggest. Honest microbenchmark territory. |
| L3-resident | < few MB | Mostly compute with some bandwidth pressure. |
| DRAM-resident | > L3 size | Bandwidth-bound. SIMD wins shrink; once you're at DRAM's ~50 GB/s ceiling, more compute throughput is wasted. |

Pass 2 Example 8 deliberately pushed the workload past L3 (50M values × 17 bits ≈ 106 MB) and measured **3.74×**, *below* the 5.3× compute ceiling — that gap is the bandwidth tax.

### Real-world consequence

For a real 100 GB OLAP column, you're firmly in DRAM-resident regime. FastLanes' decode-compute speedup matters less than the *compressed bytes you have to move*. **A 2× tighter compression ratio is worth more than a 2× decode speedup at scale**, because both halve the bandwidth need but tighter compression *also* halves disk and network cost.

This is why the FastLanes paper's pitch is "**4× decode at equal or better compression**" — the "equal or better compression" half does most of the user-visible work in real deployments.

### What to look for

Demand the column-size axis: a sensitivity plot showing throughput as the column grows from L1 → L2 → L3 → DRAM. FastLanes' advantage should narrow as data gets bigger, but it should *still win*, just by less. If the paper only reports L1-resident numbers, the real-deployment behavior is unmeasured.

---

## Rung 6 — Microbenchmark vs end-to-end

Two flavors of measurement:

### Microbenchmark — decode-only

A tight loop that decodes values into a sink (sometimes just `volatile` to prevent the compiler from optimizing the load away). Measures the decoder alone.

This is what Pass 2 Example 8 measured: 3.74×.

### End-to-end — full query

Storage → decode → filter → aggregate → output. Measures what the user sees.

If decode is 30% of query time and you make decode 4× faster, end-to-end speedup is:

```
new_query = 0.30 / 4 + 0.70 = 0.075 + 0.70 = 0.775   (of original time)
end-to-end speedup = 1 / 0.775 ≈ 1.29×
```

A 4× decode win → **1.29× end-to-end**. Smaller, but real.

### When end-to-end EXCEEDS microbenchmark

Pass 3 pushdown changes this. If 99% of rows are filtered out and FastLanes-friendly codecs (FOR, DICT) let the planner skip decode entirely on those rows, the saved decode time is enormous — much larger than a 4× per-value speedup on the 1% that pass.

Example: 1B rows, 1% selectivity, decoder runs only on 10M rows = 100× fewer values touched at all. End-to-end could be **10–100×** if storage I/O also drops proportionally.

So the right framing: **microbenchmark = decoder potential. End-to-end with pushdown = user-visible win.** The paper should report both. The pushdown win is the bigger story but harder to measure cleanly because it depends on the query.

### What to look for

A trustworthy paper reports both microbenchmark *and* end-to-end TPC-H (or similar). If only microbenchmark is reported, the user-facing impact is uncertain.

---

## Rung 7 — What a trustworthy systems paper does *not* claim

The strongest signal of an honest paper is what it carves out as **not** in its claim. For FastLanes, the implicit limits are:

| Limit | Reason | What the paper should acknowledge |
|---|---|---|
| Not a win on RLE-heavy columns | RLE decode is output-bandwidth-bound (Pass 4 Rung 4); SIMD vs scalar barely matters | RLE is in the cascade list, but the headline isn't about RLE |
| Not a win on encoder throughput | Encoder ~20× slower than decoder (Pass 5); sampling helps but it's still write-once cost | Encoder throughput numbers reported separately, not buried |
| Not a win on point access | Layout is sequential; reading row N requires decoding its 1024-value block | "Best for full or filtered scans, not point lookups" |
| Not a win on updates | Per-block metadata commits to decisions; updating a value requires re-encoding the block | "OLAP, not OLTP" |
| Not a win on tiny columns | Encoder's sample-and-pick has fixed overhead; <10K-value columns can't amortize it | "Block-aligned; expects ≥ ~64K values per column" |
| Not a win on hardware without SIMD | The 4× evaporates on pure-scalar fallback | "Same bytes decode on scalar; expect raw bit-packing perf" |

A paper that includes a "Limitations" or "Discussion" section calling these out **earns trust**. A paper that elides them is overclaiming.

---

## Rung 8 — The seven questions a good experimental section answers

If I were reviewing the paper, I'd look for the following plots/tables, in roughly this order:

1. **Compression ratio table.** FastLanes vs baselines on 5+ datasets. *Does the layout sacrifice ratio for speed?*
2. **Decode throughput table.** Per-codec GB/s on multiple SIMD widths. *What's the raw speed?*
3. **Pareto plot.** Throughput vs ratio, scatterplot. FastLanes should sit upper-left. *Is the win on both axes or only one?*
4. **End-to-end query benchmark.** TPC-H or similar, FastLanes-backed engine vs Parquet/Arrow baseline. *What does the user see?*
5. **Hardware portability table.** Same bytes decoded on AVX-512 / AVX2 / NEON / scalar. *Does interpretability hold in practice?*
6. **Sensitivity studies.** Bit-width 3 → 30, selectivity 1% → 99%, column size L1 → DRAM. *Where does the win shrink?*
7. **Encoder cost.** Throughput, latency, sampling overhead. *Is the encoder honest about its cost?*

Tick boxes as you read. Each missing one is a question the paper didn't answer.

---

## Rung 9 — Order-of-magnitude numbers to anchor against

*These are estimates from general columnar-storage literature and the playground, not directly from the FastLanes paper text. Use them as a **sanity bound** — if the paper's numbers fall wildly outside these ranges, look for an explanation in the paper.*

| Metric | Plausible range |
|---|---|
| FastLanes decode throughput, plain bit-pack, AVX-512 | 5–15 GB/s per core |
| FastLanes decode, DELTA + FOR + bit-pack, AVX-512 | 3–8 GB/s per core |
| Encoder throughput (sampled cascade selection) | 200–500 MB/s per core |
| Compression ratio vs raw u32, typical timestamp column | 5–20× |
| Compression ratio, low-cardinality string column, DICT+pack | 50–200× |
| End-to-end TPC-H speedup vs Parquet baseline | 1.5–3× |
| BP128 decode throughput | 60–80% of FastLanes' (slower, but close) |
| Buffered-scalar decode throughput | 0.5–1.5 GB/s per core |

If the paper claims:
- 50 GB/s decode on AVX-512 — suspicious (approaches single-channel DRAM ceiling).
- 10× end-to-end TPC-H — suspicious (decode is rarely > 50% of query time).
- 1000× compression — suspicious (column would have to be near-constant).

If it claims:
- 8 GB/s decode AVX-512, 1.7× end-to-end on TPC-H — plausible and consistent.

---

## Glossary additions (Pass 6)

| Term | Meaning |
|---|---|
| **Headline claim** | The single number sold in the abstract. |
| **Iso-compression** | Comparing decode throughput at equal compression ratio. |
| **Pareto frontier** | Set of points where no axis can improve without another sacrificing. |
| **Microbenchmark** | One-loop measurement of one component. |
| **End-to-end** | Whole-system measurement of a user-visible task. |
| **Cache regime** | Which level of the memory hierarchy holds the working set: L1, L2, L3, DRAM. |
| **Sensitivity study** | A plot of metric vs parameter, showing how a result responds to changes. |

---

## The 3 checkpoint questions

1. **The paper claims "4× faster decompression than the state of the art." Pass 2 measured 3.74× on a DRAM-resident, DELTA-cascaded benchmark; the compute-only ceiling vs buffered scalar is ~3.6×. Reconcile: how can the paper's headline be 4× when the playground measures 3.74× and the analysis says 3.6×? List two plausible explanations and identify which one would be *honest* and which would be *misleading*.**

2. **You see this table in the paper:**

   | Codec | Throughput (GB/s) | Compression ratio |
   |---|---|---|
   | FastLanes plain bit-pack | 12.5 | 4.0× |
   | BP128 | 9.8 | 2.7× |
   | Buffered scalar | 1.5 | 4.0× |

   **Which baseline makes FastLanes look most impressive? Which baseline is the *fair iso-compression* comparison? Explain in one sentence why the choice of baseline matters more than the FastLanes number itself.**

3. **The paper reports an end-to-end TPC-H query getting 1.4× faster when storage switches to FastLanes. The decode microbenchmark shows 4× faster. Explain the gap from first principles (using the math in Rung 6). Then: name one query *shape* — described in Pass 3 — where the end-to-end speedup might *exceed* the microbenchmark speedup, and explain why.**

Also flag:
- Whether the "what the paper does *not* claim" rung felt like real reading-discipline or like meta-philosophy.
- Whether the order-of-magnitude numbers in Rung 9 felt like useful sanity bounds, or like guesses you'd rather not anchor on.
- Whether you'd want Pass 7 to be hands-on: actually run the playground at the paper's scales, measure against buffered scalar and BP128 if available, and compare your numbers to the paper's.

Pass 7 candidate: **validating the claims experimentally**. Run the playground's Example 8 at multiple scales, run a BP128 reference if you can get one, and see how the numbers match the paper. This is the falsification step that turns a model into knowledge.
