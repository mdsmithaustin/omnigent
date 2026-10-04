# Deploy the session work schema

Deploy revision `mm1a2b3c4d5e` before enabling callers that write session work.
It follows `ll1a2b3c4d5e` and adds `session_work_admission` and `session_work`.
It changes no existing columns and backfills no ownership rows.

Keep this schema change in a standalone PR before the application implementation.
Follow the [required database review](../CONTRIBUTING.md#database-migration-reviews).
Ownership ORM models, codecs, and runner behavior belong in the subsequent PR.

## Select the database role

Use the schema artifact whose exact Alembic head is `mm1a2b3c4d5e`.
The deployment command rejects a different artifact head before changing the database.
Supply the database URI through your existing secret configuration.
`AP_DEPLOY_URI` and `SHARED_DEPLOY_URI` below are names you choose for that secret.
They are not server configuration variables.

For a split deployment, target the Agent Platform conversation database.
From the repository root, run:

```sh
uv run --no-sync python scripts/deploy_session_work_schema.py --role split-ap --target mm1a2b3c4d5e --uri-env AP_DEPLOY_URI
```

A nonzero exit means deployment or verification failed.
The split AP operation creates only the two ownership tables and their required index.
It preserves existing AP tables and rows and creates no metadata history.
It rejects a target containing `alembic_version` or the shared CRDB bootstrap marker.
An empty AP target receives only the ownership tables. The existing AP initializer
remains responsible for conversation tables and search objects.

For a shared database, run:

```sh
uv run --no-sync python scripts/deploy_session_work_schema.py --role shared --target mm1a2b3c4d5e --uri-env SHARED_DEPLOY_URI
```

A nonzero exit means deployment or verification failed.
The shared operation uses the existing managed initializer and metadata Alembic history.
Existing supported `ll` databases upgrade through the migration.
Fresh CRDB databases use the existing bootstrap owner, which creates and verifies
ownership tables before stamping the resolved revision.
Do not replay the historical PostgreSQL migration chain against empty CRDB.

In split mode, deploy the metadata database through its normal shared history too.
Its copies of the ownership tables are unused by the ownership store.
Do not run or stamp the metadata lineage onto the AP database.

A successful command prints one JSON object. For split SQLite, the result is:

```json
{"role":"split-ap","target":"mm1a2b3c4d5e","backend":"sqlite","verified_objects":["session_work_admission","session_work","ck_session_work_claim","ix_session_work_unfinished"]}
```

Exit zero means the committed shape passed inspection through a new connection.
The result does not certify actor enrollment or an admission fence.
Failures print a bounded error without the URI or raw driver diagnostics.

Python deployment callers can use the same managed operation:

```python
from omnigent.db.utils import deploy_session_work_schema

result = deploy_session_work_schema(
	secret_config.conversation_database_uri,
	role="split-ap",
	target="mm1a2b3c4d5e",
)
```

The operation owns and disposes an uncached engine on success and failure.
It retains the managed engine's pooling, token-refresh, and CRDB transaction preparation.

## Retry an interrupted deployment

Use one deployment writer per target. After an interruption, rerun the same command.
The operation inspects both ownership tables before adding missing tables or the index.
It preserves existing admission, reservation, incarnation, and claim values.
An accidental concurrent deployment can fail on a DDL conflict. Retry after the other
writer finishes.

An incompatible column, key, check, default, foreign key, expiry, restrictive unique
constraint, or required index causes refusal. The operation does not replace tables,
reset rows, or rewrite constraints. Keep the existing data and inspect the reported
object with the database owner before changing an incompatible schema.

CRDB can publish DDL before a later failure. A retained shared bootstrap marker must
match both its token and the artifact target before recovery or cleanup.
An index added to an existing CRDB table becomes visible after commit. Shared upgrades
verify the committed ownership schema before Alembic records the new revision.
The initializer also repairs compatible missing ownership objects on an existing `mm`
database. It refuses incompatible objects and retains the marker on failure.
Split AP deployment uses no bootstrap marker.

## Preserve the schema contract

The immutable Core definitions live in
`omnigent/db/migrations/schema/mm1a2b3c4d5e.py`.
Online Alembic upgrades and the AP operator use the same applicator and verifier.
Offline SQL emits the same definitions without reflection.
Add a new immutable definition and migration for future changes. Do not edit this
revision to follow current ORM metadata or use its verifier for an altered later schema.

The unfinished-work index contains `(workspace_id, session_id, state, operation_id)`.
Work states are pending `1`, claimed `2`, uncertain `3`, settled `4`, and aborted `5`.
Shell kind is `1`. Claimed, uncertain, and settled rows require a claim.
Pending and aborted rows require no claim.
There are no foreign keys, server defaults, or expiry timestamps.

UUID columns use binary storage. SQLite BLOB and PostgreSQL BYTEA do not enforce a
16-byte value. The later application write boundary must enforce exactly 16 bytes,
the runner identifier contract, and a byte limit below 16,384 for every new value.
Declared character lengths alone do not prove stored-byte limits on every engine.
Schema deployment does not qualify those future writers.

## Verify the artifact and database

Run the migration and deployment tests from the repository root:

```sh
uv run --no-sync pytest -q tests/db/test_migration_session_work.py tests/db/test_session_work_deployment.py tests/db/test_session_work_catalog.py
```

A nonzero exit, zero collected cases, or an absent passing summary fails this check.
Without `OMNIGENT_TEST_DB_URI`, the suite uses fresh SQLite files.
CRDB-only and MySQL-only cases explicitly skip. Those skips are missing server coverage.
Recorded catalog tests exercise verification logic without connecting to a database.

For server qualification, supply `OMNIGENT_TEST_DB_URI` through test secrets and run
the same command separately against each required backend.
The configured user must be able to create and drop disposable test databases.
The new fixture creates a unique, unmigrated database per case. It does not reuse the
ordinary already migrated `db_uri` fixture.
For CRDB, also set `OMNIGENT_TEST_CRDB_VERSION` to the exact expected server version.
A backend mismatch, skipped required case, or missing version result fails qualification.

The local implementation checks cover SQLite deployment, repeat, drift refusal,
process interruption, and shared upgrades. Offline DDL covers SQLite, PostgreSQL,
MySQL, and CRDB. Raw PostgreSQL 16.15, MySQL 8.0.46, and CRDB 23.2.28 catalogs informed
verification. The MySQL operator accepts the native forms captured with MySQL 8.0.46
and the locked PyMySQL 1.2.0 driver. It requires a visible, ascending BTREE index with
complete column keys and an enforced claim check. Native MySQL smoke checks cover
independent AP deployment, preserved AP and ownership rows, repeat deployment, and
refusal of invisible indexes, prefix indexes, and disabled checks.

Catalog fixtures and smoke checks do not establish full backend coverage. Qualify
shared upgrades, fault recovery, query plans, and row preservation with the full suite
on each intended server and driver combination. Other MySQL versions and drivers
remain unqualified. The CRDB matrix is 23.2.28, 24.3.20, 25.2.10, and 25.4.5.

For a manual check on a disposable AP database, keep a conversation, item, and label
with known values. Run the split command, insert a known claimed ownership record,
and run the command again in a new process. Confirm both results report the expected
objects, every recorded value is unchanged, and no `alembic_version` or shared marker
exists. Inspect the unfinished index's complete ordered key through the database catalog.

## Roll back the application while retaining ownership

Adding empty tables does not establish that existing work is idle.
Enroll actors and retain unresolved ownership before switching behavior.
The schema operation provides no atomic enrollment across split databases.

Retain the schema and every admission and ownership row when rolling back the application.
The `.1` shared binary rejects the unfamiliar `mm` revision, so use a schema-aware
application artifact that retains the required admission fence.
Do not disable the fence or drop the tables to restore availability.
The migration downgrade drops both tables and is only for disposable test databases
before ownership is used. Production cleanup needs a separate reviewed migration after
all durable ownership has been reconciled.
