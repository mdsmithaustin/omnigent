from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/fork_release.py"
VERSION = "0.16.0+mdsmithaustin.1"
JOBS = ("source", "lint", "python", "web", "addons", "build")
POLICY_FILES = (
    ".github/fork-release.json",
    "scripts/fork_release.py",
    "scripts/build_fork_release.sh",
    ".github/workflows/fork-release.yml",
    "tests/scripts/test_fork_release.py",
    "docs/FORK_RELEASES.md",
    "designs/fork-release/DECISIONS.tsv",
)


def command(root: Path, *args: str) -> str:
    return subprocess.check_output(args, cwd=root, text=True).strip()


def commit(root: Path, message: str) -> str:
    command(root, "git", "add", "-A")
    command(root, "git", "commit", "-qm", message)
    return command(root, "git", "rev-parse", "HEAD")


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")


@pytest.fixture
def release(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    command(root, "git", "init", "-q")
    command(root, "git", "config", "user.email", "fixture@example.com")
    command(root, "git", "config", "user.name", "Release fixture")
    (root / "runtime.py").write_text("stable = True\n")
    (root / ".gitignore").write_text(".cache/\n")
    base = commit(root, "stable")
    (root / "runtime.py").write_text("stable = True\naddon = True\n")
    payload = commit(root, "addon")
    pin = {
        "upstream": {
            "repository": "omnigent-ai/omnigent",
            "tag": "v0.16.0",
            "commit": base,
            "release_id": 42,
            "published_at": "2026-09-29T20:34:22Z",
        },
        "addons": [
            {
                "name": "fixture addon",
                "source_commit": payload,
                "applied_commit": payload,
                "reason": "reviewed fixture",
            }
        ],
        "payload_commit": payload,
        "version": VERSION,
        "profile": "python-distributions-v1",
    }
    for name in POLICY_FILES:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("policy\n")
    write_json(root / ".github/fork-release.json", pin)
    source = commit(root, "complete release policy")
    api_file = tmp_path / "api.json"
    write_json(
        api_file,
        {
            "release": {
                "id": 42,
                "tag_name": "v0.16.0",
                "draft": False,
                "prerelease": False,
                "published_at": "2026-09-29T20:34:22Z",
            },
            "ref": {"object": {"type": "commit", "sha": base}},
        },
    )
    executable = tmp_path / "bin/gh"
    executable.parent.mkdir()
    executable.write_text(
        "#!"
        + sys.executable
        + "\n"
        + """import json, os, pathlib, shutil, sys
state = json.loads(pathlib.Path(os.environ["FIXTURE_API"]).read_text())
args = sys.argv[1:]
with open(os.environ["FIXTURE_CALLS"], "a") as log:
    log.write(json.dumps(args) + "\\n")
if args[0] == "api":
    if state.get("unavailable"):
        sys.exit("HTTP 503 unavailable")
    if "mdsmithaustin/omnigent" in args[1]:
        key = "fork_ref" if "/git/ref/" in args[1] else "fork_release"
        if key not in state:
            sys.exit("HTTP 404 not found")
    else:
        key = "release" if "/releases/" in args[1] else "ref"
    print(json.dumps(state[key]))
elif args[:2] == ["release", "download"]:
    destination = pathlib.Path(args[args.index("--dir") + 1])
    for file in pathlib.Path(os.environ["FIXTURE_EXISTING"]).iterdir():
        shutil.copy2(file, destination / file.name)
elif args[:2] == ["release", "create"]:
    source = args[args.index("--target") + 1]
    state["fork_ref"] = {"object": {"type": "commit", "sha": source}}
    files = [pathlib.Path(name) for name in args[3:args.index("--repo")]]
    destination = pathlib.Path(os.environ["FIXTURE_EXISTING"])
    for file in files:
        shutil.copy2(file, destination / file.name)
    state["fork_release"] = {
        "draft": False, "prerelease": False,
        "assets": [{"name": file.name} for file in files],
    }
    pathlib.Path(os.environ["FIXTURE_API"]).write_text(json.dumps(state))
else:
    sys.exit("unexpected fixture command")
"""
    )
    executable.chmod(0o755)
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("GITHUB_") and not key.startswith("FORK_RELEASE_")
    }
    environment.update(
        PATH=str(executable.parent) + os.pathsep + environment["PATH"],
        FIXTURE_API=str(api_file),
        FIXTURE_CALLS=str(tmp_path / "calls.jsonl"),
    )
    return {
        "root": root,
        "base": base,
        "payload": payload,
        "source": source,
        "pin": pin,
        "env": environment,
        "tmp": tmp_path,
        "api": api_file,
    }


