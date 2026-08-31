# Architecture

The repository is a local control surface for one experimental Steam layout: AppID `2440510` and `GE-Proton11-3-FM`. Bash owns launcher, diagnostic, and installation workflows; the extensionless Python helper owns no-follow installation transactions; the extensionless Python patcher owns exact-build patching.

## Service ownership

`forza-linux` serializes launches with a non-truncating, no-follow runtime lock validated as a current-user regular file before locking. It writes a random mode-0600 pending token and starts the user service. `xodus-forza.service` consumes that pending token atomically into an active token before `ExecStart`. The launcher records the unit invocation ID and token, waits for the exact filesystem socket, then runs Steam's game command. On exit it stops only a still-matching invocation it started. A direct unit start lacks the pending token and therefore fails closed.

## Read-only checks

`forza-doctor` does not start, stop, enable, install, patch, or log in. It checks Steam/AppID paths, installed artifacts, disk space, unit disabled state, patch state, and D-Bus `org.freedesktop.secrets`. KDE Wallet is accepted through its Secret Service provider, not by adding GNOME Keyring.

## Exact-build patches

`supported-builds.toml` declares the only accepted SHA-256 originals, patched SHA-256 values, and byte edits. The patcher preflights all four controller/mountmgr targets. It rejects unknown or mixed states, requires backups to resolve outside `compatibilitytools.d`, and uses held no-follow directory descriptors plus atomic exchange to bind publication to the validated inode and digest. After the first exchange, any durability or validation failure enters the same recovery path: it exchanges the displaced object back regardless of file type, or atomically preserves the displaced object under a deterministic recovery name if the restorative exchange fails. Post-exchange names are never unlinked or overwritten. Every successful recovery rename is reported as an exact `.forza-recovery-*` path before control returns; if its directory sync fails, that exact final path is carried by the error. If the filesystem rejects the recovery rename itself, the error instead names the exact still-private source. Restore consumes the backup manifest and rechecks every target and backup before changing any target. External backups remain authoritative; the same-directory recovery objects are retained race evidence and intentionally consume additional disk space.

## Installation and recovery

The installer accepts a fixed allowlist of user-local destinations. Its `--check` path performs the same source and conflict preflight without mutation, and normal installation prints the exact action plan before staging. Installation writes a durable transaction journal, publishes only verified files, then writes the install manifest. It never recursively deletes a mutable pathname. Instead it moves installer-owned or replaced material into a mode-restricted per-transaction recovery directory. A caught failure retires the journal only after both rollback and exact stage retirement are proven; otherwise recovery evidence remains discoverable.
