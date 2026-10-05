from __future__ import annotations

import sys
from dataclasses import dataclass, replace
from types import CodeType, FunctionType, ModuleType, TracebackType
from typing import Literal, Self

Boundary = Literal["deployment", "dispose", "cli"]
Kind = Literal[
    "type-error",
    "attribute-error",
    "assertion-error",
    "runtime-error",
    "import-error",
    "os-error",
    "other",
]
DEPLOYMENT_ERROR_MESSAGE = "Database schema deployment failed."


@dataclass(frozen=True)
class SafeFrame:
    code: str
    line: int


@dataclass(frozen=True)
class DeploymentDiagnostic:
    boundary: Boundary
    kind: Kind
    frames: tuple[SafeFrame, ...]
    truncated: bool


@dataclass(frozen=True)
class DeploymentFailure:
    message: str
    diagnostics: tuple[DeploymentDiagnostic, ...]


class ExpectedSchemaFailure(RuntimeError):
    """A known schema refusal whose detailed message stays inside the boundary."""

    def __init__(self, message: str) -> None:
        super().__init__(message)


class SessionWorkDeploymentError(RuntimeError):
    """A bounded operator error with detached, credential-free diagnostics."""

    def __init__(
        self, message: str, *, diagnostics: tuple[DeploymentDiagnostic, ...] = ()
    ) -> None:
        super().__init__(message)
        self.diagnostics = diagnostics


_KINDS: dict[type[Exception], Kind] = {
    TypeError: "type-error",
    AttributeError: "attribute-error",
    AssertionError: "assertion-error",
    RuntimeError: "runtime-error",
    ImportError: "import-error",
    ModuleNotFoundError: "import-error",
    OSError: "os-error",
}
_APPROVED_FUNCTIONS = (
    ("omnigent.db.utils", "deploy_session_work_schema", "db.utils.deploy_session_work_schema"),
    ("omnigent.db.utils", "_create_engine", "db.utils._create_engine"),
    ("omnigent.db.utils", "_apply_session_work_schema", "db.utils._apply_session_work_schema"),
    ("omnigent.db.utils", "_get_head_db_revision", "db.utils._get_head_db_revision"),
    ("omnigent.db.utils", "_build_alembic_config", "db.utils._build_alembic_config"),
    ("omnigent.db.utils", "_get_current_db_revision", "db.utils._get_current_db_revision"),
    ("omnigent.db.utils", "_initialize_or_verify_schema", "db.utils._initialize_or_verify_schema"),
    ("omnigent.db.utils", "_run_migrations", "db.utils._run_migrations"),
    ("omnigent.db.cockroachdb", "_crdb_server_version", "db.cockroachdb._crdb_server_version"),
    (
        "omnigent.db.cockroachdb",
        "_initialize_or_verify_crdb_schema",
        "db.cockroachdb._initialize_or_verify_crdb_schema",
    ),
    (
        "omnigent.db.cockroachdb",
        "_prepare_crdb_schema_transaction",
        "db.cockroachdb._prepare_crdb_schema_transaction",
    ),
    (
        "omnigent.db.cockroachdb",
        "_repair_and_verify_crdb_model_indexes",
        "db.cockroachdb._repair_and_verify_crdb_model_indexes",
    ),
    ("omnigent.db.migrations.schema.mm1a2b3c4d5e", "apply", "db.schema.mm1a2b3c4d5e.apply"),
    (
        "omnigent.db.migrations.schema.mm1a2b3c4d5e",
        "preflight",
        "db.schema.mm1a2b3c4d5e.preflight",
    ),
    ("omnigent.db.migrations.schema.mm1a2b3c4d5e", "verify", "db.schema.mm1a2b3c4d5e.verify"),
    ("__main__", "main", "cli.deploy_session_work_schema.main"),
    ("scripts.deploy_session_work_schema", "main", "cli.deploy_session_work_schema.main"),
)


def _diagnostic(
    boundary: Boundary, exc: Exception, traceback: TracebackType | None
) -> DeploymentDiagnostic:
    approved: dict[int, tuple[CodeType, str, set[int]]] = {}
    for module_name, function_name, label in _APPROVED_FUNCTIONS:
        module = sys.modules.get(module_name)
        if type(module) is not ModuleType:
            continue
        function = vars(module).get(function_name)
        if type(function) is FunctionType:
            code = function.__code__
            lines = {line for _, _, line in code.co_lines() if line is not None}
            approved[id(code)] = (code, label, lines)
    frames: list[SafeFrame] = []
    truncated = False
    for _ in range(64):
        if traceback is None:
            break
        code = traceback.tb_frame.f_code
        entry = approved.get(id(code))
        line = traceback.tb_lineno
        if entry is not None and entry[0] is code and line in entry[2] and 1 <= line <= 1_000_000:
            frames.append(SafeFrame(entry[1], line))
            if len(frames) > 6:
                del frames[0]
                truncated = True
        traceback = traceback.tb_next
    kind: Kind = "other"
    for builtin, category in _KINDS.items():
        if type(exc) is builtin:
            kind = category
            break
    return DeploymentDiagnostic(boundary, kind, tuple(frames), truncated or traceback is not None)


class SessionWorkFailureCapture:
    def __init__(self, boundary: Boundary) -> None:
        self.boundary = boundary
        self.failure: DeploymentFailure | None = None

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        if not isinstance(exc, Exception):
            return False
        if type(exc) is SessionWorkDeploymentError:
            self.failure = DeploymentFailure(str(exc), exc.diagnostics)
        elif type(exc) is ExpectedSchemaFailure:
            self.failure = DeploymentFailure(DEPLOYMENT_ERROR_MESSAGE, ())
        else:
            self.failure = DeploymentFailure(
                DEPLOYMENT_ERROR_MESSAGE, (_diagnostic(self.boundary, exc, traceback),)
            )
        return True


def _combine_failures(*failures: DeploymentFailure | None) -> DeploymentFailure | None:
    present = [failure for failure in failures if failure is not None]
    if not present:
        return None
    diagnostics = tuple(item for failure in present for item in failure.diagnostics)
    if len(diagnostics) > 3:
        diagnostics = (*diagnostics[:2], replace(diagnostics[2], truncated=True))
    return DeploymentFailure(
        DEPLOYMENT_ERROR_MESSAGE if diagnostics else present[-1].message, diagnostics
    )
