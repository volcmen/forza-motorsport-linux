# Forza Motorsport Linux tools

## Status and tested matrix

**Experimental.** This project targets Steam **AppID 2440510** with the reviewed `GE-Proton11-3-FM` layout. Local tests cover launcher ownership, read-only diagnostics, exact-build patch transactions, and reversible user-local installation. They do not prove that every Linux system can launch, authenticate, play, or use multiplayer.

| Area | Boundary |
| --- | --- |
| Launcher | Starts Xodus on demand for one game command and preserves its exit status. |
| Diagnostics | Reports readiness without changing Steam, credentials, or systemd state. |
| Patches | Only reviewed controller/mountmgr hashes, with backups and restoration. |
| Install | User-local, manifest-backed, reversible, and never enables the service. |
| Social | Outgoing Microsoft/Xbox invite and join are experimental through Xodus. |

## What this project does

The launcher writes a one-shot, mode-0600 pending ownership token in the user runtime directory. `xodus-forza.service` atomically consumes it into a matching active token before starting. Direct manual `systemctl --user start xodus-forza.service` has no reservation and fails closed. The launcher waits for the expected socket, runs the game command, and stops only the Xodus invocation whose token and systemd invocation identity it owns. A pre-existing service is left alone.

`forza-doctor` is read-only. It checks the AppID, compatibility-tool directory, Xodus service executable, user-supplied runtime file, Secret Service, disabled unit state, exact-build state, and disk-space threshold.

## Legal prerequisites

Use a licensed Steam copy of Forza Motorsport, the reviewed `GE-Proton11-3-FM` layout, and a user-built Xodus directory containing `xodus-service`, `xodus-cli`, and `xodus-overlay`. **Microsoft binaries are not distributed** by this repository. Supply only artifacts you are licensed to use, including any required runtime from your own licensed Windows installation. Do not obtain DLLs from download sites.

The repository does not download credentials, game assets, Microsoft binaries, or third-party binaries. Every command below is user-local; do not use `sudo`.

## Read-only preflight

From a checkout, run:

```bash
bin/forza-doctor
```

It exits nonzero for missing prerequisites and prints `PASS`, `WARN`, and `FAIL`. Before installation, a missing installed patcher or manifest is expected. KDE Wallet is checked through the D-Bus **Secret Service** interface (`org.freedesktop.secrets`): an existing owner or activatable KDE provider is accepted. Do not install or substitute GNOME Keyring for this workflow.

## User-local install

Pass an absolute path to your own Xodus build directory:

```bash
scripts/install-user --xodus-build-dir /absolute/path/to/xodus-build
```

The installer writes only the launcher and doctor under `~/.local/bin`, Xodus and the patcher under `~/.local/libexec`, the build manifest under `~/.local/share`, and the user unit under `~/.config/systemd/user`. It reloads the user systemd daemon but does not start or enable the service. Re-run `~/.local/bin/forza-doctor` afterwards.

## Steam configuration

Generate the single Steam launch-options line from the checkout:

```bash
scripts/print-steam-options
```

Paste its output into Forza Motorsport's Steam launch options. It uses the installed `~/.local/bin/forza-linux`, the current user's Xodus socket path, and `WINEDLLOVERRIDES=xgameruntime=b`. It does not edit Steam files. Do not add unrelated Proton flags: this integration is not a collection of generic Proton flags.

## Known-build patch fallback

If `forza-doctor` reports `WARN` for controller and mountmgr known-build state, the reviewed fallback is available only for the exact hashes in `supported-builds.toml`. Unknown, mixed, changed, missing, or partly patched targets fail closed. The fallback covers `windows.gaming.input.dll` and `mountmgr.sys`; it is not a generic Proton flag.

Choose a private backup directory, record the printed backup-manifest path, and run:

```bash
patcher="$HOME/.local/libexec/forza-motorsport-linux/patch-known-build"
manifest="$HOME/.local/share/forza-motorsport-linux/supported-builds.toml"
steam_root="${XDG_DATA_HOME:-$HOME/.local/share}/Steam"
backup_root="$HOME/.local/state/forza-motorsport-linux/patch-backups"
"$patcher" apply-forza --manifest "$manifest" --steam-root "$steam_root" --backup-root "$backup_root"
```

The patcher preflights all four in-scope targets before writing, creates verified user-owned backups, and records a backup manifest. Restore only with that manifest:

```bash
"$patcher" restore-forza --manifest "$manifest" --steam-root "$steam_root" --backup-manifest /absolute/path/to/forza-patch-TIMESTAMP.json
```

Restore also verifies current and backup hashes before changing files.

## Launch and logs

Steam invokes the generated line, which runs `forza-linux` around Steam's game command. Xodus starts only after the launcher reserves its token. For investigation, use the normal user journal and redact account, authorization, proof-key, gamertag, XUID, and invite data before sharing. This repository does not collect or upload runtime logs.

## Invite/join workflow

Outgoing Microsoft/Xbox invite and join are experimental through Xodus. They require a successful game session and are not a promised feature. **Incoming invite notifications are not supported.** Steam invites are not a replacement for Microsoft/Xbox invites.

## Known limitations

- This is not ordinary upstream Proton support and cannot guarantee launch, sign-in, gameplay, networking, or social UI behavior.
- Accepted patches are tied to exact hashes and the named compatibility-tool layout.
- The unit deliberately fails when directly started without the launcher's token.
- A passing doctor report is readiness evidence, not proof of a game session.

## Restore and uninstall

Restore patches with the recorded patch backup manifest before altering compatibility-tool files manually. To remove the user-local integration, run:

```bash
scripts/uninstall-user
```

Uninstall removes only files proven by its install manifest. Modified, replaced, missing, or unproven files are preserved with a warning. Interrupted-install and replaced owned material is moved into the private project recovery directory under `~/.local/state/forza-motorsport-linux/recovery` and retained rather than unlinked through a mutable name. This intentional recovery retention consumes disk space; inspect and purge it manually only after confirming it is no longer needed.

## Upstream branches and provenance

This repository contains only scripts, manifests, documentation, and synthetic test fixtures. It does not redistribute Wine, Proton, Xodus, Microsoft, Steam, or game binaries. The supported-build manifest records reviewed hashes and byte edits. See [architecture](docs/architecture.md), [troubleshooting](docs/troubleshooting.md), and [verification](docs/verification.md).