def cli(release, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=cwd or release["root"],
        env=release["env"],
        capture_output=True,
        text=True,
        check=False,
    )


def check(release) -> subprocess.CompletedProcess[str]:
    return cli(release, "check-source", "--source-sha", release["source"])


def amend_pin(release) -> None:
    write_json(release["root"] / ".github/fork-release.json", release["pin"])
    release["source"] = commit(release["root"], "update policy pin")


def test_complete_policy_tail_and_ignored_build_output_pass(release):
    cache = release["root"] / ".cache"
    cache.mkdir()
    (cache / "build").write_text("ignored")
    result = check(release)
    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout)
    assert receipt["source_commit"] == release["source"]
    assert receipt["pin"]["payload_commit"] == release["payload"]
    assert receipt["tree"] == command(release["root"], "git", "rev-parse", "HEAD^{tree}")


@pytest.mark.parametrize(
    "case", ["extra", "omitted", "duplicate", "abbreviated", "allowlist", "profile", "version"]
)
def test_invalid_inventory_is_rejected(release, case):
    if case == "extra":
        root = release["root"]
        command(root, "git", "reset", "--hard", release["payload"])
        (root / "runtime.py").write_text("undeclared = True\n")
        extra = commit(root, "undeclared extra commit")
        command(root, "git", "cherry-pick", release["source"])
        release["pin"]["payload_commit"] = extra
    elif case == "omitted":
        release["pin"]["addons"] = []
    elif case == "duplicate":
        release["pin"]["addons"] *= 2
    elif case == "abbreviated":
        release["pin"]["addons"][0]["applied_commit"] = release["payload"][:8]
    elif case == "allowlist":
        release["pin"]["policy_paths"] = ["runtime.py"]
    elif case == "profile":
        release["pin"]["profile"] = "everything-certified"
    else:
        release["pin"]["version"] = "0.16.0"
    amend_pin(release)
    result = check(release)
    assert result.returncode == 1
    assert "fork release rejected" in result.stderr


@pytest.mark.parametrize(
    "case", ["runtime", "reverted", "rename", "symlink", "submodule", "merge"]
)
def test_every_policy_tail_commit_and_mode_is_checked(release, case):
    root = release["root"]
    if case in {"runtime", "reverted"}:
        (root / "runtime.py").write_text("undeclared = True\n")
        changed = commit(root, "runtime edit after anchor")
        if case == "reverted":
            command(root, "git", "revert", "--no-edit", changed)
    elif case == "rename":
        command(root, "git", "mv", "docs/FORK_RELEASES.md", "undeclared.py")
        commit(root, "cross-boundary rename")
    elif case == "symlink":
        path = root / "scripts/build_fork_release.sh"
        path.unlink()
        path.symlink_to("../runtime.py")
        commit(root, "policy symlink")
    elif case == "submodule":
        path = root / "scripts/build_fork_release.sh"
        path.unlink()
        path.mkdir()
        command(
            root,
            "git",
            "update-index",
            "--cacheinfo",
            "160000," + release["base"] + ",scripts/build_fork_release.sh",
        )
        command(root, "git", "commit", "-qm", "policy submodule")
    else:
        command(root, "git", "checkout", "-qb", "side")
        (root / "docs/FORK_RELEASES.md").write_text("side policy\n")
        commit(root, "side policy")
        command(root, "git", "checkout", "-q", "-")
        (root / "scripts/fork_release.py").write_text("other policy\n")
        commit(root, "other policy")
        command(root, "git", "merge", "--no-ff", "-m", "merge policy", "side")
    release["source"] = command(root, "git", "rev-parse", "HEAD")
    result = check(release)
    assert result.returncode == 1
    assert (
        "undeclared policy-tail path" in result.stderr
        if case in {"runtime", "reverted", "rename"}
        else "unsafe policy-tail mode" in result.stderr
        if case in {"symlink", "submodule"}
        else "policy tail is not linear" in result.stderr
    )


