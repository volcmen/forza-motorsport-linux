# Preview 2 local validation — 2026-10-17

This maintenance preview updates the installer, recovery, launcher and
diagnostics. Its five runtime/source packages are byte-identical to the approved
`v0.3.0-preview.1` packages. The original [build and source identities](composed-preview-build-2026-10-15.md)
remain authoritative; no new Proton or Xodus binary build is claimed.

## Installer and recovery

The initial repository gate at `3182ee3` passed: 1,009 Python tests, one optional
rootless-Docker skip, 113 shell tests, systemd unit validation, ShellCheck,
shfmt 3.14.0, Ruff, REUSE, 21 policy tests and whitespace checks.

The full gate after the recovery correction and upstream-test preparation
passed **1,015 Python tests**, with the same one optional skip and 113 shell
tests. All lint, unit, license, policy and whitespace checks passed.

The real-account SteamOS 3.8.14 VM was booted with its existing private account,
Steam-created prefix and verified game build 18920732. It uses eight virtual
CPUs, 16 GiB RAM and Venus on an NVIDIA RTX 4080 SUPER. Its OS root remained
read-only. The host installation, credentials and game files were not modified.

The earlier storage experiment had changed the game from a 9P share to ext4 at
the same path. The original composed status and rollback both rejected the game
directory identity. Rollback does not modify that directory, so the correction
allows its device/inode to differ only during rollback. Home, Steam and library
bindings remain exact; prefix, licensed-DLL journal, user files, saved helper and
published tool still undergo their independent checks. The game path cannot
change. Prepare, Finish and status retain the strict game binding.

Six regression cases cover successful rollback after a game replacement,
preservation of both game directories, rejection of changed writable roots or
game path, and strict Finish behavior. The corrected rollback completed against
the real VM installation, retaining its prefix and recovery material.

Fresh Prepare using candidate `4c44b26` reached `AWAITING_STEAM_PREFIX`. Finish
used the existing Steam-created prefix and real licensed threading DLL. Its
first attempt correctly stopped because the actual game filesystem had only
20 GiB available, below Doctor's 30 GiB minimum. The isolated raw game disk was
expanded from 160 to 192 GiB and its ext4 filesystem grew online, providing
53 GiB free. Retrying the recorded Finish plan reached `READY_TO_ATTEMPT`;
status and Doctor passed, with the expected warning below 100 GiB free.
No journal, hash or space check was bypassed. This was a fresh tool/user
installation using an existing prefix, not a new Steam account or prefix test.

The v0.2 stock-GE fresh-download path is now explicitly retired and directs new
installations to `setup composed`. Existing v0.2 trust pins, status, resume and
rollback behavior remain available; no automatic migration is introduced.

## Installed runtime checks

These executables ran through the newly installed `forza-linux`, its native
Xodus service and packaged Proton:

| Check | Result |
| --- | --- |
| XGameRuntime Wine regression | 26 tests, zero failures; optional invite-relay test skipped |
| Storage-property regression | 18 checks, zero failures |
| Upstream public controller-test executable | 60 tests, zero failures; controller-dependent identity assertions skipped because no controller was connected |
| Service lifecycle | Inactive after each completed launcher session |

The new launcher activated the existing KDE Secret Service after this boot.
The tests did not require a new credential daemon or changes to SteamOS's OS
image. The standalone older Wine build still failed before controller-test
dispatch with `kernel32.dll` status `c0000135`; the successful packaged-Proton
execution is a separate result.

The [upstream controller test patch and semantic review](../patches/wine-controller/README.md#public-api-test-preparation-2026-10-17)
are preparatory work. Native Windows, connected-controller assertions,
cross-application identity and reboot persistence remain unverified.

## Real-game follow-up

The installed candidate launched the licensed game through Steam, loaded the
saved account/profile and entered Free Play. The scoped Venus presentation
override from the [October 16 test](steamos-venus-presentation-test-2026-10-16.md)
was retained; this is not a physical-GPU default. The native threading DLL was
confirmed in the actual game process's mappings.

A one-lap Brands Hatch Indy Circuit event used the owned 2017 Acura NSX, noon,
clear weather and no AI opponents. Braking and steering were temporarily set to
Fully Assisted and throttle to Assisted to exercise a complete lap. The race
rendered and advanced, then stalled with the lap timer at **00:46.858**. Repeated
captures showed the same frame; Escape did not produce the pause menu. A complete
race was not achieved. A private debugger sample found a busy thread in
`NtYieldExecution` and the main thread waiting through `linux_wait_objs`;
the Xodus service remained active and no GPU reset appeared in the guest kernel
journal. These observations do not identify the cause. Terminating only the
stalled game returned Steam to Play and the owned service became inactive.

A controlled retry added only `PROTON_NO_NTSYNC=1`. The actual process had that
setting, no `WINENTSYNC`, and `WINEFSYNC=1`; Proton logged `fsync: up and running`.
The native threading DLL remained mapped. Sign-in, shaders and the menu loaded,
but the same car/event settings did not finish loading within three minutes
(11:51:51–11:54:54 UTC). A sample again found a busy `NtYieldExecution` thread,
with the main thread now in `fsync_wait_objects`. This comparison did not solve
the failure and does not justify a default synchronization change. The game was
stopped and the owned service again became inactive.

### DLL-free comparison

The [Reddit threading suggestion](community-feedback-2026-10-17.md#threading-suggestion-what-the-source-establishes)
was tested against the shipped Wine/runtime, not substituted with a different
upstream tree. A process-scoped Wine override disabled loading
`xgameruntime.dll.threading` while retaining `xgameruntime=b`; the installed
licensed file and recovery records were left intact. Native ntsync and the
existing Venus settings were restored, with DLL-load tracing enabled.

The actual game process had the disabling override and **no native threading
DLL mapping**. The log identified builtin `xgameruntime.dll`, and its warning
confirmed that the game requested the builtin XThreading fallback. After the
warning was acknowledged, the game exited before reaching the title screen
(the guest recorded `SIGABRT`);
Steam returned to Play and the owned service became inactive. No successful
DLL-free game launch is claimed, and the installer requirement remains.

The standalone XGameRuntime executable then ran through the installed launcher
with the same native-DLL disabling override: **26 tests, zero failures, one
optional invite-relay skip**, exit 0. Its task-queue/async assertions passing does
not establish Forza compatibility. This result applies to the preview's pinned
runtime, not all newer upstream XThreading implementations.

A native-threading control retained the same DLL tracing. Its native DLL mapping
was confirmed and it advanced to shader optimization, but it also exited before
the menu. Consequently, these traced runs do not isolate the cause of either
exit or prove that the missing native DLL caused it. All experimental launch
overrides were then removed and the original Steam launch-options string was
restored exactly. A further untraced launch also exited early, so the isolated
VM was powered off and restarted before cleanup.

## Release input checks

All five release packages matched the original published `SHA256SUMS`. The
initial local selection found an older Flatpak at the build directory's top
level and rejected it. The final selection uses the security-updated package:
4,809,632 bytes, SHA-256
`a6c6257f4334ae65425371427f71141d46d4e13a2315e1337d0671fb67aaca61`.
No checksum was changed to accommodate a different binary.

Microsoft binaries, the game, account data and raw private logs are excluded
from release assets. The runtime still requires the separately supplied
licensed Microsoft threading DLL. Physical Steam Deck, Gaming Mode,
controllers, microSD removal, suspend/resume and measured performance need
their own tests; this VM cannot establish those results.
