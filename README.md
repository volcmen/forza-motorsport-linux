# Forza Motorsport Linux tools

## Status and tested matrix

**Experimental.** This project targets Steam **AppID 2440510** with the reviewed `GE-Proton11-3-FM` layout. Local tests cover launcher ownership, read-only diagnostics, exact-build patch transactions, and reversible user-local installation. They do not prove that every Linux system can launch, authenticate, play, or use multiplayer.

| Area | Boundary |
| --- | --- |
| Launcher | Starts Xodus on demand for one game command and preserves its exit status. |
| Diagnostics | Reports readiness without changing Steam, credentials, or systemd state. |
| Patches | Only reviewed controller/mountmgr hashes, with backups and restoration. |
| Install | User-local and runtime-component transactions are manifest-backed, recoverable, and never enable the service. |
| Social | Outgoing Microsoft/Xbox invite and join are experimental through Xodus. |

## What this project does

The launcher writes a one-shot, mode-0600 pending ownership token in the user runtime directory. `xodus-forza.service` atomically consumes it into a matching active token before starting. Direct manual `systemctl --user start xodus-forza.service` has no reservation and fails closed. The launcher waits for the expected socket, runs the game command, and stops only the Xodus invocation whose token and systemd invocation identity it owns. A pre-existing service is left alone.

`forza-doctor` is read-only. It checks the AppID, compatibility-tool directory, Xodus service executable, user-supplied runtime file, Secret Service, disabled unit state, exact-build state, and disk-space threshold.

## Legal prerequisites

Use a licensed Steam copy of Forza Motorsport, the reviewed `GE-Proton11-3-FM` layout, and a user-built Xodus directory containing `xodus-service`, `xodus-cli`, and `xodus-overlay`. **Microsoft binaries are not distributed** by this repository. Supply only artifacts you are licensed to use, including any required runtime from your own licensed Windows installation. Do not obtain DLLs from download sites.

The repository does not download credentials, game assets, Microsoft binaries, or third-party binaries. Every command below is user-local; do not use `sudo`.

To place a runtime you are licensed to use, set the Steam root for the account whose prefix you are preparing and place its renamed file at the exact doctor-required path:

```bash
steam_root="${XDG_DATA_HOME:-$HOME/.local/share}/Steam"
prefix_root="$steam_root/steamapps/compatdata/2440510/pfx/drive_c"
mkdir -p -- "$prefix_root/windows/system32"
cp -- /absolute/path/to/your/licensed/xgameruntime.dll "$prefix_root/windows/system32/xgameruntime.dll.threading"
```

The required destination is `steamapps/compatdata/2440510/pfx/drive_c/windows/system32/xgameruntime.dll.threading` beneath the selected Steam root. This repository neither supplies nor hashes that Microsoft file in public output.

## Read-only preflight

From a checkout, run:

```bash
bin/forza-doctor
scripts/install-user --check --xodus-build-dir /absolute/path/to/xodus-build
```

Both commands are read-only. The install check validates every source artifact and destination, reports conflicts with corrective actions, and prints the exact install plan without creating directories, copying files, writing a journal, or invoking systemd. The doctor exits nonzero for missing prerequisites and prints `PASS`, `WARN`, and `FAIL`. It accepts only `disabled` and `static` unit states; mixed or unknown patch states fail. Before installation, a missing installed patcher or manifest is expected. KDE Wallet is checked through the D-Bus **Secret Service** interface (`org.freedesktop.secrets`): an existing owner or activatable KDE provider is accepted. Do not install or substitute GNOME Keyring for this workflow.

## User-local install

Pass an absolute path to your own Xodus build directory:

```bash
scripts/install-user --xodus-build-dir /absolute/path/to/xodus-build
```

The installer prints every destination and action before its first mutation. It writes only the launcher and doctor under `~/.local/bin`, Xodus and the patcher under `~/.local/libexec`, the build manifest under `~/.local/share`, and the user unit under `~/.config/systemd/user`. It reloads the user systemd daemon but does not start or enable the service. Re-run `~/.local/bin/forza-doctor` afterwards.

## Steam configuration

Generate the single Steam launch-options line from the checkout:

```bash
scripts/print-steam-options
```

Paste its output into Forza Motorsport's Steam launch options. It emits the approved ordered baseline: `WINEDLLOVERRIDES=xgameruntime=b`, `PROTON_VKD3D_HEAP=1`, `VKD3D_CONFIG=skip_application_workarounds,descriptor_heap,avoid_image_buffer_aliasing`, the current user's Xodus socket path, and the shell-quoted installed launcher before literal `%command%`. It does not edit Steam files. Do not add `PROTON_DISABLE_HIDRAW`, `PROTON_ENABLE_WAYLAND`, `-SkipTargetHardwareProfiler`, or unrelated Proton flags.

