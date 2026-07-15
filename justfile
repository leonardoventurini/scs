set shell := ["zsh", "-cu"]

setup:
    uv sync --all-groups
    ./scripts/build-native.sh

test:
    uv run --all-groups pytest -v

typecheck:
    uv run python -m compileall -q src tests

native-test:
    cargo test --workspace

native-build:
    ./scripts/build-native.sh

verify: typecheck test native-test
