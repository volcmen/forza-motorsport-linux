# Forza Motorsport on Linux: Publication and Upstream Design

Date: 2026-08-31
Status: Superseded by `2026-09-02-forza-v0.1-publication-design.md`
Owner: volcmen

This document is retained as design history. The 2026-09-02 design controls
release status, provenance, verification, and publication sequencing wherever
the two documents differ.

## Purpose

Publish the experimentally working Forza Motorsport Linux setup in a form that is useful to players and reviewable by upstream maintainers.

The published work must explain and, where possible, automate four independently diagnosed problems:

1. Starting the Steam edition through the Xodus Xbox Gaming Services bridge.
2. Making a physical Xbox Series controller visible to Forza through Windows.Gaming.Input.
3. Preventing the false AP702 HDD warning on an SSD.
4. Supporting Xbox Multiplayer Activity invite and join flows through Xodus.

The project must not overstate ordinary Proton compatibility. It documents and packages an experimental stack built from a custom Proton/Wine runtime, Xodus, and one Microsoft runtime component supplied by the user from their own Windows installation.

## Goals

- Give another Linux user a deterministic, auditable path from an installed Steam copy to a working game.
- Keep Xodus stopped when Forza is not running.
- Integrate with KDE Wallet through Secret Service without adding GNOME Keyring.
- Replace fragile folklore and accumulated launch flags with root-cause fixes and explicit version checks.
- Make every source change available in the repository that owns the corresponding component.
- Provide small upstream pull requests with tests instead of one cross-project patch dump.
- Publish a concise, evidence-backed compatibility update in ValveSoftware/Proton issue 7151.
- Protect account data, friend identities, authentication material, and copyrighted Microsoft binaries.

## Non-goals

- Claiming official support from Valve, Microsoft, Turn 10, Wine, GE-Proton, or Xodus.
- Distributing `xgameruntime.dll.threading`, `GameConfigHelper.dll`, Xbox tokens, proof keys, or any other Microsoft binary or credential.
- Installing or enabling GNOME Keyring.
- Running Xodus permanently as a login or graphical-session service.
- Presenting automatic incoming Xbox invite notifications as working. The tested RTA attempt currently receives an HTTP 400 response and remains experimental.
- Generalizing Forza-specific Xbox service values into a universal Xbox social implementation without upstream review.
- Publishing a binary patch that silently accepts unknown files or future Proton versions.

## Confirmed Baseline

The local working system currently uses:

- Steam AppID `2440510`.
- `GE-Proton11-3-FM`.
- AllanVester's `forza-online` Xodus and Wine work as the original experimental base.
- A user-supplied Microsoft `xgameruntime.dll` renamed to `xgameruntime.dll.threading`.
- A per-game launcher that starts `xodus-forza.service`, waits for `$XDG_RUNTIME_DIR/xodus.sock`, launches Proton, and stops the service when the game exits.
- KDE Plasma 6 `ksecretd`/KWallet as the Secret Service provider.
- Steam Input disabled for this game after the physical-controller Windows.Gaming.Input path was repaired.

The currently verified user-visible outcomes are:

- The game reaches its online menus and downloads online content.
- A wired Xbox Series controller works in Forza after the Windows.Gaming.Input identity fix.
- AP702 disappears after Wine reports `StorageDeviceTrimProperty` successfully.
- The in-game Invite Friends action opens the Xodus social picker.
- An Xbox invite can be sent to a Windows player from a published Private Multiplayer activity.
- The Linux client can join a Windows player's published activity.

These facts are environment- and revision-specific. The publication will label them as a tested matrix, not a general compatibility guarantee.

## Publication Architecture

The work will be published in two layers: an integration repository for users and narrowly scoped branches/PRs for maintainers.

### 1. Integration repository

Repository: `volcmen/forza-motorsport-linux`

The repository is the stable entry point. It owns documentation, validation, user-local installation, lifecycle management, version pinning, and links to upstream source branches. It does not become a permanent fork of Wine, Proton, or Xodus.

Planned structure:

```text
forza-motorsport-linux/
├── README.md
├── LICENSES/
├── REUSE.toml
├── bin/
│   ├── forza-linux
│   └── forza-doctor
├── config/
│   └── xodus-forza.service
├── docs/
│   ├── architecture.md
│   ├── troubleshooting.md
│   ├── verification.md
│   └── superpowers/
├── manifests/
│   └── supported-builds.toml
├── patches/
│   ├── wine-controller/
│   └── wine-storage-trim/
├── scripts/
│   ├── install-user
│   ├── uninstall-user
│   └── print-steam-options
└── tests/
```

The repository will initially target Arch Linux with KDE/Hyprland because that is the tested system. Distribution-neutral interfaces such as systemd user units, XDG paths, Secret Service, and Steam library discovery will remain generic where evidence supports them. Untested distributions will be documented as unverified rather than implied to work.

### 2. Upstream branches and pull requests

The existing local histories will not be pushed unchanged. AllanVester's Xodus branch is behind current `xodus-gaming/xodus` main and the social code overlaps Xodus issue 94 and PR 105. Each contribution must therefore be ported onto the current canonical repository and reviewed for overlap.

The intended contribution stack is:

#### Xodus core and UI

Target: `xodus-gaming/xodus`

- Add the minimum Xbox friends and Multiplayer Activity operations needed for invite and join.
- Add a desktop-neutral service boundary for opening a social picker.
- Keep compositor integration optional; core behavior must not require Hyprland, `hyprctl`, or Fuzzel.
- Keep incoming RTA push support out of the initial working PR.
- Reference Social issue 94 and coordinate with the existing xgameui work in PR 105.
- Split API/model code from UI code if that materially reduces reviewer scope.

#### XGameRuntime bridge

Target: an explicitly AI-assisted experimental branch from `Weather-OS/WineGDK` `b03ba49c4f326c36aa6930fbe6cf72841ef3738c`; do not submit it as an upstream `xodus-gaming/xgameruntime` code PR.

- Implement only the Forza-relevant `XGameUiShowMultiplayerActivityGameInviteAsync` call; keep the semantically different session-specific legacy invite API unimplemented.
- Relay UI requests to Xodus over a documented Unix-socket protocol.
- Relay validated invite-accept activation URIs to registered game callbacks.
- Follow the existing XAsync provider contract: schedule, complete, query status, and return results correctly.
- Keep framing and concurrent writes deterministic and tested.

The standalone `oot-cpp` line is the better long-term architectural target, but current PR 19 contains no Unixlib transport and the branch cannot replace the Wine submodule layout. The reviewed amendment in `docs/superpowers/plans/2026-09-01-xgameruntime-winegdk-amendment.md` controls the installable Linux implementation and publication boundary.

#### Windows.Gaming.Input controller identity

Target: `xodus-gaming/wine` first, followed by the canonical Wine/Proton path chosen by maintainers.

- Replace the current physical-controller `E_NOTIMPL` result from `wine_provider_get_NonRoamableId` with a stable identity derived from the actual device path.
- Preserve the special Steam virtual-controller behavior.
- Avoid a vendor/product allow-list for one Xbox controller.
- Add tests for a physical XInput-backed controller identity and the existing Steam virtual-controller case.
- Prove that the fix is generic before requesting canonical Wine inclusion.

The existing four-byte binary modification is evidence and an exact-build fallback only. It is not the upstream implementation.

#### Storage TRIM reporting

Target: `xodus-gaming/wine` first, followed by canonical Wine if maintainers agree on semantics.

- Implement `StorageDeviceTrimProperty` in `mountmgr.sys` using the correct `DEVICE_TRIM_DESCRIPTOR` structure.
- Preserve existing buffer-size behavior and unsupported-property handling.
- Do not report TRIM unconditionally if Wine can obtain trustworthy filesystem/device capability information without disproportionate complexity.
- If the first scoped implementation must report a compatibility value, state that policy explicitly in the commit and tests.
- Add query tests for full, header-only, invalid, and unrelated property requests.

The existing AP702 binary patch remains a version-pinned fallback until a source build containing the reviewed implementation is available.

## User-facing Commands

The first supported interface will be small and explicit:

```text
./scripts/install-user --check
./scripts/install-user
forza-doctor
./scripts/print-steam-options
./scripts/uninstall-user
```

`install-user --check` and `forza-doctor` are read-only. They report each dependency and exact corrective action without invoking `sudo`, installing packages, starting Steam, launching the game, or changing the prefix.

