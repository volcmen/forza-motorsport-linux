# Architecture

The repository is a local control surface for one experimental Steam layout: AppID `2440510` and `GE-Proton11-3-FM`. Bash owns launcher, diagnostic, and installation workflows; the extensionless Python helper owns no-follow installation transactions; the extensionless Python patcher owns exact-build patching.

## Service ownership

`forza-linux` serializes launches with a runtime lock, writes a random mode-0600 pending token, and starts the user service. `xodus-forza.service` consumes that pending token atomically into an active token before `ExecStart`. The launcher records the unit invocation ID and token, waits for the exact filesystem socket, then runs Steam's game command. On exit it stops only a still-matching invocation it started. A direct unit start lacks the pending token and therefore fails closed.

## Read-only checks

`forza-doctor` does not start, stop, enable, install, patch, or log in. It checks Steam/AppID paths, installed artifacts, disk space, unit disabled state, patch state, and D-Bus `org.freedesktop.secrets`. KDE Wallet is accepted through its Secret Service provider, not by adding GNOME Keyring.

## Exact-build patches

`supported-builds.toml` declares the only accepted SHA-256 originals, patched SHA-256 values, and byte edits. The patcher preflights all four controller/mountmgr targets. It rejects unknown or mixed states, writes verified backups to an explicit user directory, replaces files atomically, and verifies results. Restore consumes the backup manifest and rechecks every target and backup before changing any target.

## Installation and recovery

The installer accepts a fixed allowlist of user-local destinations, stages content, writes a durable transaction journal, publishes only verified files, then writes the install manifest. It never recursively deletes a mutable pathname. Instead it moves installer-owned or replaced material into a mode-restricted per-transaction recovery directory. The journal remains discoverable unless retirement of its recorded staging inode is proven; recovery is therefore retained at the cost of additional local disk space.
