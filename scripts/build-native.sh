#!/usr/bin/env bash
# Build and install the private SCS PyO3 extension into the project environment.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
VENV="$PROJECT_ROOT/.venv"

if [[ ! -x "$VENV/bin/python" ]]; then
    echo "SCS virtual environment is missing; run 'uv sync --all-groups' first." >&2
    exit 2
fi

export VIRTUAL_ENV="$VENV"
export PATH="$VENV/bin:$PATH"
export PYO3_USE_ABI3_FORWARD_COMPATIBILITY=1

cd "$PROJECT_ROOT"
uvx --from maturin maturin develop "$@"
