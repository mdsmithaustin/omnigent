"""Verify the pinned fork source and its Python distribution certificate."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import tarfile
import tempfile
import zipfile
from dataclasses import asdict, dataclass, replace
from email.parser import BytesParser
from pathlib import Path, PurePosixPath
from typing import Literal

import tomllib

UPSTREAM = "omnigent-ai/omnigent"
FORK = "mdsmithaustin/omnigent"
PROFILE = "python-distributions-v1"
MANIFEST = ".github/fork-release.json"
POLICY_PATHS = frozenset(
    {
        MANIFEST,
        "scripts/fork_release.py",
        "scripts/build_fork_release.sh",
        ".github/workflows/fork-release.yml",
        "tests/scripts/test_fork_release.py",
        "docs/FORK_RELEASES.md",
        "designs/fork-release/DECISIONS.tsv",
    }
)
REQUIRED_JOBS = ("source", "lint", "python", "web", "addons", "build")
PACKAGES = {
    "omnigent": ("omnigent-client", "omnigent-ui-sdk", "omnigent-slack"),
    "omnigent-client": ("omnigent",),
    "omnigent-ui-sdk": ("omnigent-client",),
    "omnigent-slack": (),
}
CLAIMS = (
    "Inventoried stable upstream source and fork addons",
    "Successful fixed source, lint, Python, web, addon, and build checks",
    "Four Python distributions installed from wheels and rebuilt sdists",
    "Bundled UI matches the recorded filename and SHA256 inventory",
)
EXCLUSIONS = (
    "Independent third-party approval",
    "Live Prime runtime",
    "Desktop and mobile applications",
    "Docker and provider images",
    "Byte-identical rebuilds",
)
UI_PREFIX = "omnigent/server/static/web-ui/"


class ReleaseError(ValueError):
    """The source or certificate does not meet release policy."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ReleaseError(message)


def sha(value: object) -> str:
    require(
        isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value) is not None,
        "expected a full lowercase Git SHA",
    )
    return str(value)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()


def write_json(path: Path, value: object) -> None:
    path.write_bytes(canonical(value))


