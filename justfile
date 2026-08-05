set shell := ["zsh", "-cu"]

setup:
    uv sync --all-groups
    ./scripts/build-native.sh
    uv run --all-groups pre-commit install

test:
    uv run --all-groups pytest -v

typecheck:
    uv run --all-groups basedpyright

lint:
    uv run ruff check src tests proxy/src proxy/tests

native-test:
    cargo test --workspace

native-build:
    ./scripts/build-native.sh

verify: typecheck lint test native-test
