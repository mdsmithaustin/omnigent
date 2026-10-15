# Build and install certified fork releases

The `python-distributions-v1` profile verifies the released upstream source, ordered fork addons, required checks, and the eight Python distribution files. It also binds the bundled UI filenames and SHA256 hashes. It covers `omnigent`, `omnigent-client`, `omnigent-ui-sdk`, and `omnigent-slack` at one version.

The stamped version in [`pyproject.toml`](../pyproject.toml) is `0.18.0+mdsmithaustin.4`, upstream `0.18.0` plus fork addon revision `4`. Once published, its GitHub release tag is `mdsmithaustin/v0.18.0-r4` and its title is `Omnigent 0.18.0, mdsmithaustin revision 4`. Future releases use `mdsmithaustin/vX.Y.Z-rN` and `Omnigent X.Y.Z, mdsmithaustin revision N`, derived from the pinned upstream tag and fork revision. The `mdsmithaustin/` prefix avoids the upstream `v*` release triggers. Python distribution versions remain `X.Y.Z+mdsmithaustin.N`, and distribution names remain compatible with existing imports. The `0.18.0` line retains the [separate local fork defaults](FORK_DEFAULTS.md) introduced in `0.17.0+mdsmithaustin.2` and reuses existing fork state.

Revision `.4` fixes `omnigent-fork upgrade` in `.3`, which refused the fork's published assets because GitHub percent-encodes the `+` of a fork version in download URLs. Move `.3` or earlier installs to `.4` with the manual install steps below; from `.4` on, `omnigent-fork upgrade` installs later fork releases.

Revision `.3` makes fork builds track fork releases. The update notice and `omnigent-fork upgrade --check` read the fork's GitHub Releases instead of PyPI, and `omnigent-fork upgrade` installs a newer fork release as described in [Verify and install downloaded distributions](#verify-and-install-downloaded-distributions). Installing a harness extra on a fork build installs only that extra's packages. Prime Native accepts Prime `0.9.6` or newer when the release still loads extensions. The skill admission card appears only when a delivery needs attention. Installs of `.2` or earlier predate the upgrade command.

Revision `.2` follows the `0.18.0` sync with three fixes. Stopping a native session while its host is offline returns HTTP 503 instead of reporting success, so the turn and later input keep working ([Native Stop](NATIVE_STOP.md)). The install ledger records its own version instead of running another `omnigent` from `PATH`. E2E helpers read the isolated stack's logs from its pinned data directory.

On the `0.17.0` line, revision `.3` repairs native Codex startup when the server requires authenticated runner proof. Admission and validation use the runner's existing HTTP client, including credential refresh and runner identity. Policy-hook headers remain separate, and the server's ownership checks still apply. This repair does not change the `native_skill_routing` opt-in or establish a token or speed benefit.

