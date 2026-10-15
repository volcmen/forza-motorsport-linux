# SteamOS real-account and game test — 2026-10-15

Microsoft sign-in completed and its Xodus wallet records survived a VM reboot.
Real Forza launches reached a white game window without reaching the menu.
**Gameplay and physical Steam Deck compatibility remain unverified.**

This follows the earlier [controlled installer/runtime qualification](composed-preview-validation-2026-10-15.md).
The account, licensed game, threading DLL, prefix, wallet, and raw logs remain
private. This record contains no account identifiers or authentication material.

## Environment and actual inputs

- SteamOS 3.8.14, build 20260707.10; kernel
  `6.16.12-valve24.4-1-neptune-616-gfe145653a794`; Plasma X11.
- QEMU 11.1.1 with KVM, eight virtual CPUs and 16 GiB RAM;
  `honor-guest-pat=on`; SteamOS root stayed read-only.
- Venus on an NVIDIA RTX 4080 SUPER, guest Vulkan 1.4.307 and Mesa
  25.3.0-devel (`59b552c765`). This is virtualized graphics, not a Deck AMD APU.
- The six published `v0.3.0-preview.1` artifacts and integration checkout
  `5d0dbacad609cc6274b8d4588266d2d9e84c5d62`, with the hashes recorded in the
  [build record](composed-preview-build-2026-10-15.md).
- Genuine Steam AppID 2440510, game build 18920732, and
  SteamLinuxRuntime_4 build 25607596. Steam selected `GE-Proton11-3-FM`.
- A separate writable copy of the licensed game. The original host installation
  and working host prefix were preserved.

## Completed checks

1. Prepare reached `AWAITING_STEAM_PREFIX`. An actual Steam launch created the
   prefix and displayed the expected missing-Xodus-service checkpoint.
2. Finish installed the real, separately supplied threading DLL and reached
   `READY_TO_ATTEMPT`. The installed DLL matched its input byte for byte.
3. The Flatpak Microsoft login accepted password and MFA, completed its final
   continuation, and exited. Xodus user/account-token entries were present in
   the existing KDE Secret Service. Metadata checks after a full reboot found
   both entries again, without reading or exporting their secret values.
4. Actual Steam game launches started the native Xodus service and established
   Unix-socket connections. The earlier missing-service warning disappeared.
   This establishes connectivity, not successful completion of every Xbox API.
5. The interactive VM viewer's keyboard path was checked through GtkVnc, QEMU,
   and a guest input dialog. The working viewer used the X11 backend.

## Real game attempts

The published launch options were retained, with private Proton diagnostics.
The first post-login attempt produced a white fullscreen window for more than
ten minutes, with no menu. Its log grew to 689,466,848 bytes, mostly Wine
exception tracing. The process remained alive; handled exception messages alone
do not establish a fatal crash or its cause.

A second attempt explicitly set `WINEDEBUG=-all` and `VKD3D_DEBUG=info` while
retaining `PROTON_LOG=1`. The log confirmed the effective Wine setting. It stayed
at approximately 20 KiB, but the white window persisted for more than ten
minutes, including after switching to windowed mode. Both attempts exited after
closing the game window; the owned Xodus service stopped afterward.

For the storage comparison, a 160 GiB ext4 filesystem on a separate virtio block
disk replaced the 9P game-folder mount at the same guest-visible path. All 5,294
copied files, totaling 145,746,575,472 bytes, matched their source SHA-256 hashes.
The existing prefix, tool, service payload, and diagnostic options were retained.
The third attempt again produced a white game window, with no menu after more
than seven minutes of observation. A later check found that the ext4 root
directory itself was root-owned and unwritable by the guest user, despite the
copied files matching. The corrected storage control is recorded below.

### Additional verification and controlled comparisons

Steam's first official integrity check stopped with `Install folder not writable`.
Only the isolated ext4 filesystem's root directory ownership was corrected to the
guest user; this was not a recursive permission change. Steam then completed
`Verifying Installed` on build 18920732 between 16:39:46 and 16:43:34 UTC, ending
with `result No Error`.

A sixth game launch used this verified, writable game directory and the original
quiet diagnostic settings, with the default synchronization backend restored.
It again remained at a white window for more than five minutes without a menu.
Closing the game window eventually exited the game and stopped its owned Xodus
service without forced termination. Thus the directory permission correction
enabled Steam validation but did not resolve the observed game startup blocker.

Two intervening diagnostic attempts and a filename audit established narrower
facts:

- A recursive audit found no case-insensitive sibling-name collisions in the
  copied game or the prefix's `system32` and `syswow64` directories. No DLLs were
  renamed or removed.
- Targeted GDK tracing reached the preferred UDP-port result function, three
  URL security-information result calls, and certificate-verification calls.
  These are observed function entries, not proof of successful HRESULTs, all
  asynchronous queues completing, or a completed Xbox user flow.
- Setting only `PROTON_NO_NTSYNC=1` relative to that traced run changed the active
  backend from NTSYNC to fsync, confirmed by both the log and sampled wait stacks.
  The white window persisted. The override and extra tracing were removed for
  the final verified-files run; neither is proposed as a fix.
- Repeated native stack samples found a busy thread yielding through
  `NtYieldExecution`. In the final run, reading only saved instruction addresses
  and loader metadata resolved its `SwitchToThread` caller to the game executable
  at RVA `0x629ad3` for build 18920732. The main thread was waiting. This localizes
  an observed wait; it does not identify the missing completion or its cause.

The source readiness diagnostic now checks read/write/search access to the actual
game directory without writing a probe file, and measures free space on that
directory's filesystem instead of assuming it shares the library root's mount.
Regression tests cover an unwritable directory without altering its files or
permissions and separate game/library free-space results. In the real guest, the
candidate reported game access available, 20 GiB free on the game filesystem
(warning), and 34 GiB at the Steam tools root. Other platform warnings/failures
remain distinct from these scoped results. The tagged preview predates this
diagnostic refinement.

## Reboot exposed a launcher defect

After reboot, KDE advertised the activator `org.kde.secretservicecompat`, while
`org.freedesktop.secrets` had no owner and was not itself activatable. Starting
the published native Xodus service then failed during keychain initialization
with `org.freedesktop.DBus.Error.ServiceUnknown`. The Flatpak login wrapper
already handles this KDE activation path; the native game launcher did not.

The launcher correction queries ownership, tries the standard activator and
then KDE's existing compatibility activator, and requires the standard name to
have an owner before reserving a new Xodus session. Calls are bounded. An
existing provider or existing Xodus session is reused. Provider activation is
independent of game-service cleanup.

In the real rebooted guest, the candidate launcher activated KDE's provider,
started Xodus, ran a successful `/usr/bin/true` handoff, and stopped its owned
service. The accepted installation files and journals were kept unchanged while
testing this candidate entrypoint. The account records were then accessible and
the published game entrypoint could launch again.

Validation: 41 launcher tests passed, including seven provider-activation cases;
180 composed-setup and runtime-component tests and 20 repository-policy checks
passed. ShellCheck, shfmt, REUSE, and whitespace checks passed.

For an existing `v0.3.0-preview.1` installation on the tested SteamOS KDE image,
the following session-scoped workaround activates the already installed wallet:

```sh
busctl --user --timeout=5 call org.freedesktop.DBus /org/freedesktop/DBus \
  org.freedesktop.DBus StartServiceByName su org.kde.secretservicecompat 0
```

Then launch normally. Preserve the accepted installation; replacing managed
files by hand invalidates its integrity checks. New installations from the
corrected checkout include the launcher fix.

## Graphics diagnostic and limits

A scratch Direct3D 12 probe used the same composed Proton and Steam Linux Runtime
with its own prefix. Device creation at feature level 12_0, render-target clear,
GPU-fence completion, readback, and presentation succeeded. The returned RGBA
pixel was `64,127,191,255` against an expected `64,128,191,255` with one-unit
tolerance, and the blue window was visible in the guest capture. A host control
also passed and exited normally. The VM probe runner needed timeout cleanup;
full probe lifecycle success is not claimed.

A follow-up probe added flushed markers around each cleanup operation. In the
guest, the last marker was the entry to `IDXGISwapChain::Release`; it did not
return before the 50-second runner timeout. A control that moved window
destruction after swap-chain release stopped at the same call. Both probes still
passed the fence, readback and presentation checks. This identifies an unresolved
probe cleanup failure in the tested graphics stack, not the cause of the game's
startup wait.

Basic Vulkan and Direct3D rendering therefore work in this VM. That does not
prove Forza-specific rendering, its startup dependencies, or sustained stability.
No game menu, driving, online content, saves, token refresh, or Invite/Join success
was observed. Physical Deck controls, AMD graphics, Gaming Mode, microSD,
suspend/resume, battery behavior and performance still require hardware tests.
