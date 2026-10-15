"""The fork's release channel: certified builds published as GitHub Releases.

Fork builds (``X.Y.Z+mdsmithaustin.N``) are never published to a package
index. Each one is a GitHub Release tagged ``mdsmithaustin/vX.Y.Z-rN``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from omnigent.installation_defaults import (
    RELEASE_REPOSITORY,
    RELEASE_TAG_PREFIX,
    RELEASE_VERSION_LABEL,
)

if TYPE_CHECKING:
    from packaging.version import Version

RELEASES_URL = f"https://github.com/{RELEASE_REPOSITORY}/releases"
INSTALL_DOC_URL = (
    f"https://github.com/{RELEASE_REPOSITORY}/blob/main/docs/FORK_RELEASES.md"
    "#verify-and-install-downloaded-distributions"
)
_API_URL = f"https://api.github.com/repos/{RELEASE_REPOSITORY}/releases"
# The newest releases come first, so one page covers the latest by a wide margin.
_PAGE_SIZE = 100
_TIMEOUT_SECONDS = 3.0
_TAG_PATTERN = re.compile(rf"^{re.escape(RELEASE_TAG_PREFIX)}(\d+\.\d+\.\d+)-r([1-9]\d*)$")
_LOCAL_PATTERN = re.compile(rf"^{re.escape(RELEASE_VERSION_LABEL)}\.[1-9]\d*$")


@dataclass(frozen=True)
class ForkRelease:
    """A published fork release.

    :param version: e.g. ``Version("0.18.0+mdsmithaustin.2")``.
    :param tag: e.g. ``"mdsmithaustin/v0.18.0-r2"``.
    :param html_url: The release page.
    :param assets: Asset file name to download URL.
    """

    version: Version
    tag: str
    html_url: str
    assets: Mapping[str, str]


def is_fork_version(version: str) -> bool:
    """Return whether *version* is a fork build, e.g. ``0.18.0+mdsmithaustin.2``."""
    from packaging.version import InvalidVersion, Version

    try:
        local = Version(version).local
    except InvalidVersion:
        return False
    return local is not None and _LOCAL_PATTERN.match(local) is not None


def release_url(version: str) -> str:
    """Return the release page for fork *version*, e.g. ``0.18.0+mdsmithaustin.2``."""
    from packaging.version import Version

    parsed = Version(version)
    revision = (parsed.local or "").rpartition(".")[2]
    return f"{RELEASES_URL}/tag/{RELEASE_TAG_PREFIX}{parsed.public}-r{revision}"


def _version_for_tag(tag: str) -> Version | None:
    from packaging.version import Version

    match = _TAG_PATTERN.match(tag)
    if match is None:
        return None
    return Version(f"{match.group(1)}+{RELEASE_VERSION_LABEL}.{match.group(2)}")


def parse_fork_releases(payload: object) -> list[ForkRelease]:
    """Parse a GitHub ``list releases`` response into published fork releases.

    Drafts, prereleases, malformed entries and tags outside the fork's tag
    scheme are skipped.
    """
    if not isinstance(payload, list):
        return []
    releases: list[ForkRelease] = []
    for entry in payload:
        if not isinstance(entry, dict) or entry.get("draft") or entry.get("prerelease"):
            continue
        tag = entry.get("tag_name")
        html_url = entry.get("html_url")
        if not isinstance(tag, str) or not isinstance(html_url, str):
            continue
        version = _version_for_tag(tag)
        if version is None:
            continue
        assets = {
            asset["name"]: asset["browser_download_url"]
            for asset in entry.get("assets") or []
            if isinstance(asset, dict)
            and isinstance(asset.get("name"), str)
            and isinstance(asset.get("browser_download_url"), str)
        }
        releases.append(ForkRelease(version, tag, html_url, assets))
    return releases


def latest_fork_release(
    *, timeout: float = _TIMEOUT_SECONDS, attempts: int = 1
) -> ForkRelease | None:
    """Fetch the newest published fork release, or ``None`` on any failure.

    Never raises, so the detached update refresh can call it. Transient
    network errors retry until *attempts* is exhausted; a response is final.
    """
    import httpx

    response = None
    for _ in range(max(1, attempts)):
        try:
            response = httpx.get(
                f"{_API_URL}?per_page={_PAGE_SIZE}",
                headers={
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                timeout=timeout,
                follow_redirects=True,
            )
            break
        except httpx.HTTPError:
            response = None
    if response is None or response.status_code != 200:
        return None
    try:
        payload = response.json()
    except ValueError:
        return None
    releases = parse_fork_releases(payload)
    return max(releases, key=lambda release: release.version) if releases else None
