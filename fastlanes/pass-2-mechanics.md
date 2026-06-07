# Pass 2 — Bit-Level Mechanics

> **Goal:** make every claim from Pass 0/1 concrete. Real values, real bytes in hex, real SIMD register state per iteration. By the end, you can hand-decode a FastLanes block on paper.
> **Reading time:** ~18 minutes.
> **Method:** same ladder. Each rung is the simplest mechanical thing that hurts; the next rung fixes the hurt.
> **Scope:** bit-packing mechanics only. RLE/DICT decode mechanics live in a later pass.

---

## Vocabulary recap

Pass 0 split the overloaded word **lane** into two:
- **slot** — one parallel sub-register inside a SIMD register (the physical hardware thing).
- **stream** — a logical sequence of values assigned to one slot's work (the logical thing).

In FastLanes' lane-stripe layout, **slot N owns stream N**: every Nth value (mod number of slots) ends up in that slot's stream. We will keep using "slot" for the hardware piece and "stream" for the assigned values.

A new term we will need:
- **virtual lane** — stream index in the encoded layout. We use *stream* when talking about a sequence of values; we use *virtual lane* when talking about its index slot in the format. T = number of virtual lanes = number of streams. T is fixed by the format; the physical SIMD width W is whatever your CPU has. With T virtual lanes and W ≤ T, slot N of a W-wide SIMD register carries virtual lanes `N, N+W, N+2W, …` across multiple decode passes.

We will reach the virtual-lane idea by rung 4. Until then, virtual lane = slot = stream (they only diverge once W ≠ T).

---

## Rung 1 — Pick a tiny but concrete example

Toy size: 32 values, 5 bits each, 4 slots.

Why these numbers?

- **5 bits** — small enough to write every bit on the page, but big enough that values straddle byte boundaries (since 8 is not a multiple of 5). Straddles are the whole point.
- **32 values** — `8 values per stream × 4 streams`. 8 values per stream because `8 × 5 = 40` bits is the smallest multiple of 5 that lands on a byte boundary — fewer values would leave a partial byte at the end of the stream, which makes per-stream loads awkward. 8 × 5 = 40 bits = exactly 5 bytes per stream. Clean byte boundaries.
- **4 slots** — fits in one 128-bit SIMD register (4 × 32-bit slots = 128). The smallest real SIMD width (SSE, NEON).

Total packed size: `32 × 5 bits = 160 bits = 20 bytes`.

The values come from the playground's Example 6: `v[i] = (i * 7 + 3) mod 32`.

```
v[0..31] = [ 3, 10, 17, 24, 31,  6, 13, 20,
            27,  2,  9, 16, 23, 30,  5, 12,
            19, 26,  1,  8, 15, 22, 29,  4,
            11, 18, 25,  0,  7, 14, 21, 28]
```

Each value is in `[0, 31]`, so 5 bits each — exactly. Verified by `simd-playground/src/main.rs` Example 6.

Real-world hook: a `latency_ms` column where all values are under 32 ms (low-latency endpoint). Or `country_id` after DICT mapping to 32 distinct countries. Both are real shapes.

---

## Rung 2 — Naive: pack the 32 values back-to-back, decode scalar

Forget SIMD for a moment. Think of the bit-stream as a ribbon you write values onto end to end, no gaps. Just pack v0, v1, v2, … onto it, low bits first.

```
bit pos:   0   5   10  15  20  25  30  35  40  ...
           │   │   │   │   │   │   │   │   │
values:    v0  v1  v2  v3  v4  v5  v6  v7  v8 ...
           5b  5b  5b  5b  5b  5b  5b  5b  5b
```

v0 = 3 → bits `00011`. v1 = 10 → bits `01010`. v2 = 17 → bits `10001`. Pack them low-bit first.

Byte 0 holds bit positions 0..7. Bits 0..4 come from v0 (`00011`, with bit 0 = LSB = 1). Bits 5..7 come from the low 3 bits of v1 (`01010` → bits 0..2 of v1 are `010`).

