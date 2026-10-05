from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from omnigent.db import utils

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/deploy_session_work_schema.py"
CANARY = "ap-error-secret-canary"


def test_python_unexpected_failure_retains_safe_evidence(monkeypatch):
    def fail_engine(uri):
        raise TypeError(CANARY)

    monkeypatch.setattr(utils, "_create_engine", fail_engine)
    with pytest.raises(utils.SessionWorkDeploymentError) as caught:
        utils.deploy_session_work_schema("sqlite://", role="split-ap", target="mm1a2b3c4d5e")

    error = caught.value
    assert str(error) == "Database schema deployment failed."
    assert [item.kind for item in error.diagnostics] == ["type-error"]
    assert error.diagnostics[0].boundary == "deployment"
    assert "db.utils.deploy_session_work_schema" in {
        frame.code for frame in error.diagnostics[0].frames
    }
    assert CANARY not in json.dumps([asdict(item) for item in error.diagnostics])
    assert error.__context__ is None
    assert error.__cause__ is None


def test_cli_unexpected_failure_retains_safe_evidence():
    result = run_cli(f"""
import sys
from omnigent.db import utils
def fail_engine(uri):
    raise TypeError({CANARY!r})
utils._create_engine = fail_engine
""")
    payload = cli_error(result)
    assert payload["error"] == "Database schema deployment failed."
    assert payload["diagnostics"][0]["kind"] == "type-error"
    assert payload["diagnostics"][0]["boundary"] == "deployment"


def run_cli(setup, *, role="split-ap"):
    code = (
        setup
        + "\nimport runpy\nsys.argv = sys.argv[1:]\n"
        + 'runpy.run_path(sys.argv[0], run_name="__main__")\n'
    )
    with TemporaryDirectory() as directory:
        return subprocess.run(
            [
                sys.executable,
                "-c",
                code,
                str(SCRIPT),
                "--role",
                role,
                "--target",
                "mm1a2b3c4d5e",
                "--uri-env",
                "SCHEMA_TEST_URI",
            ],
            cwd=ROOT,
            env={**os.environ, "SCHEMA_TEST_URI": f"sqlite:///{directory}/deployment.db"},
            capture_output=True,
            text=True,
            timeout=30,
        )


def cli_error(result):
    assert result.returncode == 1
    assert result.stdout == ""
    assert len(result.stderr.splitlines()) == 1
    assert len(result.stderr.encode()) < 4096
    assert CANARY not in result.stderr
    return json.loads(result.stderr)


@pytest.mark.parametrize("point", ["apply", "verify", "preflight"])
def test_core_failure_is_detached_and_disposes_once(monkeypatch, tmp_path, point):
    from omnigent.db.migrations.schema import mm1a2b3c4d5e as revision

    engine = utils._create_engine(f"sqlite:///{tmp_path / 'deployment.db'}")
    disposed = []
    original_dispose = engine.dispose

    def dispose():
        disposed.append("disposed")
        original_dispose()

    def fail(connection):
        raise AttributeError(CANARY)

    monkeypatch.setattr(engine, "dispose", dispose)
    monkeypatch.setattr(utils, "_create_engine", lambda uri: engine)
    monkeypatch.setattr(revision, point, fail)
    with pytest.raises(utils.SessionWorkDeploymentError) as caught:
        utils.deploy_session_work_schema(
            f"sqlite:///{tmp_path / 'deployment.db'}", role="split-ap", target="mm1a2b3c4d5e"
        )
    error = caught.value
    assert str(error) == "Database schema deployment failed."
    assert [(item.boundary, item.kind) for item in error.diagnostics] == [
        ("deployment", "attribute-error")
    ]
    labels = {frame.code for frame in error.diagnostics[0].frames}
    assert "db.utils._apply_session_work_schema" in labels
    assert f"db.schema.mm1a2b3c4d5e.{point}" in labels
    assert error.__context__ is None
    assert disposed == ["disposed"]


