test:
    uv run pytest -q
    bash tests/test_launcher.bash
    bash tests/test_doctor.bash
    bash tests/test_setup.bash
    bash tests/test_bootstrap_cli.bash
    bash tests/test_steam_options.bash
    bash tests/test_installation.bash
    systemd-analyze --user verify config/xodus-forza.service

lint:
    shellcheck -e SC2317,SC2329 setup bin/forza-linux bin/forza-doctor scripts/print-steam-options scripts/install-user scripts/uninstall-user containers/bootstrap-builder/*.sh tests/*.bash
    shfmt -d -i 4 -ci setup bin/forza-linux bin/forza-doctor scripts/print-steam-options scripts/install-user scripts/uninstall-user containers/bootstrap-builder/*.sh tests/*.bash
    uv run ruff check scripts/secure-user-files scripts/patch-known-build scripts/install-runtime-components scripts/forza-bootstrap forza_bootstrap/*.py tools/*.py tools/build-bootstrap-builder tools/build-bootstrap-bundle tests/*.py

reuse:
    uv run reuse lint

privacy:
    uv run pytest tests/test_repository_policy.py -q

verify: test lint reuse privacy
    git diff --check
