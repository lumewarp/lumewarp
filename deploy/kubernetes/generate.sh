#!/usr/bin/env bash
# Render the plain Kubernetes manifests from the lumewarp-stack chart: PostgreSQL, OpenFGA,
# the control plane and a gateway.
#
#   deploy/kubernetes/generate.sh                         rewrite base/ (one file per kind + kustomization.yaml)
#   deploy/kubernetes/generate.sh --single FILE [--version X]
#                                                         one combined YAML with a Namespace, for the release asset
#
# The input is deploy/kubernetes/values.yaml; the release is `lumewarp` in namespace
# `lumewarp`, and the Lumewarp image tags are the chart appVersion (--version overrides them
# in --single mode). Helm-only metadata and test hooks are removed. Both schema migrations
# are plain Jobs that wait for PostgreSQL: the control plane reports /readyz only once its
# database is at the migration head, and OpenFGA's pods wait for their Job to complete.
# Nothing secret is rendered (create-secrets.sh makes the Secrets).
# Uses helm from PATH, or the alpine/helm image through docker; the OpenFGA chart is fetched
# from its repository for `helm dependency build`. Output is deterministic.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
here="$root/deploy/kubernetes"
namespace=lumewarp

single=""
version=""
while (($#)); do
  case "$1" in
    --single) single="${2:?--single needs a file}"; shift 2 ;;
    --version) version="${2:?--version needs a value}"; shift 2 ;;
    *) echo "usage: generate.sh [--single FILE [--version X]]" >&2; exit 2 ;;
  esac
done
[[ -z "$version" || -n "$single" ]] || { echo "generate.sh: --version needs --single" >&2; exit 2; }

set_args=()
[[ -n "$version" ]] && set_args=(--set "global.imageTag=${version}")

chart=deploy/helm/lumewarp-stack
repo=https://openfga.github.io/helm-charts
if command -v helm >/dev/null 2>&1; then
  cd "$root"
  helm repo add openfga "$repo" >/dev/null 2>&1 || true
  helm dependency build "$chart" >/dev/null
  helm_run=(helm)
elif command -v docker >/dev/null 2>&1; then
  docker run --rm --entrypoint sh -e HOME=/tmp -v "$root:/c" -w /c alpine/helm:3.16.4 \
    -c "helm repo add openfga $repo >/dev/null && helm dependency build $chart >/dev/null"
  helm_run=(docker run --rm -v "$root:/c:ro" -w /c alpine/helm:3.16.4)
else
  echo "generate.sh needs helm on PATH, or docker for alpine/helm:3.16.4" >&2
  exit 1
fi

rendered="$("${helm_run[@]}" template lumewarp "$chart" -n "$namespace" --skip-tests \
  -f deploy/kubernetes/values.yaml ${set_args[@]+"${set_args[@]}"})"

app_version="$(sed -n 's/^appVersion: "\{0,1\}\([^"]*\)"\{0,1\}$/\1/p' "$root/deploy/helm/lumewarp/Chart.yaml")"

HELM_OUT="$rendered" NAMESPACE="$namespace" SINGLE="$single" HERE="$here" APP_VERSION="${version:-$app_version}" \
  python3 -I - <<'PY'
import os, re
from pathlib import Path

namespace = os.environ["NAMESPACE"]
single = os.environ["SINGLE"]
here = Path(os.environ["HERE"])
tag = os.environ["APP_VERSION"]

docs = []
for chunk in re.split(r"^---\s*$", os.environ["HELM_OUT"], flags=re.M):
    # Helm-only metadata: the chart label, the managed-by label, the source comment.
    lines = [
        l.rstrip()
        for l in chunk.splitlines()
        if not re.match(r"\s*(helm\.sh/chart:|app\.kubernetes\.io/managed-by: Helm|# Source:)", l)
    ]
    text = "\n".join(lines).strip("\n")
    if not re.search(r"^kind:", text, flags=re.M):
        continue
    # Hook annotations (on an object or a pod template) make no sense without Helm; an
    # annotations block left empty, with any comment line above it, goes too.
    text = "\n".join(l for l in text.splitlines() if not re.match(r"\s*helm\.sh/hook", l))
    text = re.sub(r"^(\s*#[^\n]*\n)*(\s*)annotations:\n(?!\2\s)", "", text + "\n", flags=re.M).rstrip("\n")
    docs.append(text + "\n")

by_kind = {}
for text in docs:
    kind = re.search(r"^kind: (\S+)", text, flags=re.M)[1]
    by_kind.setdefault(kind, []).append(text)

ns_doc = f"apiVersion: v1\nkind: Namespace\nmetadata:\n  name: {namespace}\n"

if single:
    parts = [ns_doc] + [t for kind in sorted(by_kind) for t in by_kind[kind]]
    Path(single).write_text("---\n" + "---\n".join(
        # An explicit namespace on everything namespaced, so the file applies as is.
        re.sub(r"^metadata:\n", f"metadata:\n  namespace: {namespace}\n", t, count=1, flags=re.M)
        if not t.startswith("apiVersion: v1\nkind: Namespace") else t
        for t in parts
    ))
    raise SystemExit

base = here / "base"
base.mkdir(parents=True, exist_ok=True)
for old in base.glob("*.yaml"):
    old.unlink()
files = {"namespace.yaml": ns_doc}
for kind, texts in by_kind.items():
    files[f"{kind.lower()}.yaml"] = "---\n".join(texts)
# Only the Lumewarp images follow the release; PostgreSQL, OpenFGA and helpers stay pinned.
images = sorted({
    m for t in docs for m in re.findall(r'image: "?([^":\s]+):[^"\s]*"?', t)
    if m.startswith("ghcr.io/lumewarp/")
})
for name, text in files.items():
    (base / name).write_text(text)
kustomization = [
    "apiVersion: kustomize.config.k8s.io/v1beta1",
    "kind: Kustomization",
    f"namespace: {namespace}",
    "resources:",
    *[f"  - {n}" for n in sorted(files)],
    "images:",
]
for image in images:
    kustomization += [f"  - name: {image}", f"    newTag: \"{tag}\""]
(base / "kustomization.yaml").write_text("\n".join(kustomization) + "\n")
PY
