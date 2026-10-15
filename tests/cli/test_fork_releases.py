"""Tests for :mod:`omnigent.fork_releases` (the fork's GitHub Releases channel)."""

from __future__ import annotations

import httpx
import pytest
from packaging.version import Version

from omnigent.fork_releases import (
    ForkRelease,
    is_fork_version,
    latest_fork_release,
    parse_fork_releases,
)

_RELEASES_URL = "https://api.github.com/repos/mdsmithaustin/omnigent/releases"


def _release(tag: str, *, draft: bool = False, prerelease: bool = False) -> dict[str, object]:
    return {
        "tag_name": tag,
        "html_url": f"https://github.com/mdsmithaustin/omnigent/releases/tag/{tag}",
        "draft": draft,
        "prerelease": prerelease,
        "assets": [
            {
                "name": "SHA256SUMS",
                "browser_download_url": f"https://example.test/{tag}/SHA256SUMS",
            },
            {"name": "broken-asset"},
        ],
    }


class _FakeResponse:
    def __init__(self, status_code: int, body: object) -> None:
        self.status_code = status_code
        self._body = body

    def json(self) -> object:
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def test_parse_maps_tags_to_versions_and_skips_unpublished_or_foreign() -> None:
    payload = [
        _release("mdsmithaustin/v0.18.0-r2"),
        _release("mdsmithaustin/v0.18.0-r3", draft=True),
        _release("mdsmithaustin/v0.18.0-r4", prerelease=True),
        _release("v0.18.0"),
        _release("fork/v0.17.0+mdsmithaustin.3"),
        _release("mdsmithaustin/v0.18.0-r0"),
        "not a release",
        {"tag_name": 7},
    ]

    releases = parse_fork_releases(payload)

    assert releases == [
        ForkRelease(
            version=Version("0.18.0+mdsmithaustin.2"),
            tag="mdsmithaustin/v0.18.0-r2",
            html_url="https://github.com/mdsmithaustin/omnigent/releases/tag/mdsmithaustin/v0.18.0-r2",
            assets={"SHA256SUMS": "https://example.test/mdsmithaustin/v0.18.0-r2/SHA256SUMS"},
        )
    ]


def test_parse_rejects_non_list_payload() -> None:
    assert parse_fork_releases({"message": "Not Found"}) == []


def test_latest_selects_highest_pep440_revision(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = [
        _release("mdsmithaustin/v0.18.0-r9"),
        _release("mdsmithaustin/v0.18.0-r10"),
        _release("mdsmithaustin/v0.17.0-r12"),
    ]
    seen: dict[str, object] = {}

    def _get(url: str, **kwargs: object) -> _FakeResponse:
        seen["url"] = url
        seen["timeout"] = kwargs.get("timeout")
        return _FakeResponse(200, payload)

    monkeypatch.setattr(httpx, "get", _get)

    release = latest_fork_release()

    assert release is not None
    assert str(release.version) == "0.18.0+mdsmithaustin.10"
    assert release.tag == "mdsmithaustin/v0.18.0-r10"
    assert seen == {"url": f"{_RELEASES_URL}?per_page=100", "timeout": 3.0}


@pytest.mark.parametrize(
    "outcome",
    [
        httpx.ConnectError("offline"),
        _FakeResponse(403, {"message": "API rate limit exceeded"}),
        _FakeResponse(200, ValueError("not json")),
        _FakeResponse(200, []),
    ],
    ids=["network", "rate-limited", "bad-json", "no-releases"],
)
def test_latest_returns_none_on_any_failure(
    monkeypatch: pytest.MonkeyPatch, outcome: object
) -> None:
    def _get(_url: str, **_kwargs: object) -> object:
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(httpx, "get", _get)

    assert latest_fork_release() is None


def test_latest_retries_transient_errors_when_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    def _get(_url: str, **_kwargs: object) -> _FakeResponse:
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ReadTimeout("slow")
        return _FakeResponse(200, [_release("mdsmithaustin/v0.18.0-r2")])

    monkeypatch.setattr(httpx, "get", _get)

    release = latest_fork_release(timeout=10.0, attempts=2)

    assert release is not None and release.tag == "mdsmithaustin/v0.18.0-r2"
    assert len(calls) == 2


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("0.18.0+mdsmithaustin.2", True),
        ("0.18.0+mdsmithaustin.10", True),
        ("0.18.0", False),
        ("0.18.0+local.2", False),
        ("0.18.0+mdsmithaustin", False),
        ("not-a-version", False),
    ],
)
def test_is_fork_version(version: str, expected: bool) -> None:
    assert is_fork_version(version) is expected
