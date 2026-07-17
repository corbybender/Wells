---
name: project-recon
description: Discover a repo's REAL test/build/lint commands before running anything — CI config is ground truth, guessing wastes rounds.
---

Before running any test, build, or lint command, spend ONE round establishing ground truth instead of guessing — a wrong guess costs a full failed round-trip, and most repos tell you exactly what to run if you look in the right place.

Check in this order, stop at the first hit:

1. **CI workflow files** — `.github/workflows/*.yml`, `.gitlab-ci.yml`, `.circleci/config.yml`, `azure-pipelines.yml`. These are GROUND TRUTH: the maintainers' own automation runs these exact commands. grep for `run:` steps.
2. **Package manifest scripts** — `package.json` "scripts" block (npm/yarn/pnpm), `pyproject.toml` ([tool.pytest.ini_options], [project.scripts]), a `Makefile` target, `Cargo.toml` (cargo test/build are usually plain unless a workspace has a custom xtask), `go.mod` (go test ./...).
3. **Makefile / justfile / Taskfile** — `make test`, `make lint`, `make build` targets, or `justfile`/`Taskfile.yml` equivalents.
4. **README.md** — often has a "Development" or "Running tests" section.
5. **Lockfile presence** tells you the package manager: `package-lock.json`→npm, `yarn.lock`→yarn, `pnpm-lock.yaml`→pnpm, `uv.lock`→uv, `poetry.lock`→poetry, `Cargo.lock`→cargo, `go.sum`→go modules.

Only fall back to a bare framework-default guess (`pytest`, `npm test`, `go test ./...`) if none of the above yields an answer — and even then, run it once to confirm before relying on it for verification later in the task.

Record what you find in your own working notes so you don't re-derive it if you need to run tests again later in the same run.