`install-user` may write only to documented user-local destinations after showing its plan. It installs the launcher and user service but leaves the service disabled. Existing files must be backed up or rejected unless they are byte-for-byte identical.

`uninstall-user` removes only files recorded in the installation manifest. It does not delete the Steam prefix, game files, Xodus credentials, or user-supplied Microsoft runtime.

## Launcher Lifecycle

The launcher owns the Xodus process lifecycle for one game session:

1. Resolve the current XDG runtime directory and Steam AppID paths.
2. Complete the existing KDE Wallet PAM handoff when the session exposes `PAM_KWALLET5_LOGIN`.
3. Start the dedicated Xodus user service only if it is inactive.
4. Wait for the Unix socket with a bounded readiness check.
5. Execute Steam's `%command%` without an intermediate shell interpretation of user arguments.
6. Stop Xodus on launcher exit only when this invocation started it.
7. Preserve and report the game exit status.

The service remains disabled globally. The launcher must not stop a service that was already active before launch.

## Patch Safety

Binary fallback patchers are allowed only for the exact known build and must satisfy all of these constraints:

- Refuse an unknown SHA-256 before opening the file for writing.
- Accept the known original hash and the known already-patched hash only.
- Verify expected instruction bytes in addition to the whole-file hash.
- Create a timestamped backup outside the compatibility-tool directory.
- Patch only the dedicated Forza compatibility tool and AppID `2440510` prefix.
- Verify the resulting SHA-256 and changed byte ranges.
- Provide a restore operation that verifies both current and backup hashes.
- Never follow an unresolved glob, `$HOME`, `~`, or a broad directory as a destructive target.

The README will prefer source builds and reviewed releases. Binary patching will be clearly marked as a temporary, pinned fallback.

## Steam Configuration

The repository will generate, rather than silently edit, the recommended Steam launch options:

```text
WINEDLLOVERRIDES=xgameruntime=b PROTON_VKD3D_HEAP=1 VKD3D_CONFIG=skip_application_workarounds,descriptor_heap,avoid_image_buffer_aliasing PRESSURE_VESSEL_FILESYSTEMS_RW=/run/user/<uid>/xodus.sock <launcher> %command%
```

The generated value must use the current numeric UID and resolved launcher path. The tool will display it for manual review and copying. Direct edits to Steam's local configuration are outside the initial scope because Steam can overwrite concurrent edits and its VDF state is not a stable public API.

The final guide will explicitly remove unsuccessful diagnostic flags, including `PROTON_DISABLE_HIDRAW=1` and `-SkipTargetHardwareProfiler`, from the tested baseline.

## Authentication, Secrets, and Privacy

- Xodus continues to store its proof key and authentication state through Secret Service.
- KDE Wallet is the supported provider on the tested system.
- No alternate plaintext key storage will be enabled by default.
- Diagnostics must redact authorization headers, tokens, proof-key material, XUID values, gamertags, email addresses, and socket payloads that contain invite URIs.
- Tests use synthetic XUIDs, gamertags, connection strings, and invite URIs.
- Screenshots and public issue comments must not contain friend identities.
- Git commits use the GitHub noreply author address.

## Licensing and Provenance

- Original integration scripts and documentation will use `GPL-3.0-or-later` to keep the repository's original material under one unambiguous license.
- Wine-derived patch files retain Wine's applicable LGPL notices.
- Xodus-derived code stays in the Xodus fork and retains GPL licensing.
- The integration repository will use SPDX identifiers and a REUSE-compatible license layout.
- No Microsoft binary, decompiled Microsoft source, access token, proof key, or game asset will be committed or attached to a release.
- Instructions may explain how a user supplies a runtime from their own licensed Windows installation, but will not link to third-party DLL download sites.

## Test Strategy

### Integration repository

- Shell formatting and static analysis for launcher/install scripts.
- Unit tests for path resolution, UID-specific launch options, service ownership, cleanup, and exit-status propagation.
- Unit tests for redaction.
- Unit tests for accepted, already-patched, and refused binary hashes.
- Round-trip install/uninstall tests inside a temporary HOME-like test root.
- `systemd-analyze --user verify` for the user unit.
- A clean checkout test proving that no local absolute path is required.

### Xodus