@pytest.mark.parametrize(
    "case", ["draft", "prerelease", "unpublished", "identity", "moved", "unavailable"]
)
def test_stable_release_api_fails_closed(release, case):
    response = json.loads(release["api"].read_text())
    if case in {"draft", "prerelease"}:
        response["release"][case] = True
    elif case == "unpublished":
        response["release"]["published_at"] = None
    elif case == "identity":
        response["release"]["id"] = 43
    elif case == "moved":
        response["ref"]["object"]["sha"] = "f" * 40
    else:
        response["unavailable"] = True
    write_json(release["api"], response)
    result = check(release)
    assert result.returncode == 1
    assert "fork release rejected" in result.stderr


@pytest.mark.parametrize("case", ["tracked", "untracked", "wrong-sha"])
def test_source_checkout_must_be_exact_and_clean(release, case):
    if case == "tracked":
        (release["root"] / "runtime.py").write_text("dirty\n")
    elif case == "untracked":
        (release["root"] / "untracked.py").write_text("dirty\n")
    else:
        release["source"] = release["payload"]
    result = check(release)
    assert result.returncode == 1
    assert (
        "dirty tracked or nonignored source" in result.stderr
        if case != "wrong-sha"
        else "checkout and source SHA differ" in result.stderr
    )


@pytest.fixture
def artifacts(release):
    directory = release["tmp"] / "dist"
    directory.mkdir()
    evidence = release["tmp"] / "evidence"
    evidence.mkdir()
    ui = {"index.html": b"<html>fixture</html>", "assets/app.js": b"window.fixture = true;"}
    import hashlib

    write_json(
        evidence / "ui-inventory.json",
        {name: hashlib.sha256(data).hexdigest() for name, data in ui.items()},
    )
    for package, siblings in {
        "omnigent": ("omnigent-client", "omnigent-ui-sdk", "omnigent-slack"),
        "omnigent-client": ("omnigent",),
        "omnigent-ui-sdk": ("omnigent-client",),
        "omnigent-slack": (),
    }.items():
        normalized = package.replace("-", "_")
        metadata = (
            f"Metadata-Version: 2.4\nName: {package}\nVersion: {VERSION}\n"
            + "".join(f"Requires-Dist: {sibling}=={VERSION}\n" for sibling in siblings)
            + "\n"
        )
        files = {f"{normalized}-{VERSION}.dist-info/METADATA": metadata.encode()}
        if package == "omnigent":
            files.update(
                {"omnigent/server/static/web-ui/" + name: data for name, data in ui.items()}
            )
        with zipfile.ZipFile(directory / f"{normalized}-{VERSION}-py3-none-any.whl", "w") as wheel:
            for name, contents in files.items():
                wheel.writestr(name, contents)
        prefix = f"{normalized}-{VERSION}/"
        files = {prefix + "PKG-INFO": metadata.encode()}
        if package == "omnigent":
            files.update(
                {
                    prefix + "omnigent/server/static/web-ui/" + name: data
                    for name, data in ui.items()
                }
            )
        with tarfile.open(directory / f"{normalized}-{VERSION}.tar.gz", "w:gz") as sdist:
            for name, contents in files.items():
                member = tarfile.TarInfo(name)
                member.size = len(contents)
                sdist.addfile(member, io.BytesIO(contents))
    needs = {
        name: {
            "result": "success",
            "outputs": {"source_sha": release["source"], "run_id": "123", "run_attempt": "1"},
        }
        for name in JOBS
    }
    needs_file = evidence / "needs.json"
    write_json(needs_file, needs)
    return directory, evidence, needs


