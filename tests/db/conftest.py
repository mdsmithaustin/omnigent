"""Database lifecycle tests own their schema, including after failed migrations."""

from __future__ import annotations

import os
import uuid
from collections.abc import Generator

import pytest
from filelock import FileLock

from tests.conftest import _server_test_database


@pytest.fixture()
def _worker_db_uri(tmp_path_factory: pytest.TempPathFactory) -> Generator[str, None, None]:
    base_uri = os.environ.get("OMNIGENT_TEST_DB_URI", "")
    if not base_uri:
        yield ""
        return
    directory = tmp_path_factory.getbasetemp()
    if os.environ.get("PYTEST_XDIST_WORKER"):
        directory = directory.parent
    # Separate databases still share CockroachDB's schema catalog.
    with FileLock(directory / "database-schema.lock"):
        with _server_test_database(base_uri, f"omnigent_schema_{uuid.uuid4().hex}") as uri:
            yield uri
