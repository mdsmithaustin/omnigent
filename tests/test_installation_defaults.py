"""Behavioral checks for independent core and UI SDK defaults."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest


def _run_python(tmp_path: Path, source: str) -> subprocess.CompletedProcess[str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("OMNIGENT_") and key != "PYTHONPATH"
    }
    env["HOME"] = str(tmp_path)
    env["OMNIGENT_NO_UPDATE_CHECK"] = "1"
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(source)],
        cwd=tmp_path,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )


def test_core_and_sdk_roots_preserve_evaluation_timing(tmp_path: Path) -> None:
    _run_python(
        tmp_path,
        """
        import os
        from pathlib import Path
        from omnigent import cli, config
        from omnigent.chat import _omnigent_persistent_dir
        from omnigent.host.local_server import _local_data_dir
        from omnigent.process_logging import data_dir
        from omnigent_ui_sdk.terminal import state_dir, user_config_path

        home = Path.home()
        assert config.global_config_path() == home / '.omnigent-mdsmithaustin/config.yaml'
        assert cli._effective_global_config_path() == home / '.omnigent-mdsmithaustin/config.yaml'
        assert cli._GLOBAL_AGENTS_DIR == home / '.omnigent-mdsmithaustin/agents'
        for resolve in (data_dir, _local_data_dir, _omnigent_persistent_dir, state_dir):
            assert resolve() == home / '.omnigent-mdsmithaustin'
        assert user_config_path() == home / '.omnigent-mdsmithaustin/config.yaml'

        moved_home = home / 'changed-home'
        os.environ['HOME'] = str(moved_home)
        assert config.global_config_path() == home / '.omnigent-mdsmithaustin/config.yaml'
        assert cli._effective_global_config_path() == home / '.omnigent-mdsmithaustin/config.yaml'
        assert cli._GLOBAL_AGENTS_DIR == home / '.omnigent-mdsmithaustin/agents'
        for resolve in (data_dir, _local_data_dir, _omnigent_persistent_dir, state_dir):
            assert resolve() == moved_home / '.omnigent-mdsmithaustin'
        assert user_config_path() == moved_home / '.omnigent-mdsmithaustin/config.yaml'
        """,
    )


def test_explicit_selectors_keep_precedence_and_expansion(tmp_path: Path) -> None:
    _run_python(
        tmp_path,
        """
        import os
        from pathlib import Path
        from omnigent import cli, config
        from omnigent.chat import _omnigent_persistent_dir
        from omnigent.host.local_server import _local_data_dir
        from omnigent.process_logging import data_dir
        from omnigent_ui_sdk.terminal import state_dir, user_config_path

        home = Path.home()
        os.environ['OMNIGENT_DATA_DIR'] = '~/data'
        os.environ['OMNIGENT_CONFIG_HOME'] = '~/config'
        for resolve in (data_dir, _local_data_dir, _omnigent_persistent_dir, state_dir):
            assert resolve() == home / 'data'
        assert config.global_config_path(Path('ignored')) == Path('~/config/config.yaml')
        assert cli._effective_global_config_path() == Path('~/config/config.yaml')
        assert user_config_path() == home / 'config/config.yaml'
        assert user_config_path('~/explicit') == home / 'explicit/config.yaml'
        assert user_config_path('relative') == Path('relative/config.yaml')
        Path('explicit.yaml').write_text('profile: explicit\\n')
        assert config.load_global_config(Path('explicit.yaml')) == {'profile': 'explicit'}
        del os.environ['OMNIGENT_DATA_DIR']
        assert data_dir() == home / '.omnigent-mdsmithaustin'
        del os.environ['OMNIGENT_CONFIG_HOME']
        assert config.global_config_path(Path('fallback.yaml')) == Path('fallback.yaml')
        os.environ['OMNIGENT_DATA_DIR'] = 'relative-data'
        for resolve in (data_dir, _local_data_dir, _omnigent_persistent_dir, state_dir):
            assert resolve() == Path('relative-data')
        assert user_config_path() == home / '.omnigent-mdsmithaustin/config.yaml'
        """,
    )


def test_project_config_ignores_upstream_and_keeps_merge(tmp_path: Path) -> None:
    _run_python(
        tmp_path,
        """
        from pathlib import Path
        from omnigent import cli, config
        import os

        Path('project').mkdir()
        os.chdir('project')
        upstream = Path('.omnigent/config.yaml')
        upstream.parent.mkdir()
        upstream.write_text('profile: upstream\\n')
        assert config.load_local_config() == {}
        assert cli._load_local_config() == {}
        assert config.load_local_config(upstream) == {'profile': 'upstream'}
        config.save_global_config({'profile': 'global', 'model': 'global-model'})
        project = Path('.omnigent-mdsmithaustin/config.yaml')
        project.parent.mkdir()
        project.write_text('profile: project\\n')
        assert config.load_effective_config() == {'profile': 'project', 'model': 'global-model'}
        assert cli._load_effective_config() == {'profile': 'project', 'model': 'global-model'}
        assert upstream.read_text() == 'profile: upstream\\n'
        """,
    )


@pytest.mark.parametrize("seed_upstream", [False, True])
def test_cli_startup_leaves_legacy_and_upstream_state_untouched(
    tmp_path: Path, seed_upstream: bool
) -> None:
    names = [".omniagents", ".omnigents"]
    if seed_upstream:
        names.append(".omnigent")
    sentinels = {}
    for name in names:
        root = tmp_path / name
        root.mkdir()
        for filename in ("config.yaml", "chat.db", "auth_tokens.json"):
            path = root / filename
            value = f"{name}/{filename}\n"
            path.write_text(value)
            sentinels[path] = value

    result = _run_python(
        tmp_path,
        """
        import sys
        from omnigent.cli import main
        sys.argv = ['omnigent', '--help']
        main()
        """,
    )

    assert "Usage:" in result.stdout
    assert {path: path.read_text() for path in sentinels} == sentinels
    assert sorted(
        path.relative_to(tmp_path).as_posix()
        for name in names
        for path in (tmp_path / name).rglob("*")
    ) == sorted(path.relative_to(tmp_path).as_posix() for path in sentinels)
    assert (tmp_path / ".omnigent").exists() == seed_upstream


def test_sdk_defaults_import_with_only_stdlib(tmp_path: Path) -> None:
    module_path = (
        Path(__file__).resolve().parents[1] / "sdks/ui/omnigent_ui_sdk/installation_defaults.py"
    )
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-c",
            textwrap.dedent(
                """
                import importlib.util
                import os
                import sys
                from pathlib import Path

                spec = importlib.util.spec_from_file_location('sdk_defaults', sys.argv[1])
                defaults = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(defaults)
                os.environ['HOME'] = sys.argv[2]
                assert defaults.default_user_dir() == Path(sys.argv[2]) / '.omnigent-mdsmithaustin'
                assert defaults.DEFAULT_HISTORY_FILE == '~/.omnigent-mdsmithaustin_history'
                assert 'omnigent' not in sys.modules
                print('standalone SDK defaults verified')
                """
            ),
            str(module_path),
            str(tmp_path),
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout == "standalone SDK defaults verified\n"