def certify_cli(release, artifacts):
    directory, evidence, needs = artifacts
    write_json(evidence / "needs.json", needs)
    return cli(
        release,
        "certify",
        "--source-sha",
        release["source"],
        "--run-id",
        "123",
        "--needs-json",
        str(evidence / "needs.json"),
        "--artifacts",
        str(directory),
        "--ui-inventory",
        str(evidence / "ui-inventory.json"),
        "--out",
        str(evidence / "certificate.json"),
    )


def test_certificate_binds_final_source_and_downloaded_bytes(release, artifacts):
    result = certify_cli(release, artifacts)
    assert result.returncode == 0, result.stderr
    directory, evidence, _ = artifacts
    receipt = json.loads((evidence / "certificate.json").read_text())
    assert receipt["source"]["source_commit"] == release["source"]
    assert receipt["source"]["source_commit"] != release["payload"]
    assert len(receipt["artifacts"]) == 8
    assert "Live Prime runtime" in receipt["exclusions"]
    verified = cli(
        release,
        "verify",
        "--certificate",
        str(evidence / "certificate.json"),
        "--artifacts",
        str(directory),
        cwd=release["tmp"],
    )
    assert verified.returncode == 0, verified.stderr
    assert "8 distributions; scoped integrity, not independent approval" in verified.stdout


@pytest.mark.parametrize(
    "case", ["missing", "failed", "skipped", "cancelled", "sha", "run", "attempt", "workflow"]
)
def test_certificate_rejects_missing_or_mixed_job_evidence(release, artifacts, case):
    needs = artifacts[2]
    if case == "missing":
        del needs["python"]
    elif case in {"failed", "skipped", "cancelled"}:
        needs["python"]["result"] = case
    elif case == "workflow":
        release["env"]["GITHUB_SHA"] = "f" * 40
    else:
        needs["python"]["outputs"][
            {"sha": "source_sha", "run": "run_id", "attempt": "run_attempt"}[case]
        ] = "f" * 40 if case == "sha" else "2"
    result = certify_cli(release, artifacts)
    assert result.returncode == 1
    assert not (artifacts[1] / "certificate.json").exists()


@pytest.mark.parametrize("case", ["missing", "ui", "digest", "unsafe-sdist", "sibling-pin"])
def test_artifact_contract_is_enforced(release, artifacts, case):
    directory, evidence, _ = artifacts
    if case == "missing":
        next(directory.glob("*.whl")).unlink()
    elif case == "ui":
        write_json(evidence / "ui-inventory.json", {"index.html": "f" * 64})
    elif case == "unsafe-sdist":
        path = next(directory.glob("*.tar.gz"))
        with tarfile.open(path, "w:gz") as archive:
            member = tarfile.TarInfo("../outside")
            member.size = 1
            archive.addfile(member, io.BytesIO(b"x"))
    elif case == "sibling-pin":
        path = next(directory.glob("omnigent_client-*.whl"))
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(
                f"omnigent_client-{VERSION}.dist-info/METADATA",
                f"Name: omnigent-client\nVersion: {VERSION}\n\n",
            )
    else:
        assert certify_cli(release, artifacts).returncode == 0
        path = next(directory.glob("*.whl"))
        with zipfile.ZipFile(path, "a") as archive:
            archive.writestr("extra.py", "modified")
        result = cli(
            release,
            "verify",
            "--certificate",
            str(evidence / "certificate.json"),
            "--artifacts",
            str(directory),
        )
        assert result.returncode == 1
        assert "artifact digest or metadata differs" in result.stderr
        return
    result = certify_cli(release, artifacts)
    assert result.returncode == 1
    assert not (evidence / "certificate.json").exists()


