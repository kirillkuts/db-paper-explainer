# db-paper-explainer

A pair of Claude Code sub-agents that turn dense technical material (database papers, systems specs, protocol RFCs, anything mechanism-heavy) into a **progressive ladder of short learning passes**. Each pass starts from the simplest thing that fails and climbs one rung at a time until the real design is justified.

The agents are topic-agnostic. The name *db-paper-explainer* is just the directory this workflow was first used in.

---

## What's in here

```
.claude/agents/
├── pass-builder.md     # Writes pass-NN-<topic>.md files
└── pass-reviewer.md    # Adversarial read-only reviewer, outputs strict JSON
```

Two sub-agents that play complementary roles in a builder ↔ reviewer loop:

| Agent | Role | Side effects | Output |
|---|---|---|---|
| `pass-builder` | Drafts or revises a single learning pass | Writes one `pass-NN-<topic>.md` | The file + a 3-line summary |
| `pass-reviewer` | Critiques a pass from a probing-learner perspective | None (read-only) | Strict JSON feedback |

The reviewer is deliberately read-only, deliberately adversarial, and deliberately JSON-only so an orchestrator can parse its output and decide whether to loop or ship.

---

## The teaching model these agents enforce

Both agents are opinionated about *how* to teach a hard topic:

- **Evolution ladder.** Each section is the simplest design that fails. The next section exists to fix that pain. No magic.
- **Justify every number.** No unexplained bit widths, sizes, or ratios.
- **Pre-empt obvious alternatives.** Name the simpler thing the reader would propose, and explain why the real design rejects it.
- **Toy numbers first.** Build intuition on small examples before scaling.
- **Visual primary.** ASCII tables and bit layouts lead; prose explains them.
- **Short sentences, simple vocab.** Optimized for re-readability, not compactness.
- **Real-world hook per section.** Every section ends on a concrete workload or production system.
- **3 checkpoint questions** at the end of every pass.

The reviewer encodes the same rules as nine probes (`unexplained_number`, `missing_rung`, `unaddressed_alternative`, etc.) and produces severity-tagged violations.

---

## How to use

### 1. Drop the agents into your project

```bash
mkdir -p your-project/.claude/agents
cp .claude/agents/pass-builder.md  your-project/.claude/agents/
cp .claude/agents/pass-reviewer.md your-project/.claude/agents/
```

Claude Code picks them up automatically from `.claude/agents/`.

### 2. Write a `CLAUDE.md` for your project

Both agents **require** a `CLAUDE.md` at the project root. Without it they refuse to run. It needs to define:

- **Topic** — what you're learning (e.g. "the FAST paper on cache-conscious search trees")
- **Source material** — paper PDF, RFC link, spec section
- **Learner profile** — your background; what you can skip, what you need spelled out
- **Teaching rules** — any project-specific overrides on top of the load-bearing rules baked into the agents

A minimal example:

```markdown
# Topic
The Roaring Bitmap paper (Lemire et al., 2016).

# Source material
- ./papers/roaring.pdf

# Learner profile
- Senior backend engineer, ~10 years.
- Comfortable with bit manipulation, weaker on cache-line / SIMD reasoning.
- Prefers re-readable prose; flag every unexplained number.

# Teaching rules
- Max 1 pass per "container type" (array, bitmap, run).
- Always show toy 8-bit examples before 16-bit production sizes.
```

### 3. Invoke the loop

In Claude Code, ask the orchestrator to run the loop. A simple pattern:

> "Use the pass-builder agent to write `pass-01-roaring-array-container.md`. Then run pass-reviewer on it. If verdict is `revise`, feed the JSON back to pass-builder and rerun. Cap at 3 rounds."

Both agents respect a **3-round revision cap** by contract — on round 3 the reviewer will return `verdict: "reject"` rather than `"revise"`, so the orchestrator stops and escalates to you.

### 4. File naming convention

The builder always writes:

```
pass-<NN>-<kebab-topic-slug>.md
```

with **zero-padded** numbers (`pass-01`, `pass-02`, …, `pass-10`) so alphabetical and numeric ordering agree. The reviewer uses the same convention to find the previous pass for vocabulary-continuity checks.

---

## Reviewer JSON schema (cheat sheet)

```json
{
  "file_reviewed": "pass-02-bitmap-container.md",
  "verdict": "ship_it" | "revise" | "reject" | "blocked",
  "violation_count": 0,
  "strengths": ["..."],
  "violations": [
    {
      "probe": "unexplained_number",
      "severity": "blocking" | "nit",
      "section": "...",
      "line_range": "lines 45-47",
      "quote": "...",
      "issue": "...",
      "suggestion": "..."
    }
  ],
  "learner_would_ask": [
    { "question": "...", "defer_to_next_pass": true }
  ],
  "prior_feedback_recheck": [
    { "previous_issue": "...", "status": "resolved" | "still_violating", "note": "..." }
  ]
}
```

`prior_feedback_recheck` is only emitted on revision rounds, when the orchestrator passes prior reviewer JSON back in.

---

## Why a loop and not a single pass?

A first draft will almost always violate at least one of the probes — usually unjustified numbers or a missing rung. The reviewer catches them mechanically; the builder fixes them with surgical context (the prior JSON), without re-deriving the whole pass. Three rounds is enough in practice; if it isn't, the topic needs a human re-scope, not more agent spins.

---

## Design notes

- The reviewer's `tools` are restricted to `Read, Glob, Grep` — it can't accidentally write.
- The builder's `tools` are restricted to `Read, Write, Edit, Glob` — no shell.
- Neither agent calls the network or any MCP server.
- The probe list is closed and slug-stable so downstream tooling (dashboards, metrics) can group by probe over time.

---

## License

MIT.
