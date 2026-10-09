# Build and install certified fork releases

The `python-distributions-v1` profile verifies the released upstream source, ordered fork addons, required checks, and the eight Python distribution files. It also binds the bundled UI filenames and SHA256 hashes. It covers `omnigent`, `omnigent-client`, `omnigent-ui-sdk`, and `omnigent-slack` at one version.

`0.17.0+mdsmithaustin.3` means upstream `0.17.0` plus fork addon revision `3`. The GitHub release tag is `fork/v0.17.0+mdsmithaustin.3`. The `fork/` prefix avoids the upstream `v*` release triggers. Distribution names remain compatible with existing imports. Revision `.3` retains the [separate local fork defaults](FORK_DEFAULTS.md) introduced in `.2`. Existing `.2` fork state is reused. Revision `.1` remains available with its original shared defaults.

Revision `.3` repairs native Codex startup when the server requires authenticated runner proof. Admission and validation use the runner's existing HTTP client, including credential refresh and runner identity. Policy-hook headers remain separate, and the server's ownership checks still apply. This repair does not change the `native_skill_routing` opt-in or establish a token or speed benefit.

The certificate records source and artifact integrity under the reviewed workflow. It does not establish independent third-party approval. It excludes live Prime runtime qualification, desktop and mobile applications, Docker and provider images, and byte-identical rebuilds. The build embeds timestamps, so the certificate hashes the actual released bytes.

## Source line

Fork `main` is the only release line. It is a stable upstream tag commit followed by linear fork commits. Merge commits after the tag are forbidden. `.github/fork-release.json` pins the upstream release and inventories the first fork commits as ordered addons ending at `payload_commit`. Each commit after `payload_commit` is either policy-only, changing only the release-policy paths in `scripts/fork_release.py`, or pending, meaning it changes other paths and is not yet inventoried. `main` is release-ready when no commit is pending.

`check-line` checks this shape offline and reports pending commits. Any nonzero exit is a shape violation: the upstream commit is not an ancestor, a merge follows it, the inventory is not the exact linear prefix, or the payload anchor differs.

```sh
python scripts/fork_release.py check-line --source-sha "$(git rev-parse HEAD)"
```

It prints the upstream tag, the inventoried count, the pending SHAs, and `release_ready`. Ordinary feature PRs leave `main` pending, which is expected.

Releases are cut from `main` and recorded by `fork/v*` tags. The branches `release/v0.17.0-mdsmithaustin.1` through `.3` are historical records. Do not push to them or create new release branches.

## Check the source

Use a clean checkout of a release-ready `main` commit or a `fork/v*` tag with its full Git history. Install the repository prerequisites from [CONTRIBUTING.md](../CONTRIBUTING.md). `gh` must be authenticated and able to read the public upstream release and tag APIs.

Run these commands from the repository root. Any nonzero exit means the check failed.

```sh
source_sha=$(git rev-parse HEAD)
python scripts/fork_release.py check-source --source-sha "$source_sha"
uv run --no-sync python scripts/update_versions.py check --expect '0.17.0+mdsmithaustin.3'
uv lock --check
```

`check-source` prints the checked source, tree, manifest digest, and pin as JSON. The version command prints `0.17.0+mdsmithaustin.3`. The lock check must exit zero without changing the lockfile.

The verifier reads `.github/fork-release.json` from the exact source commit. It requires the published stable upstream release ID, publication timestamp, and current peeled tag SHA to match. A failed or unavailable lookup fails the check.

The addon inventory must account for every linear commit through `payload_commit`. Original addon Git objects must remain available. Every later commit may change only the exact release-policy paths defined in `scripts/fork_release.py`. The manifest cannot enlarge that list. Runtime edits, including edits later reverted, merges, cross-boundary renames, symlinks, and submodules in the policy tail fail validation. Dirty tracked files and nonignored untracked files also fail.

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

The `source` job runs `check-line` on every pull request and push, so a shape violation fails immediately. It runs `check-source` and starts the certification jobs only when the line is release-ready. A pending source passes `source` and skips the rest. Manual dispatch rejects a pending source, and publication rejects any ref other than `main`.

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

Create an isolated environment and install all four local wheels. Each command must exit zero.

```sh
uv venv --python 3.12 /tmp/omnigent-fork-install
uv pip install --python /tmp/omnigent-fork-install/bin/python dist/*.whl
/tmp/omnigent-fork-install/bin/omnigent --version
```

