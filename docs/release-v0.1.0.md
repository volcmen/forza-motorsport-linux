<!--
SPDX-FileCopyrightText: 2026 Forza Motorsport Linux tools contributors
SPDX-License-Identifier: GPL-3.0-or-later
-->

# v0.1.0 release evidence

[`Forza Motorsport Linux tools`](https://github.com/volcmen/forza-motorsport-linux)
v0.1.0 is an experimental, source-only release
for the Steam edition, AppID `2440510`, with the reviewed
`GE-Proton11-3-FM` layout. It documents one reproducible configuration and is
not ordinary upstream Proton support.

## Verified on the tested system

The exact `legacy-v0.1` profile passed the complete two-launch matrix on
2026-09-02:

- cold launch with Xodus initially stopped;
- online profile and content download;
- controller menu navigation, driving, disconnect, and reconnect;
- no AP702 HDD/SSD warning;
- explicit Microsoft/Xbox Invite from Linux to Windows;
- explicit Join from Linux to a compatible Windows activity;
- keyboard and controller operation in the social picker;
- clean service, socket, and ownership-token removal after exit; and
- a second successful launch without a system reboot.

The redacted observation and installed open-source artifact hashes are recorded
in [`docs/evidence/v0.1-live-validation.md`](evidence/v0.1-live-validation.md).

## Source revisions

| Component | Revision | Release role |
| --- | --- | --- |
| Xodus legacy | [`7b236772297b3475ea4f3cb830feb5b224f3064a`](https://github.com/volcmen/xodus/commit/7b236772297b3475ea4f3cb830feb5b224f3064a) | Reviewed v0.1 service, CLI, and social overlay |
| XGameRuntime legacy | [`a1548b1cf57371715d10b608bc81a77a188e40d4`](https://github.com/volcmen/wine-forza-motorsport/commit/a1548b1cf57371715d10b608bc81a77a188e40d4) | Reviewed v0.1 bridge |
| Wine controller source | [`ddd302d97c6008d79ea4f3e3ad56014cb548e514`](https://github.com/volcmen/wine-forza-motorsport/commit/ddd302d97c6008d79ea4f3e3ad56014cb548e514) | Experimental source proposal; not installed directly |
| Wine storage source | [`a7719bd8d0719e5ea6061387db020fa6a6b39d27`](https://github.com/volcmen/wine-forza-motorsport/commit/a7719bd8d0719e5ea6061387db020fa6a6b39d27) | Experimental compatibility source; not installed directly |

The immutable `v0.1.0` tag identifies the integration-repository revision.
The newer WineGDK/Xodus protocol pair is rejected live and is not part of this
profile.

## Known limitations

- Automatic incoming Xbox invite notifications are not supported. Invite and
  Join are explicit actions in the opened social picker.
- Results are bound to the exact source revisions, locally recorded artifact
  hashes, game AppID, compatibility-tool layout, and tested system.
- The standalone controller and storage Wine branches have green source
  compile/link evidence, but their PE tests remain blocked before dispatch by
  the known build-prefix initialization failure.
- The storage branch reports a fixed compatibility capability; it is not a
  canonical host-device TRIM detector.
- Future game, Microsoft service, Xodus, Wine, Proton, driver, or distribution
  changes can invalidate the result.

## Installation boundary

The repository contains scripts, manifests, documentation, and synthetic test
fixtures only. It distributes no compiled Wine, Proton, Xodus, Microsoft,
Steam, or game artifact. Users build the open-source components themselves and
must supply any required Microsoft threading runtime only from a source they
are licensed to use. The project never reads, hashes, copies, publishes, or
downloads that separately supplied file.

Installation is user-local and digest-bound. The user unit is static and is
started only by the per-game launcher reservation; it is not enabled as a
persistent service. KDE Wallet is accepted through the standard Secret Service
interface, and this workflow does not require GNOME Keyring.

## Rollback

Before acceptance, `scripts/install-runtime-components rollback` restores every
destination from the transaction journal. After acceptance,
`scripts/install-runtime-components restore-runtime` restores only the legacy
XGameRuntime pair while preserving the accepted user-local integration files.
The exact commands and conflict-preserving uninstall behavior are documented in
the README. Never delete transaction journals or recovery files before their
recorded state has been verified.