Revision `0.17.0+mdsmithaustin.5` supersedes `.4`, which was certified but not published. For Prime runtime deletion, admission tombstone retention, and rollback compatibility, see [Prime Native state and compatibility](PRIME_NATIVE.md#state-and-compatibility).

Revision `0.17.0+mdsmithaustin.6` introduces [shared Prime authentication](PRIME_NATIVE.md#launch-and-reattach). Before rolling back, follow [Prime Native state and compatibility](PRIME_NATIVE.md#state-and-compatibility).

The certificate records source and artifact integrity under the reviewed workflow. It does not establish independent third-party approval. It excludes live Prime runtime qualification, desktop and mobile applications, Docker and provider images, and byte-identical rebuilds. The build embeds timestamps, so the certificate hashes the actual released bytes.

## Source line

Fork `main` is the only release line. It is a stable upstream tag commit followed by linear fork commits. Merge commits after the tag are forbidden. `.github/fork-release.json` pins the upstream release and inventories the first fork commits as ordered addons ending at `payload_commit`. Each commit after `payload_commit` is either policy-only, changing only the release-policy paths in `scripts/fork_release.py`, or pending, meaning it changes other paths or uses an unsafe file mode and is not yet inventoried. `main` is release-ready when no commit is pending.

`check-line` checks this shape offline and reports pending commits. A nonzero exit means the manifest or source line is invalid, or required Git objects are unavailable. Shape violations include an upstream commit that is not an ancestor, a merge after it, an inventory that is not the exact linear prefix, or a mismatched payload anchor.

```sh
python scripts/fork_release.py check-line --source-sha "$(git rev-parse HEAD)"
```

It prints the upstream tag, the inventoried count, the pending SHAs, and `release_ready`. Ordinary feature PRs leave `main` pending, which is expected.

Releases are cut from `main` and recorded by `mdsmithaustin/vX.Y.Z-rN` tags. Existing `fork/v*` tags are the historical records of revisions .1 through .3. Their `release/v0.17.0-mdsmithaustin.*` branches were removed because `main` and those tags already contain every commit on them. Do not move those tags or create release branches. `main` is the repository default branch, so pull requests target it. Repository settings enforce the line: merge commits are disabled, so pull requests land by squash or rebase, and the `main source line` ruleset requires linear history and blocks force-pushes and deletion of `main`. Repository admins bypass it only to replace `main` during an upstream sync. `check-line` also runs on pull request heads, so update a feature branch with `git rebase origin/main`, never by merging `main` into it.

## Check the source

Use a clean checkout of a release-ready `main` commit or a `mdsmithaustin/vX.Y.Z-rN` tag with its full Git history. For an existing `fork/v*` release, use its historical source and verifier. Install the repository prerequisites from [CONTRIBUTING.md](../CONTRIBUTING.md). `gh` must be authenticated and able to read the public upstream release and tag APIs.

Run these commands from the repository root. Any nonzero exit means the check failed.

```sh
source_sha=$(git rev-parse HEAD)
python scripts/fork_release.py check-source --source-sha "$source_sha"
uv run --no-sync python scripts/update_versions.py check --expect "$(python -c 'import json; print(json.load(open(".github/fork-release.json"))["version"])')"
uv lock --check
```

`check-source` prints the checked source, tree, manifest digest, and pin as JSON. The version command checks package versions, sibling pins, and the runtime constant against `[project].version` in `pyproject.toml`, requires it to equal the manifest `version`, then prints that version. The lock check must exit zero without changing the lockfile.

The verifier reads `.github/fork-release.json` from the exact source commit. It requires the published stable upstream release ID, publication timestamp, and current peeled tag SHA to match. A failed or unavailable lookup fails the check.

The addon inventory must account for every linear commit through `payload_commit`. Every later commit may change only the exact release-policy paths defined in `scripts/fork_release.py`. The manifest cannot enlarge that list. Runtime edits, including edits later reverted, merges, cross-boundary renames, symlinks, and submodules in the policy tail fail validation. Dirty tracked files and nonignored untracked files also fail.

Keep the `archive/*` and `codex/fork-isolation-*` branches that retain the original addon source objects required by the verifiers shipped in published releases `fork/v0.17.0+mdsmithaustin.1` through `.3` (11 objects across `.2` and `.3`, 6 for `.1`). Those verifiers run `git cat-file -t` on each non-null `source_commit`, so `check-source`, `certify` and `publish` at those revisions need those refs. The `verify` command does not. Any release published from a source cut before the removal of that check also runs the old verifier and needs those refs, including the certified `.4` and `.5` sources and the `.6` source on main. Only sources containing the check-removal change omit addon source object checks; they still validate source SHA format and uniqueness and the exact linear inventory of applied commits.

## Build and certify

Run the canonical builder with an output directory outside the source tree. A nonzero exit or an absent final `Built and checked eight distributions` line means the build failed.

```sh
bash scripts/build_fork_release.sh "$source_sha" /tmp/omnigent-fork-build
```

The builder installs the locked web dependencies and builds the UI from scratch. It builds four wheels and four sdists with the upstream builders, validates metadata and sibling pins, and runs Twine. It installs the wheels in clean Python 3.12 environments outside the checkout. It checks installed versions, imports, CLI output, Prime adapter registration, the opt-in skill flag, and bundled UI hashes. It separately rebuilds every sdist and repeats installation checks. These import checks do not qualify a live Prime session. The builder also installs the pinned upstream wheel and runs `scripts/verify_fork_coexistence.py --require-ui` against both installed environments under one temporary HOME. It checks separate defaults, distinct live servers, both HTML UIs, and both fork Stop commands while upstream and an unrecorded healthy listener survive. Its receipt and command logs are retained in `evidence/coexistence/`. A failed assertion fails the build.

The workflow runs source validation, all-files lint and type checks, canonical upstream Python test shards, mock integration, Slack and Databricks tests, web lint, types, tests and build, every changed addon unit and server test, and the distribution builder. Every job checks out the same full SHA and records the same run ID and attempt. A missing, failed, skipped, cancelled, or mixed-source required job cannot produce a certificate.

The Python gate reads the 16 shard rows from `ci.yml` at the immutable payload anchor and retains their paths, extras, marks, worker counts, and distribution modes. It adds a required `codex-parity` row with the canonical Rust and Codex setup, cached sidecar build, `CODEX_PARITY_SIDECAR_BIN`, and `--codex-parity` opt-in. The parity JUnit check rejects zero collected tests, all-skipped tests, and failures. Individual skips remain allowed when other parity tests run. Matrix fail-fast is disabled; certification requires all 17 Python shards to succeed. Collection failures and empty selections remain failures.

The addon gate reserves `tests/scripts/test_fork_release.py` for the serial `release-policy` shard, which runs the release CLI and workflow-matrix checks. It assigns every other eligible changed unit and server test file to exactly one populated canonical directory shard. Ordinary addon shards retain their worker and distribution settings and the `not databricks` marker. A parity addon shard uses the same canonical parity setup, opt-in, and JUnit check as the required parity row. The final matrix covers the union of eligible changed test files and the policy test file exactly once, even when the payload anchor already contains the policy tests. Every addon shard must succeed too.


Check the matrix inventory from the reviewed workflow. A nonzero exit means the row count or canonical definitions differ; success prints `17 Python shards; N addon shards; no missing or duplicate files`. The addon count depends on the changed test files.

```sh
uv run --no-project --with PyYAML==6.0.3 python - <<'PY'
import json, os, shlex, subprocess, tempfile
from pathlib import Path
import yaml
workflow = yaml.safe_load(Path(".github/workflows/fork-release.yml").read_bytes())
step = next(step for step in workflow["jobs"]["source"]["steps"] if step.get("id") == "matrix")
with tempfile.NamedTemporaryFile() as output:
    subprocess.run(["bash", "-c", step["run"]], env={**os.environ, "GITHUB_OUTPUT": output.name}, check=True)
    outputs = dict(line.split("=", 1) for line in Path(output.name).read_text().splitlines())
    rows = json.loads(outputs["python_matrix"])["include"]
    addons = json.loads(outputs["addon_matrix"])["include"]
pin = json.loads(Path(".github/fork-release.json").read_bytes())
canonical = yaml.safe_load(subprocess.check_output(["git", "show", pin["payload_commit"] + ":.github/workflows/ci.yml"]))["jobs"]["pytest"]["strategy"]["matrix"]["include"]
assert len(rows) == 17 and len({row["group"] for row in rows}) == 17 and rows[:16] == canonical
assert rows[-1] == {"group": "codex-parity", "paths": "tests/codex_parity"}
changed = subprocess.check_output(["git", "diff", "--name-only", pin["upstream"]["commit"] + ".." + pin["payload_commit"], "--", "tests"], text=True).splitlines()
expected = [path for path in changed if path.endswith(".py") and Path(path).is_file() and not path.startswith(("tests/e2e/", "tests/e2e_ui/", "tests/browser_ui/"))]
actual = [path for row in addons for path in shlex.split(row["paths"])]
assert sorted(actual) == sorted(set(expected) | {"tests/scripts/test_fork_release.py"})
assert len(actual) == len(set(actual)) and len(addons) == len({row["group"] for row in addons})
assert addons[-1] == {"group": "release-policy", "paths": "tests/scripts/test_fork_release.py", "workers": "0", "dist": "loadfile"}
print(f"17 Python shards; {len(addons)} addon shards; no missing or duplicate files")
PY
```


Failed Python and addon shards upload `fork-python-GROUP-RUN_ID-RUN_ATTEMPT` or `fork-addons-GROUP-RUN_ID-RUN_ATTEMPT` with pytest output, JUnit, per-worker progress, and memory/process summaries. The collection-time shutdown cascade after a worker loss was reproduced in xdist; the initial worker loss remains unexplained. Use these diagnostics to establish that cause. No retry or pytest-exit masking is applied.

The `source` job runs `check-line` on every triggered pull request and push, so a shape violation fails immediately. It runs `check-source` and starts the certification jobs only when the line is release-ready. A pending source passes `source` and skips the rest. Manual dispatch rejects a pending source, and publication rejects any ref other than `main`.

Pull requests run read-only checks against the PR head. Certificates are emitted only for pushes and manual dispatches whose workflow SHA equals the source SHA. This avoids treating GitHub's PR merge workflow as accepted head policy. Push checks run on `main` and on `codex/fork-release-*` candidate branches.

To recheck a release-ready `main` without publishing, run a manual check with publication disabled. A nonzero dispatch exit means the request failed. The run must later show every required job and `certificate` as successful.

```sh
gh workflow run fork-release.yml --repo mdsmithaustin/omnigent --ref main -f publish=false
```

The `fork-certified-RUN_ID-RUN_ATTEMPT` artifact contains `dist/` with eight distributions and `evidence/` with `certificate.json`, `ui-inventory.json`, and `SHA256SUMS`. The certificate binds the final source SHA, tree, payload anchor, upstream release identity, addon inventory, manifest digest, workflow, run, checks, UI inventory, and artifact hashes.

The CLI `certify` command consumes the workflow's fixed `needs` results. It offers no check-selection or skip inputs. Local caller-supplied job records do not establish trusted CI execution. Compare a downloaded certificate's source, run ID, and attempt against the reviewed GitHub workflow run.

## Verify and install downloaded distributions

Download the certified run artifact or fork GitHub release assets into `dist/` and `evidence/`. Use the verifier from the reviewed source commit. Run verification before installing. A nonzero exit means the bytes, metadata, UI inventory, or stated check scope differ.

```sh
python scripts/fork_release.py verify --certificate evidence/certificate.json --artifacts dist
```

Successful output includes `8 distributions; scoped integrity, not independent approval`. Checksums alone do not authenticate the publisher. Trust the reviewed source and workflow identity, then verify the downloaded bytes.

Install all four local wheels into their own environment in a durable location, then link the fork launcher. `omnigent-fork upgrade` keeps this environment for rollback, so do not put it under `/tmp`. Set `version` to the certified version. Each command must exit zero.

```sh
version=0.18.0+mdsmithaustin.4
venv="$HOME/.local/share/omnigent-fork/$version"
uv venv --python 3.12 "$venv"
uv pip install --python "$venv/bin/python" dist/*.whl
"$venv/bin/omnigent" --version
mkdir -p ~/.local/bin && ln -sfn "$venv/bin/omnigent" ~/.local/bin/omnigent-fork
```

Run the fork as `omnigent-fork` from then on, with `~/.local/bin` on `PATH`. It stays separate from `~/.local/bin/omni` and `~/.local/bin/omnigent`, which an upstream install may own. The CLI must print the version recorded in the verified certificate. Open the installed application and confirm the bundled UI loads. Test Prime sessions separately with [the Prime verification skill](../.agents/skills/verify-prime-native/SKILL.md) when you need live runtime evidence.

Install future fork updates from their verified fork assets. A fork build, one whose version carries the `+mdsmithaustin.N` local segment, checks for updates against the published GitHub Releases of `mdsmithaustin/omnigent` instead of a package index. Drafts, prereleases, and tags outside `mdsmithaustin/vX.Y.Z-rN` are ignored. For a fork installed from the release distributions, the CLI update notice names the newer fork version, the upgrade command, its release page, and this section. The command is `omnigent-fork upgrade` when `~/.local/bin/omnigent-fork` runs this install, and otherwise the full path of this install's `omnigent`. It uses the same cache and four-hour cadence as the index check, so a machine makes about one unauthenticated GitHub API call per window, also after a failed call. `omnigent-fork upgrade` queries GitHub directly instead of reading that cache.

`omnigent-fork upgrade --check` reports the newest fork release. It exits zero when the build is current and non-zero when a newer fork release exists or GitHub is unreachable. It refuses `--nightly` and `--target-version`, which would install upstream omnigent over the fork, before stopping any local server.

The fork's `upgrade` command takes this path on any fork install that is neither a `git+` install nor an editable install: a `uv tool` or `pipx` install or a plain venv. The current environment, `sys.prefix` of the running CLI, is the previous venv kept for rollback, wherever it lives. Run the upgrade through the fork's own entry point, `omnigent-fork upgrade` or the install's full path, because `omni` on `PATH` may be upstream. It installs the newest fork release beside the current one. `--dry-run` prints the plan and changes nothing. On a terminal it asks for confirmation unless `--force` is given. It first prepares the new install, which changes nothing in use:

1. Download the four wheels, `SHA256SUMS` and `certificate.json` into a temporary directory. Each asset URL must be `https://github.com/mdsmithaustin/omnigent/releases/download/TAG/NAME` for the resolved release tag and that asset's name; any other URL stops the upgrade.
2. Verify that `certificate.json` names the release version, that `SHA256SUMS` lists exactly the certificate's artifact hashes, and that every wheel matches its `SHA256SUMS` entry. This is the part of `verify` that holds without the source checkout. Any mismatch stops the upgrade. `SHA256SUMS` and `certificate.json` come from the same release as the wheels, so this check proves the files agree with that certificate, not who published them. Trust rests on the repository's release permissions; for stronger evidence, compare the certificate's run ID with the reviewed workflow run as described above.
3. Run `uv venv --python 3.12 ~/.local/share/omnigent-fork/VERSION` and `uv pip install` the four wheels, then require that venv's `omnigent --version` to print the release version. `uv` must be on `PATH`. A failed install removes that new directory. An existing directory is reused only when it already holds the release version; otherwise the upgrade stops and asks you to move it aside.

Only then does it switch. It drains and stops the local server. If the fork host service (`com.mdsmithaustin.omni.host` or `omni-mdsmithaustin-host.service`) runs the current install in local mode, it runs the current `omnigent host disable`, then the new venv's `omnigent host enable --server '' --non-interactive` with the service's recorded environment, such as `OMNIGENT_FEATURES`. The empty `--server` keeps local mode even when the fork config names a `server:`. Disabling first avoids launchd's bootstrap error 5 for a loaded label. If the new enable fails, the upgrade re-enables the previous service and stops. If that re-enable also fails, the service is left disabled and its definition removed, and the error prints the exact command to re-enable it from the previous venv. Run that command yourself: a rerun of `omnigent-fork upgrade` finds no service and does not restore it. A service that is absent, unreadable, remote, runs another install, or already runs the new release is left unchanged and reported. Finally the upgrade atomically points `~/.local/bin/omnigent-fork` at the new venv's `omnigent`. It never touches `~/.local/bin/omni` or `~/.local/bin/omnigent`, which belong to an upstream install.

The previous venv is kept, and the upgrade prints the rollback commands. When it re-pointed a local-mode service, they start with the new venv's `omnigent host disable` and the previous venv's `omnigent host enable --server '' --non-interactive` with the service's non-secret `OMNIGENT_*` variables. They always end with an `ln -sfn` that points `omnigent-fork` back at the previous `omnigent`. Export any provider credentials the service needs before running the enable command. The upgrade installs the four wheels without extras. Running `omnigent-fork upgrade` again after a failure resumes from what is already in place. A fork installed from a `git+` URL re-pulls its own ref on a plain upgrade and refuses `--nightly` and `--target-version`. A build without the fork's local version segment keeps the upstream package-index and nightly behaviour.

When setup installs a harness extra into a fork build, it installs only the packages that extra adds, never `omnigent` itself, and the executors' install hints show the same command. Fork builds are not on any package index, so reinstalling `omnigent[<extra>]` can replace the fork with upstream omnigent from PyPI; a `uv tool` install that did not come from git does exactly that.

## Verify local coexistence

Use separate virtual environments for upstream and the fork. `$venv` is the fork environment from the install step above. Port `6768` must be available for the test listener. Run the maintained acceptance check from the reviewed fork checkout, with a new evidence directory. A nonzero exit, a false `qualified` receipt, or any `owned_survivors` means the check failed.

```sh
uv venv --python 3.12 /tmp/omnigent-upstream-install
(cd /tmp && uv pip install --python /tmp/omnigent-upstream-install/bin/python 'omnigent==0.18.0')
"$venv/bin/python" -I scripts/verify_fork_coexistence.py \
    --upstream-python /tmp/omnigent-upstream-install/bin/python \
    --fork-python "$venv/bin/python" \
    --require-ui --evidence /tmp/omnigent-coexistence-check
```

The check creates its own HOME and workspace and cleans up its recorded test processes. It does not launch native providers or qualify arbitrary corrupt PID records. For an interactive check, start each environment's `omnigent server --background`, open each printed URL, then run the fork environment's `omnigent server stop`. The upstream UI must still respond. Use the upstream environment's recorded process owner to stop upstream.

## Publish the same certified bytes

Publication requires an explicit manual dispatch with `publish=true` on release-ready `main`. It is disabled by default. This command authorizes a new run and its final publication job. A nonzero dispatch exit or any failed required job prevents publication.

```sh
gh workflow run fork-release.yml --repo mdsmithaustin/omnigent --ref main -f publish=true
```

Only the final publisher has release-write permission. It validates repository, dispatch intent, workflow, run, source, tag, certificate, checksums, UI evidence, and downloaded artifact bytes. It promotes those bytes to `mdsmithaustin/omnigent` GitHub Releases without rebuilding. A tag pointing elsewhere or existing different assets fail without overwrite. An existing identical release succeeds without changing it.

The workflow has no PyPI or GHCR publication target. It reuses upstream canonical version, build, and default test mechanics. It does not replicate upstream's secure-release repository, benchmark or administrative approval gates, Homebrew, mobile, or image publication.

## Cut a release from main

Cut revision `N` as two pull requests to `main`. Replace `N` with the next positive revision number in the commands below. Use the version-stamping helper's `pre-release` command; its `post-release` command belongs to upstream's development-version workflow.

1. Stamp the version. This is an ordinary pending commit. Each command must exit zero.

   ```sh
   uv run --no-sync python scripts/update_versions.py pre-release --new-version 0.18.0+mdsmithaustin.N
   uv lock
   python scripts/normalize_uv_lock_registry.py uv.lock
   uv run --no-sync python scripts/update_versions.py check --expect 0.18.0+mdsmithaustin.N
   ```

2. After the first PR merges, inventory the pending commits on a branch from `main`.

   ```sh
   python scripts/fork_release.py inventory --version 0.18.0+mdsmithaustin.N
   ```

   It refuses unless `pyproject.toml` and `omnigent/version.py` at committed `HEAD` already carry that version. Uncommitted version edits do not count. It appends every commit through the last pending one as an addon named from its subject and short SHA, with the subject as its reason. Policy-only commits before that point join the inventory too, because the addons must stay an exact linear prefix. It then moves `payload_commit` to the last pending commit and sets the version. Review the generated reasons and update this document and `designs/fork-release/DECISIONS.tsv`. Commit only policy paths, so `main` becomes release-ready. `check-line` must report `"release_ready": true`.

The push run on `main` then certifies that exact source. After review, dispatch publication with `publish=true` as shown above. The publisher creates the tag `mdsmithaustin/v0.18.0-rN` on that commit with the title `Omnigent 0.18.0, mdsmithaustin revision N`. The Python distributions retain the stamped version `0.18.0+mdsmithaustin.N`.

## Sync to a new upstream tag

Rebuild `main` on a published stable upstream tag instead of merging upstream. A merge would place upstream history after the pinned tag, which `check-line` rejects, and would hide which fork commits remain. Replay keeps the fork as a short, reviewable patch series on the new tag. For `v0.18.0`:

Each Git, generation, stamping, inventory, and verification command must exit zero before continuing.

1. Fetch and record the old fork tip, fetch both upstream tags, and branch from the new one:

   ```sh
   git fetch origin main
   old_main_sha=$(git rev-parse origin/main)
   git fetch https://github.com/omnigent-ai/omnigent.git tag v0.17.0 tag v0.18.0
   git switch -c sync/v0.18.0 v0.18.0
   ```

2. Replay the fork commits since the old tag in order with `git cherry-pick "v0.17.0..${old_main_sha}"`. `git rebase --onto v0.18.0 v0.17.0` on a copy of `main` is equivalent. Drop commits that upstream superseded and the old manifest-only commits.
3. Resolve conflicts surgically and record their reasons. Regenerate OpenAPI with `uv run --no-sync python scripts/dump_openapi.py` if necessary.
4. If upstream added migrations after the fork's, add a merge revision with `uv run --no-sync alembic -c omnigent/db/alembic.ini merge heads -m "merge upstream v0.18.0"`. `tests/db/test_migration_connections.py::test_single_alembic_head` must pass.
5. Reset the manifest and commit it: pin the new upstream tag, commit SHA, release ID, and publication time from the upstream APIs, clear `addons`, set `payload_commit` to the tag commit, and set `version` to `0.18.0+mdsmithaustin.1`. Stamp the same version and normalize and check the lockfile as above. Commit those version and lockfile changes before running `python scripts/fork_release.py inventory --version 0.18.0+mdsmithaustin.1`, because inventory reads the version from committed `HEAD`. Review the generated reasons and commit the result so the replayed commits become the addons.
6. Run `pre-commit run --all-files`, the changed addon tests, and `check-source`. Archive the old line and replace `main`:

   ```sh
   git push origin "${old_main_sha}:refs/heads/archive/main-v0.17.0"
   git push --force-with-lease="main:${old_main_sha}" origin sync/v0.18.0:main
   ```

Python distribution versions then continue as `0.18.0+mdsmithaustin.N`. GitHub releases use tags `mdsmithaustin/v0.18.0-rN` and titles `Omnigent 0.18.0, mdsmithaustin revision N`. The archive branch and existing `fork/v0.17.0+mdsmithaustin.*` tags keep earlier certificates verifiable. Never move an existing fork tag.
