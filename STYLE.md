# Workshop prose style — the MacLeod voice

The target voice for workshop `.md` prose, use it for the reader-facing prose in `XYz-*.md` files.

**This governs voice, not scope or structure.** `AUTHORING.md` still rules:
hard rule 2 (no Rust implementations — signatures and constants only) is
*non-negotiable* and overrides MacLeod's "show the code failing, fix it live"
habit. The complexity rubric still sets *where* density goes. This file sets
*how the prose sounds* once those decisions are made.

---

## The seven moves

1. **Analogy first, mechanism second.** Before the precise definition, give one
   homely analogy that carries the intuition — then define, then loop back to
   the analogy to confirm. (MacLeod: pointer = table of contents; stack = stack
   of dishes; reference-to-reference = "a friend of a friend.") The mechanism
   lands because the picture is already in place.

2. **Self-answering questions drive the pacing.** Ask the reader's question for
   them, then answer it. "Why two passes? Because checking every row is
   expensive." This is the engine — question, answer, question, answer. It also
   pre-empts the objection the careful reader is already forming ("Wait — it
   throws away the flag?").

3. **Short sentences punch.** A long explanatory sentence is followed by a tiny
   one for emphasis. "That's a wasted read." / "Over-include, never
   under-include." / "The answer is still correct." Break the rhythm to land
   the point.

4. **Lower anxiety on the hard parts.** Name the difficulty honestly, then
   reframe it as the machine doing your thinking. Tell the reader what *not* to
   worry about ("the diagnostic goes to stderr; don't compare it"). Don't
   pretend a hard thing is easy — walk them to why it's manageable.

5. **Concrete over abstract, always.** Real numbers, real keys, real output.
   "Granule 0 holds keys `0..=99`" beats "a granule holds a range of keys." Show
   the actual `k > 99` case, not "an edge case." If you wrote an abstract noun
   phrase, replace it with the instance.

6. **Gloss every term in plain words the first time.** MacLeod's "this chapter
   covers" bullets are always *term + everyday meaning*: "Type inference (how
   Rust knows the type)." Never a bare jargon term on first use.

7. **Walk the reader to the rule; don't just assert it.** When a claim matters
   (the over-include guarantee, why inclusivity is a flag), show the reasoning
   step by step so they arrive at it — "it's worth seeing exactly why" — rather
   than stating it and moving on.

## Voice checklist

- Second person, contractions, direct address. One reader, not an audience.
- Conversational asides are allowed ("Annoying, but…", "Here's the thing").
- It should read like a patient friend at a whiteboard, not a reference manual.

## What he does that we DON'T (constraint reconciliation)

- **He writes full working code and debugs it live on the page. We don't** —
  hard rule 2. Adopt his *voice* around signatures, specs, and diagrams; never
  his habit of handing over the implementation.
- He's exhaustive and gentle-paced for absolute beginners. Our reader has CMU
  15-445 + real eng background (`GOALS.md`) — keep the voice, cut the
  hand-holding on fundamentals they already own. Reassure on *our* hard parts
  (CH internals, Rust idioms), not on what a loop is.

## Where the voice goes vs. where it doesn't

Per the 03b feedback (`03b-feedback.txt`): density and full voice belong in the
**body** of a section. **Headlines, learning objectives, and overviews stay
skimmable** — name the topic in one line; the body earns it. Don't pour
analogy-and-punch prose into a bullet list that's meant to be scanned.

## Calibration reference

The reworked "Two levels: coarse prune, then exact filter" section (03b) is the
worked example of this voice applied to a real workshop concept. When unsure how
hard to turn a dial, compare against it.
