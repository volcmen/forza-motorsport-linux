# v0.2.0 — an easier setup, ready for testing

This release is about making setup less of a project in itself. Instead of
building and arranging every open-source component by hand, you can download
the bundle and let `./setup bootstrap` walk you through the installation.
If you stop partway through, you can resume it.

**This is still an experimental prerelease.** The older v0.1 setup worked in
the game on one machine. These rebuilt binaries haven't completed those game
tests yet. Build, download, and recovery checks have passed, but that's not the
same as testing a race or joining a friend. Keep your working setup for now.

## Try it

You'll need Arch Linux x86_64, native Steam, your Steam copy of Forza Motorsport,
and a Microsoft threading DLL from a source you're licensed to use. That DLL
is not included. Downloads don't require a GitHub login.

Start in a separate checkout (do not replace a working installation blindly):

```bash
git clone --branch v0.2.0 --depth 1 https://github.com/volcmen/forza-motorsport-linux.git forza-motorsport-linux-v0.2.0
cd forza-motorsport-linux-v0.2.0
./setup bootstrap --check
./setup bootstrap
```

The immutable tag retains its prepublication candidate documentation. These
release notes record the later publication and public-download verification.

## What's easier now

- Setup downloads the selected GE-Proton version through ProtonUp and prepares
  a separate `GE-Proton11-3-FM` tool, leaving your other Proton tools alone.
- The Xodus/XGameRuntime bundle is checked against its expected hashes before use.
- Controller and AP702 fixes are applied only to the exact supported builds.
- Setup keeps backups and recovery records so you can resume or undo the work.
- Prefer to build from source? The optional Docker build compiles offline.

## What remains manual

Steam must create the game's prefix, and you must supply your own licensed
Microsoft threading runtime. That proprietary DLL is not distributed here.
Bootstrap does not edit Steam launch options, launch the game, install system
packages, or log into Microsoft. Use the generated Steam launch line.

KDE Wallet can provide Secret Service; GNOME Keyring is not required. Xodus
starts when you play and stops with that game session. You can open the social
picker to Invite or Join; automatic incoming invite notifications aren't supported.

## Evidence and limits

Two clean builds produced the same bundle. We also downloaded the public
release without signing in and verified it against the shipped manifest.
Bundle SHA-256:
`68d7fc9e70a665c575f34432a5136e22bc07c840a028a52ec83b06f889c2b9b1`.

The [build verification record](https://github.com/volcmen/forza-motorsport-linux/blob/feature/hybrid-bootstrap/docs/evidence/bootstrap-build.md) documents the
real offline compilation, repeated archive comparison, binary loader/CLI smoke
test, and synthetic clean-start/recovery checks. Immutable release identities
are recorded in `manifests/bootstrap-v1.toml`; do not use the earlier local
candidate hash as a release checksum.

Read the [bootstrap guide](https://github.com/volcmen/forza-motorsport-linux/blob/feature/hybrid-bootstrap/docs/bootstrap.md) before installation. Keep recovery
journals and backups. `./setup bootstrap --rollback` reverses the composed
bootstrap; `./setup rollback` retains its older runtime-only meaning.

Before treating the rebuilt stack as accepted, verify two Steam launches,
online content/profile, controller driving and hotplug, AP702 absence,
Invite/Join with keyboard and controller navigation, and clean service exit.
Automated checks cannot prove those game or account behaviors.
