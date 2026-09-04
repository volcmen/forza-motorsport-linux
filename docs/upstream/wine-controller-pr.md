# windows.gaming.input: identify physical XInput controllers

I ran into a confusing Forza Motorsport problem: the controller responded in
XInput tests and Steam's overlay, but the game only offered keyboard bindings.
This draft explores the Windows.Gaming.Input identity side of that problem.

It targets `xodus-gaming/wine:bleeding-edge` from public branch
[`wgi-physical-nonroamable-id`](https://github.com/volcmen/wine-forza-motorsport/tree/wgi-physical-nonroamable-id).

Base: `b1dd32734a34472a28eb5be9922df06e07ac0834`

Tip: [`ddd302d97c6008d79ea4f3e3ad56014cb548e514`](https://github.com/volcmen/wine-forza-motorsport/commit/ddd302d97c6008d79ea4f3e3ad56014cb548e514)

## Problem

The existing physical XInput provider does not expose the stable
`NonRoamableId` shape expected by Windows.Gaming.Input consumers. Forza
Motorsport can enumerate the controller through XInput while presenting only
keyboard bindings in the game UI.

## Change

The branch derives a physical-provider identity from VID, PID, and the full
device-instance discriminator. Parsing is case-insensitive and accepts only a
boundary-anchored `&XI_nn#` segment with exactly two decimal slot digits. The
existing Steam virtual-controller reservation for VID:PID `28de:11ff` is
preserved. The helper is linked as a private archive into both the production
DLL and its tests and is not exported or installed.

## Verification

- `windows.gaming.input.dll`, its PE test executable, and the neighboring
  DirectInput test executable compile and link with Clang/LLD 22.1.8.
- A production-source harness covers valid, malformed, bounds,
  distinct-instance, case, and Steam-reservation inputs.
- The standard Windows.Gaming.Input and DirectInput PE suites remain
  **BLOCKED before dispatch**: this old build tree cannot initialize its
  Wine test prefix (`secur32.dll`, then `kernel32.dll` status `c0000135`).
  Those suites did not run, so I'm not counting them as passed.
- The controller navigation, driving, and hotplug matrix passed in Forza through
  the reviewed exact-build fallback. This standalone source branch itself was
  not installed for that observation.

I'd appreciate feedback on the identity format and whether this is the right
place to handle physical XInput devices. This remains a draft, not a confirmed
standalone game fix.

No compiled artifact is attached. The redacted
[integration evidence](https://github.com/volcmen/forza-motorsport-linux/blob/d46640d20c304c9329a99ba823052289ed1f06b6/docs/evidence/v0.1-live-validation.md)
records the separate exact-build fallback observation and its limitations.
