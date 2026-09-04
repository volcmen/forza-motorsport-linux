# v0.2.0 — hybrid bootstrap prerelease

This prerelease adds a resumable bootstrap for the Steam edition of Forza
Motorsport on Arch Linux x86_64 with native Steam. It remains experimental.
The rebuilt artifacts require live acceptance; the earlier v0.1 gameplay
results are not a substitute for testing these new binaries.

Anonymous registry access and an unauthenticated download of the published
bundle have passed verification. Bundle SHA-256:
`68d7fc9e70a665c575f34432a5136e22bc07c840a028a52ec83b06f889c2b9b1`.

Start in a separate checkout (do not replace a working installation blindly):

```bash
git clone --branch v0.2.0 --depth 1 https://github.com/volcmen/forza-motorsport-linux.git forza-motorsport-linux-v0.2.0
cd forza-motorsport-linux-v0.2.0
./setup bootstrap --check
./setup bootstrap
```

The immutable tag retains its prepublication candidate documentation. These
release notes record the later publication and public-download verification.

## What is included

- Pinned GE-Proton acquisition through the repository-local ProtonUp integration.
- A dedicated `GE-Proton11-3-FM` layout, leaving other compatibility tools alone.
- A hash-verified open-source Xodus/XGameRuntime component bundle.
- Optional source builds in a pinned rootless Docker environment, with network
  disabled during compilation.
- Coordinated exact-build controller and AP702 patches, private snapshots,
  interrupted-operation recovery, and composed rollback.

## What remains manual

Steam must create the game's prefix, and you must supply your own licensed
Microsoft threading runtime. That proprietary DLL is not distributed here.
Bootstrap does not edit Steam launch options, launch the game, install system
packages, or log into Microsoft. Use the generated Steam launch line.

KDE Wallet can provide Secret Service; GNOME Keyring is not required. Xodus
uses the per-game-session launcher lifecycle, not permanent startup enablement.
Explicit Invite/Join is distinct from automatic incoming invite notifications,
which are not supported.

## Evidence and limits

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