@pytest.mark.parametrize("operation", [None, TypeError, utils.SessionWorkDeploymentError])
@pytest.mark.parametrize("cleanup", [RuntimeError, utils.SessionWorkDeploymentError])
def test_operation_and_disposal_precedence(monkeypatch, tmp_path, operation, cleanup):
    from omnigent.db.migrations.schema import mm1a2b3c4d5e as revision

    engine = utils._create_engine(f"sqlite:///{tmp_path / 'deployment.db'}")
    original_dispose = engine.dispose
    disposed = []

    def dispose():
        original_dispose()
        disposed.append("disposed")
        raise cleanup("Shared revision verification failed.")

    def fail(connection):
        raise operation("Split AP target contains shared history.")

    monkeypatch.setattr(engine, "dispose", dispose)
    monkeypatch.setattr(utils, "_create_engine", lambda uri: engine)
    if operation is not None:
        monkeypatch.setattr(revision, "apply", fail)
    with pytest.raises(utils.SessionWorkDeploymentError) as caught:
        utils.deploy_session_work_schema(
            f"sqlite:///{tmp_path / 'deployment.db'}", role="split-ap", target="mm1a2b3c4d5e"
        )
    error = caught.value
    expected = []
    if operation is TypeError:
        expected.append(("deployment", "type-error"))
    if cleanup is RuntimeError:
        expected.append(("dispose", "runtime-error"))
    assert [(item.boundary, item.kind) for item in error.diagnostics] == expected
    assert str(error) == (
        "Database schema deployment failed."
        if expected
        else "Shared revision verification failed."
    )
    assert error.__context__ is None
    assert error.__cause__ is None
    assert disposed == ["disposed"]


@pytest.mark.parametrize("interruption", [KeyboardInterrupt, SystemExit])
def test_interruption_preserves_identity_and_disposes_once(monkeypatch, tmp_path, interruption):
    from omnigent.db.migrations.schema import mm1a2b3c4d5e as revision

    engine = utils._create_engine(f"sqlite:///{tmp_path / 'deployment.db'}")
    original_dispose = engine.dispose
    disposed = []
    failure = interruption(73)

    def dispose():
        original_dispose()
        disposed.append("disposed")
        raise TypeError(CANARY)

    def fail(connection):
        raise failure

    monkeypatch.setattr(engine, "dispose", dispose)
    monkeypatch.setattr(utils, "_create_engine", lambda uri: engine)
    monkeypatch.setattr(revision, "apply", fail)
    with pytest.raises(interruption) as caught:
        utils.deploy_session_work_schema(
            f"sqlite:///{tmp_path / 'deployment.db'}", role="split-ap", target="mm1a2b3c4d5e"
        )
    assert caught.value is failure
    assert caught.value.args == (73,)
    assert disposed == ["disposed"]


@pytest.mark.parametrize("point", ["omnigent", "omnigent.db.utils"])
def test_cli_import_canary_is_captured(point):
    result = run_cli(f"""
import sys
from importlib.abc import MetaPathFinder
class FailImport(MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == {point!r}:
            raise ImportError({CANARY!r})
sys.meta_path.insert(0, FailImport())
""")
    payload = cli_error(result)
    assert payload["error"] == "Database schema deployment failed."
    assert payload["diagnostics"][0]["boundary"] == "cli"
    assert payload["diagnostics"][0]["kind"] == "import-error"
    assert any(
        frame["code"] == "cli.deploy_session_work_schema.main"
        for frame in payload["diagnostics"][0]["frames"]
    )


@pytest.mark.parametrize("failure_type", ["OSError", "TypeError"])
@pytest.mark.parametrize("point", ["open", "close"])
def test_cli_stream_failure(point, failure_type):
    result = run_cli(f"""
import sys, builtins, os
original_open = builtins.open
class NullStream:
    def __enter__(self):
        self.stream = original_open(os.devnull, "w")
        return self.stream
    def __exit__(self, *args):
        self.stream.close()
        raise {failure_type}({CANARY!r})
def open_file(file, *args, **kwargs):
    if file == os.devnull:
        if {point!r} == "open":
            raise {failure_type}({CANARY!r})
        return NullStream()
    return original_open(file, *args, **kwargs)
builtins.open = open_file
""")
    payload = cli_error(result)
    if failure_type == "OSError":
        assert result.stderr == '{"error": "Database schema deployment failed."}\n'
    else:
        assert payload["diagnostics"][0]["kind"] == "type-error"
        assert payload["diagnostics"][0]["boundary"] == "cli"
        assert (
            payload["diagnostics"][0]["frames"][-1]["code"]
            == "cli.deploy_session_work_schema.main"
        )


