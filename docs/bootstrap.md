# Bootstrap installation (release candidate)

The hybrid bootstrap is **not released yet**. The production bundle and its
immutable trust manifest must pass real build and reproduction checks before
the commands below become an installation path for users. For the existing
release, use [the guided/manual installation guide](install.md).

## What it will automate

The supported target is Arch Linux x86_64 with native Steam. Bootstrap acquires
the pinned GE-Proton base through the repository-local ProtonUp integration,
prepares a dedicated `GE-Proton11-3-FM`, verifies the open-source component
bundle, and coordinates the existing recoverable installers and exact-build
controller/AP702 patches. It does not replace your other Proton tools.

It does not edit Steam settings, launch Forza, log into Microsoft, or obtain
Microsoft's proprietary threading runtime for you. KDE Wallet can supply the
standard Secret Service API; do not install GNOME Keyring merely for this tool.

## Planned user flow

Once a verified release is available, run from its checkout, without `sudo`:

```bash
./setup bootstrap --check
./setup bootstrap
```

Follow the printed checkpoints: restart Steam so it discovers the dedicated
tool, select `GE-Proton11-3-FM` for Forza, then start and close the game to create
its prefix. Resume with your own licensed Microsoft runtime:

```bash
./setup bootstrap --threading-dll /absolute/path/to/licensed/xgameruntime.dll
./setup steam-options
```

Review the installation plan before confirming it. Paste the generated launch
line into Steam, without adding unrelated Proton flags. `READY TO ATTEMPT`
means file/setup checks passed, not that Microsoft sign-in or gameplay passed.

Xodus uses the launcher's per-game-session service lifecycle. It is not meant
to be enabled as a permanently running login service. Explicit Invite and Join
are separate from incoming invite notifications, which are not supported.

## Alternative acquisition

`./setup bootstrap --bundle /absolute/path/to/forza-bootstrap-bundle-v1.tar.zst`
accepts only the bundle matching the shipped trust manifest. It does not make
the entire installation offline: an uncached GE-Proton base still needs to be
acquired.

`./setup bootstrap --build-from-source` uses rootless Docker on x86_64.
Source/dependency acquisition needs network access; compilation runs with no
network and read-only source mounts. This is a reproducibility option, not a
way to bypass the approved artifact hashes. Build workspaces are retained for
diagnosis and their locations are printed; allow space for source trees and
dependency caches.

## Inspect and recover

```bash
./setup bootstrap --check
./setup snapshot --output /absolute/private/path/before.json
# After the reviewed operation:
./setup snapshot --output /absolute/private/path/after.json
./setup compare --before /absolute/private/path/before.json --after /absolute/private/path/after.json
```

Keep snapshots private. Review reports for local paths and identifying details
before sharing; never attach credentials or the licensed DLL.

Close Forza before making installation or recovery changes. To reverse the
composed installation, use `./setup bootstrap --rollback`; do not substitute
`./setup rollback`, which retains its older runtime-only meaning. Let the
coordinator restore the recorded stages in reverse order. Do not manually
delete transaction journals or backups to clear an error. Interrupted work
must be inspected and resumed using the same checkout and recorded inputs.

## Acceptance is a game test

After automated checks, verify two launches through Steam, online profile and
content, controller navigation/driving/hotplug, AP702 absence, explicit Invite
and Join, keyboard/controller picker navigation, and clean service shutdown.
Container builds and synthetic Steam tests cannot prove those behaviors.