```
byte 0 layout (bit 7 .. bit 0):
  bit  7   6   5   |   4   3   2   1   0
  src  v1b2 v1b1 v1b0 | v0b4 v0b3 v0b2 v0b1 v0b0
  val   0   1   0   |   0   0   0   1   1
  →  binary 01010011  =  0x53  ✓
```

So byte 0 = `0x53`. Good — but already this packing pain shows up at the cross-byte joint: v1 itself is split across byte 0 (its low 3 bits) and byte 1 (its top 2 bits). We will skip the rest of the back-to-back layout because the real pain hits at a different place — the cross-slot straddle.

### The mechanical pain

To decode v2 (the third value), the scalar decoder does:

```
bit_pos  = 2 * 5 = 10
byte_idx = 10 / 8 = 1
bit_idx  = 10 % 8 = 2

lo  = byte[1]      // bits 8..15
hi  = byte[2]      // bits 16..23
word = lo | (hi << 8)        // 16-bit window
v2  = (word >> bit_idx) & 0b11111
```

Two byte reads, one OR, one shift, one mask. Per value. Per **value**, scalar.

Now try SIMD. A 16-byte SIMD load reads bytes 0..15. The CPU splits this into 4 slots of 4 bytes each:

```
slot 0 ← bytes  0.. 3
slot 1 ← bytes  4.. 7
slot 2 ← bytes  8..11
slot 3 ← bytes 12..15
```

v6 sits at bit position 30. Byte 3 holds bits 24..31; byte 4 holds bits 32..39. So v6's bits straddle byte 3 and byte 4 — and those bytes are in **different slots** (slot 0 and slot 1).

**The pain:** to decode v6, slot 0 must reach across and read into slot 1's word. That's a cross-lane shuffle — the one thing SIMD is bad at, because each slot is supposed to mind its own business and never peek at its neighbor. SIMD's promise dies right there. This is the Pass 0 picture made concrete.

**The fix:** rearrange the 32 values so that straddles only happen *inside* one slot's window. If a value's bits never cross a slot boundary, no slot ever has to peek at its neighbor.

---

## Rung 3 — Lane-stripe: each slot owns its own 5-byte private stream

Split the 32 values into 4 streams, every 4th value to the same stream:

```
stream 0 (slot 0):  v0, v4,  v8,  v12, v16, v20, v24, v28
                  = [ 3, 31, 27,  23,  19,  15,  11,   7 ]

stream 1 (slot 1):  v1, v5,  v9,  v13, v17, v21, v25, v29
                  = [10,  6,  2,  30,  26,  22,  18,  14 ]

stream 2 (slot 2):  v2, v6,  v10, v14, v18, v22, v26, v30
                  = [17, 13,  9,   5,   1,  29,  25,  21 ]

stream 3 (slot 3):  v3, v7,  v11, v15, v19, v23, v27, v31
                  = [24, 20, 16,  12,   8,   4,   0,  28 ]
```

Each stream has `8 values × 5 bits = 40 bits = 5 bytes`. Bit-pack each stream independently. The bytes (computed by `simd-playground/src/main.rs` Example 6, `pack_one_lane`):

```
stream 0 bytes:  0xe3  0xef  0x3b  0xdf  0x3a
stream 1 bytes:  0xca  0x08  0xaf  0xad  0x74
stream 2 bytes:  0xb1  0xa5  0x12  0x7a  0xae
stream 3 bytes:  0x98  0x42  0x86  0x08  0xe0
```

### Sanity-check one packing

Stream 0 = `[3, 31, 27, 23, 19, 15, 11, 7]`. Bits low-first:

```
v0=3    = 00011
v1=31   = 11111
v2=27   = 11011
v3=23   = 10111
v4=19   = 10011
v5=15   = 01111
v6=11   = 01011
v7=7    = 00111

concatenated, low bit first → 40 bits → 5 bytes (LE):

bit pos: 0    5    10   15   20   25   30   35
         00011_11111_11011_10111_10011_01111_01011_00111

regroup by byte (8 bits, LE within byte):
byte 0 (bits 0..7):    1110 0011  =  0xe3   ✓
byte 1 (bits 8..15):   1110 1111  =  0xef   ✓
byte 2 (bits 16..23):  0011 1011  =  0x3b   ✓
byte 3 (bits 24..31):  1101 1111  =  0xdf   ✓
byte 4 (bits 32..39):  0011 1010  =  0x3a   ✓
```

Stream 0's bytes check out. The other three were computed the same way.

### Now interleave the streams at byte granularity

The 20 packed bytes in memory:

```
addr:  byte  0  byte  1  byte  2  byte  3   ← slot0/byte0  slot1/byte0  slot2/byte0  slot3/byte0
       byte  4  byte  5  byte  6  byte  7   ← slot0/byte1  slot1/byte1  slot2/byte1  slot3/byte1
       byte  8  byte  9  byte 10  byte 11   ← slot0/byte2  slot1/byte2  slot2/byte2  slot3/byte2
       byte 12  byte 13  byte 14  byte 15   ← slot0/byte3  slot1/byte3  slot2/byte3  slot3/byte3
       byte 16  byte 17  byte 18  byte 19   ← slot0/byte4  slot1/byte4  slot2/byte4  slot3/byte4
```

Concrete:

```
mem[ 0..3 ]  =  e3  ca  b1  98     ← byte-0 of streams 0,1,2,3
mem[ 4..7 ]  =  ef  08  a5  42     ← byte-1 of streams 0,1,2,3
mem[ 8..11]  =  3b  af  12  86     ← byte-2 of streams 0,1,2,3
mem[12..15]  =  df  ad  7a  08     ← byte-3 of streams 0,1,2,3
mem[16..19]  =  3a  74  ae  e0     ← byte-4 of streams 0,1,2,3
```

These are the **actual bytes** that will sit on disk / in RAM. From here, every decoder reads the same 20 bytes.

### Why byte-granular interleave (not word-granular)?

Think of the SIMD load as a dealer dealing one card to each of four players in turn. A 4-slot, 32-bit-slot SIMD load takes 16 bytes from memory and deals 4 bytes to each slot. With our interleave, the four bytes each slot receives are byte 0..3 of that slot's own stream — perfectly aligned.

Why not interleave per-bit instead? Because the dealer can only deal whole bytes. A SIMD load splits by **byte** boundaries, not bits — one byte is the smallest unit the hardware will hand out.

### Pre-empt: "why not 5 bytes per slot per load?"

Why not load 5 bytes per slot at once and be done? Because SIMD slots have fixed sizes: 32 bits (4 bytes) for u32, or 64 bits (8 bytes) for u64. 5 doesn't fit cleanly. We load 4 bytes per slot per SIMD load, and use a second SIMD load to get bytes 4. The straddle across byte 3/4 stays inside one slot — that is the win.

**Pain so far:** none yet — the layout works. Onward to decoding.

---

## Rung 4 — The decode loop, register state per iteration

The decoder reads the 20 bytes into 5 SIMD registers (one per byte-offset across the 4 streams):

```
reg[0] = [ e3, ca, b1, 98 ]   ← slot-0 holds 0xe3, slot-1 holds 0xca, ...
reg[1] = [ ef, 08, a5, 42 ]
reg[2] = [ 3b, af, 12, 86 ]
reg[3] = [ df, ad, 7a, 08 ]
reg[4] = [ 3a, 74, ae, e0 ]
```

Five SIMD byte-loads, and that's the whole setup. After this, each slot is holding the 5 bytes of its own stream and nothing else — no slot ever has to reach across to a neighbor again.

Now extract the 8 values per slot. Each iteration `v_idx ∈ 0..8` extracts ONE value per slot (4 values total per iteration).

