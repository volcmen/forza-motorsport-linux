# Forza Motorsport on Linux

[![Verify](https://github.com/volcmen/forza-motorsport-linux/actions/workflows/verify.yml/badge.svg)](https://github.com/volcmen/forza-motorsport-linux/actions/workflows/verify.yml)
[![Release](https://img.shields.io/github/v/release/volcmen/forza-motorsport-linux)](https://github.com/volcmen/forza-motorsport-linux/releases)
[![License: GPL-3.0-or-later](https://img.shields.io/badge/License-GPL--3.0--or--later-blue.svg)](LICENSES/GPL-3.0-or-later.txt)

I bought Forza Motorsport to drive the Nürburgring and revisit memories of
playing Forza Motorsport 4 with my cousin on Xbox 360. Getting there on Linux
turned into a detour through Gaming Services, a controller the game couldn't
see, an SSD warning, and Xbox invites.

This project collects the setup tools and fixes that came out of that work.
It's for the **Steam edition of Forza Motorsport** (`AppID 2440510`), using
`GE-Proton11-3-FM` and Xodus. The aim is to make the next person's setup easier
to follow—and possible to undo if something goes wrong.

> [!IMPORTANT]
> **Experimental:** the older v0.1 setup worked on my machine. The new v0.2.0
> installer has passed build and recovery tests, but its rebuilt components
> still need testing in the game. This is not ordinary upstream Proton support.
> If you already have a working setup, there's no need to replace it yet.

Looking to try it? Start with the [bootstrap guide](docs/bootstrap.md).
Already have the required local builds? Use the [manual setup](#guided-setup).
For problems, see [troubleshooting](docs/troubleshooting.md).

### Bootstrap prerelease

The [v0.2.0 prerelease](https://github.com/volcmen/forza-motorsport-linux/releases/tag/v0.2.0)
adds `./setup bootstrap`: it downloads the open-source components, prepares a
separate Proton tool, and walks you through installation. You don't need to
compile the bundle yourself, and its download doesn't require a GitHub login.

```bash
git clone --branch v0.2.0 --depth 1 https://github.com/volcmen/forza-motorsport-linux.git forza-motorsport-linux-v0.2.0
cd forza-motorsport-linux-v0.2.0
./setup bootstrap --check
./setup bootstrap
```

Run these without `sudo`, in a separate checkout. The tagged README was written
before publication; the [release notes](https://github.com/volcmen/forza-motorsport-linux/releases/tag/v0.2.0)
have the latest download information. Repeat builds produced identical bundles,
and public downloads passed verification. Those checks don't prove gameplay.

You still need to let Steam create the game's Windows environment (its
"prefix"), supply your own licensed Microsoft threading DLL, and paste the
printed launch options into Steam. Setup never launches the game for you.
Use `./setup bootstrap --rollback` to undo a bootstrap installation;
`./setup rollback` is for the older runtime-only setup.

See the [bootstrap prerelease guide](docs/bootstrap.md) for the manual
checkpoints, acquisition choices, privacy boundary, and recovery commands.

## Status and tested matrix

The results below belong to **v0.1**, not the rebuilt v0.2.0 bundle.

On that setup, I could play online, drive with the controller, reconnect it,
and invite or join a friend on Windows. The AP702 warning was gone.
Incoming invite notifications are not supported.

<details>
<summary>Tested versions, results, and the experiment to avoid</summary>

| State | Meaning |
| --- | --- |
| **VERIFIED — `legacy-v0.1`** | The exact reviewed component pair completed the live matrix below on one tested system. |
| **NOT SUPPORTED** | Automatic incoming Xbox invite notifications. |
| **REJECTED EXPERIMENT** | The newer WineGDK/Xodus protocol pair described below. Do not install it as an alternative. |

The v0.1 installable profile is `legacy-v0.1`. It binds Xodus commit
`7b236772297b3475ea4f3cb830feb5b224f3064a` to the matching legacy
XGameRuntime generation and this repository's recoverable installation tools.

- **Verified live matrix.** On 2026-09-02, the exact pair passed two launches
  without a reboot, online profile/content, controller navigation, driving and
  hotplug, AP702 absence, explicit Invite and Join actions,
  keyboard/controller social-picker input, and clean per-session shutdown.
- **Incoming invite notifications are not supported.** An explicitly opened
  invite/join picker is a different capability.

**Rejected live experiment:** WineGDK
`d96a768e25f632b04a457e4cb9f585e89ef5d095` with Xodus
`ee9db0f68122a9731b9b66cc24767693d1737f5c` is **REJECTED LIVE**. Although its
hermetic source tests remain useful, the pair produced protocol rejection,
flicker, or exit after the logo. It is not a v0.1 dependency or installable
profile.

| Area | Boundary |
| --- | --- |
| Launch | Forza starts through Steam; Xodus exists only for that game session. |
| Online | Profile and content worked in the dated live matrix, not as a general compatibility guarantee. |
| Controller | Navigation, driving, and hotplug worked with the reviewed exact-build fallback. |
| Storage | AP702 was absent with the reviewed `StorageDeviceTrimProperty` fallback. |
| Social | Outgoing Microsoft/Xbox invite and join are **VERIFIED** through Xodus on the tested system. |
| Recovery | Installation is manifest-backed, digest-bound, and conflict-preserving. |

See the [v0.1.0 release evidence](docs/release-v0.1.0.md) and the complete
[verification record](docs/verification.md) for the exact boundary behind those
statements.

</details>

## What this project does

Forza needs more than a different Proton version here. This project brings
together:

1. a dedicated `GE-Proton11-3-FM` compatibility-tool layout;
2. a locally built, reviewed Xodus service, CLI, and social overlay;
3. the matching locally built legacy XGameRuntime bridge; and
4. one Microsoft threading runtime supplied by the user from a licensed source.

Xodus handles the Xbox side of the setup. The launcher starts it when you play
and stops its session when you leave. It doesn't run permanently in the
background. Start Forza through Steam with the generated launch options rather
than starting `xodus-forza.service` yourself.

`forza-doctor` checks your setup without changing it. A clean report means
you're ready to try launching, not that the game is guaranteed to work.
The service details are in [Architecture](docs/architecture.md).

## Before you start

The bootstrap targets **Arch Linux x86_64 with native Steam**. Follow its
[guide](docs/bootstrap.md) for downloads and prerequisites. The list below is
for the **v0.1 manual workflow**, where you provide the local builds yourself.

You need all of the following:

- a licensed Steam copy of Forza Motorsport;
- the reviewed `GE-Proton11-3-FM` compatibility-tool directory;
- a clean local build of the reviewed Xodus revision, with `xodus-service`,
  `xodus-cli`, and `xodus-overlay` directly under `target/release` (see the
  [exact source/build evidence](docs/evidence/v0.1-xodus.md));
- a clean local build of the matching legacy XGameRuntime revision (see its
  [out-of-tree build evidence](docs/evidence/v0.1-xgameruntime.md));
- a licensed `xgameruntime.dll` from your own Windows installation, renamed as
  described below; and
- KDE Wallet exposing the standard Secret Service D-Bus API.

Install as your regular user, without `sudo`. **Microsoft binaries are not distributed**
by this project. You must supply the threading DLL from a source you're licensed
to use; don't get it from a DLL download site. The v0.1 workflow is source-only;
the v0.2.0 prerelease provides the open-source component bundle separately.

KDE Wallet is supported through `org.freedesktop.secrets`. **Do not install GNOME Keyring** for this workflow or add a second daemon competing for the same
D-Bus service.

## Guided setup

For the manual workflow, clone the source and check the available commands:

```bash
git clone https://github.com/volcmen/forza-motorsport-linux.git
cd forza-motorsport-linux
./setup --help
./setup
```

`./setup` asks where your three local builds are, checks them, and shows what it
would install. It asks before saving the installation record, then requires you
to type the printed `PLAN_SHA256` to confirm that exact plan. Backups and recovery
records are kept so you can undo the installation later.

### What setup automates

- path and prerequisite validation;
- the read-only user-install preflight;
- exact-source evidence locking and the digest-bound plan;
- the reviewed runtime/user-local transaction after exact confirmation;
- post-install transaction status and `forza-doctor`; and
- generation of the Steam launch-options line.

### What remains deliberate and manual

- supplying all licensed or locally built inputs;
- choosing whether to adopt any pre-existing unmanaged files;
- applying the exact-build controller/AP702 fallback;
- pasting launch options into Steam;
- launching and validating the game;
- accepting an installed transaction after live validation; and
- deleting any retained recovery material.

In this manual workflow there is no `--yes`, smart repair, automatic patching, automatic
rollback, Steam-file editing, binary download, or automatic game launch.

Useful commands:

```bash
./setup check           # read-only current-environment check
./setup status          # transaction state, then readiness diagnostics
./setup rollback        # explicit pre-acceptance rollback
./setup steam-options   # print; paste into Steam yourself
./setup uninstall       # manifest-proven files; Forza/Xodus must be stopped
```

Rollback and uninstall fail closed unless the launcher lock is free and the
tool can prove that both Forza and `xodus-forza.service` are inactive. Uninstall
holds that same lock until the conflict-preserving user uninstaller exits.
Rollback asks for the compatibility-tool directory used by the transaction;
press Enter for the displayed standard Steam path.

A fresh system can report expected missing installed components during
`./setup check`; the guided flow performs its own source and destination
preflight before asking to prepare a plan.

## Supply the licensed threading runtime

The separately supplied Microsoft file is outside repository transactions and
public evidence. Place it at this exact path beneath the Steam root used for the
game:

```bash
steam_root="${XDG_DATA_HOME:-$HOME/.local/share}/Steam"
prefix_root="$steam_root/steamapps/compatdata/2440510/pfx/drive_c"
mkdir -p -- "$prefix_root/windows/system32"
cp -- /absolute/path/to/your/licensed/xgameruntime.dll \
  "$prefix_root/windows/system32/xgameruntime.dll.threading"
```

The required destination is
`steamapps/compatdata/2440510/pfx/drive_c/windows/system32/xgameruntime.dll.threading`.
The project does not read, hash, publish, or download that file.

## Steam and first launch

After setup completes without a doctor failure, generate the one line Steam
needs:

```bash
./setup steam-options
```

Paste it into **Forza Motorsport → Properties → General → Launch Options** and
select `GE-Proton11-3-FM` under **Compatibility**. Keep Steam Input disabled for
this exact tested controller path.

Do not add `PROTON_DISABLE_HIDRAW`, `PROTON_ENABLE_WAYLAND`,
`-SkipTargetHardwareProfiler`, or unrelated Proton flags. The generated line is
the approved baseline and includes the Xodus socket exposure required by
Steam's pressure-vessel container.

If `forza-doctor` reports a known-build `WARN`, stop and review the
[exact-build fallback](docs/install.md#exact-build-controller-and-ap702-fallback).
It is not a generic fix: unknown, mixed, changed, missing, or partly patched
targets fail closed. The storage fallback addresses Forza's **AP702 HDD/SSD launch rejection** by implementing the reviewed `StorageDeviceTrimProperty`
response for this build.

## Invite and join

Invite and Join worked with a friend on Windows using the Xbox social picker,
with both keyboard and controller navigation. Enter a compatible multiplayer
lobby first: Forza needs to make that session available before friends can join.

These are Microsoft/Xbox invites, not Steam invites. Incoming invite
notifications are not supported: you need to open the picker yourself.

## Recovery and Uninstall

Do not delete transaction journals, backups, stages, or adjacent
`.forza-recovery-*` files to force a clean-looking state. They are the evidence
that makes interrupted work recoverable.

| Situation | Command |
| --- | --- |
| Inspect current state | `./setup status` |
| Undo an unaccepted runtime transaction | `./setup rollback` |
| Keep a validated installation | Advanced `accept` command in the [install guide](docs/install.md#accept-or-restore) |
| Restore runtime after acceptance | Advanced `restore-runtime` command in the [install guide](docs/install.md#accept-or-restore) |
| Remove user-local integration | `./setup uninstall` |

Uninstall removes only files proven by its install manifest. Modified,
replaced, or unproven files are preserved with a warning; **already-missing paths are left absent**. It does not restore compatibility-tool runtime files,
delete recovery evidence, or touch the separately supplied Microsoft DLL.

## Manual and technical documentation

- [Installation and recovery](docs/install.md) — the full manual transaction,
  patch, acceptance, restoration, and logging workflow.
- [Architecture](docs/architecture.md) — launcher ownership, runtime
  generations, fail-closed state, and exact-build boundaries.
- [Troubleshooting](docs/troubleshooting.md) — known failure states and safe
  recovery routing.
- [Verification](docs/verification.md) — automated gates and dated live
  evidence.
- [v0.1.0 release evidence](docs/release-v0.1.0.md) — the shipped evidence
  boundary.
- [Upstream publication ledger](docs/upstream.md) — public branches, drafts,
  issue comments, and immutable revisions.

For the personal story behind the work, read
[I Just Wanted to Drive the Nürburgring](https://blog.volc.men/blog/forza-motorsport-linux/).
The repository, not the story, is authoritative for the current technical state.

## Known limitations

- This cannot guarantee launch, sign-in, gameplay, networking, or social UI on
  another machine or after an upstream update.
- Accepted patches are tied to exact hashes and the named compatibility-tool
  layout.
- A passing doctor report means **ready to attempt**, not “working.”
- Incoming invite notifications are not supported.
- The unit deliberately rejects a direct start without the launcher's
  one-shot ownership token.

## Upstream branches and provenance

<details>
<summary>Source revisions and build references for contributors</summary>

The Git repository contains source, scripts, documentation, and test fixtures.
The v0.2.0 release separately offers an open-source Xodus/XGameRuntime bundle;
it contains no Microsoft DLL or game files. The source branches are public at the immutable revisions
recorded in the [publication ledger](docs/upstream.md):

- legacy Xodus [`forza-social-invite-join-v0.1`](https://github.com/volcmen/xodus/tree/forza-social-invite-join-v0.1) at [`7b236772297b3475ea4f3cb830feb5b224f3064a`](https://github.com/volcmen/xodus/commit/7b236772297b3475ea4f3cb830feb5b224f3064a);
- legacy XGameRuntime [`forza-xgameui-invite-join-v0.1`](https://github.com/volcmen/wine-forza-motorsport/tree/forza-xgameui-invite-join-v0.1) at [`a1548b1cf57371715d10b608bc81a77a188e40d4`](https://github.com/volcmen/wine-forza-motorsport/commit/a1548b1cf57371715d10b608bc81a77a188e40d4);
- Wine controller [`wgi-physical-nonroamable-id`](https://github.com/volcmen/wine-forza-motorsport/tree/wgi-physical-nonroamable-id) at [`ddd302d97c6008d79ea4f3e3ad56014cb548e514`](https://github.com/volcmen/wine-forza-motorsport/commit/ddd302d97c6008d79ea4f3e3ad56014cb548e514);
- Wine storage [`storage-trim-property`](https://github.com/volcmen/wine-forza-motorsport/tree/storage-trim-property) at [`a7719bd8d0719e5ea6061387db020fa6a6b39d27`](https://github.com/volcmen/wine-forza-motorsport/commit/a7719bd8d0719e5ea6061387db020fa6a6b39d27); and
- **REJECTED LIVE** WineGDK/Xodus [`xodus-social-invite-bridge-experimental`](https://github.com/volcmen/WineGDK/tree/xodus-social-invite-bridge-experimental) and [`xodus-social-invite-bridge`](https://github.com/volcmen/xodus/tree/xodus-social-invite-bridge).

The selected implementation bases remain `xodus-gaming/wine` `bleeding-edge`,
`xodus-gaming/xodus` `main`, and `Weather-OS/WineGDK` `b03ba49c4f326c36aa6930fbe6cf72841ef3738c`. The experimental XGameRuntime
deliverable is not an upstream `xodus-gaming/xgameruntime` code PR.

Canonical source checkouts for audit purposes:

```bash
git clone https://github.com/xodus-gaming/wine.git wine
git -C wine switch bleeding-edge
git clone https://github.com/xodus-gaming/xodus.git xodus
git -C xodus switch main
git clone https://github.com/Weather-OS/WineGDK.git winegdk
git -C winegdk switch --detach b03ba49c4f326c36aa6930fbe6cf72841ef3738c
```

These bases are provenance references, not a claim that cloning them alone
builds or installs the complete integration.

</details>
