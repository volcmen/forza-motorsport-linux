# SteamOS Venus presentation investigation — 2026-10-16

Disabling both generations of Vulkan present-id/wait extensions allowed the
isolated SteamOS VM to pass the earlier white window and reach Forza's title
screen, Welcome Center, home menu, existing player profile, and settings.
**A menu is not a gameplay result. Physical Steam Deck compatibility remains
unverified.**

This follows the [real-account and verified-game test](steamos-real-account-test-2026-10-15.md).
Private account data, licensed files, screenshots containing profile information,
and raw authentication/game logs are not published.

## Tested environment

- SteamOS 3.8.14 build 20260707.10, kernel
  `6.16.12-valve24.4-1-neptune-616-gfe145653a794`, KDE Plasma X11; OS root read-only.
- QEMU 11.1.1/KVM, eight virtual CPUs, 16 GiB RAM, `honor-guest-pat=on`,
  virtio-vga-gl/Venus, and an NVIDIA RTX 4080 SUPER host GPU.
- Host kernel `7.2.8-zen1-2-zen`, NVIDIA driver `615.71.09`; guest Mesa
  25.3.0-devel (`59b552c765`). This does not reproduce a Deck AMD APU.
- The published `v0.3.0-preview.1` composed tool, `GE-Proton11-3-FM`,
  vkd3d-proton 3.1.0 build `3dfc6f07d0953b1`, game build 18920732,
  displayed game version `1.859.7102.0`, and SteamLinuxRuntime_4 build 25607596.
- The same verified, writable ext4 game copy and existing private prefix/account
  from the preceding test. The host game and prefix were preserved. Managed
  runtime files and accepted installation journals were not replaced.
- The existing KDE wallet provider was activated after boot with the previously
  tested launcher correction. Normal NTSYNC was retained.

## Isolating the graphics wait

The account-free D3D12 probe uses a separate prefix with the same composed Proton
and Steam Linux Runtime. It creates a feature-level 12_0 device, clears a render
target, copies a pixel back to the CPU, waits for a GPU fence, presents a blue
window, and records each cleanup operation. The window is destroyed after the
swap chain to exclude premature window destruction as the explanation.

The [exact probe source](steamos-venus-probe-2026-10-16.cpp) is included for
reproduction. Copy it into a scratch build directory and build it with the MinGW
compiler in the pinned SDK used in the
[preview build record](composed-preview-build-2026-10-15.md):

```sh
x86_64-w64-mingw32-g++ -O2 -static steamos-venus-probe-2026-10-16.cpp \
  -o graphics-probe.exe -ld3d12 -ldxgi -ldxguid -lole32 -luuid
```

Run it with the composed Proton inside SteamLinuxRuntime_4 and a dedicated
scratch prefix, supplying one Windows-visible output-file path as its argument.
The test runner imposed a 50-second outer timeout and cleaned up only the scratch
process group/prefix after a timeout. No game account or licensed game files are
needed for this probe. Its one-frame test does not measure performance.

| Extension override | Rendering and readback | Cleanup and process exit |
| --- | --- | --- |
| None | Passed; blue window visible | Stopped inside swap-chain release; 50-second runner timeout |
| Disable only `VK_KHR_present_id2,VK_KHR_present_wait2` | Passed | Same cleanup timeout |
| Disable `VK_KHR_present_id,VK_KHR_present_wait,VK_KHR_present_id2,VK_KHR_present_wait2` | Passed | Every cleanup marker completed; exit 0 without timeout |

All three returned RGBA `64,127,191,255` against expected `64,128,191,255`, within
the probe's one-unit tolerance. Native stack sampling of the baseline found a
waiter in `libvulkan_virtio.so` through `vkWaitForPresent2KHR`. The guest advertises
both v1 and v2 present-id/wait extensions and features. The earlier October 15
comparison disabled only v1; it did not disable the v2 path.