## Reviewed runtime-component transaction

The source-reviewed Xodus/WineGDK integration has a separate fail-closed installer. It accepts only the exact clean integration, WineGDK, and Xodus revisions, binds ten source artifacts into a private mode-0600 evidence manifest, and prints a digest-bound plan before installation. It stages every source before the first destination changes, holds the same exclusive lock as the game launcher, and refuses to mutate while Forza or `xodus-forza.service` is active. It never starts or stops either one.

Set the four absolute roots for the reviewed local builds and compatibility tool:

```bash
user_root="$HOME"
compat_tool="${XDG_DATA_HOME:-$HOME/.local/share}/Steam/compatibilitytools.d/GE-Proton11-3-FM"
winegdk_build=/absolute/path/to/reviewed-winegdk-build
xodus_build=/absolute/path/to/reviewed-xodus/target/release
evidence="$user_root/.local/state/forza-motorsport-linux/runtime-transactions/artifact-evidence.json"
runtime_args=(
    --compat-tool-root "$compat_tool"
    --user-root "$user_root"
    --xgameruntime-build-dir "$winegdk_build"
    --xodus-build-dir "$xodus_build"
    --artifact-evidence-manifest "$evidence"
)
scripts/install-runtime-components lock-evidence "${runtime_args[@]}"
scripts/install-runtime-components plan "${runtime_args[@]}"
```

Review all ten printed source roles, revisions, before hashes, fixed modes, ownership decision, and `PLAN_SHA256`. Installation requires that exact digest and recomputes every input:

```bash
scripts/install-runtime-components install "${runtime_args[@]}" --plan-sha256 EXACT_PRINTED_DIGEST
scripts/install-runtime-components status "${runtime_args[@]}"
```

If the ownership manifest is absent but one or more of the eight user destinations already exist, the plan refuses them by default. Inspect those files first, then pass `--adopt-unmanaged` to both `plan` and `install`; the adoption decision is included in the plan digest. This is intended for an explicitly reviewed legacy installation, not as a conflict override.

Before manual validation, `rollback` restores the prior launcher, doctor, patcher, build manifest, Xodus executables, systemd unit, ownership manifest, and both WineGDK files. The launcher is published first: before that rename no functional component has changed, and after it every unfinished state is launch-blocking. Publishing or restoring the unit requires a successful user-manager `daemon-reload`; rollback keeps the recovery-aware launcher in place if that reload fails. After successful validation, `accept` retains the backups; a later `restore-runtime` restores only the WineGDK pair and leaves all accepted user files owned by the normal user-install manifest:

```bash
scripts/install-runtime-components rollback "${runtime_args[@]}"
# Or, only after the full manual matrix passes:
scripts/install-runtime-components accept "${runtime_args[@]}"
scripts/install-runtime-components restore-runtime "${runtime_args[@]}"
```

Do not delete transaction directories or adjacent backup/recovery files. The launcher allows the `installed` state for manual game validation but blocks `prepared`, `installing`, `rolling_back`, and `recovery_required`. This transaction changes only the eight allowlisted user payloads, their ownership manifest, and the WineGDK pair inside the selected compatibility tool. It does not touch Steam launch options, the game prefix, credentials, or the separately supplied `xgameruntime.dll.threading` file.

## Known-build patch fallback

If `forza-doctor` reports `WARN` for controller and mountmgr known-build state, the reviewed fallback is available only for the exact hashes in `supported-builds.toml`. Unknown, mixed, changed, missing, or partly patched targets fail closed. The fallback covers `windows.gaming.input.dll` and `mountmgr.sys`; it is not a generic Proton flag.

The `mountmgr.sys` fallback is for AP702, Forza's HDD/SSD launch rejection. On this dedicated compatibility build only, the reviewed exact-build patch reports the reviewed TRIM/storage capability through `StorageDeviceTrimProperty`. It is temporary evidence for a source fix, not a claim about other Wine, Proton, disks, or game builds.

Choose a private backup directory, record the printed backup-manifest path, and run:

```bash
patcher="$HOME/.local/libexec/forza-motorsport-linux/patch-known-build"
manifest="$HOME/.local/share/forza-motorsport-linux/supported-builds.toml"
steam_root="${XDG_DATA_HOME:-$HOME/.local/share}/Steam"
backup_root="$HOME/.local/state/forza-motorsport-linux/patch-backups"
"$patcher" apply-forza --manifest "$manifest" --steam-root "$steam_root" --backup-root "$backup_root"
```

