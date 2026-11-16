#!/usr/bin/env bash
# Render the plain Kubernetes manifests from the lumewarp-stack chart: PostgreSQL, OpenFGA,
# the control plane and a gateway.
#
#   deploy/kubernetes/generate.sh                         rewrite manifests/ (one file per component)
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

HELM_OUT="$rendered" NAMESPACE="$namespace" SINGLE="$single" HERE="$here" \
  python3 -I - <<'PY'
import os, re
from pathlib import Path

namespace = os.environ["NAMESPACE"]
single = os.environ["SINGLE"]
here = Path(os.environ["HERE"])

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

ns_doc = f"apiVersion: v1\nkind: Namespace\nmetadata:\n  name: {namespace}\n"


def with_namespace(text):
    """An explicit namespace on every object, so each file applies as is with kubectl."""
    return re.sub(r"^metadata:\n", f"metadata:\n  namespace: {namespace}\n", text, count=1, flags=re.M)


# One file per component, numbered in the order the pieces depend on each other; inside a
# file, the objects in the order kubectl should create them.
COMPONENTS = [
    ("10-postgres.yaml", ("lumewarp-postgres",)),
    ("20-openfga.yaml", ("lumewarp-openfga",)),
    ("30-control-plane.yaml", ("lumewarp-control-plane", "lumewarp-migrate")),
    ("40-gateway.yaml", ("lumewarp-gateway",)),
]
KIND_ORDER = ["ServiceAccount", "Role", "RoleBinding", "ConfigMap", "Secret", "Service",
              "StatefulSet", "Deployment", "Job"]


def component(text):
    name = re.search(r"^  name: (\S+)", text, flags=re.M)[1]
    for file, prefixes in COMPONENTS:
        if name.startswith(prefixes):
            return file
    raise SystemExit(f"generate.sh: no component for object {name!r}; extend COMPONENTS")


def kind_rank(text):
    kind = re.search(r"^kind: (\S+)", text, flags=re.M)[1]
    return KIND_ORDER.index(kind) if kind in KIND_ORDER else len(KIND_ORDER)


files = {"00-namespace.yaml": [ns_doc]}
for text in docs:
    files.setdefault(component(text), []).append(with_namespace(text))
for name in files:
    if name != "00-namespace.yaml":
        files[name].sort(key=kind_rank)  # stable: Helm's order within a kind is kept

if single:
    Path(single).write_text("---\n" + "---\n".join(t for n in sorted(files) for t in files[n]))
    raise SystemExit

out = here / "manifests"
out.mkdir(parents=True, exist_ok=True)
for old in out.glob("*.yaml"):
    old.unlink()
for name, texts in files.items():
    (out / name).write_text("---\n".join(texts))
PY
