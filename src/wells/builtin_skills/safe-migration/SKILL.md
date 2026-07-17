---
name: safe-migration
description: Database schema migration checklist — backward compatible, reversible, tested — for any task that adds or changes a migration file.
---

A schema migration is one of the few changes where "it ran without error" is not sufficient evidence of correctness — a bad migration can lose data or take down a service on deploy, and by the time it's caught in production, rolling back is often harder than the original migration was to write.

Before writing or modifying a migration:

1. **Additive first**: prefer adding a new nullable column / new table over altering or dropping an existing one. A column that's `NOT NULL` with no default breaks every existing row and any in-flight write during deploy — use a default or backfill in a SEPARATE step from adding the constraint.
2. **Never combine a schema change with a data backfill in one migration** for a large table — the backfill can lock the table or time out; large data changes belong in a separate, resumable script, not the DDL migration itself.
3. **Write the down/rollback migration too**, and actually verify it's the true inverse — a migration framework that "supports" rollback doesn't guarantee the down-migration is correct unless someone wrote it deliberately.
4. **Check for the same column/table name already used elsewhere** — renamed-but-not-removed legacy fields are a common footgun; grep the codebase, not just the current schema file, before reusing a name.
5. **Verify against production-shaped data volume if at all possible** — an empty test database will not surface a lock timeout or a slow backfill that only shows up at real row counts.
6. **State the deploy order explicitly if code and migration must ship together**: does the migration need to run BEFORE the new code deploys (new code expects the new column) or can it run after (old code still works against the new schema)? Get this backwards and you get a window of errors during rollout.

Report your migration's compatibility class explicitly (additive-safe / requires-coordinated-deploy / destructive) rather than leaving it implicit — this is exactly what a reviewer needs to know, and a silent migration file doesn't communicate it.
