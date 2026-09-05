# Play and verify

[Home](Home.md) · [Install](Install.md) → **Play and verify** · [Troubleshooting](Troubleshooting.md) · [Recovery](Recovery.md)

## Launch through Steam

After installation, run this from your setup checkout:

```bash
./setup steam-options
```

Paste the printed line into **Forza Motorsport → Properties → General → Launch
Options**. Under **Compatibility**, select `GE-Proton11-3-FM`. Keep Steam Input
disabled for the tested physical-controller path.

Launch Forza from Steam. The launcher starts Xodus for this game session and
cleans up its owned service when the game exits. Do not start or enable
`xodus-forza.service` yourself.

Keep the generated launch line intact. Do not add `PROTON_DISABLE_HIDRAW`,
`PROTON_ENABLE_WAYLAND`, `-SkipTargetHardwareProfiler`, or unrelated flags.

## Invite or join a friend

Use the game's explicit Xbox social picker. These are **Microsoft/Xbox invites**,
not Steam invites. Enter a compatible multiplayer lobby first so Forza can make
the session available, then use Invite or Join in the picker.

Explicit Invite and Join with a Windows friend, including keyboard and controller
navigation, passed the [v0.1 live matrix](Evidence.md#what-was-tested).
**Incoming invite notifications are not supported.** Open the picker yourself;
a missing notification is not evidence that installation failed.

## Verify your first session

Use this checklist on your own system before accepting the installation:

- [ ] Launch past the logo through Steam.
- [ ] Confirm online profile and content load.
- [ ] Navigate and drive with the controller, then disconnect and reconnect it.
- [ ] Confirm the AP702 storage warning is absent.
- [ ] Explicitly Invite a Windows friend and Join a compatible published activity.
- [ ] Try both keyboard and controller input in the social picker.
- [ ] Exit the game and confirm the session's Xodus service shuts down.
- [ ] Launch and exit a second time without rebooting.

You can inspect the service locally after exit:

```bash
systemctl --user is-active xodus-forza.service
```

It should report `inactive` (with a nonzero exit status). That service check is
one part of the checklist, not proof of online play or clean account behavior.

If a step fails, record the symptom and go to [Troubleshooting](Troubleshooting.md).
A passing doctor or build test cannot fill in an untested checkbox. The lower-level
runtime acceptance command is documented in [Advanced setup](Advanced-setup.md#accept-or-restore).
