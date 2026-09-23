set shell := ["sh", "-cu"]

repository := "leonardoventurini/scs"

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
    uv run ruff check src tests

native-test:
    cargo test --workspace

native-build:
    ./scripts/build-native.sh

eval-search suite="evals/scs-search-v1.json" repo="." k="10" repeats="1":
    uv run python scripts/evaluate-search.py --suite "{{suite}}" --repo "{{repo}}" --k "{{k}}" --repeats "{{repeats}}" --result-detail compact

eval-query suite="evals/scs-query-v2.json" repo="." mode="balanced" k="10" repeats="1":
    uv run --extra laya python scripts/evaluate-query.py --suite "{{suite}}" --repo "{{repo}}" --mode "{{mode}}" --k "{{k}}" --repeats "{{repeats}}"

verify: typecheck lint coverage native-test

# Install or upgrade the local SCS tool from a GitHub release (default: latest).
install version="":
    #!/usr/bin/env bash
    set -euo pipefail
    version="{{version}}"
    if [ -z "$version" ]; then
        tag="$(gh release view --repo {{repository}} --json tagName --jq .tagName)"
        version="${tag#v}"
    fi
    scripts/install.sh --version "$version"

alias upgrade := install
