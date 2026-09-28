from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from omnigent._platform import stable_user_id
from omnigent.harnesses.pi_native.bridge import enqueue_interrupt as enqueue_interrupt
from omnigent.process_logging import data_dir

PRIME_NATIVE_BRIDGE_DIR_ENV_VAR = "HARNESS_PRIME_NATIVE_BRIDGE_DIR"
PRIME_NATIVE_CONFIG_ENV_VAR = "OMNIGENT_EXTENSION_NATIVE_CONFIG"
_DATA_ROOT = data_dir()
_COMPACT_ROOT = Path("/tmp") / f"ogp-{stable_user_id()}"


def bridge_roots() -> tuple[Path, Path]:
    return _DATA_ROOT / "prime-native", _COMPACT_ROOT


@dataclass(frozen=True)
class PrimeRuntimePaths:
    root: Path

    @property
    def agent_dir(self) -> Path:
        return self.root / "agent"

    @property
    def temp_dir(self) -> Path:
        return self.root / "tmp"

    @property
    def session_dir(self) -> Path:
        return self.root / "sessions"

    @property
    def env(self) -> dict[str, str]:
        return {
            "PRIME_AGENT_CODING_AGENT_DIR": str(self.agent_dir),
            "TMPDIR": str(self.temp_dir),
        }

    def prepare(self) -> None:
        from omnigent.harnesses.claude_native.bridge import ensure_secure_dir

        self.validate_existing()
        for directory in self._directories:
            ensure_secure_dir(directory)

    @property
    def _directories(self) -> tuple[Path, ...]:
        return (
            self.root,
            self.agent_dir,
            self.temp_dir,
            self.session_dir,
            self.root / "inbox",
        )

    def validate_existing(self) -> bool:
        from omnigent.harnesses.claude_native.bridge import (
            _absolute_syntactic_path,
            _ensure_private_dir,
            _trusted_parent_for_bridge_dir,
            ensure_secure_dir,
        )
        from omnigent.harnesses.pi_native.bridge import config_path, extension_path

        root = _absolute_syntactic_path(self.root)
        if root.parent not in {_absolute_syntactic_path(path) for path in bridge_roots()}:
            raise RuntimeError(f"refusing Prime runtime outside its bridge roots: {self.root}")
        trusted_parent = _trusted_parent_for_bridge_dir(root)
        ancestors = []
        ancestor = root
        while ancestor != trusted_parent and ancestor != ancestor.parent:
            ancestors.append(ancestor)
            ancestor = ancestor.parent
        if ancestor != trusted_parent:
            raise RuntimeError(
                f"Prime runtime {root} is not under trusted parent {trusted_parent}"
            )
        getuid = getattr(os, "getuid", None)
        uid = getuid() if getuid is not None else None
        for directory in reversed(ancestors):
            try:
                directory.lstat()
            except FileNotFoundError:
                return False
            _ensure_private_dir(directory, uid)
        lock_dir = root.parent / ".locks"
        for directory in (*self._directories[1:], lock_dir):
            try:
                directory.lstat()
            except FileNotFoundError:
                continue
            ensure_secure_dir(directory)
        for path in (
            *(self.agent_dir / name for name in ("auth.json", "settings.json", "models.json")),
            self.root / "executable",
            self.root / "terminal.json",
            config_path(self.root),
            extension_path(self.root),
            lock_dir / f"{root.name}.lock",
        ):
            try:
                metadata = path.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(metadata.st_mode):
                raise RuntimeError(f"refusing to use {path}: is a symlink")
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise RuntimeError(f"refusing to use {path}: not a regular file with one link")
            if uid is not None and metadata.st_uid != uid:
                raise RuntimeError(f"refusing to use {path}: owned by another user")
        return True


def bridge_dir_for_session_id(session_id: str) -> Path:
    digest = hashlib.sha256(session_id.encode()).hexdigest()[:32]
    root = bridge_roots()[0] / digest
    worker_socket = (
        root / "tmp" / f"prime-agent-{stable_user_id()}" / ("worker-" + "0" * 25 + ".sock")
    )
    if len(str(worker_socket.resolve()).encode()) >= 100:
        namespace = hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:20]
        return _COMPACT_ROOT / namespace
    return root


def runtime_paths(session_id: str) -> PrimeRuntimePaths:
    return PrimeRuntimePaths(bridge_dir_for_session_id(session_id))


def build_prime_native_spawn_env(conversation_id: str) -> dict[str, str]:
    return {PRIME_NATIVE_BRIDGE_DIR_ENV_VAR: str(bridge_dir_for_session_id(conversation_id))}


def executor_bridge_dir() -> Path:
    raw = os.environ.get(PRIME_NATIVE_BRIDGE_DIR_ENV_VAR, "").strip()
    if not raw:
        raise RuntimeError(f"{PRIME_NATIVE_BRIDGE_DIR_ENV_VAR} is required for prime-native")
    return Path(raw)