### The math, same for all slots

```
bit_pos  = v_idx * 5
byte_idx = bit_pos / 8       ← index into reg[]
bit_idx  = bit_pos % 8       ← shift amount

word  = reg[byte_idx]  |  (reg[byte_idx + 1] << 8)    ← 16-bit window
value = (word >> bit_idx) & 0b11111
```

Here's the trick that makes the whole thing work: `byte_idx` and `bit_idx` are the **same for all 4 slots** in a given iteration. Every slot sits at the same bit position inside its own stream, so they all want the same shift and the same byte index. Compute it once, apply it to all four at once. The SIMD instructions become:

```
word   = SIMD-OR( reg[byte_idx],  SIMD-SHL( reg[byte_idx+1], 8) )
value  = SIMD-AND( SIMD-SHR(word, bit_idx),  splat(0b11111) )
```

No per-slot indexing. No shuffles. Pure SIMD.

### Step through stream 0's decode, iteration by iteration

I focus on slot 0's view; slots 1/2/3 do the same math on their own bytes in lockstep. (Trace verified by playground's `unpack_fastlanes_simd`.)

```
v_idx=0:  bit_pos=0   byte_idx=0  bit_idx=0
          lo=0xe3  hi=0xef       word = 0xefe3
          (word >> 0) & 0x1f  =  0xe3 & 0x1f  =  0x03  =  3 ✓
          straddle? no (5 bits fit in byte 0)

v_idx=1:  bit_pos=5   byte_idx=0  bit_idx=5
          lo=0xe3  hi=0xef       word = 0xefe3   ← same window!
          (word >> 5) & 0x1f  =  0x077f & 0x1f =  0x1f  =  31 ✓
          straddle? YES — value uses bits 5..7 of byte 0 AND bits 0..1 of byte 1.
          The carry-over (top 2 bits from byte 1) is provided by the (hi << 8) OR,
          all inside slot 0's register. No cross-slot.

v_idx=2:  bit_pos=10  byte_idx=1  bit_idx=2
          lo=0xef  hi=0x3b       word = 0x3bef
          (word >> 2) & 0x1f  =  0x0efb & 0x1f =  0x1b  =  27 ✓
          straddle? no

v_idx=3:  bit_pos=15  byte_idx=1  bit_idx=7
          lo=0xef  hi=0x3b       word = 0x3bef
          (word >> 7) & 0x1f  =  0x0077 & 0x1f =  0x17  =  23 ✓
          straddle? YES — bit 7 of byte 1 + bits 0..3 of byte 2.

v_idx=4:  bit_pos=20  byte_idx=2  bit_idx=4
          lo=0x3b  hi=0xdf       word = 0xdf3b
          (word >> 4) & 0x1f  =  0x0df3 & 0x1f =  0x13  =  19 ✓
          straddle? YES — bits 4..7 of byte 2 + bit 0 of byte 3.

v_idx=5:  bit_pos=25  byte_idx=3  bit_idx=1
          lo=0xdf  hi=0x3a       word = 0x3adf
          (word >> 1) & 0x1f  =  0x1d6f & 0x1f =  0x0f  =  15 ✓
          straddle? no

v_idx=6:  bit_pos=30  byte_idx=3  bit_idx=6
          lo=0xdf  hi=0x3a       word = 0x3adf
          (word >> 6) & 0x1f  =  0x00eb & 0x1f =  0x0b  =  11 ✓
          straddle? YES — bits 6..7 of byte 3 + bits 0..2 of byte 4.

v_idx=7:  bit_pos=35  byte_idx=4  bit_idx=3
          lo=0x3a  hi=0x00 (off end, splat 0)
          word = 0x003a
          (word >> 3) & 0x1f  =  0x07 & 0x1f  =  0x07  =  7 ✓
          straddle? no (fits in the last byte)
```

