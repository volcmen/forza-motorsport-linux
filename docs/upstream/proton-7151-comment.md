Building on the GE-Proton/Xodus setup already documented in this thread, I have
prepared a source-only, recoverable integration for Forza Motorsport AppID
2440510. This is experimental and not ordinary upstream Proton support.

The tested profile is `legacy-v0.1`, using `GE-Proton11-3-FM`, Xodus revision
`7b236772297b3475ea4f3cb830feb5b224f3064a`, and the matching legacy
XGameRuntime revision `a1548b1cf57371715d10b608bc81a77a188e40d4`.
The Xodus service is a static user unit: a per-game launcher starts it on
demand, waits for its private socket, and stops only the exact invocation it
owns when the game exits. It is not enabled as a persistent background service.

On the tested Arch Linux/KDE Plasma system, the exact installed hashes passed:

- two successful launches without a reboot;
- online profile and content download;
- controller navigation and driving;
- controller disconnect and reconnect;
- the AP702 warning was absent;
- Microsoft/Xbox Invite from Linux to Windows;
- Microsoft/Xbox Join from Linux to a compatible Windows activity;
- keyboard and controller input in the social picker;
- clean Xodus shutdown after each game exit; and
- a second launch without a system reboot.

The tested compatibility tool used reviewed exact-build fallbacks for the
Windows.Gaming.Input physical-controller identity and mountmgr
`StorageDeviceTrimProperty` behavior. Separate source branches document those
narrow changes, but neither branch is being represented as a general Wine or
Proton fix.

Automatic incoming Xbox invite notifications are not supported. Invite and
Join are explicit actions in the opened picker. KDE Wallet is used through the
standard Secret Service interface; GNOME Keyring is not required. The Steam
options come from one generator and do not add `PROTON_DISABLE_HIDRAW`,
`PROTON_ENABLE_WAYLAND`, or `-SkipTargetHardwareProfiler`.

The project distributes no binaries. A required Microsoft threading runtime is
not included, downloaded, read, or hashed; users must provide it only from a
source they are licensed to use. Public source links, immutable revisions, and
the redacted live matrix will be inserted after the repositories and branches
exist, before this comment is posted.
