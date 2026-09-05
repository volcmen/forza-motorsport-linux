# Advanced setup

[Home](Home.md) · [Install with bootstrap](Install.md) · [Recovery](Recovery.md)

**Most players should use [Install](Install.md).** This page is for users who
already have the reviewed local builds and contributors inspecting transactions.

This guide is the auditable, manual counterpart to the top-level `./setup`
wizard. Both paths delegate to the same installers and enforce the same exact
source revisions, hashes, locks, journals, and rollback rules.

> [!WARNING]
> This is an experimental, source-only workflow for Steam AppID `2440510` and
> the reviewed `GE-Proton11-3-FM` layout. Do not substitute similarly named
> builds, use `sudo`, or delete recovery material to bypass a failed state.

## Choose the installation path

For the guided path:

```bash
./setup --help
./setup
```

For a fully manual review, follow the remaining sections. Do not mix partial
steps from the guided and manual flows without first checking the current
transaction state:

```bash
./setup status
```

## Required inputs

Prepare these before installation:

1. A licensed Steam installation of Forza Motorsport.
2. The reviewed `GE-Proton11-3-FM` compatibility-tool directory.
3. A clean build of Xodus commit
   `7b236772297b3475ea4f3cb830feb5b224f3064a`; the supplied path must be the
   direct `target/release` directory containing `xodus-service`, `xodus-cli`,
   and `xodus-overlay`. See the
   [v0.1 Xodus evidence](../evidence/v0.1-xodus.md) for the exact source/build
   record.
4. A clean out-of-tree build of legacy XGameRuntime commit
   `a1548b1cf57371715d10b608bc81a77a188e40d4` containing the reviewed PE and
   Unix artifacts. See the
   [v0.1 XGameRuntime evidence](../evidence/v0.1-xgameruntime.md) for the exact
   source/build boundary.
5. A licensed Microsoft `xgameruntime.dll` from your own Windows installation.

This manual workflow downloads none of those artifacts. The separate bootstrap
workflow can acquire the open-source components.

## Place the separately licensed threading runtime

Let Steam create the game prefix first by starting and closing Forza with the
reviewed compatibility tool selected.

The Microsoft threading runtime is intentionally outside the repository's
transaction and evidence manifests:

```bash
steam_root="${XDG_DATA_HOME:-$HOME/.local/share}/Steam"
prefix_root="$steam_root/steamapps/compatdata/2440510/pfx/drive_c"
mkdir -p -- "$prefix_root/windows/system32"
cp -- /absolute/path/to/your/licensed/xgameruntime.dll \
  "$prefix_root/windows/system32/xgameruntime.dll.threading"
```

Do not publish the file, its contents, or a download location.

## Read-only user-install preflight

From the integration checkout:

```bash
bin/forza-doctor
scripts/install-user --check \
  --xodus-build-dir /absolute/path/to/xodus/target/release
```

`forza-doctor` reports the current installed environment. On a fresh system,
missing user-local components are expected. `install-user --check` validates
the user payload sources and destinations and prints its proposed actions
without creating directories, writing a journal, or invoking systemd.

KDE Wallet is accepted through the standard Secret Service D-Bus API. GNOME
Keyring is not required.

## Reviewed runtime-component transaction

Set the exact roots once:

```bash
user_root="$HOME"
compat_tool="${XDG_DATA_HOME:-$HOME/.local/share}/Steam/compatibilitytools.d/GE-Proton11-3-FM"
xgameruntime_build=/absolute/path/to/reviewed-xgameruntime-build
xodus_build=/absolute/path/to/reviewed-xodus/target/release
evidence="$user_root/.local/state/forza-motorsport-linux/runtime-transactions/artifact-evidence.json"
runtime_args=(
  --compat-tool-root "$compat_tool"
  --user-root "$user_root"
  --xgameruntime-build-dir "$xgameruntime_build"
  --xodus-build-dir "$xodus_build"
  --artifact-evidence-manifest "$evidence"
)
```

`lock-evidence` is the first persistent action: it writes a private mode-0600
manifest after verifying the exact clean source repositories and all ten source
artifacts. Then `plan` recomputes those inputs and prints every destination,
before-state, source hash, mode, ownership decision, and the final digest:

```bash
scripts/install-runtime-components lock-evidence "${runtime_args[@]}"
scripts/install-runtime-components plan "${runtime_args[@]}"
```

Review the complete output. Installation accepts only the exact lowercase
digest just printed, and recomputes the plan before its first mutation:

```bash
scripts/install-runtime-components install "${runtime_args[@]}" \
  --plan-sha256 EXACT_PRINTED_PLAN_SHA256
scripts/install-runtime-components status "${runtime_args[@]}"
```

The installer stages all inputs first, holds the same exclusive lock as the
game launcher, and refuses to mutate while Forza or `xodus-forza.service` is
active. The launcher is the transaction's publication gate. An unfinished
state remains launch-blocking rather than looking successfully installed.