def test_cli_retains_operation_disposal_and_stream_teardown():
    result = run_cli(f"""
import sys, builtins, os
from omnigent.db import utils
from omnigent.db.migrations.schema import mm1a2b3c4d5e as revision
original_create = utils._create_engine
def create(uri):
    engine = original_create(uri)
    original_dispose = engine.dispose
    def dispose():
        original_dispose()
        raise AttributeError({CANARY!r})
    engine.dispose = dispose
    return engine
def fail(connection):
    raise TypeError({CANARY!r})
utils._create_engine = create
revision.apply = fail
original_open = builtins.open
class NullStream:
    def __enter__(self):
        self.stream = original_open(os.devnull, "w")
        return self.stream
    def __exit__(self, *args):
        self.stream.close()
        raise RuntimeError({CANARY!r})
def open_file(file, *args, **kwargs):
    return NullStream() if file == os.devnull else original_open(file, *args, **kwargs)
builtins.open = open_file
""")
    payload = cli_error(result)
    assert [(item["boundary"], item["kind"]) for item in payload["diagnostics"]] == [
        ("deployment", "type-error"),
        ("dispose", "attribute-error"),
        ("cli", "runtime-error"),
    ]
    assert all(item["frames"] for item in payload["diagnostics"])


def test_cli_expected_teardown_replaces_expected_call():
    result = run_cli("""
import sys, builtins, os
from omnigent.db import utils
def fail(*args, **kwargs):
    raise utils.SessionWorkDeploymentError("Split AP target contains shared history.")
utils.deploy_session_work_schema = fail
original_open = builtins.open
class NullStream:
    def __enter__(self):
        self.stream = original_open(os.devnull, "w")
        return self.stream
    def __exit__(self, *args):
        self.stream.close()
        raise OSError("teardown")
def open_file(file, *args, **kwargs):
    return NullStream() if file == os.devnull else original_open(file, *args, **kwargs)
builtins.open = open_file
""")
    assert cli_error(result) == {"error": "Database schema deployment failed."}
    assert result.stderr == '{"error": "Database schema deployment failed."}\n'


def test_hostile_exception_metadata_is_never_read(monkeypatch):
    class Hostile(Exception):
        def __str__(self):
            pytest.fail("Unexpected exception was formatted")

    Hostile.__name__ = CANARY
    namespace = {"Hostile": Hostile, "CANARY": CANARY}
    exec(
        compile(
            "def fail(uri):\n    private = CANARY\n"
            "    raise Hostile(private) from ValueError(private)\n",
            CANARY,
            "exec",
        ),
        namespace,
    )
    namespace["fail"].__code__ = namespace["fail"].__code__.replace(
        co_name=CANARY, co_qualname=CANARY
    )
    monkeypatch.setattr(utils, "_create_engine", namespace["fail"])
    with pytest.raises(utils.SessionWorkDeploymentError) as caught:
        utils.deploy_session_work_schema("sqlite://", role="split-ap", target="mm1a2b3c4d5e")
    error = caught.value
    assert str(error) == "Database schema deployment failed."
    assert error.diagnostics[0].kind == "other"
    assert CANARY not in json.dumps([asdict(item) for item in error.diagnostics])
    assert error.__context__ is None
    assert error.__cause__ is None


def test_owner_local_import_failure_is_captured(monkeypatch):
    import builtins

    original_import = builtins.__import__

    def fail(name, *args, **kwargs):
        if name == "omnigent.db.migrations.schema":
            raise TypeError(CANARY)
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fail)
    with pytest.raises(utils.SessionWorkDeploymentError) as caught:
        utils.deploy_session_work_schema("sqlite://", role="split-ap", target="mm1a2b3c4d5e")
    assert caught.value.diagnostics[0].kind == "type-error"
    assert caught.value.diagnostics[0].frames[-1].code == "db.utils.deploy_session_work_schema"
    assert caught.value.__context__ is None


def test_capture_rejects_forged_code_identity_and_line(monkeypatch):
    from types import FunctionType, TracebackType

    from omnigent_session_work_failure import SessionWorkFailureCapture

    def trusted(uri):
        return sys._getframe()

    monkeypatch.setattr(utils, "_create_engine", trusted)
    clone = FunctionType(trusted.__code__.replace(), globals())
    for frame, line in [
        (trusted(None), 999_999),
        (clone(None), trusted.__code__.co_firstlineno + 1),
    ]:
        capture = SessionWorkFailureCapture("deployment")
        failure = TypeError(CANARY)
        capture.__exit__(TypeError, failure, TracebackType(None, frame, frame.f_lasti, line))
        assert asdict(capture.failure) == {
            "message": "Database schema deployment failed.",
            "diagnostics": (
                {"boundary": "deployment", "kind": "type-error", "frames": (), "truncated": False},
            ),
        }


