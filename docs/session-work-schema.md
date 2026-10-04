# Session work schema deployment

Revision `mm1a2b3c4d5e` adds `session_work_admission` and `session_work` after
`ll1a2b3c4d5e`. It does not change existing columns or backfill ownership.
The migration is in
`omnigent/db/migrations/versions/mm1a2b3c4d5e_add_session_work_admission.py`.

Land the migration, migration tests, and this document in a standalone schema
PR before the runtime admission implementation. ORM models, domain codecs,
and runner changes belong in the subsequent application PR. Follow the
[required database review](../CONTRIBUTING.md#database-migration-reviews).

## Deployment

Deploy the schema revision before enabling callers that write these tables.
Set `OMNIGENT_DB_URL` to the target SQLAlchemy database URL through the existing
secret configuration. From the repository root, the shared database uses its
existing Alembic history:

```sh
uv run --no-sync alembic -c omnigent/db/alembic.ini upgrade mm1a2b3c4d5e
```

A nonzero exit means the upgrade failed.

Split deployments store ownership in the Agent Platform conversation database.
The metadata database keeps its normal Alembic history. Its copies of these
tables are unused by the ownership store. The AP database currently uses
`ConversationBase.metadata.create_all()` without an Alembic version table.
Do not run or stamp the metadata migration history onto a split AP database.
The later application ORM models create missing AP tables through this managed
initialization path. The schema-only commit contains no new ORM models, so it
cannot independently deploy these tables to a split AP database through the
current managed interface. A separately reviewed AP deployment interface is
required before claiming independent schema deployment for split databases.

The isolated AP migration test exercises the additive DDL through Alembic's
operations context. It is a test fixture, not a supported operator deployment
command. SQLite upgrades and PostgreSQL DDL generation are covered; PostgreSQL
and CockroachDB runtime migration execution remain unverified.

## Compatibility and rollback

The tables use workspace and binary UUID identities. Work states are pending
`1`, claimed `2`, uncertain `3`, settled `4`, and aborted `5`; shell kind is `1`.
Claimed, uncertain, and settled rows require a claim UUID. Pending and aborted
rows must not have one. The unfinished-work index includes the complete primary
key. There are no foreign keys, server defaults, or expiry timestamps.

Adding empty tables does not establish that existing work is idle. The runtime
implementation must enroll actors and retain unresolved ownership before a
switch can rely on these records. This schema change alone does not qualify
agent switching or authorize a rollout of partially enrolled behavior.

Retain the schema revision when rolling back application changes. Existing
startup checks reject a database revision newer than the application's known
migration head, so an older binary that lacks this migration is not a compatible
rollback target. After ownership has been recorded, a rollback must also retain
the admission fence and unresolved rows. Do not disable the fence or drop the
tables to restore availability. The migration's downgrade drops both tables;
it is appropriate only for disposable test databases before ownership is used.
Destructive production cleanup requires a separate reviewed migration after
all durable ownership has been reconciled.

## Verification

```sh
uv run --no-sync pytest -q tests/db/test_migration_session_work.py
```

Expect `4 passed`. A nonzero exit or a missing passing summary fails this check.

Confirm that an upgrade from `ll1a2b3c4d5e` preserves an inserted claimed row
when the upgrade is repeated, and that the isolated AP DDL fixture adds only the
two ownership tables without creating metadata tables or an Alembic history.
The AP fixture does not close the independent deployment gap above.
