I originally just wanted to drive the Nürburgring. Thanks to the work shared
in this thread and Xodus, I eventually got Forza running on Linux—including
the controller and playing with a friend on Windows.

I've collected the setup tools and fixes in
[forza-motorsport-linux](https://github.com/volcmen/forza-motorsport-linux).
This is experimental and not ordinary upstream Proton support.

The tested profile is `legacy-v0.1`, using `GE-Proton11-3-FM`, Xodus revision
[`7b236772297b3475ea4f3cb830feb5b224f3064a`](https://github.com/volcmen/xodus/commit/7b236772297b3475ea4f3cb830feb5b224f3064a),
and the matching legacy XGameRuntime revision
[`a1548b1cf57371715d10b608bc81a77a188e40d4`](https://github.com/volcmen/wine-forza-motorsport/commit/a1548b1cf57371715d10b608bc81a77a188e40d4).
The launcher starts Xodus when the game needs it and stops that session when
the game exits; there's no permanent background service to enable.

Here is what I checked on my Arch Linux setup with v0.1:

- two successful launches without a reboot (including a second launch without a system reboot);
- online profile and content download;
- controller navigation and driving;
- controller disconnect and reconnect;
- the AP702 warning was absent;
- Microsoft/Xbox Invite from Linux to Windows;
- Microsoft/Xbox Join from Linux to a compatible Windows activity;
- keyboard and controller input in the social picker;
- clean Xodus shutdown after each game exit.

The controller was visible in XInput but ignored by the game. The tested
workaround handles its Windows.Gaming.Input identity; the AP702 workaround
handles `StorageDeviceTrimProperty`. Both are limited to the documented build,
not general Wine or Proton fixes. The separate source proposals are here:
[controller](https://github.com/volcmen/wine-forza-motorsport/commit/ddd302d97c6008d79ea4f3e3ad56014cb548e514)
and [storage](https://github.com/volcmen/wine-forza-motorsport/commit/a7719bd8d0719e5ea6061387db020fa6a6b39d27).

Automatic incoming Xbox invite notifications are not supported. Invite and
Join are explicit actions in the opened picker. KDE Wallet is used through the
standard Secret Service interface; GNOME Keyring is not required. The Steam
options come from one generator and do not add `PROTON_DISABLE_HIDRAW`,
`PROTON_ENABLE_WAYLAND`, or `-SkipTargetHardwareProfiler`.

**Update: there's now a [v0.2.0 installer prerelease](https://github.com/volcmen/forza-motorsport-linux/releases/tag/v0.2.0).**
It downloads the open-source components and walks through setup. Repeat builds
and public-download checks passed, but the rebuilt binaries still need testing
in the game. The v0.1 results above don't establish that v0.2.0 works, so I'd
keep an already-working installation for now.

You still need your own licensed Microsoft threading DLL; it isn't included
or downloaded. The [test record](https://github.com/volcmen/forza-motorsport-linux/blob/d46640d20c304c9329a99ba823052289ed1f06b6/docs/evidence/v0.1-live-validation.md)
has the exact versions and results if you want to compare your setup.
