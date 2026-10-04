# Build and install certified fork releases

The `python-distributions-v1` profile verifies the released upstream source, ordered fork addons, required checks, and the eight Python distribution files. It also binds the bundled UI filenames and SHA256 hashes. It covers `omnigent`, `omnigent-client`, `omnigent-ui-sdk`, and `omnigent-slack` at one version.

`0.16.0+mdsmithaustin.1` means upstream `0.16.0` plus fork addon revision `1`. The GitHub release tag is `fork/v0.16.0+mdsmithaustin.1`. The `fork/` prefix avoids the upstream `v*` release triggers. Distribution names remain compatible with existing imports.

The certificate records source and artifact integrity under the reviewed workflow. It does not establish independent third-party approval. It excludes live Prime runtime qualification, desktop and mobile applications, Docker and provider images, and byte-identical rebuilds. The build embeds timestamps, so the certificate hashes the actual released bytes.

## Check the source

Use a clean checkout of the authoritative permanent branch `release/v0.16.0-mdsmithaustin.1` with its full Git history. Install the repository prerequisites from [CONTRIBUTING.md](../CONTRIBUTING.md). `gh` must be authenticated and able to read the public upstream release and tag APIs.

Run these commands from the repository root. Any nonzero exit means the check failed.

```sh
source_sha=$(git rev-parse HEAD)
python scripts/fork_release.py check-source --source-sha "$source_sha"
uv run --no-sync python scripts/update_versions.py check --expect '0.16.0+mdsmithaustin.1'
uv lock --check
```

`check-source` prints the checked source, tree, manifest digest, and pin as JSON. The version command prints `0.16.0+mdsmithaustin.1`. The lock check must exit zero without changing the lockfile.

The verifier reads `.github/fork-release.json` from the exact source commit. It requires the published stable upstream release ID, publication timestamp, and current peeled tag SHA to match. A failed or unavailable lookup fails the check.

The addon inventory must account for every linear commit through `payload_commit`. Original addon Git objects must remain available. Every later commit may change only the exact release-policy paths defined in `scripts/fork_release.py`. The manifest cannot enlarge that list. Runtime edits, including edits later reverted, merges, cross-boundary renames, symlinks, and submodules in the policy tail fail validation. Dirty tracked files and nonignored untracked files also fail.

## Build and certify

Run the canonical builder with an output directory outside the source tree. A nonzero exit or an absent final `Built and checked eight distributions` line means the build failed.

```sh
bash scripts/build_fork_release.sh "$source_sha" /tmp/omnigent-fork-build
```

The builder installs the locked web dependencies and builds the UI from scratch. It builds four wheels and four sdists with the upstream builders, validates metadata and sibling pins, and runs Twine. It installs the wheels in clean Python 3.12 environments outside the checkout. It checks installed versions, imports, CLI output, Prime adapter registration, the opt-in skill flag, and bundled UI hashes. It separately rebuilds every sdist and repeats installation checks. These import checks do not qualify a live Prime session.

The workflow runs source validation, all-files lint and type checks, default upstream Python tests, mock integration and Slack tests, web lint, types, tests and build, every changed addon unit and server test, and the distribution builder. Every job checks out the same full SHA and records the same run ID and attempt. A missing, failed, skipped, cancelled, or mixed-source required job cannot produce a certificate.

Pull requests run read-only checks against the PR head. Certificates are emitted only for pushes and manual dispatches whose workflow SHA equals the source SHA. This avoids treating GitHub's PR merge workflow as accepted head policy. Push checks run on `main`, `release/**`, and `codex/fork-release-*`. A development `main` history that violates the pin cannot certify.

GitHub requires the workflow file on the repository default branch before it accepts [manual dispatch](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/manually-run-a-workflow). Push the reviewed source to permanent branch `release/v0.16.0-mdsmithaustin.1` and let its push workflow pass every required check. After independent review and those checks succeed, select that release branch as the fork default. This keeps the default pinned to released source while retaining development `main` and its history.

Then run a manual check with publication disabled. A nonzero dispatch exit means the request failed. The run must later show every required job and `certificate` as successful.

```sh
gh workflow run fork-release.yml --repo mdsmithaustin/omnigent --ref release/v0.16.0-mdsmithaustin.1 -f publish=false
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

The CLI must print `0.16.0+mdsmithaustin.1`. Open the installed application and confirm the bundled UI loads. Test Prime sessions separately with [the Prime verification skill](../.agents/skills/verify-prime-native/SKILL.md) when you need live runtime evidence.

Install future fork updates from their verified fork assets. `omni upgrade` for a wheel installation uses package-index releases or prints an index installation command. `omni upgrade --nightly` selects upstream GitHub tags. Those channels can replace the fork addons. This release process does not change the runtime updater.

## Publish the same certified bytes

Publication requires an explicit manual dispatch with `publish=true` on the reviewed release branch. It is disabled by default. This command authorizes a new run and its final publication job. A nonzero dispatch exit or any failed required job prevents publication.

```sh
gh workflow run fork-release.yml --repo mdsmithaustin/omnigent --ref release/v0.16.0-mdsmithaustin.1 -f publish=true
```

Only the final publisher has release-write permission. It validates repository, dispatch intent, workflow, run, source, tag, certificate, checksums, UI evidence, and downloaded artifact bytes. It promotes those bytes to `mdsmithaustin/omnigent` GitHub Releases without rebuilding. A tag pointing elsewhere or existing different assets fail without overwrite. An existing identical release succeeds without changing it.

The workflow has no PyPI or GHCR publication target. It reuses upstream canonical version, build, and default test mechanics. It does not replicate upstream's secure-release repository, benchmark or administrative approval gates, Homebrew, mobile, or image publication.

## Start a new stable release branch

Keep the current release branch and its recorded commits immutable. Protect it with review rules and retain the original addon source objects. A squash, rebase, or merge into development `main` changes the history and invalidates the inventory. Do not certify that altered history or force-update an existing release tag. Development `main` remains separate from the release source.

Start the next release in another worktree at an explicitly selected published stable upstream tag. Resolve its peeled full SHA and release identity through the upstream APIs. Do not merge upstream `main` into a release branch.

Replay the original addons in order with `git cherry-pick -n FULL_SOURCE_SHA`. Resolve conflicts surgically and record their reasons. Regenerate generated OpenAPI with `uv run --no-sync python scripts/dump_openapi.py` if necessary. A nonzero exit means generation failed. Before each commit, run `uv run --no-sync pre-commit run --all-files`. A nonzero hook exit means the commit is not ready. Record each original and applied full SHA immediately after committing.

Run all changed addon unit and server tests on the integrated result. Then stamp the selected stable version plus the next fork revision with `uv run --no-sync python scripts/update_versions.py pre-release --new-version VERSION`. Run `uv lock`, normalize with `python scripts/normalize_uv_lock_registry.py uv.lock`, check versions and frozen resolution, and retain upstream third-party lock entries. Inventory every repair and the version commit. Finish any measured release-gate repairs, rerun their checks, and use the last inventoried payload commit as the anchor. All four package versions must still match.

Copy the reviewed release tooling after the anchor and regenerate `.github/fork-release.json` with the new stable identity, exact ordered commit inventory, anchor, and version. Review both the payload and tooling tail. Run `check-source` on the final committed SHA and let the workflow check that same source. Preserve the recorded chain when pushing the release branch. Publication creates the fork tag from that chain.

For the next stable update, push the new permanent release branch with its reviewed inventory and let its push workflow certify that exact source. After all checks and independent review succeed, select the new branch as the fork default, then dispatch its publication explicitly. Retain prior release branches and tags unchanged so their certificates remain verifiable.
