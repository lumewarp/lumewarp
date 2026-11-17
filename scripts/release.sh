#!/usr/bin/env bash
# Prepare a release: set every version to X.Y.Z, commit "Release vX.Y.Z" and tag it.
#
#   scripts/release.sh 0.2.0            bump, commit and tag (nothing is pushed)
#   scripts/release.sh 0.2.0-beta.1     a pre-release
#   scripts/release.sh --check 0.2.0    exit 1 unless every version is 0.2.0 (CI)
#
# Versions kept in step: the gateway workspace (and its Cargo.lock entries, also in the
# example plugins' lockfiles), the control-plane package (pyproject, __init__, uv.lock), the UI package and the
# Helm charts (version and appVersion, the umbrella's dependency on lumewarp) and
# the plain manifests in deploy/kubernetes, which are regenerated. Pushing `main`
# and then the tag runs the full CI once on the release commit and the release workflow reuses it.
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$root"

die() { echo "release: $*" >&2; exit 1; }

check=0
if [[ "${1:-}" == "--check" ]]; then
  check=1
  shift
fi
version="${1:-}"
[[ "$version" =~ ^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-[0-9A-Za-z.-]+)?$ ]] \
  || die "usage: scripts/release.sh [--check] X.Y.Z[-pre] (got '${version}')"

# path|python regex with one group around the version|description
locations=(
  'gateway/Cargo.toml|(?m)^\[workspace\.package\][^\[]*?^version = "([^"]+)"|gateway workspace version'
  'control-plane/pyproject.toml|(?m)^\[project\][^\[]*?^version = "([^"]+)"|control-plane pyproject'
  'control-plane/src/lumewarp_control_plane/__init__.py|(?m)^__version__ = "([^"]+)"|control-plane __version__'
  'control-plane/uv.lock|(?m)^name = "lumewarp-control-plane"\nversion = "([^"]+)"|control-plane uv.lock'
  'ui/package.json|(?m)^  "version": "([^"]+)"|ui package.json'
  'deploy/helm/lumewarp/Chart.yaml|(?m)^version: (\S+)$|chart version'
  'deploy/helm/lumewarp/Chart.yaml|(?m)^appVersion: "([^"]+)"|chart appVersion'
  'deploy/helm/lumewarp-stack/Chart.yaml|(?m)^version: (\S+)$|umbrella chart version'
  'deploy/helm/lumewarp-stack/Chart.yaml|(?m)^appVersion: "([^"]+)"|umbrella chart appVersion'
  'deploy/helm/lumewarp-stack/Chart.yaml|(?m)^  - name: lumewarp\n(?:    (?!version:).*\n)*?    version: "?([^"\n]+?)"?$|umbrella dependency on lumewarp'
  'deploy/helm/lumewarp-stack/Chart.lock|(?m)^- name: lumewarp\n  repository: [^\n]*\n  version: (\S+)$|umbrella Chart.lock lumewarp'
)
# Every workspace member of the gateway in Cargo.lock.
while IFS= read -r manifest; do
  grep -q '^version.workspace = true' "$manifest" || continue
  name="$(sed -n 's/^name = "\(.*\)"/\1/p' "$manifest" | head -1)"
  locations+=("gateway/Cargo.lock|(?m)^name = \"${name}\"\\nversion = \"([^\"]+)\"|Cargo.lock ${name}")
  # The example plugins lock the SDK crates by path; their lockfiles carry the version too.
  while IFS= read -r lock; do
    grep -q "^name = \"${name}\"$" "$lock" || continue
    locations+=("${lock}|(?m)^name = \"${name}\"\\nversion = \"([^\"]+)\"|${lock#gateway/plugins/} ${name}")
  done < <(find gateway/plugins -name Cargo.lock | sort)
done < <(find gateway/crates -name Cargo.toml | sort)

# Prints "<found>\t<description>" per location; with a second argument, rewrites it.
versions() {
  python3 -I - "$1" "${2:-}" "${locations[@]}" <<'PY'
import re, sys
check, new, *locations = sys.argv[1:]
missing = False
for entry in locations:
    path, pattern, what = entry.split("|")
    text = open(path).read()
    match = re.search(pattern, text)
    if not match:
        print(f"MISSING\t{what} ({path})")
        missing = True
        continue
    print(f"{match[1]}\t{what} ({path})")
    if new:
        text = text[: match.start(1)] + new + text[match.end(1) :]
        open(path, "w").write(text)
sys.exit(1 if missing else 0)
PY
}

if (( check )); then
  bad=0
  while IFS=$'\t' read -r found what; do
    if [[ "$found" != "$version" ]]; then
      echo "::error::${what} is ${found}, the tag is ${version}" >&2
      bad=1
    fi
  done < <(versions "$version")
  (( bad == 0 )) || die "versions do not match v${version}; run scripts/release.sh ${version} and push the release commit"
  echo "release: every version is ${version}"
  exit 0
fi

[[ "$(git rev-parse --abbrev-ref HEAD)" == "main" ]] || die "release from main"
[[ -z "$(git status --porcelain --untracked-files=no)" ]] || die "commit or stash tracked changes first"
git rev-parse -q --verify "refs/tags/v${version}" >/dev/null && die "tag v${version} already exists"

versions "$version" "$version" >/dev/null
scripts/release.sh --check "$version"

# The lock's digest covers the dependency versions: let helm recompute it (helm from PATH,
# or the alpine/helm image through docker; the OpenFGA chart comes from its repository).
stack=deploy/helm/lumewarp-stack
openfga_repo=https://openfga.github.io/helm-charts
if command -v helm >/dev/null 2>&1; then
  helm repo add openfga "$openfga_repo" >/dev/null 2>&1 || true
  helm dependency update "$stack" >/dev/null
else
  docker run --rm --entrypoint sh -e HOME=/tmp -u "$(id -u):$(id -g)" -v "$PWD:/c" -w /c alpine/helm:3.16.4 \
    -c "helm repo add openfga $openfga_repo >/dev/null && helm dependency update $stack >/dev/null"
fi
scripts/release.sh --check "$version"

git add gateway/Cargo.toml gateway/Cargo.lock control-plane/pyproject.toml \
  control-plane/src/lumewarp_control_plane/__init__.py control-plane/uv.lock \
  ui/package.json deploy/helm/lumewarp/Chart.yaml "$stack/Chart.yaml" "$stack/Chart.lock"
# The images in the plain manifests carry the new version.
deploy/kubernetes/generate.sh
git add deploy/kubernetes
# --allow-empty: the first release can already carry its version everywhere.
git commit -q --allow-empty -m "Release v${version}"
git tag -a "v${version}" -m "Release v${version}"
echo "release: committed and tagged v${version}. Push both:"
echo "  git push origin main && git push origin v${version}"