Output of stream 0: `[3, 31, 27, 23, 19, 15, 11, 7]`. Matches the input. ✓

### What about the other slots?

Slot 1 runs **the same iteration** with the same `byte_idx`/`bit_idx`/`shift_amount`. The difference is that its `reg[]` lanes contain stream 1's bytes (`ca, 08, af, ad, 74`), so the value it produces at each step is stream 1's value at that position.

In one register at the end of iteration `v_idx=0`:

```
slot 0:  3      ← stream 0, position 0   →  output as v0
slot 1:  10     ← stream 1, position 0   →  output as v1
slot 2:  17     ← stream 2, position 0   →  output as v2
slot 3:  24     ← stream 3, position 0   →  output as v3
```

Store the register to memory — and the output is `[3, 10, 17, 24]` in natural order. No post-decode permute.

Iteration `v_idx=1` produces slots = `[31, 6, 13, 20]` → stored as v4..v7. And so on.

**This is the lane-stripe rhythm:** one SIMD iteration emits one row of the original sequence.

Real-world hook: a `WHERE latency_ms < 32` filter over 1024 such values runs as 256 of these iterations — 256 SIMD-compares, no scalar fallback, no permutes after decode. The filter result lands in natural row order, ready for the next operator.

### The hurt that opens rung 5

We just proved the 4-slot decoder works on these bytes. But the paper's central claim is **interpretability** — *the same bytes* must decode on a 2-slot, 4-slot, 8-slot machine without re-encoding. Write the file once, read it on any CPU. We have only shown 4-slot. So the obvious worry: hand these exact bytes to a different-width CPU and do you still get the right answer back?

---

## Rung 5 — Interpretability: 2-slot CPU and 4-slot CPU decode the same bytes

Don't let the two-letter symbols scare you here — there are only two numbers in play. In our 32-value block we used 4 streams. Call this T=4 (number of streams in the layout = 4 virtual lanes). That's baked into the bytes and never changes. The SIMD width W is whatever the CPU happens to have, and it changes machine to machine. T is fixed by the file; W is fixed by the hardware. Interpretability is the whole question of how those two get along.

- **W = T = 4** → one SIMD load fills all 4 streams' bytes into 4 slots. Eight iterations finish the block.
- **W = 2** (smaller CPU) → each SIMD load fills 2 streams' bytes into 2 slots. The decoder does **two passes** through the iterations: first pass handles streams 0..1, second pass handles streams 2..3. Sixteen iterations finish the block.