def run(*args: str, cwd: Path | None = None) -> str:
    result = subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=False)
    require(result.returncode == 0, f"{' '.join(args[:3])} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def git(root: Path, *args: str) -> str:
    return run("git", "-C", str(root), *args)


def api(path: str) -> object:
    return json.loads(run("gh", "api", path))


@dataclass(frozen=True)
class PublishedStable:
    repository: str
    tag: str
    commit: str
    release_id: int
    published_at: str


@dataclass(frozen=True)
class AddonCommit:
    name: str
    source_commit: str | None
    applied_commit: str
    reason: str


@dataclass(frozen=True)
class ReleasePin:
    upstream: PublishedStable
    addons: tuple[AddonCommit, ...]
    payload_commit: str
    version: str
    profile: Literal["python-distributions-v1"]


@dataclass(frozen=True)
class CheckedSource:
    source_commit: str
    tree: str
    manifest_sha256: str
    pin: ReleasePin


def read_pin(raw: bytes) -> ReleasePin:
    data = json.loads(raw)
    require(
        set(data) == {"upstream", "addons", "payload_commit", "version", "profile"},
        "unexpected release pin fields",
    )
    upstream = PublishedStable(**data["upstream"])
    require(upstream.repository == UPSTREAM, "wrong upstream repository")
    require(
        re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", upstream.tag) is not None,
        "upstream tag must be stable",
    )
    sha(upstream.commit)
    require(
        type(upstream.release_id) is int
        and upstream.release_id > 0
        and bool(upstream.published_at),
        "missing published release identity",
    )
    require(
        re.fullmatch(
            re.escape(upstream.tag[1:]) + r"\+mdsmithaustin\.[1-9][0-9]*", data["version"]
        )
        is not None,
        "wrong fork version",
    )
    require(data["profile"] == PROFILE, "unsupported certification profile")
    addons = tuple(AddonCommit(**entry) for entry in data["addons"])
    for addon in addons:
        sha(addon.applied_commit)
        if addon.source_commit is not None:
            sha(addon.source_commit)
        require(bool(addon.name) and bool(addon.reason), "addon needs a name and review reason")
    require(
        len({addon.applied_commit for addon in addons}) == len(addons), "duplicate applied addon"
    )
    require(
        len({addon.source_commit for addon in addons if addon.source_commit})
        == len([addon for addon in addons if addon.source_commit]),
        "duplicate source addon",
    )
    return ReleasePin(upstream, addons, sha(data["payload_commit"]), data["version"], PROFILE)


def check_upstream(upstream: PublishedStable) -> None:
    release = api(f"repos/{UPSTREAM}/releases/tags/{upstream.tag}")
    require(isinstance(release, dict), "invalid release response")
    require(
        release.get("id") == upstream.release_id
        and release.get("tag_name") == upstream.tag
        and release.get("draft") is False
        and release.get("prerelease") is False
        and release.get("published_at") == upstream.published_at,
        "upstream is not the pinned published stable release",
    )
    ref = api(f"repos/{UPSTREAM}/git/ref/tags/{upstream.tag}")
    target = ref["object"]
    seen = set()
    while target["type"] == "tag":
        target_sha = sha(target["sha"])
        require(target_sha not in seen, "cyclic upstream tag")
        seen.add(target_sha)
        target = api(f"repos/{UPSTREAM}/git/tags/{target_sha}")["object"]
    require(
        target["type"] == "commit" and sha(target["sha"]) == upstream.commit, "upstream tag moved"
    )


def read_manifest(root: Path, source: str) -> tuple[bytes, ReleasePin]:
    raw = subprocess.check_output(["git", "-C", str(root), "show", f"{source}:{MANIFEST}"])
    return raw, read_pin(raw)


def policy_violation(root: Path, previous: str, commit: str) -> str | None:
    changed = subprocess.check_output(
        [
            "git",
            "-C",
            str(root),
            "diff-tree",
            "--no-commit-id",
            "--raw",
            "-r",
            "--no-renames",
            "-z",
            previous,
            commit,
        ]
    ).split(b"\0")
    for index in range(0, len(changed) - 1, 2):
        modes = changed[index].decode().split()
        path = changed[index + 1].decode()
        if path not in POLICY_PATHS:
            return f"undeclared policy-tail path: {path}"
        if not (
            modes[0][1:] in {"000000", "100644", "100755"}
            and modes[1] in {"000000", "100644", "100755"}
        ):
            return f"unsafe policy-tail mode: {path}"
    return None


@dataclass(frozen=True)
class TailCommit:
    commit: str
    violation: str | None


@dataclass(frozen=True)
class SourceLine:
    """Upstream tag, inventoried addons through payload_commit, then the tail."""

    pin: ReleasePin
    tail: tuple[TailCommit, ...]

    @property
    def pending(self) -> tuple[str, ...]:
        return tuple(entry.commit for entry in self.tail if entry.violation)


def trace_tail(root: Path, source: str, pin: ReleasePin) -> tuple[TailCommit, ...]:
    expected = [addon.applied_commit for addon in pin.addons]
    actual = git(
        root, "rev-list", "--reverse", f"{pin.upstream.commit}..{pin.payload_commit}"
    ).splitlines()
    require(actual == expected, "addon history differs from ordered inventory")
    require(
        pin.payload_commit == (expected[-1] if expected else pin.upstream.commit),
        "payload anchor differs from inventory",
    )
    previous = pin.upstream.commit
    for addon in pin.addons:
        require(
            git(root, "rev-list", "--parents", "-n", "1", addon.applied_commit).split()
            == [addon.applied_commit, previous],
            "addon is not a linear successor",
        )
        if addon.source_commit:
            require(
                git(root, "cat-file", "-t", addon.source_commit) == "commit",
                "source addon object is missing",
            )
        previous = addon.applied_commit
    git(root, "merge-base", "--is-ancestor", pin.payload_commit, source)
    tail = []
    for commit in git(
        root, "rev-list", "--reverse", f"{pin.payload_commit}..{source}"
    ).splitlines():
        require(
            git(root, "rev-list", "--parents", "-n", "1", commit).split() == [commit, previous],
            "policy tail is not linear",
        )
        tail.append(TailCommit(commit, policy_violation(root, previous, commit)))
        previous = commit
    return tuple(tail)


def check_line(root: Path, source: str) -> SourceLine:
    sha(source)
    _, pin = read_manifest(root, source)
    upstream = pin.upstream.commit
    ancestor = subprocess.run(
        ["git", "-C", str(root), "merge-base", "--is-ancestor", upstream, source],
        capture_output=True,
        check=False,
    )
    require(ancestor.returncode == 0, f"upstream commit {upstream} is not an ancestor")
    merges = git(root, "rev-list", "--merges", f"{upstream}..{source}").split()
    require(not merges, f"merge commit after upstream: {' '.join(merges)}")
    return SourceLine(pin, trace_tail(root, source, pin))


def check_source(root: Path, source: str) -> CheckedSource:
    sha(source)
    require(git(root, "rev-parse", "HEAD") == source, "checkout and source SHA differ")
    require(
        not git(root, "status", "--porcelain", "--untracked-files=all"),
        "dirty tracked or nonignored source",
    )
    raw, pin = read_manifest(root, source)
    check_upstream(pin.upstream)
    for entry in trace_tail(root, source, pin):
        if entry.violation:
            raise ReleaseError(entry.violation)
    return CheckedSource(
        source, sha(git(root, "rev-parse", f"{source}^{{tree}}")), digest(raw), pin
    )


def inventoried_addon(root: Path, commit: str) -> AddonCommit:
    subject = git(root, "log", "-1", "--format=%s", commit)
    slug = re.sub(r"[^a-z0-9]+", "-", subject.lower()).strip("-")[:48].rstrip("-")
    return AddonCommit(f"{slug}-{commit[:12]}", None, commit, subject)


def inventory(root: Path, version: str) -> ReleasePin:
    """Extend the inventory through the last pending commit at HEAD."""
    source = git(root, "rev-parse", "HEAD")
    line = check_line(root, source)
    project = tomllib.loads(git(root, "show", f"{source}:pyproject.toml"))["project"]["version"]
    runtime = re.findall(
        r'^VERSION = "([^"]*)"$', git(root, "show", f"{source}:omnigent/version.py"), re.MULTILINE
    )
    require(
        project == version and runtime == [version],
        f"pyproject and runtime version must already be {version}; "
        "stamp it with scripts/update_versions.py first",
    )
    pending = [index for index, entry in enumerate(line.tail) if entry.violation]
    added = line.tail[: pending[-1] + 1] if pending else ()
    pin = replace(
        line.pin,
        addons=(*line.pin.addons, *(inventoried_addon(root, entry.commit) for entry in added)),
        payload_commit=added[-1].commit if added else line.pin.payload_commit,
        version=version,
    )
    raw = canonical(asdict(pin))
    read_pin(raw)
    (root / MANIFEST).write_bytes(raw)
    return pin


def inventory_ui(directory: Path) -> dict[str, str]:
    entries = {}
    for path in sorted(directory.rglob("*")):
        require(not path.is_symlink(), "UI inventory contains a symlink")
        if path.is_file():
            entries[path.relative_to(directory).as_posix()] = digest(path.read_bytes())
    require(
        "index.html" in entries and any(name.startswith("assets/") for name in entries),
        "UI bundle is empty or incomplete",
    )
    return entries


def safe_archive_path(name: str) -> None:
    pure = PurePosixPath(name)
    require(
        not pure.is_absolute() and ".." not in pure.parts and "\\" not in name,
        "unsafe archive path",
    )


def archive_contents(path: Path) -> dict[str, bytes]:
    contents = {}
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            for entry in archive.infolist():
                safe_archive_path(entry.filename)
                require(
                    (entry.external_attr >> 16) & 0o170000 != 0o120000, "wheel contains a symlink"
                )
                if not entry.is_dir():
                    require(entry.filename not in contents, "duplicate wheel path")
                    contents[entry.filename] = archive.read(entry)
    else:
        with tarfile.open(path) as archive:
            for entry in archive.getmembers():
                safe_archive_path(entry.name)
                require(entry.isfile() or entry.isdir(), "sdist contains a link or special file")
                if entry.isfile():
                    file = archive.extractfile(entry)
                    require(file is not None, "unreadable sdist member")
                    require(entry.name not in contents, "duplicate sdist path")
                    contents[entry.name] = file.read()
    return contents


def inspect_artifacts(
    directory: Path, version: str, ui: dict[str, str]
) -> list[dict[str, object]]:
    require(
        "index.html" in ui and any(name.startswith("assets/") for name in ui),
        "UI inventory needs index.html and assets",
    )
    artifacts = []
    inventory = set()
    paths = sorted(directory.iterdir())
    require(
        len(paths) == 8
        and all(
            path.is_file()
            and not path.is_symlink()
            and (path.name.endswith(".whl") or path.name.endswith(".tar.gz"))
            for path in paths
        ),
        "require exactly four wheels and four sdists",
    )
    for path in paths:
        files = archive_contents(path)
        kind = "wheel" if path.suffix == ".whl" else "sdist"
        metadata_files = [
            name
            for name in files
            if name.endswith(".dist-info/METADATA")
            or (kind == "sdist" and name.count("/") == 1 and name.endswith("/PKG-INFO"))
        ]
        require(len(metadata_files) == 1, "artifact needs one distribution metadata record")
        metadata = BytesParser().parsebytes(files[metadata_files[0]])
        package = str(metadata["Name"]).replace("_", "-").lower()
        require(
            package in PACKAGES and metadata["Version"] == version,
            "artifact package or version differs",
        )
        require((package, kind) not in inventory, "duplicate distribution artifact")
        inventory.add((package, kind))
        requirements = metadata.get_all("Requires-Dist", [])
        for sibling in PACKAGES[package]:
            require(
                any(
                    re.match(
                        re.escape(sibling) + r"\s*==\s*" + re.escape(version) + r"(?:\s*;|$)",
                        requirement,
                    )
                    for requirement in requirements
                ),
                f"missing exact sibling pin: {sibling}",
            )
        if package == "omnigent":
            packaged = {}
            for name, contents in files.items():
                relative = name.split("/", 1)[1] if kind == "sdist" else name
                if relative.startswith(UI_PREFIX):
                    packaged[relative[len(UI_PREFIX) :]] = digest(contents)
            require(packaged == ui, "packaged UI inventory differs")
        artifacts.append(
            {
                "filename": path.name,
                "package": package,
                "kind": kind,
                "sha256": digest(path.read_bytes()),
                "size": path.stat().st_size,
            }
        )
    require(
        inventory == {(package, kind) for package in PACKAGES for kind in ("wheel", "sdist")},
        "incomplete distribution inventory",
    )
    return artifacts


def check_jobs(needs: object, source: str, run_id: str, run_attempt: str) -> list[dict[str, str]]:
    require(
        isinstance(needs, dict) and set(needs) == set(REQUIRED_JOBS),
        "missing or unexpected required jobs",
    )
    checks = []
    for name in REQUIRED_JOBS:
        job = needs[name]
        require(job.get("result") == "success", f"required job did not succeed: {name}")
        require(
            job.get("outputs", {}).get("source_sha") == source
            and job.get("outputs", {}).get("run_id") == run_id
            and job.get("outputs", {}).get("run_attempt") == run_attempt,
            f"mixed source or run in job: {name}",
        )
        checks.append(
            {
                "name": name,
                "source_commit": source,
                "run_id": run_id,
                "run_attempt": run_attempt,
                "result": "success",
            }
        )
    return checks


def certify(
    root: Path, source: str, directory: Path, needs: object, run_id: str, ui_path: Path
) -> dict[str, object]:
    require(re.fullmatch(r"[1-9][0-9]*", run_id) is not None, "invalid workflow run ID")
    run_attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "1")
    require(re.fullmatch(r"[1-9][0-9]*", run_attempt) is not None, "invalid workflow run attempt")
    require(
        os.environ.get("GITHUB_RUN_ID", run_id) == run_id
        and os.environ.get("GITHUB_REPOSITORY", FORK) == FORK,
        "workflow run/repository mismatch",
    )
    checked = check_source(root, source)
    require(
        os.environ.get("GITHUB_SHA", source) == source, "workflow policy and source SHA differ"
    )
    ui_raw = ui_path.read_bytes()
    ui = json.loads(ui_raw)
    require(ui_raw == canonical(ui), "UI inventory must use canonical JSON")
    require(
        isinstance(ui, dict)
        and ui
        and all(
            isinstance(name, str)
            and isinstance(value, str)
            and re.fullmatch(r"[0-9a-f]{64}", value)
            for name, value in ui.items()
        ),
        "invalid UI inventory",
    )
    return {
        "schema": 1,
        "profile": PROFILE,
        "repository": FORK,
        "source": asdict(checked),
        "verifier_commit": source,
        "workflow": ".github/workflows/fork-release.yml",
        "run_id": run_id,
        "run_attempt": run_attempt,
        "checks": check_jobs(needs, source, run_id, run_attempt),
        "artifacts": inspect_artifacts(directory, checked.pin.version, ui),
        "ui_inventory": ui,
        "ui_inventory_sha256": digest(ui_raw),
        "claims": list(CLAIMS),
        "exclusions": list(EXCLUSIONS),
    }


