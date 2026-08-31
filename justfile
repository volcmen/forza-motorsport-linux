test:
    uv run pytest -q

lint:
    shellcheck -e SC2329 bin/forza-linux bin/forza-doctor scripts/print-steam-options scripts/install-user scripts/uninstall-user tests/*.bash
    shfmt -d -i 4 -ci bin/forza-linux bin/forza-doctor scripts/print-steam-options scripts/install-user scripts/uninstall-user tests/*.bash
    uv run ruff check scripts/secure-user-files scripts/patch-known-build tests/*.py

privacy:
    uv run pytest tests/test_repository_policy.py -q

verify: test lint privacy
    git diff --check