def prepare_publisher(release, artifacts):
    assert certify_cli(release, artifacts).returncode == 0
    directory, evidence, _ = artifacts
    receipt = json.loads((evidence / "certificate.json").read_text())
    (evidence / "SHA256SUMS").write_text(
        "".join(f"{entry['sha256']}  {entry['filename']}\n" for entry in receipt["artifacts"])
    )
    release["env"].update(
        GITHUB_REPOSITORY="mdsmithaustin/omnigent",
        GITHUB_EVENT_NAME="workflow_dispatch",
        GITHUB_SHA=release["source"],
        GITHUB_RUN_ID="123",
        GITHUB_RUN_ATTEMPT="1",
        FORK_RELEASE_PUBLISH="true",
        FORK_RELEASE_TAG="fork/v" + VERSION,
    )
    return directory, evidence


@pytest.mark.parametrize(
    "case",
    [
        "tag",
        "repository",
        "automatic",
        "disabled",
        "digest",
        "moved-tag",
        "different-assets",
        "run",
    ],
)
def test_publisher_rejects_wrong_target_or_bytes_without_writing(release, artifacts, case):
    directory, evidence = prepare_publisher(release, artifacts)
    state = json.loads(release["api"].read_text())
    if case == "tag":
        release["env"]["FORK_RELEASE_TAG"] = "v0.16.0"
    elif case == "repository":
        release["env"]["GITHUB_REPOSITORY"] = "omnigent-ai/omnigent"
    elif case == "automatic":
        release["env"]["GITHUB_EVENT_NAME"] = "push"
    elif case == "disabled":
        release["env"]["FORK_RELEASE_PUBLISH"] = "false"
    elif case == "run":
        release["env"]["GITHUB_RUN_ID"] = "456"
    elif case == "digest":
        path = next(directory.glob("*.whl"))
        with zipfile.ZipFile(path, "a") as archive:
            archive.writestr("modified.py", "x")
    else:
        state["fork_ref"] = {
            "object": {
                "type": "commit",
                "sha": "f" * 40 if case == "moved-tag" else release["source"],
            }
        }
        if case == "different-assets":
            state["fork_release"] = {"draft": False, "prerelease": False, "assets": []}
        write_json(release["api"], state)
    result = cli(
        release,
        "publish",
        "--certificate",
        str(evidence / "certificate.json"),
        "--artifacts",
        str(directory),
        "--evidence",
        str(evidence),
    )
    assert result.returncode == 1
    calls = [
        json.loads(line) for line in Path(release["env"]["FIXTURE_CALLS"]).read_text().splitlines()
    ]
    assert [call for call in calls if call[:2] == ["release", "create"]] == []


def test_matching_existing_release_is_idempotent(release, artifacts):
    directory, evidence = prepare_publisher(release, artifacts)
    existing = release["tmp"] / "existing"
    existing.mkdir()
    files = list(directory.iterdir()) + [
        evidence / name for name in ("certificate.json", "ui-inventory.json", "SHA256SUMS")
    ]
    for path in files:
        shutil.copy2(path, existing / path.name)
    state = json.loads(release["api"].read_text())
    state["fork_ref"] = {"object": {"type": "commit", "sha": release["source"]}}
    state["fork_release"] = {
        "draft": False,
        "prerelease": False,
        "assets": [{"name": path.name} for path in files],
    }
    write_json(release["api"], state)
    release["env"]["FIXTURE_EXISTING"] = str(existing)
    result = cli(
        release,
        "publish",
        "--certificate",
        str(evidence / "certificate.json"),
        "--artifacts",
        str(directory),
        "--evidence",
        str(evidence),
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "Existing fork release matches certified bytes\n"


def test_new_release_promotes_checked_files_and_verifies_downloads(release, artifacts):
    directory, evidence = prepare_publisher(release, artifacts)
    existing = release["tmp"] / "new-assets"
    existing.mkdir()
    release["env"]["FIXTURE_EXISTING"] = str(existing)
    result = cli(
        release,
        "publish",
        "--certificate",
        str(evidence / "certificate.json"),
        "--artifacts",
        str(directory),
        "--evidence",
        str(evidence),
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "Published certified fork bytes\n"
    for path in [
        *directory.iterdir(),
        *(evidence / name for name in ("certificate.json", "ui-inventory.json", "SHA256SUMS")),
    ]:
        assert (existing / path.name).read_bytes() == path.read_bytes()