def verify_certificate(certificate: Path, directory: Path) -> dict[str, object]:
    receipt = json.loads(certificate.read_bytes())
    require(
        receipt["schema"] == 1 and receipt["profile"] == PROFILE and receipt["repository"] == FORK,
        "unsupported certificate",
    )
    require(
        re.fullmatch(r"[1-9][0-9]*", receipt["run_id"]) is not None
        and re.fullmatch(r"[1-9][0-9]*", receipt["run_attempt"]) is not None,
        "invalid certificate run identity",
    )
    source = receipt["source"]
    sha(source["source_commit"])
    sha(source["tree"])
    require(
        re.fullmatch(r"[0-9a-f]{64}", source["manifest_sha256"]) is not None,
        "invalid manifest digest",
    )
    pin = read_pin(canonical(source["pin"]))
    require(
        receipt["verifier_commit"] == source["source_commit"]
        and receipt["workflow"] == ".github/workflows/fork-release.yml",
        "certificate policy/source mismatch",
    )
    needs = {
        check["name"]: {
            "result": check["result"],
            "outputs": {
                "source_sha": check["source_commit"],
                "run_id": check["run_id"],
                "run_attempt": check["run_attempt"],
            },
        }
        for check in receipt["checks"]
    }
    require(
        len(receipt["checks"]) == len(REQUIRED_JOBS), "duplicate or missing certificate checks"
    )
    check_jobs(needs, source["source_commit"], receipt["run_id"], receipt["run_attempt"])
    require(
        receipt["claims"] == list(CLAIMS) and receipt["exclusions"] == list(EXCLUSIONS),
        "certificate claim scope differs",
    )
    require(
        digest(canonical(receipt["ui_inventory"])) == receipt["ui_inventory_sha256"],
        "UI inventory digest differs",
    )
    require(
        inspect_artifacts(directory, pin.version, receipt["ui_inventory"]) == receipt["artifacts"],
        "artifact digest or metadata differs",
    )
    return receipt


