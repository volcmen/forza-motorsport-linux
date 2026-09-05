# Resume, undo, or remove setup

[Home](Home.md) · [Install](Install.md) · [Troubleshooting](Troubleshooting.md)

**Keep journals and backups.** They let the tools distinguish your original
files from installed files and recover interrupted changes. Close Forza before
installation or recovery changes. Commands run from your original setup checkout.

## Choose the operation

| What you used or want | Command | Scope |
| --- | --- | --- |
| Inspect bootstrap progress | `./setup bootstrap --check` | Read-only bootstrap checks |
| Undo bootstrap | `./setup bootstrap --rollback` | Reverse the composed bootstrap stages |
| Inspect a runtime transaction | `./setup status` | Runtime state, then readiness diagnostics |
| Undo an unaccepted manual runtime transaction | `./setup rollback` | Older runtime-only transaction |
| Restore runtime after acceptance | See [Advanced setup](Advanced-setup.md#accept-or-restore) | Restore the recorded XGameRuntime pair |
| Remove user-local integration | `./setup uninstall` | Remove manifest-proven integration files |

The two rollback commands have different scopes. **Do not substitute
`./setup rollback` for `./setup bootstrap --rollback`.** Uninstall is not a
replacement for either rollback path.

## Resume an interrupted bootstrap

Run `./setup bootstrap --check` and read the state. To resume, rerun your original
`./setup bootstrap` command with the same input arguments in the same checkout. Complete any printed Steam or licensed-DLL checkpoint.
For the exact resume command, return to [Install, step 4](Install.md#4-supply-the-licensed-runtime-and-finish).
Cancellation or closed terminal input does not automatically undo completed stages.

If a recovery error remains, preserve the state and inspect it before changing
inputs. Do not delete a journal to make setup look fresh.

## Undo bootstrap

```bash
./setup bootstrap --rollback
```

Let the coordinator restore the recorded stages in reverse order, then run
`./setup bootstrap --check`. The recorded transaction should report `rolled-back`
without a recovery error; this checks the restored state, not readiness to play.
Keep recovery material if the check fails. Do not manually remove stages or
switch to the runtime-only rollback halfway through.

## Recover a manual runtime transaction

For an unaccepted transaction:

```bash
./setup status
./setup rollback
```

Rollback asks for the compatibility-tool directory used by the transaction.
Press Enter only if the displayed standard path is the original path; otherwise
supply the original absolute path. The tools refuse changes unless they can
prove Forza and the service are inactive and acquire the launcher lock.

For lower-level `status`, `rollback`, `accept`, or `restore-runtime`, reuse the
exact roots and inputs in [Advanced setup](Advanced-setup.md#reviewed-runtime-component-transaction).
An interrupted full rollback and an interrupted runtime-only restoration must
resume their own operation; they are not interchangeable.

## Uninstall user-local integration

First use [bootstrap rollback](#undo-bootstrap) for a bootstrap installation,
or [manual runtime recovery](#recover-a-manual-runtime-transaction) for an
unaccepted manual transaction. For an accepted manual runtime, see
[restore-runtime](Advanced-setup.md#accept-or-restore). Then, if user-local
integration remains and you want to remove it, run:

```bash
./setup uninstall
```

This removes only files proven by the installation manifest. Modified, replaced,
or unproven files are preserved; already-missing paths are left absent. It does
not restore compatibility-tool runtime files or remove the licensed Microsoft
DLL, patch backups, or recovery evidence. Standalone patch restoration uses the
[recorded backup manifest](Advanced-setup.md#exact-build-controller-and-ap702-fallback).

## Records to preserve

The user-local installer uses
`~/.local/state/forza-motorsport-linux/transaction-journal` and its recovery directory.
The runtime installer uses
`~/.local/state/forza-motorsport-linux/runtime-transactions`.
Keep adjacent `.forza-recovery-*` files, stages, and external patch backups too.
If the tool cannot prove one exact transaction, preserve everything for inspection.

Optional private before/after snapshots help compare a reviewed operation:

```bash
./setup snapshot --output /absolute/private/path/before.json
# Perform the operation you have reviewed, then:
./setup snapshot --output /absolute/private/path/after.json
./setup compare --before /absolute/private/path/before.json --after /absolute/private/path/after.json
```

Snapshots can contain identifying paths. Review them before sharing.
