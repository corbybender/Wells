---
name: incremental-refactor
description: Large refactors (3+ files) should land as a sequence of small, test-passing steps — not one giant rewrite that's all-or-nothing to review or debug.
---

A refactor touching many files is much safer done as a sequence of small, independently-verifiable steps than as one giant rewrite. This isn't just good practice — it directly avoids the failure mode where a large, ungrounded rewrite drifts off the original goal partway through and produces something that compiles but does something different from what was asked.

When a task requires touching 3+ files or fundamentally restructuring how something works:

1. **Write the sequence of steps FIRST**, each one independently completable and verifiable (a test suite that still passes, a specific behavior that still works) before starting the next. Don't start editing until you know the whole sequence.
2. **Keep the public interface stable until the last step** where possible — rename/move internals first, change call sites last, so intermediate states are still runnable.
3. **Run tests (or at least re-read the affected code) after EACH step**, not just at the end. A regression caught immediately after the step that introduced it is a one-line fix; the same regression caught after 6 more steps requires bisecting through everything since.
4. **If a step reveals the plan was wrong**, stop and re-plan rather than pushing forward — cascading fixes on top of a bad step compound the problem instead of resolving it.
5. **Never rewrite a file's full content to make one targeted change** — use edit_file for a scoped diff, not write_file for a full replacement, so the actual size of each change stays visible and reviewable.

The goal: at every intermediate point, you could stop and hand off a working (if incomplete) result — not "will work once every step is done."