def optional_api(path: str) -> object | None:
    result = subprocess.run(["gh", "api", path], capture_output=True, text=True, check=False)
    if result.returncode and "HTTP 404" in result.stderr:
        return None
    require(result.returncode == 0, f"GitHub lookup failed: {result.stderr.strip()}")
    return json.loads(result.stdout)


def publish(certificate: Path, directory: Path, evidence: Path) -> None:
    receipt = verify_certificate(certificate, directory)
    source = receipt["source"]["source_commit"]
    require(
        os.environ.get("GITHUB_REPOSITORY") == FORK
        and os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
        and os.environ.get("FORK_RELEASE_PUBLISH") == "true",
        "publication requires explicit manual fork dispatch",
    )
    require(
        os.environ.get("GITHUB_SHA") == source
        and os.environ.get("GITHUB_RUN_ID") == receipt["run_id"]
        and os.environ.get("GITHUB_RUN_ATTEMPT") == receipt["run_attempt"],
        "publisher source/run mismatch",
    )
    root = Path(git(Path.cwd(), "rev-parse", "--show-toplevel"))
    require(
        canonical(asdict(check_source(root, source))) == canonical(receipt["source"]),
        "published source pin differs",
    )
    require(certificate.name == "certificate.json", "unexpected certificate asset name")
    require(
        certificate.parent.resolve() == evidence.resolve(),
        "certificate must be in evidence directory",
    )
    require(
        (evidence / "ui-inventory.json").read_bytes() == canonical(receipt["ui_inventory"]),
        "published UI evidence differs",
    )
    checksum = "".join(
        f"{entry['sha256']}  {entry['filename']}\n" for entry in receipt["artifacts"]
    )
    require((evidence / "SHA256SUMS").read_text() == checksum, "published checksums differ")
    tag = "fork/v" + receipt["source"]["pin"]["version"]
    require(os.environ.get("FORK_RELEASE_TAG") == tag, "requested fork tag differs")
    ref = optional_api(f"repos/{FORK}/git/ref/tags/{tag}")
    if ref is not None:
        require(
            ref["object"]["type"] == "commit" and ref["object"]["sha"] == source,
            "existing fork tag targets another source",
        )
    release = optional_api(f"repos/{FORK}/releases/tags/{tag}")
    files = [
        *sorted(directory.iterdir()),
        certificate,
        evidence / "ui-inventory.json",
        evidence / "SHA256SUMS",
    ]
    existed = release is not None
    if not existed:
        run(
            "gh",
            "release",
            "create",
            tag,
            *(str(path) for path in files),
            "--repo",
            FORK,
            "--target",
            source,
            "--title",
            tag,
            "--notes",
            "Certified Python distributions and bundled UI. "
            "See certificate.json for scope and checks.",
        )
        ref = api(f"repos/{FORK}/git/ref/tags/{tag}")
        release = api(f"repos/{FORK}/releases/tags/{tag}")
    require(
        ref is not None and ref["object"]["type"] == "commit" and ref["object"]["sha"] == source,
        "published tag/source mismatch",
    )
    require(not release["draft"] and not release["prerelease"], "existing release state differs")
    require(
        {asset["name"] for asset in release["assets"]} == {path.name for path in files},
        "existing release assets differ",
    )
    with tempfile.TemporaryDirectory(prefix="fork-release-assets-") as tmp:
        run("gh", "release", "download", tag, "--repo", FORK, "--dir", tmp)
        require(
            all((Path(tmp) / path.name).read_bytes() == path.read_bytes() for path in files),
            "existing release asset bytes differ",
        )
    print(
        "Existing fork release matches certified bytes"
        if existed
        else "Published certified fork bytes"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("check-source", "check-line"):
        commands.add_parser(name).add_argument("--source-sha", required=True)
    commands.add_parser("inventory").add_argument("--version", required=True)
    cert = commands.add_parser("certify")
    cert.add_argument("--source-sha", required=True)
    cert.add_argument("--needs-json", type=Path, required=True)
    cert.add_argument("--run-id", required=True)
    cert.add_argument("--ui-inventory", type=Path, required=True)
    cert.add_argument("--out", type=Path, required=True)
    for name in ("verify", "publish"):
        command = commands.add_parser(name)
        command.add_argument("--certificate", type=Path, required=True)
        command.add_argument("--artifacts", type=Path, required=True)
        if name == "publish":
            command.add_argument("--evidence", type=Path, required=True)
    cert.add_argument("--artifacts", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command in {"check-source", "check-line", "inventory", "certify"}:
            root = Path(git(Path.cwd(), "rev-parse", "--show-toplevel"))
        if args.command == "check-source":
            print(json.dumps(asdict(check_source(root, args.source_sha)), sort_keys=True))
        elif args.command == "check-line":
            line = check_line(root, args.source_sha)
            report = {
                "upstream_tag": line.pin.upstream.tag,
                "inventoried": len(line.pin.addons),
                "pending": list(line.pending),
                "release_ready": not line.pending,
            }
            print(json.dumps(report, sort_keys=True))
        elif args.command == "inventory":
            pin = inventory(root, args.version)
            print(f"Inventoried {len(pin.addons)} addons through {pin.payload_commit}")
        elif args.command == "certify":
            receipt = certify(
                root,
                args.source_sha,
                args.artifacts,
                json.loads(args.needs_json.read_bytes()),
                args.run_id,
                args.ui_inventory,
            )
            write_json(args.out, receipt)
            print(f"Certified {args.source_sha}: 8 distributions")
        elif args.command == "verify":
            receipt = verify_certificate(args.certificate, args.artifacts)
            print(
                f"Verified {receipt['source']['source_commit']}: "
                "8 distributions; scoped integrity, not independent approval"
            )
        else:
            publish(args.certificate, args.artifacts, args.evidence)
    except (
        ReleaseError,
        OSError,
        ValueError,
        KeyError,
        TypeError,
        subprocess.CalledProcessError,
    ) as exc:
        parser.exit(1, f"fork release rejected: {exc}\n")


if __name__ == "__main__":
    main()
