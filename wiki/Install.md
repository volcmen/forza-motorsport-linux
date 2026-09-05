# Install

[Home](Home.md) · **Install → [Play and verify](Play.md)** · [Troubleshooting](Troubleshooting.md) · [Recovery](Recovery.md)

Use the **v0.2.0 bootstrap** for a new setup. It downloads the open-source
components and walks you through the remaining Steam checkpoints.
If you already have the reviewed local builds, use [Advanced setup](Advanced-setup.md).

## 1. Check the requirements

| Requirement | Check before continuing |
| --- | --- |
| System | Arch Linux, x86_64, a working user systemd session |
| Game | Licensed Steam edition, AppID `2440510`, installed in native Steam's primary library |
| Tools | Python 3.11 or newer, `uv`, and `zstd` |
| Native libraries | Arch packages `gtk4`, `libadwaita`, `sdl3`, and `webkit2gtk-4.1`, with dependencies |
| Credential storage | KDE Wallet providing `org.freedesktop.secrets` in your user session |
| Microsoft runtime | Your own licensed `xgameruntime.dll`, available at a known absolute path |

The installer does not support Flatpak Steam or additional Steam libraries.
It does not install system packages. The bundle needs the native libraries above;
it is not a self-contained AppImage. Do not install GNOME Keyring just for this
workflow or add a second Secret Service daemon alongside KDE Wallet.

**Microsoft binaries are not distributed.** Obtain the runtime from your own
licensed Windows installation, not a DLL download site. Do not attach it to an issue.

## 2. Get a separate checkout

Run without `sudo`:

```bash
git clone --branch v0.2.0 --depth 1 https://github.com/volcmen/forza-motorsport-linux.git forza-motorsport-linux-v0.2.0
cd forza-motorsport-linux-v0.2.0
./setup bootstrap --check
```

**Checkpoint:** required system packages must be installed before continuing;
bootstrap does not install them. Project components may still be absent on a
fresh setup. Resolve an unsupported host or an existing recovery problem first.
Keep an existing working installation and its recovery records.
The tag's bundled documentation is a historical snapshot; this wiki explains the workflow.

## 3. Start bootstrap and follow its Steam checkpoint

```bash
./setup bootstrap
```

Bootstrap acquires the pinned Proton base, prepares a dedicated
`GE-Proton11-3-FM`, and verifies the component bundle. Public bundle downloads
do not require a GitHub login. Review the printed plan before confirming it.

When prompted:

1. Restart Steam so it discovers the new compatibility tool.
2. Open **Forza Motorsport → Properties → Compatibility** and select `GE-Proton11-3-FM`.
3. Leave Steam launch options empty for this prefix-creation launch. Save any
   existing custom line before clearing it.
4. Start and close the game to let Steam create its Windows environment (the prefix).

**Checkpoint:** complete the steps requested by bootstrap, then resume in the
same checkout. A stop at a manual checkpoint is not a completed installation.
If this handoff fails, see [Troubleshooting](Troubleshooting.md#find-the-symptom).

## 4. Supply the licensed runtime and finish

Replace the example path with the location of your own DLL:

```bash
./setup bootstrap --threading-dll /absolute/path/to/licensed/xgameruntime.dll
```

Bootstrap copies the supplied licensed DLL to the required prefix location and
coordinates the recoverable installation and exact-build patches. You do not need
to copy or rename it yourself in this workflow. Its destination is
`steamapps/compatdata/2440510/pfx/drive_c/windows/system32/xgameruntime.dll.threading`
beneath the Steam root. Keep that proprietary file out of public evidence.

**Checkpoint:** setup must finish without an unresolved failure. If a command is
cancelled, run `./setup bootstrap --check` before resuming; cancellation does not
undo completed stages. Do not remove journals or backups to force a retry.

## 5. Configure Steam, then play

After bootstrap reports **READY TO ATTEMPT**, print the launch options:

```bash
./setup steam-options
```

Paste the generated line into **Properties → General → Launch Options**.
Keep `GE-Proton11-3-FM` selected and disable Steam Input for the tested controller path.
Use the generated line without extra Proton or hardware-profiler flags.

Setup does not edit Steam, launch the game, or sign in to Microsoft for you.
**READY TO ATTEMPT** means the setup checks passed. Continue with
[Play and verify →](Play.md) to check the actual game session.

## Other ways to acquire the bundle

Have the release archive already?

```bash
./setup bootstrap --bundle /absolute/path/to/forza-bootstrap-bundle-v1.tar.zst
```

Only the archive matching the shipped trust manifest is accepted. This does not
make the whole installation offline: an uncached Proton base still needs downloading.

To build the open-source bundle yourself:

```bash
./setup bootstrap --build-from-source
```

This requires rootless Docker on x86_64. Source and dependency acquisition use the
network; compilation runs offline with read-only source mounts. Build workspaces
and caches are retained for diagnosis, and their locations are printed. This path
still enforces the approved artifact hashes.

Need to undo bootstrap? Use [the bootstrap recovery path](Recovery.md#undo-bootstrap).
