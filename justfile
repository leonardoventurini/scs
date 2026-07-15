set shell := ["zsh", "-cu"]

setup:
    uv sync --all-groups

test:
    uv run --all-groups pytest -v

typecheck:
    uv run python -m compileall -q src tests

native-test:
    cargo test --workspace

verify: typecheck test

