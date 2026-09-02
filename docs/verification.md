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

## Current manual evidence

The table below records the human-observed live matrix for the exact
`legacy-v0.1` revisions and artifact hashes in
[`v0.1-live-validation.md`](evidence/v0.1-live-validation.md). Automated and
hermetic green results alone do not upgrade a row.

| Claim | Current status |
| --- | --- |
| Game launch past the logo | **VERIFIED CURRENT** |
| Online profile and content download | **VERIFIED CURRENT** |
| Controller navigation and driving | **VERIFIED CURRENT** |
| Controller disconnect and reconnect | **VERIFIED CURRENT** |
| AP702 absent | **VERIFIED CURRENT** |
| Linux-to-Windows Xbox invite | **VERIFIED CURRENT** |
| Linux join to a Windows player's published activity | **VERIFIED CURRENT** |
| Keyboard and controller operation in the social picker | **VERIFIED CURRENT** |
| Clean Xodus and socket shutdown after game exit | **VERIFIED CURRENT** |
| Second launch without a system reboot | **VERIFIED CURRENT** |
| Automatic incoming invite notifications | **NOT SUPPORTED** |

The newer WineGDK `d96a768e25f632b04a457e4cb9f585e89ef5d095` and Xodus
`ee9db0f68122a9731b9b66cc24767693d1737f5c` pair is **REJECTED LIVE**. Its
hermetic source gates are not game-compatibility evidence and it is not the
`legacy-v0.1` installable profile.

## Publication authority

`docs/publication-manifest.toml` is the machine-readable publication authority.
It records component revisions and public open-source artifact hashes. The final
integration revision is not stored in the manifest: it is the commit referenced
by the immutable `v0.1.0` tag. A `docs/release-v0.1.0.md` release document
cannot be created while any required manifest claim is `unresolved`.

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
| [`fix/wgi-physical-nonroamable-id`](../patches/wine-controller/README.md) | `ddd302d97c6008d79ea4f3e3ad56014cb548e514` | **GREEN** | **BLOCKED before test dispatch** | Controller behavior **VERIFIED** through the reviewed exact-build fallback; this source branch was not installed. |
| [`fix/storage-trim-property`](../patches/wine-storage-trim/README.md) | `a7719bd8d0719e5ea6061387db020fa6a6b39d27` | **GREEN** | **BLOCKED before test dispatch** | AP702 absence **VERIFIED** through the reviewed exact-build fallback; this source branch was not installed. |

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
`a92abacb0743f16c769279b9057c8c28435e5ef9`. The final reviewed public source range is
`a92abacb0743f16c769279b9057c8c28435e5ef9..1aad2cbd6059801546d9519ceab183097e84f0a0`;
the final source commit is `1aad2cbd6059801546d9519ceab183097e84f0a0`
(`feat: version the invite-only social overlay request`). Every later
experimental XGameRuntime build/install/evidence step must consume that source
tree or the byte-identical final documentation tip. The
separate Xodus
maintainer-documentation commits are
`c82ca585ad4c6bf91888db36a279a19ee2d96444` and
`655e1277cad388b996c4aacb7b4bf7b997bb65fa`, followed by final lifecycle
documentation commit `e8a466178716f6a6a0a343e761883703ffaa1a40`
and versioned-XDUI documentation commit
`ee9db0f68122a9731b9b66cc24767693d1737f5c`.

The public-history rewrite preserved every source tree and commit message while
replacing only author/committer metadata with the project's GitHub noreply
identity.

The local source commits in that range are:

| Commit | Scope |
| --- | --- |
| `e9b43ab695f3ff618bb8d2319bd75cedee7fbbe8` | Title-scoped Xbox authorization |
| `0593b875b886caaae1a558f1f3542f25e17625e7` | Authorization privacy hardening |
| `a64500a32cae7667a66f9184a67a24c293cd9c95` | PeopleHub and Multiplayer Activity operations |
| `c93bbc2c780bcede68a67d6f2d8cac5f72e11842` | Activity and PeopleHub contract hardening |
| `8c126a4900390231995129c72afbf038d7c1d414` | Bounded social and activation IPC |
| `12ed1eb0fbee891b54eb843a67734e96224cc547` | Keyboard/controller social overlay |
| `e7edf2369de0296d1250a772b4bf3ee501720802` | Overlay lifecycle and reentrancy hardening |
| `f7f773d63f306facd45e621c7362bce522b85aa8` | Service-owned XDUI launcher |
| `26688ee7edeab1133ce4df00c8761830ac1cd2b3` | Keyring-free shared social protocol crate |
| `9daccab512c3920b7275ec19f6deac2163c977fc` | PeopleHub fallback, authorization lifetime, and bounded social operations |
| `3778c93ba1bd7ec2e0479cfbccd3bd3fb8c55d4d` | Cancellation-safe session-cache invalidation |
| `9f168749f91602e6273f593113cdaccd642c31b5` | Bounded activation delivery and fail-closed socket permissions |
| `96114baea41678d112e23ba6e8152032a0e467f9` | Shared overlay in-flight gate and capacity-one request queue |
| `1aad2cbd6059801546d9519ceab183097e84f0a0` | Versioned full/invite-only XDUI request and acknowledgement |

The final lifecycle fixes prefer nonempty PeopleHub `displayName` and fall back
to nonempty `gamertag`. They preserve authorization expiry, remint an expired
session or one within 60 seconds of expiry, and invalidate the cached session
after PeopleHub or Multiplayer Activity failure, cancellation, or timeout. A
failed or cancelled operation is not automatically retried. Each XDSO social
operation has one 30-second bound across the shared mutex wait and backend I/O.

