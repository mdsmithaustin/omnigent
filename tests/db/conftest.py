"""Serialize CockroachDB schema-change DDL across xdist workers.

The migration tests here run Alembic upgrades/downgrades, and each schema
change commits under SERIALIZABLE isolation. CockroachDB's descriptor and
system tables are cluster-wide — shared even across the per-worker test
databases (``omnigent_test_w0``, ``omnigent_test_w1``, …) — so DDL from
parallel workers contends on them and CockroachDB aborts a transaction with
``RETRY_SERIALIZABLE``. The tests invoke migrations directly, bypassing the
product's serialization-retry path, so a losing worker surfaces the abort as
a hard failure — a different migration test each run.

Serialize the DDL-heavy tests behind a cross-process lock so only one worker
mutates the shared schema catalog at a time. Scoped to CockroachDB (other
dialects keep per-database isolation) and to the migration modules (the small
non-DDL ``tests/db`` unit tests stay fully parallel).

The ``test_crdb_migration`` modules leave half-migrated schemas behind, so on
CockroachDB each of their tests gets a fresh database, also under the lock.
"""

from __future__ import annotations

import tempfile
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

# One lock file per runner; all workers on the host contend on it.
_CRDB_SCHEMA_LOCK = Path(tempfile.gettempdir()) / "omnigent-crdb-schema-ddl.lock"


def _runs_schema_ddl(node: pytest.Item) -> bool:
    """Whether a test issues migration DDL (the modules that hit the catalog)."""
    name = node.path.name
    return name.startswith("test_migration") or name == "test_cockroachdb.py"


@pytest.fixture(autouse=True)
def _serialize_crdb_schema_ddl(
    request: pytest.FixtureRequest, _worker_db_uri: str
) -> Iterator[None]:
    if "cockroachdb" not in _worker_db_uri or not _runs_schema_ddl(request.node):
        yield
        return
    from filelock import FileLock

    with FileLock(str(_CRDB_SCHEMA_LOCK)):
        yield


@pytest.fixture()
def _worker_db_uri(request: pytest.FixtureRequest, _worker_db_uri: str) -> Iterator[str]:
    if "cockroachdb" not in _worker_db_uri or not request.node.path.name.startswith(
        "test_crdb_migration"
    ):
        yield _worker_db_uri
        return
    import sqlalchemy as sa
    from filelock import FileLock

    from omnigent.db.utils import _engine_cache, _engine_lock

    name = f"omnigent_schema_{uuid.uuid4().hex}"
    uri = sa.make_url(_worker_db_uri).set(database=name).render_as_string(hide_password=False)
    with FileLock(str(_CRDB_SCHEMA_LOCK)):
        root = sa.create_engine(_worker_db_uri, isolation_level="AUTOCOMMIT")
        try:
            with root.connect() as connection:
                connection.execute(sa.text(f'CREATE DATABASE "{name}"'))
            try:
                yield uri
            finally:
                with _engine_lock:
                    engine = _engine_cache.pop(uri, None)
                if engine is not None:
                    engine.dispose()
                with root.connect() as connection:
                    connection.execute(sa.text(f'DROP DATABASE "{name}" CASCADE'))
        finally:
            root.dispose()