The patcher preflights all four in-scope targets before writing, creates verified user-owned backups outside every compatibility-tool directory, and records a backup manifest. Publication is bound to the validated inode and digest through held no-follow directory descriptors; after an exchange, a concurrent regular file, symlink, or directory is either restored atomically to the public path or preserved under an exact path named in the failure. The patcher does not unlink mutable post-exchange names. Successful recovery renames use a deterministic `.forza-recovery-*` name beside the target; even a following directory-sync failure reports that exact name. Successful apply and restore also retain the other exchange object under a reported recovery name. These hidden recovery objects intentionally consume additional disk space; keep the external backup manifest authoritative and remove retained objects manually only after verifying they are no longer needed. Restore only with that manifest:

```bash
"$patcher" restore-forza --manifest "$manifest" --steam-root "$steam_root" --backup-manifest /absolute/path/to/forza-patch-TIMESTAMP.json
```

Restore also verifies current and backup hashes before changing files.

## Launch and logs

Steam invokes the generated line, which runs `forza-linux` around Steam's game command. Xodus starts only after the launcher reserves its token. For investigation, use the normal user journal and redact account, authorization, proof-key, gamertag, XUID, and invite data before sharing. This repository does not collect or upload runtime logs.

## Invite/join workflow

Outgoing Microsoft/Xbox invite and join are experimental through Xodus. They require a successful game session and are not a promised feature. **Incoming invite notifications are not supported.** Steam invites are not a replacement for Microsoft/Xbox invites.

The reviewed local WineGDK bridge also carries an activation URI from trusted
Xodus UI back into Forza through the deprecated `XGameInviteRegisterForEvent`
compatibility callback. That is the accepted Join path, not automatic receipt
of Xbox network notifications. The source gate is green; installed and live
game validation is still pending.

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

Uninstall removes only files proven by its install manifest. Modified, replaced, or unproven files are preserved with a warning; already-missing paths are left absent. Interrupted-install and replaced owned material is moved into the private project recovery directory under `~/.local/state/forza-motorsport-linux/recovery` and retained rather than unlinked through a mutable name. This intentional recovery retention consumes disk space; inspect and purge it manually only after confirming it is no longer needed.

## Upstream branches and provenance

This repository contains only scripts, manifests, documentation, and synthetic test fixtures. It does not redistribute Wine, Proton, Xodus, Microsoft, Steam, or game binaries. The supported-build manifest records reviewed hashes and byte edits.

### Component source status

The selected implementation bases are `xodus-gaming/wine` `bleeding-edge` for the standalone Wine fixes, `xodus-gaming/xodus` `main` for Xodus, and `Weather-OS/WineGDK` `b03ba49c4f326c36aa6930fbe6cf72841ef3738c` for the local experimental XGameRuntime bridge now reviewed at `6e0c6d1f951a7a457444ba8abd81374c0d093dba`. Fresh inspection found that xgameruntime PR 19 is an IDL template rather than a Unixlib transport, while `oot-cpp` cannot replace the Wine submodule layout. The component roadmap is sequential: these companion branches and immutable SHAs are not public yet, and stale local branch history is reference-only. The planned public names are `forza-social-invite-join`, `xodus-social-invite-bridge`, `wgi-physical-nonroamable-id`, and `storage-trim-property`; they are names for future reviewed publication, not current download locations. The XGameRuntime deliverable is an explicitly AI-assisted experimental WineGDK branch, not an upstream `xodus-gaming/xgameruntime` code PR.

The canonical bases can be checked out for inspection with no claim that they build the complete integration:

```bash
git clone https://github.com/xodus-gaming/wine.git wine
git -C wine switch bleeding-edge
git clone https://github.com/xodus-gaming/xodus.git xodus
git -C xodus switch main
git clone https://github.com/Weather-OS/WineGDK.git winegdk
git -C winegdk switch --detach b03ba49c4f326c36aa6930fbe6cf72841ef3738c
```

When the reviewed Xodus branch is published, its documented workspace build command is `cargo build --release --workspace`. The installer requires `--xodus-build-dir` to be an absolute directory containing exactly these direct artifacts: `xodus-service`, `xodus-cli`, and `xodus-overlay` (for a conventional Cargo release build, evaluate the verified `target/release` output directory rather than the checkout root). The planned component build checks are `make -C dlls/mountmgr.sys` for Wine storage and an isolated out-of-tree `make -C <build>/dlls/xgameruntime` for the WineGDK bridge. A complete build-from-source path cannot be claimed until the companion branches, immutable SHAs, and artifact-parity evidence are published; until then, use only the legal prerequisites and exact-build fallback described above.

See [architecture](docs/architecture.md), [troubleshooting](docs/troubleshooting.md), and [verification](docs/verification.md).
