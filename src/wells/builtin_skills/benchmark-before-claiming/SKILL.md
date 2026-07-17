---
name: benchmark-before-claiming
description: Never assert a performance improvement without measuring it — before/after numbers, not reasoning about what "should" be faster.
---

"This should be faster because X" is a hypothesis, not a result — algorithmic reasoning about complexity or I/O patterns is a fine STARTING point for a change, but it is not evidence the change actually helped, and it is wrong often enough (cache effects, JIT warmup, I/O being the real bottleneck instead of CPU, a "faster" data structure with worse constants at the actual data size) that a confident unverified claim is a real risk to the correctness of the final report.

When a task's goal (explicit or implicit) involves performance:

1. **Measure the baseline BEFORE changing anything** — run the actual workload (a benchmark script, the real test suite's timing, a representative production-shaped input) and record the number. Use run_code or run_command for this — don't estimate.
2. **Make the change.**
3. **Measure again, same method, same input size** — apples to apples. A benchmark comparing different input sizes or warm vs. cold cache states is not a valid comparison.
4. **Run more than once if the numbers are noisy** — a single sample can be dominated by scheduling jitter, especially for anything under ~1 second. Report a range or median of a few runs if the first two numbers disagree by more than a small margin.
5. **Report the actual before/after numbers in the summary**, not just "faster" — "340ms → 85ms" is verifiable; "significantly faster" is not.

If you cannot actually measure (no representative benchmark exists and building one is out of scope), say so explicitly rather than asserting an improvement — "expected to be faster due to X, not independently measured" is honest; a bare "improved performance" claim with no numbers is not.
