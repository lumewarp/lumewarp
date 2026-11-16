#!/usr/bin/env bash
# Render the plain Kubernetes manifests from the Helm chart.
#
#   deploy/kubernetes/generate.sh                         rewrite base/ (one file per kind + kustomization.yaml)
#   deploy/kubernetes/generate.sh --single FILE [--version X]
#                                                         one combined YAML with a Namespace, for the release asset
#
# The input is deploy/kubernetes/values.yaml; the release is `lumewarp` in namespace
# `lumewarp`, and image tags are the chart appVersion (--version overrides them in
# --single mode). Helm-only metadata is removed, and the migration Job is a plain Job:
# the control plane reports /readyz only once the database is at the migration head, so
# it may start before the Job finishes and just stays unready until then.
# Uses helm from PATH, or the alpine/helm image through docker. Output is deterministic.
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

if command -v helm >/dev/null 2>&1; then
  helm_run=(helm)
  chart=deploy/helm/lumewarp
  cd "$root"
elif command -v docker >/dev/null 2>&1; then
  helm_run=(docker run --rm -v "$root:/c:ro" -w /c alpine/helm:3.16.4)
  chart=deploy/helm/lumewarp
else
  echo "generate.sh needs helm on PATH, or docker for alpine/helm:3.16.4" >&2
  exit 1
fi

rendered="$("${helm_run[@]}" template lumewarp "$chart" -n "$namespace" \
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
    # Hook annotations (and the comment above them) are all the Job's annotations carry.
    text = re.sub(r"^  annotations:\n(?:    .*\n?)+", "", text, flags=re.M) if "helm.sh/hook" in text else text
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
images = sorted({m for t in docs for m in re.findall(r'image: "([^":]+):[^"]*"', t)})
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
