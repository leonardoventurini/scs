set shell := ["zsh", "-cu"]

setup:
    uv sync --all-groups
    ./scripts/build-native.sh
    uv run --all-groups pre-commit install

test:
    uv run --all-groups pytest -v

coverage: native-build
    uv run --all-groups coverage erase
    uv run --all-groups coverage run -m pytest -q
    uv run --all-groups coverage report

typecheck:
    uv run --all-groups basedpyright

lint:
    uv run ruff check src tests proxy/src proxy/tests

native-test:
    cargo test --workspace

native-build:
    ./scripts/build-native.sh

verify: typecheck lint coverage native-test
