import click
import pytest

from omnigent import cli


@pytest.mark.parametrize("outcome", ["unknown", "failed"])
def test_cli_stop_reports_unqualified_native_shutdown(monkeypatch, outcome):
    def response(**kwargs):
        if kwargs["method"] == "GET":
            return cli._HostHttpResult(status_code=200, body={})
        return cli._HostHttpResult(
            status_code=202,
            body={
                "queued": False,
                "native_stop": {"outcome": outcome, "detail": "Owner not confirmed."},
            },
        )

    monkeypatch.setattr(cli, "_host_http_json", response)
    with pytest.raises(click.ClickException, match="Different-agent fork remains blocked"):
        cli._stop_session_on_server(base_url="http://source", session_id="native-source")


def test_cli_stop_accepts_verified_native_shutdown(monkeypatch):
    monkeypatch.setattr(
        cli,
        "_host_http_json",
        lambda **kwargs: cli._HostHttpResult(
            status_code=202,
            body={"native_stop": {"outcome": "verified", "detail": ""}},
        ),
    )
    cli._stop_session_on_server(base_url="http://source", session_id="native-source")