@pytest.mark.parametrize("depth", [8, 100])
def test_capture_truncates_long_traceback_and_retains_recent_approved_frames(monkeypatch, depth):
    from omnigent_session_work_failure import SessionWorkFailureCapture

    def recurse(depth):
        if depth:
            return recurse(depth - 1)
        raise AssertionError(CANARY)

    monkeypatch.setattr(utils, "_apply_session_work_schema", recurse)
    capture = SessionWorkFailureCapture("deployment")
    with capture:
        recurse(depth)
    assert capture.failure.message == "Database schema deployment failed."
    diagnostic = capture.failure.diagnostics[0]
    assert diagnostic.kind == "assertion-error"
    assert diagnostic.truncated is True
    assert len(diagnostic.frames) == 6
    assert {frame.code for frame in diagnostic.frames} == {"db.utils._apply_session_work_schema"}
    assert CANARY not in repr(capture.failure)


def test_maximum_combined_diagnostics_fit_one_json_object():
    from omnigent_session_work_failure import (
        _APPROVED_FUNCTIONS,
        DeploymentDiagnostic,
        DeploymentFailure,
        SafeFrame,
        _combine_failures,
    )

    for _, _, label in _APPROVED_FUNCTIONS:
        assert len(label) <= 96
        assert label.isascii()
        assert label.replace(".", "_").isidentifier()
    frame = SafeFrame("a" * 96, 1_000_000)
    failures = [
        DeploymentFailure(
            "ignored", (DeploymentDiagnostic(boundary, "attribute-error", (frame,) * 6, False),)
        )
        for boundary in ("deployment", "dispose", "cli", "cli")
    ]
    combined = _combine_failures(*failures)
    payload = {
        "error": combined.message,
        "diagnostics": [asdict(item) for item in combined.diagnostics],
    }
    assert len(json.dumps(payload).encode()) < 4096
    assert [(item.boundary, item.truncated) for item in combined.diagnostics] == [
        ("deployment", False),
        ("dispose", False),
        ("cli", True),
    ]
    assert payload["error"] == "Database schema deployment failed."


@pytest.mark.parametrize(
    "failure", [ValueError(CANARY), RuntimeError(CANARY), ImportError(CANARY)]
)
def test_arbitrary_core_failures_remain_unexpected(monkeypatch, tmp_path, failure):
    from omnigent.db.migrations.schema import mm1a2b3c4d5e as revision

    def fail(connection):
        raise failure

    monkeypatch.setattr(revision, "apply", fail)
    with pytest.raises(utils.SessionWorkDeploymentError) as caught:
        utils.deploy_session_work_schema(
            f"sqlite:///{tmp_path / 'deployment.db'}", role="split-ap", target="mm1a2b3c4d5e"
        )
    assert str(caught.value) == "Database schema deployment failed."
    assert len(caught.value.diagnostics) == 1
    assert caught.value.__context__ is None


def test_missing_alembic_head_is_unexpected(monkeypatch):
    from alembic.script import ScriptDirectory

    monkeypatch.setattr(ScriptDirectory, "get_current_head", lambda self: None)
    with pytest.raises(utils.SessionWorkDeploymentError) as caught:
        utils.deploy_session_work_schema("sqlite://", role="split-ap", target="mm1a2b3c4d5e")
    diagnostic = caught.value.diagnostics[0]
    assert diagnostic.kind == "runtime-error"
    assert "db.utils._get_head_db_revision" in {frame.code for frame in diagnostic.frames}
    assert caught.value.__context__ is None


def test_pool_refusal_keeps_shared_guidance_and_is_expected_for_deployment(monkeypatch):
    from omnigent_session_work_failure import ExpectedSchemaFailure

    monkeypatch.setenv("OMNIGENT_DB_POOL_SIZE", CANARY)
    with pytest.raises(ExpectedSchemaFailure, match="must be an integer") as caught:
        utils._create_engine("postgresql://localhost/test")
    assert CANARY in str(caught.value)
    assert isinstance(caught.value.__cause__, ValueError)
    with pytest.raises(utils.SessionWorkDeploymentError) as caught:
        utils.deploy_session_work_schema(
            "postgresql://localhost/test", role="split-ap", target="mm1a2b3c4d5e"
        )
    assert str(caught.value) == "Database schema deployment failed."
    assert caught.value.diagnostics == ()
    assert caught.value.__context__ is None


