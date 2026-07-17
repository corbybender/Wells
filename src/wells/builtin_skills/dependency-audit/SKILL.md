---
name: dependency-audit
description: How to check a repo's dependencies for known vulnerabilities and outdated versions across pip/npm/cargo/go — the actual commands, not guessing.
---

Checking dependencies for known issues is verbose and tool-specific — here's the actual command per ecosystem rather than reasoning about it from scratch each time.

**Python (pip/uv/poetry):**
- `pip list --outdated` (or `uv pip list --outdated`) for stale packages.
- `pip-audit` (if installed) checks installed packages against the PyPA Advisory Database — the standard tool. Run `pip-audit` with no args against the active environment.
- No pip-audit installed? `python3 -m pip install pip-audit --break-system-packages` (or via a venv) if the task genuinely requires this check — otherwise note that the tool isn't available rather than skipping the check silently.

**Node (npm/yarn/pnpm):**
- `npm audit` (or `yarn audit` / `pnpm audit`) — built into the package manager, no install needed. `npm outdated` for staleness separately from vulnerabilities.

**Rust (cargo):**
- `cargo audit` (requires `cargo install cargo-audit` if not already present) checks Cargo.lock against the RustSec Advisory Database.
- `cargo outdated` (separate tool) for version staleness.

**Go:**
- `govulncheck ./...` (official Go vulnerability scanner) if installed; `go list -u -m all` for outdated modules.

Always report FINDINGS, don't silently fix every flagged dependency — a vulnerability fix can be a breaking change; surface what was found and let the task's actual scope decide whether upgrading is appropriate right now.
