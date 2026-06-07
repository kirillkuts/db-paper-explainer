# db-paper-explainer

A workflow for turning dense technical material (database papers, systems specs,
protocol RFCs — anything mechanism-heavy) into a **progressive ladder of short
learning passes**. Each pass starts from the simplest design that fails and
climbs one rung at a time until the real design is justified.

See `README.md` for the full design rationale and the reviewer JSON schema.

## Layout

```
.claude/agents/
├── pass-builder.md     # Writes pass-NN-<topic>.md files
└── pass-reviewer.md    # Adversarial read-only reviewer, outputs strict JSON

<topic>/                # One directory per paper (fastlanes/, aws-aurora/, …)
├── CLAUDE.md           # Per-topic config: Topic, Source, Learner profile, Rules
├── pass-0-map.md       # Overview / ladder map
└── pass-N-<slug>.md    # The progressive learning passes

pdf-to-epub/            # Utility: convert a paper PDF to EPUB for reading
STYLE.md                # The prose voice all pass .md files must follow
README.md               # How the builder ↔ reviewer loop works
```

## The two agents

- **`pass-builder`** — drafts or revises a single `pass-NN-<topic>.md`. Tools:
  `Read, Write, Edit, Glob` (no shell, no network). Refuses to run without a
  topic-level `CLAUDE.md`.
- **`pass-reviewer`** — critiques a pass from a probing-learner perspective.
  Read-only (`Read, Glob, Grep`), outputs strict JSON. Encodes the teaching
  rules as nine probes (`unexplained_number`, `missing_rung`, …).

They run as a loop: build → review → feed JSON back → rebuild, capped at 3
rounds (round 3 returns `reject` to force a human re-scope).

## Load-bearing teaching rules

Both agents enforce these; don't violate them when writing or editing passes:

- **Evolution ladder** — each section is the simplest design that fails; the
  next section exists to fix that pain. No magic.
- **Justify every number** — no unexplained bit widths, sizes, or ratios.
- **Pre-empt obvious alternatives** — name the simpler thing the reader would
  propose, explain why the real design rejects it.
- **Toy numbers first**, then scale to production sizes.
- **Visual primary** — ASCII tables and bit layouts lead; prose explains them.
- **Short sentences, simple vocab** — optimized for re-readability.
- **Real-world hook per section** — end on a concrete workload or system.
- **3 checkpoint questions** at the end of every pass.

## Prose voice

The reader-facing prose in every `pass-*.md` must follow **`STYLE.md`** — the
"MacLeod voice" (analogy first, self-answering questions, short punchy
sentences, gloss every term, walk the reader to the rule). `STYLE.md` governs
*how the prose sounds*; the teaching rules above govern *what goes where*.
Headlines, objectives, and overviews stay skimmable — full voice belongs in the
body of a section.

## Conventions

- Pass files: `pass-<N>-<kebab-slug>.md`. Newer topics zero-pad
  (`pass-01`) so alphabetical and numeric ordering agree.
- Each topic directory is self-contained with its own `CLAUDE.md`.
