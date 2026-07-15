#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
external-product=""
while (($#)); do
  case "$1" in
    --external-product)
      external-product="${2:?--external-product requires a path}"
      shift 2
      ;;
    *)
      echo "unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

if [[ -n "$external-product" && ! -d "$external-product" ]]; then
  echo "External product checkout does not exist: $external-product" >&2
  exit 2
fi

cd "$root"
uv run --all-groups pytest tests/isolation tests/integration -v

if [[ -n "$external-product" ]]; then
  if rg -n --glob '*.py' '(^|[[:space:]])(from|import)[[:space:]]+(external-product|knowledge)([.[:space:]]|$)' src proxy/src; then
    echo "SCS runtime imports a External product-owned Python package" >&2
    exit 1
  fi
  echo "External product checkout supplied only as an independence witness: $(git -C "$external-product" rev-parse --short HEAD)"
fi

echo "SCS isolation E2E: ok"
