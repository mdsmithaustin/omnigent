"""Test processes keep stdlib logging free of third-party pytest plugin config.

A plugin that installs a root stdout handler makes every ``exc_info`` log in
the suite render a rich traceback on the logging thread. On a loaded CI worker
that rendering takes seconds, long enough to stall request handlers that log
before they respond.
"""

from __future__ import annotations

import logging

import pytest


def test_logged_exception_is_not_rendered_to_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    try:
        raise RuntimeError("boom")
    except RuntimeError as exc:
        logging.getLogger("omnigent.test_isolation").warning("failed", exc_info=exc)

    assert capsys.readouterr().out == ""