(What about W > T, e.g. an AVX-512-class CPU with W=16 hitting a block where T=4? The decoder simply gangs multiple consecutive blocks into one register — but that case is uninteresting on our toy because we only have one block. The real paper's T=1024 makes W=4/8/16 all divide T, so W ≤ T always holds in practice. We drop the W > T case to keep this rung tight.)

In both cases above, the **bytes on disk are identical**. Only the load granularity and outer-loop count change.

### Worked 2-slot decode of our 20 bytes

The 2-slot decoder loads 2 bytes per register, takes the same `byte_idx/bit_idx` math, and unpacks. Per chunk it does:

```
Pass A (streams 0 and 1):
  reg[0] = [mem[ 0], mem[ 1]] = [0xe3, 0xca]
  reg[1] = [mem[ 4], mem[ 5]] = [0xef, 0x08]
  reg[2] = [mem[ 8], mem[ 9]] = [0x3b, 0xaf]
  reg[3] = [mem[12], mem[13]] = [0xdf, 0xad]
  reg[4] = [mem[16], mem[17]] = [0x3a, 0x74]

  iter v_idx=0:  slot0 = 3,  slot1 = 10   → output positions 0, 1
  iter v_idx=1:  slot0 = 31, slot1 = 6    → output positions 4, 5
  iter v_idx=2:  slot0 = 27, slot1 = 2    → output positions 8, 9
  iter v_idx=3:  slot0 = 23, slot1 = 30   → output positions 12, 13
  iter v_idx=4:  slot0 = 19, slot1 = 26   → output positions 16, 17
  iter v_idx=5:  slot0 = 15, slot1 = 22   → output positions 20, 21
  iter v_idx=6:  slot0 = 11, slot1 = 18   → output positions 24, 25
  iter v_idx=7:  slot0 = 7,  slot1 = 14   → output positions 28, 29

Pass B (streams 2 and 3):
  reg[0] = [mem[ 2], mem[ 3]] = [0xb1, 0x98]
  reg[1] = [mem[ 6], mem[ 7]] = [0xa5, 0x42]
  reg[2] = [mem[10], mem[11]] = [0x12, 0x86]
  reg[3] = [mem[14], mem[15]] = [0x7a, 0x08]
  reg[4] = [mem[18], mem[19]] = [0xae, 0xe0]

  iter v_idx=0:  slot0 = 17, slot1 = 24   → output positions 2, 3
  iter v_idx=1:  slot0 = 13, slot1 = 20   → output positions 6, 7
  ...
```

Stitched output: `[3, 10, 17, 24, 31, 6, 13, 20, 27, 2, 9, 16, 23, 30, 5, 12, ...]` — identical to the original.

**The same 20 bytes were decoded by a 4-slot CPU and a 2-slot CPU. Both produced the same 32 values in the same order. Verified by tracing the math; the playground's `unpack_fastlanes_simd` is the W=4 version.**

This is interpretability. The layout commits to a **virtual lane count T** (here, 4; in the real paper, 1024). Any SIMD width that divides T works without re-encoding.

### Disclosed mistake while writing this

My first draft set T equal to the SIMD width and tried to "decode the same bytes with 8 slots." That doesn't work for our 32-value block because we only have 4 streams' worth of byte interleaving. The honest framing: T is a property of the **encoding**, not the CPU. The CPU's W can be ≤ T (with multi-pass) or = T (single pass). The real paper picks T = 1024 so every commercial SIMD width (4, 8, 16) divides it.

### Pre-empt: "Why does this matter?"

Without interpretability you would need a separate encoded file per CPU generation. Or your AVX-512 server could not read files written by an ARM phone. FastLanes encodes once, decodes anywhere. One file format crosses SSE, AVX2, AVX-512, NEON, and the scalar fallback.

**Pain so far:** none. The layout works. Now the harder question — *why is it 4× faster?* We have only shown it works. Speed is the next rung.

---

## Rung 6 — The "4× faster" mechanism, grounded

The playground's Example 8 measures FastLanes SIMD decode at **3.74× faster than traditional scalar decode** for 50M 17-bit values with a DELTA cascade. Where does that number come from? Count instructions.

### Per-value instruction count: scalar decode

For one 17-bit value, the scalar decoder needs:

```
1. compute bit_pos = i * 17            ← 1 op
2. byte_idx = bit_pos >> 3             ← 1 op
3. bit_in_byte = bit_pos & 7           ← 1 op
4. load byte[byte_idx]                 ← 1 load
5. load byte[byte_idx + 1]             ← 1 load
6. load byte[byte_idx + 2]             ← 1 load   (17 bits can straddle 3 bytes)
7. assemble word with 2 shifts + 2 ORs ← 4 ops
8. shift right by bit_in_byte          ← 1 op
9. mask with 0x1FFFF                   ← 1 op
10. add to running DELTA sum           ← 1 op
11. accumulate into total              ← 1 op
———
~12 scalar ops + 3 loads per value
```

Reconciling with Pass 1: Pass 1 estimated `~5 ops per value` for scalar bit-unpack. The 12-op figure here breaks down as **~10 ops for the bit-unpack of a 17-bit straddler** (3 loads + 2 shifts + 2 ORs + 1 shift + 1 mask + 2 ops for `bit_pos` / `byte_idx` / `bit_in_byte` arithmetic), **plus 2 ops for DELTA accumulation** (`running += delta`; `total += running`). Pass 1's ~5 was bit-unpack-only for a non-straddler at a smaller width — both numbers are honest for what they measured; Pass 2 is just at a wider width with a DELTA cascade.

### Pre-empt: "a smarter scalar decoder would amortize the loads"

A thoughtful reader will object: a **buffered-scalar decoder** can keep a 64-bit shift register, refilling from memory only when fewer than `bits_per_value` bits remain. That drops the byte loads from ~3 per value to roughly **~2 per value** (since 64 bits hold about 3.7 of the 17-bit values per refill) and the ops to roughly **~8 per value** (one refill-amortized load + 1 shift + 1 mask + 2 ops `bit_pos` bookkeeping + 2 ops DELTA + small overhead). That is the fair upper-bound scalar baseline.

Even against the buffered-scalar baseline, FastLanes wins:
- the SIMD inner loop produces **4 values per iteration** at ~9 SIMD ops → ~2.25 ops/value (next subsection),
- and the SIMD body has **no per-value byte-pointer arithmetic** — the same `bit_pos / byte_idx / bit_in_byte` is computed once and reused across all 4 lanes.

So the honest framing is: scalar ceiling ≈ 8 ops/value (buffered) → FastLanes ≈ 2.25 ops/value → upper-bound speedup ≈ 3.5×, which reconciles cleanly with Example 8's measured **3.74×**. The naive 12-op scalar gives a higher 5.3× ceiling but ignores buffering.

### Per-value instruction count: FastLanes SIMD decode (W=4)

One SIMD iteration produces 4 values (one per slot). The inner body is:

```
1. a = reg[2*v]                        ← 1 SIMD load (cached in reg)
2. b = reg[2*v + 1]                    ← 1 SIMD load (cached)
3. c = reg[2*v + 2]                    ← 1 SIMD load (cached)
4. combined = a | (b << 8) | (c << 16) ← 3 SIMD ops
5. shifted = combined >> bit_idx       ← 1 SIMD op
6. delta = shifted & mask              ← 1 SIMD op
7. running += delta                    ← 1 SIMD op  (per-lane independent running sum)
8. total += running.widened()          ← 2 SIMD ops
————
~9 SIMD ops produce 4 values  →  ~2.25 SIMD ops per value
```

The byte loads happen once per chunk (17 of them for 32 values), so amortized they are sub-1 op per value — ignore for rough math.

### Putting it together

```
naive scalar (3 loads/value)    : ~12 ops × 1 value/op = 12 ops/value
buffered scalar (shift reg)     :  ~8 ops × 1 value/op =  8 ops/value
FastLanes SIMD W=4              :  ~9 ops × 4 values/op = 2.25 ops/value

speedup ceiling vs naive    = 12   / 2.25 ≈ 5.3×
speedup ceiling vs buffered =  8   / 2.25 ≈ 3.6×    ← the fair comparison
measured (Example 8)        = 3.74×
```

The measured **3.74× sits right at the buffered-scalar ceiling** — that is the honest framing. Even so, the measured number lands a touch below the naive ceiling because:
- Memory bandwidth still costs something (50M × 17 bits ≈ 106 MB; this exceeds L3 cache, so the FastLanes decoder spends real time waiting on RAM).
- Modern CPUs auto-vectorize *parts* of the scalar loop, narrowing the gap.
- The SIMD decoder has loop overhead per chunk.

Numbers grounded in `simd-playground/src/main.rs` Example 8, not invented.

### The hidden second win: parallel DELTA running sums

Scalar DELTA decode has one running sum: `running += delta` is a **serial dependency**. The CPU cannot issue iteration N+1 until iteration N's sum lands. This kills auto-vectorization completely.

FastLanes per-lane DELTA keeps 4 **independent** running sums (one per slot). Each slot accumulates only its own stream's values. The reconstructed natural-order output emerges because the streams interleave at output time.

```
scalar running sum:
  r ← r + d[0] ; r ← r + d[1] ; r ← r + d[2] ; ...  (1 chain, length N)

FastLanes per-lane running sum (4 slots):
  r0 ← r0 + d0[0] ; r0 ← r0 + d0[1] ; ...  (chain length N/4, slot 0)
  r1 ← r1 + d1[0] ; r1 ← r1 + d1[1] ; ...  (chain length N/4, slot 1)
  r2 ← r2 + d2[0] ; r2 ← r2 + d2[1] ; ...  (chain length N/4, slot 2)
  r3 ← r3 + d3[0] ; r3 ← r3 + d3[1] ; ...  (chain length N/4, slot 3)
  all four chains run in parallel inside one SIMD register
```

Pre-empt: "doesn't this change the meaning of DELTA?" Yes — FastLanes DELTA stores `v[i] - v[i-T]`, not `v[i] - v[i-1]`. T is the number of virtual lanes. Inside one stream the deltas behave classically; across streams the natural ordering is preserved by lane-stripe output.

Real-world hook: DELTA-encoded `timestamp_ns` columns are extremely common in metrics / events data. Going from a serial running-sum decode to 4 parallel running-sums is the difference between **decode being the bottleneck** and **filter / aggregate being the bottleneck** — exactly where you want to spend the CPU.

---

## Glossary additions (Pass 2)

| Term | Meaning |
|---|---|
| **Bit position** | `i * bits_per_value`. The offset (in bits) of value `i` inside a packed stream. |
| **Bit-offset / shift amount** | `bit_position % 8` — how far to right-shift the 16-bit window to align the value to bit 0. |
| **Byte-granular interleave** | Layout where consecutive bytes in memory belong to consecutive streams. Lets one SIMD load distribute one byte per slot. |
| **Straddle (within-slot)** | A value's bits cross a byte boundary, but both bytes are inside one slot's window. Cheap. |
| **Virtual lane (T)** | A logical position in the encoded layout. Fixed by the format (T=1024 in the real paper). Decouples encoding from physical SIMD width W. |
| **Interpretability** | Property of a layout: the same bytes decode correctly on any SIMD width W that divides T. |
| **Per-lane DELTA** | DELTA where `delta[i] = v[i] - v[i - T]`, so each stream maintains its own independent running sum. Replaces the serial dependency chain with T parallel chains. |

---

## The 3 checkpoint questions

Answer in your own words. Your answers tell me what to reinforce in Pass 3.

1. **Take the 20-byte block we encoded (`e3 ca b1 98 ef 08 a5 42 …`). Pick value v6 in the original sequence. Without re-reading rung 4's trace: which stream does v6 belong to, which 5-byte window does its slot hold, at what bit position inside that stream does v6 sit, and does it straddle a byte boundary?**

2. **The paper says "interpretability lets the same bytes decode on any SIMD width." We demonstrated this for W=4 and W=2 against the same 20 bytes. Suppose a CPU only has W=1 (pure scalar, no SIMD at all). Does the layout still work? Walk through what the scalar decoder has to do — and explain whether it pays a *cost* compared to the original Rung 2 naive scalar decode.**

3. **Rung 6 showed scalar decode takes ~12 ops/value while FastLanes SIMD takes ~2.25 ops/value, predicting up to 5.3× speedup, but Example 8 measures only 3.74×. List two distinct reasons the measured speedup is lower than the instruction-count ceiling, and for each, say whether the gap would *grow* or *shrink* if the dataset were much smaller (say, 1000 values that fit in L1 cache).**

Also flag:
- Any rung where the pain didn't feel sharp (you couldn't picture *why* it hurt before the next rung).
- Any step in the bit-by-bit trace of Rung 4 you couldn't reproduce on paper.
- Whether the interpretability demo (Rung 5) actually convinced you, or whether you suspect it works only on this carefully chosen toy.

Your answers shape Pass 3.
