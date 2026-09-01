# Architecture

The repository is a local control surface for one experimental Steam layout: AppID `2440510` and `GE-Proton11-3-FM`. Bash owns launcher, diagnostic, and installation workflows; the extensionless Python helper owns no-follow installation transactions; the extensionless Python patcher owns exact-build patching.

## Service ownership

`forza-linux` serializes launches with a non-truncating, no-follow runtime lock validated as a current-user regular file before locking. After acquiring it, the launcher validates every private runtime-component journal and refuses to continue for unfinished or recovery-required state. It writes a random mode-0600 pending token and starts the user service. `xodus-forza.service` consumes that pending token atomically into an active token before `ExecStart`. Its existing per-user runtime directory remains writable because systemd creates the mount namespace before `ExecStartPre` renames the token and before Xodus creates its socket; home and system paths remain protected. The launcher records the unit invocation ID and token, waits for the exact filesystem socket, then runs Steam's game command. On exit it stops only a still-matching invocation it started; while the matching active token still proves ownership, it also removes a non-listening socket orphaned by that stopped invocation. A direct unit start lacks the pending token and therefore fails closed.

## Read-only checks

`forza-doctor` does not start, stop, enable, install, patch, or log in. It checks Steam/AppID paths, installed artifacts, disk space, unit disabled state, patch state, and D-Bus `org.freedesktop.secrets`. KDE Wallet is accepted through its Secret Service provider, not by adding GNOME Keyring.

## Game social bridge

The game-facing WineGDK pair owns ABI and task-queue semantics; Xodus owns Xbox
authentication, HTTP operations, and UI. The main Xodus stream remains
independent from two bounded social channels:

- XDUI requests the full or invite-only overlay and returns explicit
  acknowledgement plus one terminal result;
- XDSI is the game's single activation subscription. A trusted Xodus UI
  publishes one validated, already accepted join URI through XDAP, and WineGDK
  dispatches it through `XGameInviteRegisterForEvent` on the caller's queue.

The channels use the launcher's private same-UID mode-0600 socket. They never
move authorization material, proof keys, or account identity into WineGDK and
never log activation URIs. XDSI is local push rather than application polling,
but it is not an Xbox RTA receiver; unsolicited incoming notifications are
outside the supported design.

## Exact-build patches

`supported-builds.toml` declares the only accepted SHA-256 originals, patched SHA-256 values, and byte edits. The patcher preflights all four controller/mountmgr targets. It rejects unknown or mixed states, requires backups to resolve outside `compatibilitytools.d`, and uses held no-follow directory descriptors plus atomic exchange to bind publication to the validated inode and digest. After the first exchange, any durability or validation failure enters the same recovery path: it exchanges the displaced object back regardless of file type, or atomically preserves the displaced object under a deterministic recovery name if the restorative exchange fails. Post-exchange names are never unlinked or overwritten. Every successful recovery rename is reported as an exact `.forza-recovery-*` path before control returns; if its directory sync fails, that exact final path is carried by the error. If the filesystem rejects the recovery rename itself, the error instead names the exact still-private source. Restore consumes the backup manifest and rechecks every target and backup before changing any target. External backups remain authoritative; the same-directory recovery objects are retained race evidence and intentionally consume additional disk space.

## Installation and recovery

The installer accepts a fixed allowlist of user-local destinations. Its `--check` path performs the same source and conflict preflight without mutation, and normal installation prints the exact action plan before staging. Installation writes a durable transaction journal, publishes only verified files, then writes the install manifest. It never recursively deletes a mutable pathname. Instead it moves installer-owned or replaced material into a mode-restricted per-transaction recovery directory. A caught failure retires the journal only after both rollback and exact stage retirement are proven; otherwise recovery evidence remains discoverable.

The separate runtime-component installer binds exact clean source revisions and ten artifact hashes in a private evidence manifest. Its plan also binds both approved roots, the explicit legacy-adoption decision, every before-state hash/mode/owner, and fixed target modes. The outer journal treats the eight allowlisted user payloads, updated user ownership manifest, and WineGDK PE/Unix pair as eleven ordered records. Every record is staged adjacent to its destination before a second all-destination preflight and the first rename. The launcher is the first published record, so a crash before it leaves all functional components unchanged while a crash after it activates the recovery gate. Intent is journaled before each backup or publication rename; completion is journaled after it. The user manager must successfully reload after the unit is published or restored; on rollback failure the recovery-aware launcher remains installed. An interrupted full rollback and an interrupted accepted `restore-runtime` carry different recovery intents, so resuming Wine-only restoration cannot silently roll back accepted user files.

There is no claim of cross-filesystem atomicity. Recovery instead derives each record from the exact current, stage, and backup hashes. It restores an old file only from the recorded adjacent backup, moves unexpected material to a unique adjacent recovery name, verifies original type/mode/uid/gid/hash, and retains all evidence. The accepted eight user payload hashes remain in the ordinary install manifest, so the existing check and uninstall paths continue to own them. If that manifest is absent, existing allowlisted files require explicit `--adopt-unmanaged` approval in both the plan and installation. The runtime transaction never edits the game prefix or the user-supplied threading DLL.