- Existing workspace tests.
- Fixture-driven parsing tests using synthetic Xbox responses.
- Mock HTTP tests for success and error handling without printing response secrets.
- Unix-socket tests for social UI requests, activity publication, and invite activation.
- UI action tests for both keyboard and controller navigation.
- No test may call a live Xbox endpoint in CI.

### XGameRuntime/Wine

- Interface query and XAsync lifecycle tests.
- Fragmented and concurrent Unix-socket frame tests.
- Invite registration/unregistration and validated activation delivery tests.
- Windows.Gaming.Input identity tests.
- Storage property buffer-size and descriptor-content tests.
- Record source Git revisions separately, then compare each built artifact's SHA-256 only with its installed counterpart.

### Manual evidence matrix

Before the first public release, record a dated matrix covering:

- cold login session and already-unlocked KWallet;
- Xodus initially stopped and already running;
- game launch, online content, and clean exit;
- controller menu navigation and driving input;
- AP702 before/after evidence;
- invite from Linux to a Windows player;
- join from Linux to a Windows player's published activity;
- known failure of unsolicited incoming Xbox invite notifications.

Manual checks are evidence for one environment, not substitutes for automated tests.

## CI and Releases

GitHub Actions will run only source builds and tests that do not require Steam, game files, Microsoft credentials, or Microsoft binaries.

The first release will contain documentation, scripts, source patches, license material, and SHA-256 checksums. Open-source binaries may be added only after their build inputs are pinned and the workflow is reproducible. The Microsoft threading runtime is never part of a release.

Release notes must state:

- exact tested Forza, Proton, Wine, Xodus, kernel, driver, and desktop revisions;
- features verified end to end;
- features covered only by automated tests;
- known limitations and expected failure modes;
- rollback instructions.

## GitHub Publication Sequence

1. Build the integration repository locally with tests and privacy scans.
2. Create the public GitHub repository only after the initial commit is internally coherent.
3. Create or reuse forks of the current canonical Xodus and Wine repositories plus the selected Weather-OS/WineGDK experimental base.
4. Reconstruct small branches from current upstream heads; do not publish stale local history as the review base.
5. Open issue-linked draft PRs with exact verification commands and known limitations.
6. Ask Xodus maintainers whether the social UI should extend PR 105 or land as a separate backend PR.
7. Publish a Proton issue 7151 comment only after stable public links exist.
8. Update the blog article with repository and PR links after publication.

The Proton comment will report compatibility results, not ask Valve to merge cross-project implementation code. It will include the tested controller and AP702 root causes, Xodus launch requirements, invite/join results, and the incoming-notification limitation.

## Error Handling

- Missing prerequisites produce one actionable error per failed check and a nonzero exit status.
- Unknown versions or hashes stop before mutation.
- An unavailable Secret Service provider stops Xodus startup with KDE-specific guidance on the tested system.
- A missing Xodus socket stops the game launch and prints relevant user-unit log commands.
- A game failure returns the game exit code after launcher-owned cleanup.
- Cancelled social UI actions complete the GDK asynchronous call as cancellation/failure rather than hanging the game thread.
- Xbox API failures expose service name and HTTP status while redacting response bodies by default.

## Success Criteria

The publication is complete when:

- A public integration repository exists with no secrets, personal identifiers, or prohibited binaries.
- Its clean-checkout CI and local verification suite pass.
- Installation and removal are scoped, documented, reversible, and tested.
- Controller and storage changes exist as source patches with automated tests, not only binary edits.
- Working invite/join code is published from current canonical upstream bases.
- Draft PRs or maintainer-facing issues exist in the correct component repositories.
- Proton issue 7151 contains a concise compatibility update linking the reproducible work.
- Every public statement distinguishes automated verification, single-machine end-to-end verification, experimental behavior, and unresolved behavior.

## Explicitly Deferred Work

- True push-based incoming Xbox invite notifications on Linux.
- General Xbox chat, party, and presence support beyond what invite/join needs.
- Automatic modification of Steam's VDF configuration.
- Automatic package installation with root privileges.
- Universal distribution packaging.
- A ProtonUp-Qt provider or catalog submission before the compatibility tool has a reproducible public release.

These may be reconsidered after the initial source contributions are reviewed and the integration repository has a stable release boundary.
