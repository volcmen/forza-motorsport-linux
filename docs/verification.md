# Verification

Run the complete repository gate from a clean checkout:

```bash
PATH="$PWD/.superpowers/tools/bin:$PATH" just verify
git diff --check
```

| Evidence | Command | What it proves |
| --- | --- | --- |
| Python tests | `uv run pytest -q` | Patcher, installer/recovery, and policy behavior against synthetic or temporary filesystems. |
| Shell tests | Included in the pytest gate | Launcher, doctor, installation, and Steam-option contracts. |
| Shell analysis | ShellCheck and shfmt over actual Bash entry points | Bash syntax and formatting without treating Python helpers as shell. |
| Python analysis | Ruff over Python tests and the extensionless Python helpers | Python style and correctness checks. |
| Privacy | `uv run pytest tests/test_repository_policy.py -q` | No prohibited binaries or known live-secret markers, plus public README boundaries. |
| Whitespace | `git diff --check` | No whitespace errors in the working change. |

The tests use only synthetic patch bytes and temporary roots. A passing gate proves repository contracts, not game compatibility, account authentication, online play, or invite delivery. CI runs the same `just verify` command and deliberately uploads no system information, runtime logs, or artifacts.
