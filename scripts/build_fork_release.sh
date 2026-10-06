#!/usr/bin/env bash
set -euo pipefail

[[ $# == 2 ]] || { echo 'usage: build_fork_release.sh FULL_SOURCE_SHA OUTPUT_DIRECTORY' >&2; exit 2; }
source_sha=$1
output=$2
repo_root=$(git -C "$(dirname "$0")/.." rev-parse --show-toplevel)
cd "$repo_root"
python scripts/fork_release.py check-source --source-sha "$source_sha"
python - "$repo_root" "$output" <<'PY'
from pathlib import Path
import sys
root, output = (Path(value).resolve() for value in sys.argv[1:])
if output.is_relative_to(root) or root.is_relative_to(output):
    raise SystemExit("build output must be outside the source tree and its ancestors")
owner = output / ".fork-release-build-owner"
if output.exists() and any(output.iterdir()):
    if not owner.is_file() or owner.read_text() != str(root) + "\n":
        raise SystemExit("refusing to clear an existing output directory not owned by this builder")
PY
export UV_INDEX_URL=https://pypi.org/simple
export PIP_INDEX_URL=https://pypi.org/simple
export NPM_CONFIG_REGISTRY=https://registry.npmjs.org/
version=$(python -c 'import json; print(json.load(open(".github/fork-release.json"))["version"])')
uv run --no-sync python scripts/update_versions.py check --expect "$version"
python scripts/normalize_uv_lock_registry.py --check uv.lock
uv lock --check
rm -rf "$output"
mkdir -p "$output/dist" "$output/evidence"
output=$(cd "$output" && pwd)
printf '%s\n' "$repo_root" > "$output/.fork-release-build-owner"
rm -rf omnigent/server/static/web-ui
pnpm install --frozen-lockfile --filter web
pnpm --filter web run build
python - "$output" <<'PY'
from pathlib import Path
import sys
from scripts.fork_release import inventory_ui, write_json
write_json(Path(sys.argv[1]) / "evidence/ui-inventory.json", inventory_ui(Path("omnigent/server/static/web-ui")))
PY
export OMNIGENT_SKIP_WEB_UI=true
uv build --no-create-gitignore --out-dir "$output/dist"
uv run --no-project --with build python scripts/build_subpackages.py --out-dir "$output/dist" omnigent-client omnigent-ui-sdk omnigent-slack
uvx twine check "$output"/dist/*
python - "$output" "$version" <<'PY'
import json
from pathlib import Path
import sys
from scripts.fork_release import inspect_artifacts
root = Path(sys.argv[1])
inspect_artifacts(root / "dist", sys.argv[2], json.loads((root / "evidence/ui-inventory.json").read_bytes()))
PY
smoke_root=$(mktemp -d)
trap 'rm -rf "$smoke_root"' EXIT
cat > "$smoke_root/smoke.py" <<'PY'
import importlib
import importlib.metadata
from pathlib import Path
import subprocess
import sys
version, source, ui_file = sys.argv[1:]
import json
import hashlib
for package, module in (("omnigent", "omnigent"), ("omnigent-client", "omnigent_client"), ("omnigent-ui-sdk", "omnigent_ui_sdk"), ("omnigent-slack", "omnigent_slack")):
    assert importlib.metadata.version(package) == version, package
    loaded = importlib.import_module(module)
    assert not Path(loaded.__file__).resolve().is_relative_to(Path(source)), module
import omnigent.harnesses.prime_native.main
import omnigent.harnesses.prime_native.controls
from omnigent.harness_plugins import plugin_state
from omnigent.server.feature_flags import Feature
assert any(contribution.harness_modules.get("prime-native") == "omnigent.inner.prime_native_harness" for contribution in plugin_state().contributions)
assert Feature.NATIVE_SKILL_ROUTING.value == "native_skill_routing"
from omnigent import __file__ as core_file
ui_root = Path(core_file).parent / "server/static/web-ui"
ui = {path.relative_to(ui_root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in ui_root.rglob("*") if path.is_file()}
assert ui == json.loads(Path(ui_file).read_bytes()), "installed UI inventory differs"
from omnigent.version import VERSION
assert VERSION == version, VERSION
cli = subprocess.run([str(Path(sys.executable).parent / "omnigent"), "--version"], capture_output=True, text=True, check=True)
assert version in cli.stdout, cli.stdout
print("Installed imports and CLI version pass for all four distributions")
PY
smoke_wheels() {
    local wheels=$1
    local environment=$2
    uv venv --python 3.12 "$environment"
    uv pip install --python "$environment/bin/python" "$wheels"/*.whl
    (cd "$smoke_root" && env -u PYTHONPATH "$environment/bin/python" -I "$smoke_root/smoke.py" "$version" "$repo_root" "$output/evidence/ui-inventory.json")
}
smoke_wheels "$output/dist" "$smoke_root/wheels"
mkdir -p "$smoke_root/rebuilt" "$smoke_root/sources"
export OMNIGENT_SKIP_WEB_UI=true
for sdist in "$output"/dist/*.tar.gz; do
    tar -xzf "$sdist" -C "$smoke_root/sources"
done
for project in "$smoke_root"/sources/*; do
    uv run --no-project --with build python -m build --wheel --outdir "$smoke_root/rebuilt" "$project"
done
smoke_wheels "$smoke_root/rebuilt" "$smoke_root/sdists"
python scripts/fork_release.py check-source --source-sha "$source_sha"
echo "Built and checked eight distributions from $source_sha"
