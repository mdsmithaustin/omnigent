"""Shared fixtures for the onboarding test suite."""

from __future__ import annotations

import pytest

from omnigent.onboarding import extra_install, harness_install


@pytest.fixture(autouse=True)
def _clear_probe_caches() -> None:
    """Isolate the module-level CLI probe caches between tests.

    ``harness_cli_logged_in`` / ``_harness_cli_version_string`` cache
    verdicts process-wide; a positive cached by one test's fake probe
    would leak into the next test's counting assertions.
    """
    harness_install._LOGIN_PROBE_CACHE.clear()
    harness_install._VERSION_PROBE_CACHE.clear()


@pytest.fixture(autouse=True)
def _upstream_extra_install(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin extra installs to the upstream command shapes; fork tests opt in."""
    monkeypatch.setattr(extra_install, "_is_fork_build", lambda: False)