Activation URIs are validated as nonempty and at most 4096 bytes before
enqueue. The relay uses nonblocking capacity-one backpressure, and XDSO Join
cannot report success unless the URI was accepted. Socket permission handling
fails closed unless the socket is restricted to exactly `0600`. Refresh,
Invite, Join, and row activation share one overlay busy gate across button,
keyboard, controller, and row paths; the nonblocking request queue has capacity
one and the busy state disables network-action controls until resolution.

XDUI version 1 sends a two-byte version/mode request. Pre-acknowledgement
rejection returns only `2`; an accepted request receives `0xa1` before exactly
one terminal `0`, `1`, or `2`. Mode `1` launches only the fixed invite-only UI,
hides/disables Join, and binds terminal success to a matching Invite request.
Keyboard and controller activation both dispatch Invite in that mode. Offline
cutover tests prove that an old payload-free client cannot launch the new UI
and that a new client rejects an old terminal status as acknowledgement while
the legacy child is reaped.

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
cargo build --release -p xodus-service -p xodus-cli -p xodus-overlay --offline
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
- Filtered offline workspace tests: **GREEN**, 117 passed, 1 ignored, and 1
  filtered out across 16 suites. `test_get_xbox_live_dev_token` was explicitly
  skipped because it is an upstream anonymous live Microsoft test.
- Targeted offline release build for `xodus-service`, `xodus-cli`, and
  `xodus-overlay`: **GREEN**.
- Privacy scan: 15 matches, all existing authorization format strings, static
  documentation templates, synthetic test/protocol data, or the literal scan
  expression itself.

Privacy matches were reviewed individually and fall into four safe classes:

1. static documentation templates with visible placeholders in the existing
   Xbox documentation;
2. production authorization header format strings that contain no runtime
   token/hash value;
3. visibly synthetic test sentinels and activation protocol-shape assertions;
4. the literal privacy-search expression in `docs/forza-social.md`.

No nickname or live identifier, including a raw XUID, authorization token,
connection string, or activation URI, is present in this evidence.

### Manual and installation boundary

| Check | Status |
| --- | --- |
| Keyboard navigation/action parity | **RETEST REQUIRED** |
| Physical Xbox controller navigation/hotplug | **RETEST REQUIRED** |
| Invite from a published Private Multiplayer lobby | **RETEST REQUIRED** |
| Join a compatible remote activity | **RETEST REQUIRED** |
| Deliberate cancellation | **RETEST REQUIRED** |
| Peer disconnect/service shutdown cleanup | **RETEST REQUIRED** |
| Automatic incoming invite notification | **NOT SUPPORTED** |

The game-facing path still depends on the separately reviewed `xgameruntime`
bridge and a future installed-artifact hash/parity gate. No compiled Xodus
artifact was installed. The live Xodus checkout, installed service, keychain,
compatibility prefix, Steam configuration, and game were not mutated or
started. The branch remains local: it has not been pushed and no public pull
request exists.

## Rejected live WineGDK/Xodus source experiment

WineGDK commit `d96a768e25f632b04a457e4cb9f585e89ef5d095` and Xodus commit
`ee9db0f68122a9731b9b66cc24767693d1737f5c` form the rejected newer pair. The
WineGDK source tip covers bounded Xodus transport, XGameUi invite-only flow,
XGameInvite activation delivery, reference-counted runtime lifecycle, and
runtime-owned WinRT initialization outside loader lock. It also serializes the
MSA token-request field as the canonical `ClientId` expected by Xodus.
Integration commit `d9d00144a8d845dff4871be32d14b40f1c507124` owns the
20-stage exact-artifact gate. A clean rebuild plus three complete runs were
green; the final run covered seven native transport contracts and 19 PE
matrices with zero failures (the fixture-disabled matrix has one intentional
skip).

See [`xgameruntime-baseline.md`](evidence/xgameruntime-baseline.md) for exact
source revisions, per-matrix counts, artifact hashes, unload/state semantics,
and isolation boundaries. This proves the source pair inside the hermetic
GE-Proton overlay only. It does not undo the **REJECTED LIVE** result.
Installation, live Xodus/Microsoft behavior, and the manual Forza matrix remain
outside that evidence.
Unsolicited Xbox invite notification remains **NOT SUPPORTED**; XDSI carries
only an activation URI already accepted and published by trusted Xodus UI.

## Recoverable runtime-component source gate

`scripts/install-runtime-components` completed a digest-bound live transaction
for the exact `legacy-v0.1` revisions, and the transaction was accepted only
after the two-launch manual matrix and lifecycle cleanup passed. Its targeted
temporary-filesystem suite has 97 passing cases. The parameterized matrix
force-exits the process after every stage, backup, publication, full-rollback,
and Wine-only restoration rename, then proves that the correct operation
resumes. Additional cases cover the recovery-aware launcher as the first
ordered role, all ten artifacts and eleven journal records, exact clean integration, legacy XGameRuntime, and legacy Xodus revisions and evidence
binding for `legacy-v0.1`, read-only planning, plan digest and root binding,
explicit adoption and exact restoration of a legacy unmanaged installation,
refusal to adopt a damaged managed installation, no-follow source rejection,
source and installed drift, all-before-any destination revalidation,
absent-original restoration, unexpected-file preservation, launcher-lock
exclusion, active-service refusal, systemd reload failure and recovery,
accepted ownership-manifest compatibility, and ordinary uninstall.

The launcher has 34 passing behavior cases, including refusal before service start for every unfinished runtime state, malformed private journals, and removal of an orphaned non-listening socket only after exact owned-service cleanup. `installed` remains intentionally launchable so the manual game matrix can run before `accept`. These are synthetic source gates only: no compatibility-tool file, prefix file, service, keychain, Steam option, account, or game process is changed by the tests.