def test_crdb_version_refusal_keeps_shared_guidance_and_is_expected(monkeypatch, tmp_path):
    from omnigent.db import cockroachdb
    from omnigent_session_work_failure import ExpectedSchemaFailure

    with pytest.raises(ExpectedSchemaFailure, match=r"23\.2\.28 or newer"):
        cockroachdb._parse_crdb_server_version("CockroachDB v22.2.0")

    def fail(engine):
        cockroachdb._parse_crdb_server_version("CockroachDB v22.2.0")

    monkeypatch.setattr(utils, "_apply_session_work_schema", fail)
    with pytest.raises(utils.SessionWorkDeploymentError) as caught:
        utils.deploy_session_work_schema(
            f"sqlite:///{tmp_path / 'deployment.db'}", role="split-ap", target="mm1a2b3c4d5e"
        )
    assert str(caught.value) == "Database schema deployment failed."
    assert caught.value.diagnostics == ()
    assert caught.value.__context__ is None


@pytest.mark.parametrize("failure_kind", ["database", "refusal", "unexpected", "mixed"])
@pytest.mark.parametrize("owner", ["normal", "crdb"])
def test_shared_migration_wrapper_preserves_guidance_and_classification(
    monkeypatch, tmp_path, failure_kind, owner
):
    from packaging.version import Version
    from sqlalchemy.exc import SQLAlchemyError

    from omnigent.db import cockroachdb
    from omnigent_session_work_failure import ExpectedSchemaFailure

    engine = utils._create_engine(f"sqlite:///{tmp_path / 'deployment.db'}")
    monkeypatch.setattr(utils, "_get_head_db_revision", lambda uri: "mm1a2b3c4d5e")
    monkeypatch.setattr(utils, "_get_current_db_revision", lambda engine: "ll1a2b3c4d5e")
    if owner == "crdb":
        monkeypatch.setattr(cockroachdb, "_crdb_server_version", lambda engine: Version("23.2.28"))
        monkeypatch.setattr(cockroachdb, "_verify_crdb_read_committed", lambda *args: None)
        monkeypatch.setattr(cockroachdb, "_crdb_revision_is_supported", lambda *args: True)
        monkeypatch.setattr(
            utils, "_initialize_or_verify_schema", cockroachdb._initialize_or_verify_crdb_schema
        )
    if failure_kind == "database":
        failure = SQLAlchemyError(CANARY)
    elif failure_kind == "refusal":
        failure = ExpectedSchemaFailure(CANARY)
    else:
        failure = TypeError(CANARY)
        if failure_kind == "mixed":
            failure.__cause__ = SQLAlchemyError(CANARY)
    expected = failure_kind in {"database", "refusal"}

    def fail(*args):
        raise failure

    monkeypatch.setattr(utils, "_run_migrations", fail)
    try:
        with pytest.raises(RuntimeError) as caught:
            utils._initialize_or_verify_schema(engine, "sqlite://")
        assert "omnigent debug db-upgrade 'sqlite://'" in str(caught.value)
        assert caught.value.__cause__ is failure
        assert (type(caught.value) is ExpectedSchemaFailure) == expected
    finally:
        engine.dispose()

    with pytest.raises(utils.SessionWorkDeploymentError) as caught:
        utils.deploy_session_work_schema(
            f"sqlite:///{tmp_path / 'deployment.db'}", role="shared", target="mm1a2b3c4d5e"
        )
    assert str(caught.value) == "Database schema deployment failed."
    assert [item.kind for item in caught.value.diagnostics] == (
        [] if expected else ["runtime-error"]
    )
    assert caught.value.__context__ is None


def test_missing_crdb_dependency_is_expected(monkeypatch):
    from sqlalchemy.exc import NoSuchModuleError

    def fail(*args, **kwargs):
        raise NoSuchModuleError(CANARY)

    monkeypatch.setattr(utils, "create_engine", fail)
    with pytest.raises(utils.SessionWorkDeploymentError) as caught:
        utils.deploy_session_work_schema(
            "cockroachdb://localhost/test", role="split-ap", target="mm1a2b3c4d5e"
        )
    assert str(caught.value) == "Database schema deployment failed."
    assert caught.value.diagnostics == ()
    assert caught.value.__context__ is None
