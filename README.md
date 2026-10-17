# Forza Motorsport on Linux

[![Verify](https://github.com/volcmen/forza-motorsport-linux/actions/workflows/verify.yml/badge.svg)](https://github.com/volcmen/forza-motorsport-linux/actions/workflows/verify.yml)
[![Release](https://img.shields.io/github/v/release/volcmen/forza-motorsport-linux)](https://github.com/volcmen/forza-motorsport-linux/releases)
[![License: GPL-3.0-or-later](https://img.shields.io/badge/License-GPL--3.0--or--later-blue.svg)](LICENSES/GPL-3.0-or-later.txt)

I bought Forza Motorsport to drive the Nürburgring and revisit memories of
playing Forza Motorsport 4 with my cousin on Xbox 360. Getting there on Linux
led to this collection of setup tools, controller and storage fixes, and Xbox
service integration.

**[Open the wiki →](https://github.com/volcmen/forza-motorsport-linux/wiki)**

Start with **[Install](https://github.com/volcmen/forza-motorsport-linux/wiki/Install)**, then **[Play and verify](https://github.com/volcmen/forza-motorsport-linux/wiki/Play-and-verify)**.
The wiki tracks the experimental preview, SteamOS qualification, checkpoints, and
recovery. The code and exact-version manifests stay in this repository.

## Before you start

- **Steam edition only:** Forza Motorsport, AppID 2440510.
- **Preview target:** Arch Linux or SteamOS x86_64 with native Steam; one unambiguous primary or additional game library.
- **Experimental v0.3 preview:** a complete custom Proton tool, separate user bundle, SteamOS Flatpak login/overlay, and recoverable `./setup composed` workflow. Follow [Install](https://github.com/volcmen/forza-motorsport-linux/wiki/Install). The old v0.2.0 fresh-download path is retired; its status and recovery commands remain available.
- **SteamOS / Steam Deck:** installer and runtime qualification use an installed QEMU SteamOS guest. [Real sign-in, repeat menu/profile loading, and a short Free Play drive with normal exit succeeded with a scoped Venus workaround](evidence/steamos-venus-presentation-test-2026-10-16.md); Hakone benchmark loading remains unresolved. [Preview 2 retesting](evidence/preview-2-validation-2026-10-17.md) found a race stall before completing a lap. Measured performance and physical Deck gameplay remain unverified. Run `./setup platform-check` for read-only diagnostics and read the [evidence and limits](https://github.com/volcmen/forza-motorsport-linux/wiki/Steam-Deck).
- **Bring your own licensed Microsoft threading DLL.** Microsoft binaries are not distributed.
- **Tested in-game:** the maintainer's existing custom FM setup worked on one desktop. That historical result does not establish fresh v0.2 bootstrap or Steam Deck success. The detailed dated matrix covers v0.1; see [Evidence](https://github.com/volcmen/forza-motorsport-linux/wiki/Evidence-and-history).
- **Incoming invite notifications are not supported.** Explicit Xbox Invite/Join is a separate feature.

This is a community compatibility setup, not official Proton support or a
guarantee for every system. Follow the wiki's requirements before running setup.

## Find what you need

| Task | Guide |
| --- | --- |
| Install and configure Steam | [Install](https://github.com/volcmen/forza-motorsport-linux/wiki/Install) |
| Check SteamOS prerequisites and test limits | [Steam Deck and SteamOS](https://github.com/volcmen/forza-motorsport-linux/wiki/Steam-Deck) |
| Drive, invite, and check a session | [Play and verify](https://github.com/volcmen/forza-motorsport-linux/wiki/Play-and-verify) |
| Diagnose a problem | [Troubleshooting](https://github.com/volcmen/forza-motorsport-linux/wiki/Troubleshooting) |
| Resume, roll back, or uninstall | [Recovery](https://github.com/volcmen/forza-motorsport-linux/wiki/Recovery) |
| Work with local builds and transactions | [Advanced setup](https://github.com/volcmen/forza-motorsport-linux/wiki/Advanced-setup) |
| Understand the components | [How it works](https://github.com/volcmen/forza-motorsport-linux/wiki/How-it-works) |
| Review test claims, sources, and history | [Evidence and history](https://github.com/volcmen/forza-motorsport-linux/wiki/Evidence-and-history) |

[Downloads](https://github.com/volcmen/forza-motorsport-linux/releases) ·
[Report an issue](https://github.com/volcmen/forza-motorsport-linux/issues) ·
[Read the story](https://blog.volc.men/blog/forza-motorsport-linux/)

[Contributing](.github/CONTRIBUTING.md) ·
[Security policy](.github/SECURITY.md) ·
[Repository security review](evidence/security-audit-2026-10-15.md)
· [Community feedback and fixes](evidence/community-feedback-2026-10-17.md)
