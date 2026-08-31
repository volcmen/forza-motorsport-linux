# Verification

Run the complete repository gate from a clean checkout:

```bash
PATH="$PWD/.superpowers/tools/bin:$PATH" just verify
git diff --check
```

| Evidence | Command | What it proves |
| --- | --- | --- |
| Python tests | `uv run pytest -q` | Patcher, installer/recovery, and policy behavior against synthetic or temporary filesystems. |
| Shell tests | Four `bash tests/test_*.bash` behavior suites | Launcher, doctor, installation, and Steam-option contracts. |
| User-unit syntax | `systemd-analyze --user verify config/xodus-forza.service` | The checked-in Xodus unit parses under systemd's user-unit verifier. |
| Shell analysis | ShellCheck and shfmt over actual Bash entry points | Bash syntax and formatting without treating Python helpers as shell. |
| Python analysis | Ruff over Python tests and the extensionless Python helpers | Python style and correctness checks. |
| Licensing | `uv run reuse lint` | Every tracked original file has SPDX/REUSE coverage and declared license text. |
| Privacy | `uv run pytest tests/test_repository_policy.py -q` | No prohibited binaries or known live-secret markers, plus public README boundaries. |
| Whitespace | `git diff --check` | No whitespace errors in the working change. |

The tests use only synthetic patch bytes and temporary roots. A passing gate proves repository contracts, not game compatibility, account authentication, online play, or invite delivery. CI runs the same `just verify` command and deliberately uploads no system information, runtime logs, or artifacts.

## Integration branch readiness

A green repository gate does not make `feature/integration-repository`
merge-ready or publication-ready. `scripts/patch-known-build` still has an
unresolved publication race when a validated target is concurrently replaced
by a symlink or directory: the current publication/rollback path does not
correctly restore that replacement. The checks above do not cover that
outstanding case, and this document does not claim a fix.

## Local Wine source evidence

Two source branches were built from canonical
[`xodus-gaming/wine`](https://github.com/xodus-gaming/wine) `bleeding-edge` at
`b1dd32734a34472a28eb5be9922df06e07ac0834`. As of 2026-08-31, both branches
and all listed commits remain local, planned, and unpublished. Their SHAs are
provenance records, not public fetch locations, and this repository vendors no
compiled Wine artifacts.

| Source branch | Final local commit | Compile/link | Wine PE runtime | Live/manual |
| --- | --- | --- | --- | --- |
| [`fix/wgi-physical-nonroamable-id`](../patches/wine-controller/README.md) | `ddd302d97c6008d79ea4f3e3ad56014cb548e514` | **GREEN** | **BLOCKED before test dispatch** | Controller/Forza matrix **NOT RUN** |
| [`fix/storage-trim-property`](../patches/wine-storage-trim/README.md) | `a7719bd8d0719e5ea6061387db020fa6a6b39d27` | **GREEN** | **BLOCKED before test dispatch** | Installation and Forza/AP702 A/B **NOT RUN** |

Both focused PE test commands stopped during canonical Wine bootstrap with
`secur32.dll` initialization failure and `kernel32.dll` status `c0000135`,
before either task's assertions ran. This does not downgrade the recorded
source compile/link result, but it also cannot be reported as runtime GREEN.
No source branch or artifact was installed into the live compatibility tool or
game prefix, and no live Steam setting was changed by these source tasks.

The controller evidence records the supported physical XInput identity grammar
and the outstanding manual controller matrix. The storage evidence records the
unconditional `TrimEnabled = TRUE` compatibility policy; that policy requires
maintainer agreement and a canonical device-detection design before canonical
Wine inclusion. See each linked provenance document for the exact changed
files, commands, exit states, and semantic limitations.
