# Forza Motorsport on Linux

Get from a Steam installation to your first drive, with a way back if setup goes wrong.
This community project brings together a dedicated Proton tool, Xodus for Xbox
services, and the fixes used to get Forza Motorsport running on Linux.

**New here? [Install Forza's Linux setup →](Install.md)**

## Is this for my system?

The bootstrap targets **Arch Linux x86_64, native Steam, and the Steam edition
of Forza Motorsport (AppID 2440510) in the primary Steam library**.
Flatpak Steam and additional game libraries are not supported by the installer.
You'll need your own licensed Microsoft threading DLL; it is not included.

The **v0.2.0 prerelease has been tested in-game by the maintainer on one system**.
It remains experimental. The detailed, dated gameplay matrix belongs to v0.1;
see [what was tested](Evidence.md#what-was-tested) before drawing wider conclusions.
**Incoming invite notifications are not supported.**

## Find your next step

| I want to… | Go to |
| --- | --- |
| Set up the game for the first time | [Install](Install.md) — requirements, commands, and checkpoints |
| Start driving or invite a friend | [Play and verify](Play.md) — Steam settings and a first-session checklist |
| Understand an error | [Troubleshooting](Troubleshooting.md) — symptoms and the next useful check |
| Resume, undo, or remove a setup | [Recovery](Recovery.md) — choose the command for your installation |
| Supply local builds or use lower-level tools | [Advanced setup](Advanced-setup.md) — the manual workflow |
| Understand the components | [How it works](How-it-works.md) — Proton, Xodus, and service ownership |
| Check claims, sources, or old records | [Evidence and history](Evidence.md) — versions, tests, and the archive |

## What setup handles

Bootstrap prepares a separate `GE-Proton11-3-FM`, checks the open-source bundle,
and installs the reviewed controller and AP702 fixes with recovery records.
You handle Steam's settings, the licensed DLL, Microsoft sign-in, and launching
the game. Run setup as your regular user, without `sudo`.

Already have a working installation? Read [Recovery](Recovery.md) before replacing
anything. A clean diagnostic report means **ready to attempt a launch**;
[testing a game session](Play.md#verify-your-first-session) is the next step.

[Project and downloads](https://github.com/volcmen/forza-motorsport-linux) ·
[The story: I Just Wanted to Drive the Nürburgring](https://blog.volc.men/blog/forza-motorsport-linux/)