The [tested vkd3d-proton source](https://github.com/HansKristian-Work/vkd3d-proton/blob/3dfc6f07d0953b1e8b41705275c2c59cc7374fc5/libs/vkd3d/swapchain.c)
selects present-wait v2 when its surface features
are supported, otherwise v1, and joins the waiter during swap-chain cleanup.
This explains why an override covering only one generation is insufficient in
this guest. It localizes the probe failure; it does not establish which project
owns the underlying defect or explain every Forza wait.

## Scoped VM workaround

For this Venus test environment, prepend this single environment assignment to
the **existing** Forza Steam launch options, preserving the launcher and all
other required options:

```text
VKD3D_DISABLE_EXTENSIONS=VK_KHR_present_id,VK_KHR_present_wait,VK_KHR_present_id2,VK_KHR_present_wait2
```

Removing that assignment restores the preceding launch configuration. No DLL,
Proton, service, or installation-journal edits are required. This is an
experimental VM workaround, not a default for Steam Deck, AMD, or other graphics
stacks. Upstream documents these debug controls as potentially unstable; see the
[vkd3d-proton environment-variable reference](https://github.com/HansKristian-Work/vkd3d-proton/blob/3dfc6f07d0953b1e8b41705275c2c59cc7374fc5/README.md).

Quiet private diagnostics were retained (`PROTON_LOG=1`, `WINEDEBUG=-all`,
`VKD3D_DEBUG=info`). The actual game process environment confirmed the complete
four-extension override was present; a generic log mention of present wait alone
is not proof that the feature is enabled.

## Real game observations

The first launch with the four-extension override optimized shaders, rendered
the title screen and garage, opened the Welcome Center, and loaded the existing
player profile in the home menu. Keyboard and mouse input opened settings.

The built-in benchmark was started at 05:15:12 UTC on a 1280×800 guest display,
with the existing High dynamic-render setting and Auto ray-tracing options. It remained at the
Hakone Grand Prix Circuit loading screen at 05:21:27 UTC, 6 minutes 15 seconds
after the recorded start; no driving or benchmark score was obtained. A busy
thread sampled in `NtYieldExecution` used approximately one CPU core. The graphics
log contained 26
`GetShaderIdentifier: Could not find entry point` errors and no
`VK_ERROR_DEVICE_LOST`. These messages do not establish the cause of the stall.
Closing the window removed it, but the remaining game process required SIGTERM.

A second launch with the same override repeated the title, Welcome Center, home
menu, and player-profile result without another interactive login. Saving Low
dynamic render quality and ray-tracing quality Off prompted a game-requested
restart. The old game process exited without force and a new process retained
the presentation override. Its settings page confirmed Low/Off after restart,
with estimated VRAM use reduced from 7,444 to 3,871 MB; those estimates are not
performance measurements.

The lower-settings benchmark started at 05:30:05 UTC and was still at the Hakone
loading screen at 05:36:54 UTC, without driving or a score. That new game process
logged no shader-identifier errors and no device loss. The old process's errors
remained in the shared log, so counts were separated by Wine process ID. A later
Wine debugger process-list attempt timed out without obtaining a Windows stack;
it did not establish a cause. The game again required termination after window
close. Lowering these graphics settings alone did not resolve the observed stall.

A final comparison added only `nodxr` to the existing `VKD3D_CONFIG`, retaining
Low/Off and the four-extension presentation override. The process environment
and configuration log confirmed that setting. The title screen rendered again.
The benchmark started at 05:41:26 UTC, moved from the Hakone loading screen to a
black screen, and had produced neither driving nor a score by 05:47:19 UTC. There
were no graphics errors or device-loss messages. It again required SIGTERM after
window close. `nodxr` was removed because this comparison did not establish a
benefit.

The VM was left with the presentation workaround and Low/Off settings, with
Steam available, no game process, and no owned Xodus service left running. The
lower settings are a test configuration, not an established loading fix.

## Publication checks

The published probe source is byte-identical to the source compiled for the
comparison above (SHA-256
`3a93b1da374b2072003df6117476b88c2c6a6c8d400f9b7adac5788b36d2e737`).
A fresh build in the pinned, network-disabled SDK container passed with
`-Wall -Wextra -Werror` and no compiler output. Only source is published.
Repository privacy/policy checks passed (20 tests), as did REUSE and whitespace
checks. The installed guest Doctor returned exit 0 with eight passes and the
existing warning for less than 100 GiB free. These checks do not replace a
completed game session.

## Remaining limits

The presentation control establishes a working probe lifecycle and a real game
menu/profile in this specific VM. It does not establish completed driving,
sustained rendering, benchmark performance, new-save persistence, token refresh,
or explicit Invite/Join. Physical Deck Gaming Mode, AMD graphics, controls,
microSD behavior, suspend/resume, and performance still require hardware tests.
