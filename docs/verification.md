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
merge-ready or publication-ready. The previously recorded
`scripts/patch-known-build` publication race was resolved and independently
approved at integration commit
`5d7b4fe460813d23b08b7c59731048e446ef3ddd`. Later cross-plan installed-artifact
and manual game/service gates remain outstanding, so this source result does
not claim merge, publication, installation, or gameplay readiness.

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

## Local Xodus social source evidence

The scoped Xodus social branch was developed from official `xodus/main` at
`a92abacb0743f16c769279b9057c8c28435e5ef9`. The implementation source range is
`a92abacb0743f16c769279b9057c8c28435e5ef9..fb38a3f7ad6c9112e8779f7908ffdcc2b2468f30`;
the final source commit is `fb38a3f7ad6c9112e8779f7908ffdcc2b2468f30`
(`refactor: isolate the social overlay protocol`). The separate Xodus
maintainer-documentation commits are
`966cb58586db725a1d1201a34457eb2e4a62f33f` and
`e8bcd8fcfc473e7ad019c3ee8d32841ee6f98fd3`.

The local source commits in that range are:

| Commit | Scope |
| --- | --- |
| `baa811b1d6d08b0cdd7e73a6a202d9b9e6616aec` | Title-scoped Xbox authorization |
| `c4491c0ce8107b79de284062163191008772346c` | Authorization privacy hardening |
| `4e297f65144a5d958e905e19adb9ba7927165a3c` | PeopleHub and Multiplayer Activity operations |
| `b42de8d81e5b9b0f44a2fbe12b74e143d01ab00c` | Activity and PeopleHub contract hardening |
| `778c25ae90278b600e804f32b7aee4839b3fec8d` | Bounded social and activation IPC |
| `eaaec55ea49b7c0b650429164c7c15260487eb10` | Keyboard/controller social overlay |
| `c202927a56215321064e98ccbd995533e3d7b2e8` | Overlay lifecycle and reentrancy hardening |
| `40e76edc822c5880ad9f32b2daae69f15659751e` | Service-owned XDUI launcher |
| `fb38a3f7ad6c9112e8779f7908ffdcc2b2468f30` | Keyring-free shared social protocol crate |

### Automated source gates

The following commands used cached dependencies and did not start the service,
overlay, keychain, game, or a network request:

```bash
cargo fmt --check
cargo clippy --workspace --all-targets --offline -- -D warnings
git diff --exit-code a92abacb0743f16c769279b9057c8c28435e5ef9..HEAD -- crates/xodus-cli/src/commands/streaming.rs
! cargo tree -p xodus-overlay --offline | rg 'keyring|secret-service'
! rg -n 'use xodus::|xodus::' crates/xodus-overlay
cargo clippy -p xodus-social-protocol -p xodus -p xodus-service -p xodus-overlay --all-targets --offline -- -D warnings
cargo test -p xodus-social-protocol --offline
cargo test -p xodus --offline -- --skip test_get_xbox_live_dev_token
cargo test -p xodus-service --offline
cargo test -p xodus-overlay --offline
cargo test --workspace --offline -- --skip test_get_xbox_live_dev_token
cargo build --release -p xodus-service -p xodus-overlay --offline
rg -n 'Authorization:|XBL3\.0|ms-xbl-multiplayer://inviteAccept\?.+' docs crates || true
git diff --check
```

Results on 2026-09-01:

- `cargo fmt --check`: **GREEN**.
- Full-workspace Clippy: **BLOCKED BY UNCHANGED UPSTREAM BASELINE**. Only
  `xodus-cli`'s unchanged `run_cli_reader` (11 arguments) and `run_reader` (10
  arguments) violate `clippy::too_many_arguments`' default limit of 7. The
  base-to-tip diff for `crates/xodus-cli/src/commands/streaming.rs` is empty;
  no suppression or source edit was made.
- The required dependency-tree RED found `dbus-secret-service-keyring-store`,
  `dbus-secret-service`, and `keyring-core` below `xodus-overlay`. After the
  extraction, neither the dependency scan nor the overlay `xodus::` source scan
  returns a match: **GREEN**. The new `xodus-social-protocol` crate directly
  depends only on `serde`, `serde_json`, `thiserror`, `tokio`, and `uuid`.
- Strict all-target offline Clippy for every changed package
  (`xodus-social-protocol`, `xodus`, `xodus-service`, and `xodus-overlay`):
  **GREEN**, no issues.
- Protocol compatibility tests: **GREEN**, 6 passed, including a literal XDSO
  version-1 frame assertion.
- Filtered offline workspace tests: **GREEN**, 86 passed, 1 ignored, and 1
  filtered out across 16 suites. `test_get_xbox_live_dev_token` was explicitly
  skipped because it is an upstream anonymous live Microsoft test.
- Offline release build for `xodus-service` and `xodus-overlay`: **GREEN**.
- Privacy scan: 13 matches, all classified below; no live value was recorded.

Privacy matches were reviewed individually and fall into four safe classes:

1. static documentation templates with visible placeholders in the existing
   Xbox documentation;
2. production authorization header format strings that contain no runtime
   token/hash value;
3. visibly synthetic test sentinels and activation protocol-shape assertions;
4. the literal privacy-search expression in `docs/forza-social.md`.

No raw live XUID, authorization token, connection string, activation URI, or
user/friend nickname is present in this evidence.

### Manual and installation boundary

| Check | Status |
| --- | --- |
| Keyboard navigation/action parity | **NOT RUN** |
| Physical Xbox controller navigation/hotplug | **NOT RUN** |
| Invite from a published Private Multiplayer lobby | **NOT RUN** |
| Join a compatible remote activity | **NOT RUN** |
| Deliberate cancellation | **NOT RUN** |
| Peer disconnect/service shutdown cleanup | **NOT RUN** |
| Automatic incoming invite notification | **NOT SUPPORTED** |

The game-facing path still depends on the separately reviewed `xgameruntime`
bridge and a future installed-artifact hash/parity gate. No compiled Xodus
artifact was installed. The live Xodus checkout, installed service, keychain,
compatibility prefix, Steam configuration, and game were not mutated or
started. The branch remains local: it has not been pushed and no public pull
request exists.
