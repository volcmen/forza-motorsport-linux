# Troubleshooting

[Home](Home.md) · [Install](Install.md) · [Play](Play.md) · [Recovery](Recovery.md)

Start with the check for your situation. Run setup commands from the checkout
used for installation:

```bash
# Bootstrap incomplete, interrupted, or not yet installed:
./setup bootstrap --check

# Inspect an installed runtime transaction and readiness:
./setup status

# Direct read-only diagnostics, if installed:
~/.local/bin/forza-doctor
```

`FAIL` means a required item is missing or cannot be trusted. A controller or
mountmgr `WARN` means the exact reviewed original was found; in the manual
workflow it can be considered for the [known-build fallback](Advanced-setup.md#exact-build-controller-and-ap702-fallback).
A `FAIL` in that check is not permission to patch an unknown build.

## Find the symptom

| Symptom | Next step |
| --- | --- |
| Bootstrap stopped at a Steam or DLL checkpoint | Complete the printed action, then resume from the same checkout and inputs. See [Install](Install.md#3-start-bootstrap-and-follow-its-steam-checkpoint). |
| Flatpak Steam or game in another library | This bootstrap layout is unsupported. Changing launch flags cannot fix its path assumptions. |
| Native library missing | Check the [required host libraries](Install.md#1-check-the-requirements); bootstrap does not install packages. |
| AP702 HDD/SSD rejection | Bootstrap applies the reviewed fix. For a manual setup, inspect the exact-build mountmgr fallback. Do not add an SSD launch flag. |
| Controller works elsewhere but not in Forza | Check Steam Input is disabled and the reviewed build is selected. For a manual setup, inspect the controller fallback. |
| Secret Service or credential-store failure | Ensure KDE Wallet exposes `org.freedesktop.secrets` or has an activatable KDE provider in your user D-Bus session. Do not add a competing keyring daemon. |
| Xodus service refuses a direct start | Expected: the service requires a launcher ownership token. Use the generated Steam launch options. |
| Socket readiness timeout | Inspect the user journal locally and confirm you used the generated Steam line. Do not enable a permanent Xodus service. |
| No incoming invite notification | Unsupported. Open the explicit Xbox picker; see [Invite or join](Play.md#invite-or-join-a-friend). |
| `prepared`, `installing`, `rolling_back`, or `recovery_required` | Keep the game closed, preserve all records, and follow [Recovery](Recovery.md). |
| Uninstall preserves a modified file | Expected: it only removes files proven by its manifest. Review the retained file yourself. |

## Protocol errors and rejected experiments

`Unimplemented` followed by early EOF can indicate mixed XGameRuntime and Xodus
protocol generations. Restore the matching reviewed pair; do not combine the
legacy runtime with newer Xodus.

`0x800401f0` and a missing `ClientId` were investigated in the newer experimental
line. Source fixes for those errors did not make that pair usable in-game.
The [newer pair remains rejected live](Evidence.md#runtime-versions). Do not
switch to it as a troubleshooting shortcut.

## Gather a useful report

For local service diagnostics:

```bash
journalctl --user -u xodus-forza.service -b --no-pager
```

Report your release, distribution, native/Flatpak Steam, game-library location,
selected Proton tool, failing step, and the relevant doctor result. Say which
parts of [the game checklist](Play.md#verify-your-first-session) you actually tried.

Read excerpts before sharing. Remove account identifiers, gamertags, XUIDs,
authorization data, proof keys, invite/activation data, and private paths. Never
attach the licensed DLL or credential store. The tools do not upload runtime logs.

[Open a project issue](https://github.com/volcmen/forza-motorsport-linux/issues)
