"""Standalone UI SDK defaults, checked against the core installation."""

from pathlib import Path
from typing import Final

USER_DIRNAME: Final = ".omnigent-mdsmithaustin"
DEFAULT_HISTORY_FILE: Final = f"~/{USER_DIRNAME}_history"


def default_user_dir() -> Path:
    """Resolve the installation's default directory under the current HOME."""
    return Path.home() / USER_DIRNAME
