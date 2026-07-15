#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
command="${1:-verify}"
shift || true
external-product=""
scs_baseline=""
task_9=""
task_8=""
task_7=""
rollback_dir=""
execute=false

while (($#)); do
  case "$1" in
    --external-product) external-product="${2:?}"; shift 2 ;;
    --scs-baseline) scs_baseline="${2:?}"; shift 2 ;;
    --external-product-task-9) task_9="${2:?}"; shift 2 ;;
    --external-product-task-8) task_8="${2:?}"; shift 2 ;;
    --external-product-task-7) task_7="${2:?}"; shift 2 ;;
    --rollback-dir) rollback_dir="${2:?}"; shift 2 ;;
    --execute) execute=true; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

[[ "$command" == verify || "$command" == rollback ]] || { echo "usage: $0 verify|rollback ..." >&2; exit 2; }
[[ -n "$external-product" && -d "$external-product/.git" ]] || { echo "--external-product must name a Git checkout" >&2; exit 2; }
for value in "$scs_baseline" "$task_9" "$task_8" "$task_7"; do
  [[ -n "$value" ]] || { echo "all baseline/task SHA arguments are required" >&2; exit 2; }
done

git -C "$root" diff --quiet && git -C "$root" diff --cached --quiet || { echo "SCS worktree must be clean" >&2; exit 1; }
git -C "$external-product" diff --quiet && git -C "$external-product" diff --cached --quiet || { echo "External product worktree must be clean" >&2; exit 1; }
git -C "$root" cat-file -e "${scs_baseline}^{commit}"
for revision in "$task_9" "$task_8" "$task_7"; do git -C "$external-product" cat-file -e "${revision}^{commit}"; done
git -C "$root" merge-base --is-ancestor "$scs_baseline" HEAD
git -C "$external-product" merge-base --is-ancestor "$task_7" "$task_8"
git -C "$external-product" merge-base --is-ancestor "$task_8" "$task_9"

if [[ "$command" == verify ]]; then
  echo "rollback commit graph and clean-worktree preconditions: ok"
  exit 0
fi

if [[ "$execute" != true || "${SCS_ROLLBACK_CONFIRMED:-}" != 1 ]]; then
  echo "rollback is destructive; rerun with --execute and SCS_ROLLBACK_CONFIRMED=1" >&2
  exit 2
fi
[[ -n "$rollback_dir" ]] || { echo "--rollback-dir is required for immutable rollback evidence" >&2; exit 2; }

cd "$root"
uv run scs service stop
python3 - <<'PY'
import socket
for port in (28463, 28465):
    with socket.socket() as probe:
        probe.settimeout(0.2)
        if probe.connect_ex(("127.0.0.1", port)) == 0:
            raise SystemExit(f"port remains occupied after SCS stop: {port}")
PY

git -C "$external-product" revert --no-edit "$task_9"
git -C "$external-product" revert --no-edit "$task_8"
git -C "$external-product" revert --no-edit "$task_7"
mkdir -p "$rollback_dir"
git -C "$external-product" bundle create "$rollback_dir/external-product-restored.bundle" HEAD
shasum -a 256 "$rollback_dir/external-product-restored.bundle" > "$rollback_dir/external-product-restored.bundle.sha256"
echo "External product source rollback restored and immutable bundle recorded. SCS_HOME was not modified."
