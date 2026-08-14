---
name: log-lesson
description: Capture an instructive failure, fix, or design decision from the current or a recent session as a numbered entry in docs/lessons-learned/, for later use in postmortems or a presentation on building agentic systems. Use when the user asks to "log a lesson", "write this up", "remember this pitfall", "add this to lessons learned", or at the natural end of a debugging/implementation session where something genuinely instructive happened — a bug that survived earlier confidence, a design decision that turned out wrong or right once it met reality, an infrastructure truth no code review would have found.
---

Adds one entry to this project's running "lessons learned" record — real incidents, kept for a future presentation on building AI agents and what goes wrong. See `docs/lessons-learned/README.md` for the format and the reasoning behind keeping this as one-file-per-lesson rather than a single growing document.

A lesson entry is not a changelog line. The bar is: **would this be worth a slide?** A routine bug fix with no generalizable insight isn't a lesson. A thing that passed tests, passed review, and then broke in a way that reveals something true about building agentic systems generally — that's a lesson.

## Process

### 1. Identify the candidate

If the user pointed at something specific ("log this", referencing a bug or decision just discussed), that's the candidate — skip to step 2.

If invoked with no argument, review the current session (and recent git history if the session is short) for the strongest candidate: something that

- passed a check that should have caught it (tests, code review, a prior design assumption) and then failed against something real, or
- is a design decision that only proved right or wrong once it met real load, real infrastructure, or a real model, or
- is an infrastructure or platform truth that no amount of reading this codebase's own source would have surfaced.

If nothing in scope clears that bar, say so — don't manufacture a lesson to justify having been asked. Skipping is a valid outcome of this skill.

### 2. Check for redundancy

Read `docs/lessons-learned/README.md`'s index. If a very similar lesson already exists, don't duplicate it — either this is a new angle on the same underlying issue (fold it into the existing entry instead, note that to the user) or it genuinely isn't distinct enough to add.

### 3. Gather real specifics

A lesson is only as good as its evidence. Before drafting, pin down:

- The actual numbers (how long, how many, what confidence score, what did it cost) — read back through the conversation transcript or `git log`/diffs if they're not already at hand. Never round up a "some" into a specific-sounding number that wasn't actually observed.
- The actual file(s), function(s), or commit(s) involved.
- What the fix actually was, concretely — not "we improved error handling," but the specific mechanism.

If a number genuinely isn't recoverable, write the lesson without it rather than inventing one. A slightly less quantified true lesson beats a precisely-quantified invented one.

### 4. Draft the entry

Read one existing entry in `docs/lessons-learned/` first, to match its tone and structure exactly — that consistency is what makes the collection usable as presentation material later, rather than five different writing styles bolted together. Every entry has:

- A one-line title stating the lesson itself, not just the topic (`"Mocked tests can't catch what they don't imagine"`, not `"About testing"`).
- Header lines: **Theme**, **Where this bit us** (file/module), **Cost of finding it late** (concrete — time, money, a wrong conclusion reached).
- **What happened** — the concrete story, with the real numbers from step 3.
- **Root cause** — the actual mechanism, one or two paragraphs, no hand-waving.
- **The fix** — what changed, specific enough that a reader could find it in the diff.
- **The generalizable lesson** — the paragraph that has to survive outside this codebase. This is the part that goes on a slide. State the principle, then a short "if you're building an agent" callout applying it forward.

### 5. Confirm before writing

Show the full drafted entry to the user as text and ask directly whether to save it, edit it, or drop it. This step is not optional — a lesson is a judgment call about what's worth remembering and how to frame it, and this skill should never silently commit presentation material the user hasn't seen. Incorporate any requested edits and reconfirm if the changes are substantial.

### 6. Write, index, commit

On approval:

1. Find the next number: highest `NNN` in `docs/lessons-learned/*.md` (excluding `README.md`), plus one.
2. Write `docs/lessons-learned/NNN-kebab-case-slug.md` (slug drawn from the title, matching existing filename style).
3. Add one row to the index table in `docs/lessons-learned/README.md`. Don't restructure the "For the presentation" act grouping automatically — mention to the user that the new entry may want folding into that structure, and let them decide.
4. Commit with a message in the shape `docs: add lesson NNN — <short title>`, body explaining what happened and why it's worth keeping in one or two sentences. Follow whatever commit-message conventions the rest of the repo's history uses (see `git log`).

Scope discipline: this skill touches only `docs/lessons-learned/**` and one commit. It doesn't refactor code, doesn't open a PR, doesn't touch anything else — capturing the lesson is the whole job.
