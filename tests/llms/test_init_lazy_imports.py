"""Check lazy import contracts in fresh interpreters."""

from __future__ import annotations

import subprocess
import sys


def _check_imports(script: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", script + '\nprint("IMPORT_OK")'],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.endswith("IMPORT_OK\n")


def test_sessions_routes_import_does_not_trigger_cycle() -> None:
    _check_imports("import omnigent.server.routes.sessions")


def test_short_form_import_still_works() -> None:
    _check_imports(
        "from omnigent.llms import Client, get_model_context_window\n"
        "assert Client.__name__ == 'Client'\n"
        "assert get_model_context_window.__name__ == 'get_model_context_window'"
    )


def test_module_only_import_does_not_load_client() -> None:
    _check_imports(
        "import sys\nimport omnigent.llms\n"
        "assert omnigent.llms.__name__ == 'omnigent.llms'\n"
        "assert 'omnigent.llms.client' not in sys.modules"
    )


def test_unknown_attribute_raises_attribute_error() -> None:
    _check_imports(
        "import omnigent.llms\n"
        "try:\n    omnigent.llms.does_not_exist\n"
        "except AttributeError as error:\n    assert 'does_not_exist' in str(error)\n"
        "else:\n    raise AssertionError('expected AttributeError')"
    )
