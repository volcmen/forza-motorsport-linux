# Composed preview build — 2026-10-15

This record identifies the experimental v0.3.0-preview.1 artifacts. It supersedes
artifact-readiness claims in the earlier test-build record, without changing its
historical measurements. At artifact-build time, account sign-in, an actual Steam
game handoff, and physical Steam Deck gameplay were unverified. The later
[real-account VM test](steamos-real-account-test-2026-10-15.md) records sign-in,
game startup attempts, and their remaining limits.

## Inputs

- GE-Proton11-3: `8c8003f7f5473d883fbe1bc7ac070c79955754e8`.
- Owned Wine fork: `3ecd0e8a9b8e7e0a754b09595d1aa70c9b1e1301`, including the
  async post-Detach trace correction and its regression.
- Owned Xodus fork: `850ec9477e00117960ca09ad38c6154acc56c16d`, merged in
  [Xodus PR #1](https://github.com/volcmen/xodus/pull/1).
- Proton SDK: `registry.gitlab.steamos.cloud/proton/steamrt4/sdk/x86_64@sha256:97526b794ce1a9bed5f891084462260b3a02399569f7438a3a57b5a253001db9`.
- Xodus builder: `ghcr.io/volcmen/forza-motorsport-builder@sha256:3a4274c5913404e366163e23437101f64e59ccbe1b00c74d9d57378f4ac82ae5`, Rust 1.98.0.
- [Submodule revisions](composed-proton-submodules-2026-10-15.txt),
  [source patches](../patches), [complete-tool manifest](../manifests/composed-v2.json),
  and [user-bundle manifest](../manifests/composed-bundle-v1.toml).

The no-bytecode patch prevents Python imports from changing the tool payload.
The Wine source patch admits physical XInput device paths while retaining the
XInput check, preserves the no-seek-penalty behavior, and supplies the Forza
trim descriptor with correct output-size handling. Reporting trim support is a
compatibility override, not a probe of the physical storage device. Controller
behavior requires hardware testing.

## Actual build sequence

Used an isolated GE checkout, initialized its recorded submodules, replaced its
Wine tree with the pinned owned fork, and applied GE's
`patches/protonprep-valve-staging.sh`. Applied `proton-no-bytecode.patch` before
the complete build. Source and build directories were private to this task.
The effective commands were:

```sh
mkdir build
cd build
../source/configure.sh --build-name=GE-Proton11-3-FM --container-engine=docker \
  --proton-sdk-image=registry.gitlab.steamos.cloud/proton/steamrt4/sdk/x86_64@sha256:97526b794ce1a9bed5f891084462260b3a02399569f7438a3a57b5a253001db9
make J=10 ENABLE_CCACHE=0 SOURCE_DATE_EPOCH=1792022400 \
  BASE_SOURCE_DATE_EPOCH=1792022400 CARGO_HOME="$PWD/../cargo" \
  DOCKER_OPTS='--cap-drop=ALL --security-opt=no-new-privileges --cpus=10 --memory=18g --memory-swap=18g --pids-limit=4096 -e TAR_OPTIONS=--no-same-owner' redist
```

The full `redist` build completed. Dependency acquisition had network access.
The exact Debian unzip source inputs described in the
[earlier record](custom-proton-build-2026-10-15.md) were used after the ordinary
Debian URL returned 404. They are included in the source supplements.

The controller/storage source patch was then applied to the prepared Wine source
and the build's `src-wine` tree. In the same SDK, with the source/build directories
mounted at their original paths, network disabled, and no extra capabilities,
these targeted builds completed:

```sh
make -C build/obj-wine-x86_64 -j8 \
  dlls/windows.gaming.input/x86_64-windows/windows.gaming.input.dll \
  dlls/mountmgr.sys/x86_64-windows/mountmgr.sys
make -C build/obj-wine-i386 -j8 \
  dlls/windows.gaming.input/i386-windows/windows.gaming.input.dll \
  dlls/mountmgr.sys/i386-windows/mountmgr.sys
```

Copied the complete `build/GE-Proton11-3-FM` distribution to a separate staging
tree, then copied and stripped those four built modules into their corresponding
`files/lib/wine/{x86_64,i386}-windows` directories, mode 0555. The source-built
xgameruntime pair was already part of the full distribution. No installed host
tool or prefix was used as the output. An attempted second full `redist` hit an
XZ autotools dependency error; the final output is the successful full build plus
these successful targeted module builds. A second full rebuild is not claimed.

The marker is schema 2 with composition profile `fm-composed-v1`. Its payload
fingerprint is the canonical tree digest from `forza_bootstrap.proton`, excluding
only `.forza-bootstrap.json`; it includes paths, file modes and bytes, directories,
and symlink targets. The marker separately records the four managed module hashes
and the compatibility VDF hash. Both marker and payload hashes are pinned in the
release manifest. Python cache files are rejected rather than excluded.

The complete tool archive uses GNU tar sorted names, numeric owner/group 0,
mtime `1792022400`, archive root `GE-Proton11-3-FM`, and `gzip -n -6`.
The packaged `version` file is `1792047348 GE-Proton11-3`: upstream generation of
that field remained time-dependent despite the build epoch. This is a traceable
build and tested artifact, **not a claim of independently reproduced bytes**.

## Xodus and Flatpak

Exported the pinned Xodus commit with `git archive`, resolved and vendored its
locked dependencies, and compiled all three shipped binaries in the pinned
builder. Source and dependency mounts were read-only; output was separate;
compilation had no network access. Effective build options:

```sh
CARGO_HOME=/output/cargo CARGO_TARGET_DIR=/output/target CARGO_BUILD_JOBS=8 \
SOURCE_DATE_EPOCH=1792022400 \
cargo build --release --locked --offline -p xodus-service -p xodus-cli -p xodus-overlay
```

The container used eight CPUs, 14 GiB, a 4096-process limit, dropped capabilities,
and `no-new-privileges`. Its Cargo source replacement points at the read-only
vendored dependency tree. Binary copies were stripped with `strip --strip-unneeded`.
All three reference at most GLIBC_2.39 (including weak imports); the guest provides
2.41. Actual guest runtime checks, not that symbol check alone, establish usability.

The separate user bundle contains those three binaries and the complete tool's
xgameruntime pair as verified input evidence. Version 3 user-only installation
never writes the Wine pair into the tool. Metadata records source commits,
builder, commands, licenses, and checksums. Its archive uses sorted names, fixed
mtime/owners and `zstd -T2 -19`.

`tools/build-steamos-xodus --bundle-root ABSOLUTE_BUNDLE --output NEW_DIRECTORY`
produces the Flatpak without installing a host app. It verifies the pinned inputs
before and after packaging, includes GPL licensing/source provenance, and uses a
fixed OSTree timestamp. The app and GNOME 50 runtime commits are pinned in
`composed-v2.json`; Finish rejects different deployments or permission overrides.

The Flatpak has network, Wayland/fallback X11, graphics and input-device access,
Secret Service/KDE activation permissions, its own application bus name, and the
single `xdg-run/xodus.sock` bind mount. Input permission is required by the current
SDL controller listener and exposes more than just a gamepad. A read-only socket
bind mount still permits full protocol requests. Restart the overlay after the
native service recreates its socket. These permissions are explicit, not a claim
that the application is harmless or isolated from credentials it is allowed to use.

## Security and source availability

The [Xodus dependency review](https://github.com/volcmen/xodus/blob/850ec9477e00117960ca09ad38c6154acc56c16d/SECURITY-DEPENDENCIES.md)
records the updated TLS/XML/PKCS#11/OpenSSL packages, the vendored GLib fix, and the
remaining RSA and proc-macro advisories. Private RSA signing now uses OpenSSL;
the RSA crate is not advertised as patched. The fresh Python development audit
found no known vulnerabilities in its 17 pinned packages. These are scoped audits,
not a full Proton/OS CVE certification.

The prepared-source archive contains the complete prepared GE source/submodule
snapshot and applied Wine/Proton changes, with Git metadata omitted. Source
supplements contain Wine Mono 11.2.0, Gecko 2.47.4, Xalia 0.4.9, Debian unzip inputs,
the exact Xodus source, and locked vendored Rust dependencies with their licenses.
For Xodus offline compilation, place the included offline config in
`xodus-security-source/.cargo/config.toml`; its sibling vendor directory must keep
the name `xodus-security-vendor`. SDK-provided packages remain identified by the
pinned builder images. No Microsoft threading DLL, game, account data, or synthetic
test DLL is a release asset.

## Artifact identities

| Asset | Bytes | SHA-256 |
| --- | ---: | --- |
| `GE-Proton11-3-FM-composed-preview.tar.gz` | 535,060,917 | `59792b23cfc40474764c379c4c91c255781afe8fc949f5aa614b012034784b76` |
| `forza-composed-user-bundle-v1.tar.zst` | 11,201,923 | `54a74fb48fccb3e6eb16cb83ff79962068f8a47cec95b9ccad8f26955a596f73` |
| `forza-xodus-preview.flatpak` | 4,809,632 | `a6c6257f4334ae65425371427f71141d46d4e13a2315e1337d0671fb67aaca61` |
| `GE-Proton11-3-FM-prepared-source.tar.gz` | 851,828,562 | `b15becbb70a5dadc0be3fce28d2a24481445e06ebfff22f91274a81a88d8a598` |
| `Forza-preview-source-supplements-v2.tar.gz` | 613,924,758 | `3676c4dff28869f2fd64d6057269359cda728597b0e095030c038c4599e0113e` |
