test:
    uv run pytest -q

lint:
    shellcheck bin/* tests/*.bash
    shfmt -d -i 4 -ci bin/* tests/*.bash
    uv run ruff check scripts tests scripts/patch-known-build

privacy:
    uv run pytest tests/test_repository_policy.py -q

verify: test lint privacy
    git diff --check