The CLI must print `0.17.0+mdsmithaustin.3`. Open the installed application and confirm the bundled UI loads. Test Prime sessions separately with [the Prime verification skill](../.agents/skills/verify-prime-native/SKILL.md) when you need live runtime evidence.

Install future fork updates from their verified fork assets. `omni upgrade` for a wheel installation uses package-index releases or prints an index installation command. `omni upgrade --nightly` selects upstream GitHub tags. Those channels can replace the fork addons. This release process does not change the runtime updater.

## Verify local coexistence

Use separate virtual environments for upstream and the fork. Port `6768` must be available for the test listener. Run the maintained acceptance check from the reviewed fork checkout, with a new evidence directory. A nonzero exit, a false `qualified` receipt, or any `owned_survivors` means the check failed.

```sh
uv venv --python 3.12 /tmp/omnigent-upstream-install
(cd /tmp && uv pip install --python /tmp/omnigent-upstream-install/bin/python 'omnigent==0.17.0')
/tmp/omnigent-fork-install/bin/python -I scripts/verify_fork_coexistence.py \
    --upstream-python /tmp/omnigent-upstream-install/bin/python \
    --fork-python /tmp/omnigent-fork-install/bin/python \
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

Cut revision `N` as two pull requests to `main`.

1. Stamp the version. This is an ordinary pending commit. Each command must exit zero.

   ```sh
   uv run --no-sync python scripts/update_versions.py pre-release --new-version 0.17.0+mdsmithaustin.N
   uv lock
   python scripts/normalize_uv_lock_registry.py uv.lock
   uv run --no-sync python scripts/update_versions.py check --expect 0.17.0+mdsmithaustin.N
   ```

2. After the first PR merges, inventory the pending commits on a branch from `main`.

   ```sh
   python scripts/fork_release.py inventory --version 0.17.0+mdsmithaustin.N
   ```

   It refuses unless `pyproject.toml` and `omnigent/version.py` already carry that version. It appends every commit through the last pending one as an addon named from its subject and short SHA, with the subject as its reason. Policy-only commits before that point join the inventory too, because the addons must stay an exact linear prefix. It then moves `payload_commit` to the last pending commit and sets the version. Review the generated reasons and update this document and `designs/fork-release/DECISIONS.tsv`. Commit only policy paths, so `main` becomes release-ready. `check-line` must report `"release_ready": true`.

The push run on `main` then certifies that exact source. After review, dispatch publication with `publish=true` as shown above. The publisher creates the tag `fork/v0.17.0+mdsmithaustin.N` on that commit.

## Sync to a new upstream tag

Rebuild `main` on a published stable upstream tag instead of merging upstream. A merge would place upstream history after the pinned tag, which `check-line` rejects, and would hide which fork commits remain. Replay keeps the fork as a short, reviewable patch series on the new tag. For `v0.18.0`:

1. Fetch both tags and branch from the new one: `git fetch parent tag v0.17.0 tag v0.18.0` and `git switch -c sync/v0.18.0 v0.18.0`.
2. Replay the fork commits since the old tag in order with `git cherry-pick v0.17.0..origin/main`. `git rebase --onto v0.18.0 v0.17.0` on a copy of `main` is equivalent. Drop commits that upstream superseded and the old manifest-only commits.
3. Resolve conflicts surgically and record their reasons. Regenerate OpenAPI with `uv run --no-sync python scripts/dump_openapi.py` if necessary.
4. If upstream added migrations after the fork's, add a merge revision with `uv run --no-sync alembic -c omnigent/db/alembic.ini merge heads -m "merge upstream v0.18.0"`. `tests/db/test_migration_connections.py::test_single_alembic_head` must pass.
5. Reset the manifest and commit it: pin the new upstream tag, commit SHA, release ID, and publication time from the upstream APIs, clear `addons`, set `payload_commit` to the tag commit, and set `version` to `0.18.0+mdsmithaustin.1`. Stamp the same version as above, then run `inventory --version 0.18.0+mdsmithaustin.1` and commit the result so the replayed commits become the addons.
6. Run `pre-commit run --all-files`, the changed addon tests, and `check-source`. Archive the old line and replace `main`:

   ```sh
   git fetch origin main && git push origin origin/main:refs/heads/archive/main-v0.17.0
   git push --force-with-lease=main:OLD_MAIN_SHA origin sync/v0.18.0:main
   ```

Releases then continue as `0.18.0+mdsmithaustin.N`. The archive branch and existing `fork/v0.17.0+mdsmithaustin.*` tags keep earlier certificates verifiable. Never move an existing fork tag.
