from pathlib import Path

import pytest

from tests.e2e import test_model_catalog_cli_upgrade_e2e as catalog

pytestmark = catalog.pytestmark


@pytest.mark.timeout(180)
def test_catalog_rig_rejects_server_bind_collision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    binary = bin_dir / "codex"
    catalog._write_fake_codex(binary, marker="COMPETITOR")
    competitor = catalog._CatalogRig(
        tmp_path / "competitor", bin_dir, {"OMNIGENT_CODEX_PATH": str(binary)}
    )
    target = catalog._CatalogRig(tmp_path / "target", bin_dir, {})
    free_port = catalog._free_port

    def contested_port() -> int:
        port = free_port()
        with monkeypatch.context() as patch:
            patch.setattr(catalog, "_free_port", lambda: port)
            competitor.start_server()
        competitor.start_host()
        assert competitor.model_display_names("codex-native", timeout=90) == [
            "GPT-6 Codex COMPETITOR",
            "GPT-6 Mini COMPETITOR",
        ]
        return port

    try:
        monkeypatch.setattr(catalog, "_free_port", contested_port)
        with pytest.raises(RuntimeError, match="process exited early"):
            target.start_server()
        assert target.host_id != competitor.host_id
        assert target._host is None
        assert target._server is not None and target._server.poll() != 0
    finally:
        target.stop()
        competitor.stop()


@pytest.mark.timeout(180)
def test_catalog_rig_rejects_another_online_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    with catalog._booted_rig(tmp_path / "target", bin_dir, {}) as target:
        competitor = catalog._CatalogRig(tmp_path / "competitor", bin_dir, {})
        popen = catalog.subprocess.Popen
        try:
            competitor.start_server()
            competitor.base_url = target.base_url
            competitor.start_host()

            def failed_host(args: list[str], **kwargs: object) -> catalog.subprocess.Popen[bytes]:
                return popen(
                    [
                        catalog.sys.executable,
                        "-c",
                        "import time; time.sleep(2); raise SystemExit(37)",
                    ],
                    **kwargs,
                )

            monkeypatch.setattr(catalog.subprocess, "Popen", failed_host)
            with pytest.raises(RuntimeError, match="process exited early"):
                target.start_host()
            assert target.host_id != competitor.host_id
        finally:
            competitor.stop()


@pytest.mark.timeout(180)
def test_catalog_rig_restart_waits_for_old_registration_to_disconnect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    with catalog._booted_rig(tmp_path / "target", bin_dir, {}) as target:
        target.start_host()
        old_host = target._host
        terminate = target._terminate
        try:
            monkeypatch.setattr(
                target, "_terminate", lambda proc: None if proc is old_host else terminate(proc)
            )
            monkeypatch.setattr(catalog, "_HOST_ONLINE_TIMEOUT_S", 2)
            with pytest.raises(RuntimeError, match=r"timed out.*disconnect"):
                target.start_host()
            hosts = target._get_json(f"{target.base_url}/v1/hosts")["hosts"]
            assert any(h["host_id"] == target.host_id and h["status"] == "online" for h in hosts)
        finally:
            terminate(old_host)