If user destinations exist without the matching ownership manifest, the plan
fails. Inspect them first. Only for a deliberately reviewed legacy install,
repeat both `plan` and `install` with `--adopt-unmanaged`; that decision becomes
part of the digest. It is not a general conflict override.

## Standalone user-local install

The lower-level user installer is useful when the runtime components are
already managed separately:

```bash
scripts/install-user \
  --xodus-build-dir /absolute/path/to/xodus/target/release
```

It writes only the launcher and doctor under `~/.local/bin`, project and Xodus
executables under `~/.local/libexec`, the supported-build manifest under
`~/.local/share`, and the user unit under `~/.config/systemd/user`. It reloads
the user systemd manager but does not start or enable Xodus.

Do not run this standalone mutation in the middle of an active runtime
transaction.

## Exact-build controller and AP702 fallback

If `forza-doctor` reports all reviewed targets in the original state, it emits
a `WARN`. Patching remains a separate, explicit decision. The fallback covers
only the hashes in `supported-builds.toml`; unknown, mixed, modified, missing,
or partially patched targets fail closed.

The two reviewed changes are:

- a `windows.gaming.input.dll` controller identity path for the tested physical
  Xbox controller; and
- a `mountmgr.sys` `StorageDeviceTrimProperty` response for Forza's AP702
  HDD/SSD launch rejection.

Choose a private backup directory and record the printed backup-manifest path:

```bash
patcher="$HOME/.local/libexec/forza-motorsport-linux/patch-known-build"
manifest="$HOME/.local/share/forza-motorsport-linux/supported-builds.toml"
steam_root="${XDG_DATA_HOME:-$HOME/.local/share}/Steam"
backup_root="$HOME/.local/state/forza-motorsport-linux/patch-backups"

"$patcher" apply-forza \
  --manifest "$manifest" \
  --steam-root "$steam_root" \
  --backup-root "$backup_root"
```

The patcher preflights all four targets before writing and keeps verified
backups outside every compatibility-tool directory. Restore only with the
recorded manifest:

```bash
"$patcher" restore-forza \
  --manifest "$manifest" \
  --steam-root "$steam_root" \
  --backup-manifest /absolute/path/to/forza-patch-TIMESTAMP.json
```

Retained `.forza-recovery-*` objects are intentional. Remove them only after
verifying that the external backup manifest is authoritative and recovery is no
longer required.

## Steam configuration

Generate rather than hand-edit the launch line:

```bash
./setup steam-options
```

Paste the single output line into Forza's Steam launch options. Select
`GE-Proton11-3-FM` under Compatibility and keep Steam Input disabled for the
tested controller path. It selects the
reviewed native XGameRuntime override, VKD3D settings, Xodus socket exposure,
and the installed launcher before literal `%command%`. It does not edit Steam.

Do not add `PROTON_DISABLE_HIDRAW`, `PROTON_ENABLE_WAYLAND`,
`-SkipTargetHardwareProfiler`, or unrelated folklore flags.

## Validate before acceptance

Run the game yourself through Steam. A meaningful validation covers launch,
online profile/content, controller navigation and driving, hotplug, AP702
absence, explicit Invite and Join, social-picker keyboard/controller input,
clean exit, and a second launch without rebooting.

Use the [first-session checklist](Play.md#verify-your-first-session).
Readiness checks and automated tests are not substitutes for that live matrix.

## Accept or restore

Before acceptance, roll back the entire recorded transaction:

```bash
scripts/install-runtime-components rollback "${runtime_args[@]}"
```

Only after completing the live validation should you accept it:

```bash
scripts/install-runtime-components accept "${runtime_args[@]}"
```

Acceptance retains the backups. To restore only the legacy XGameRuntime pair
later while leaving the accepted user-local integration in place:

```bash
scripts/install-runtime-components restore-runtime "${runtime_args[@]}"
```

Never delete a `prepared`, `installing`, `rolling_back`, or
`recovery_required` transaction. Keep the game closed, prove the Xodus service
is inactive, run `status` with the same roots, and resume the recorded recovery
operation. If the tool cannot prove one transaction, preserve everything for
inspection.

The guided `./setup rollback` command prompts for the compatibility-tool path
that was used by the transaction. Press Enter for the displayed standard
Steam path; if setup used a nondefault path, enter that original absolute path.

## User-local uninstall

After restoring compatibility-tool files as appropriate:

```bash
./setup uninstall
```

This delegates to the conflict-preserving user uninstaller. It removes only
manifest-proven files. Modified, replaced, and unproven content is retained;
missing paths stay absent. Runtime files, the licensed Microsoft DLL, patch
backups, and recovery evidence remain outside this command. After confirmation,
the command acquires the launcher lock, fails closed unless it can prove that
Forza and `xodus-forza.service` are both inactive, and holds the lock until the
user uninstaller exits.

## Logs and private data

Use the user journal for local diagnosis. Before sharing an excerpt, redact
account identifiers, authorization data, proof keys, gamertags, XUIDs, invite
data, and private paths. The repository does not collect or upload runtime logs.
