# Hybrid bootstrap candidate verification

Status: **release candidate, not published**. These checks do not replace live
Forza acceptance. No installed game, Steam configuration, credentials, or
licensed threading DLL was changed during this verification.

## Source and recipe

The corrected recipe is committed at `95ae981` (following `5d37a2e`). It uses
the exact Xodus and legacy XGameRuntime revisions already documented for
`legacy-v0.1`, not the rejected newer protocol pair.

Real offline compilation exposed issues the synthetic fixtures did not:

- Cargo build scripts need an executable temporary filesystem.
- Xodus needs GTK4, libadwaita, protobuf, SDL3, and WebKitGTK 4.1 in the builder.
- Revision checks need narrowly scoped Git safe-directory exceptions for the
  rootless read-only mounts.
- Wine compilation must copy source without owner-private Git metadata.
- Wine's Vulkan registry XML must be pinned, downloaded and hash-checked before
  entering the network-disabled build.
- Wine's generated `include/hstring.h` must exist before building XGameRuntime.

Both Xodus and the legacy XGameRuntime bridge subsequently compiled offline.
This proves compilation only, not their integration with a running game.

## Automated checks

- Python suite: **878 passed, 1 skipped, 1 deselected**. The deliberately
  deferred check requires the final production manifest; no placeholder hashes
  were supplied to make it pass.
- Launcher, doctor, setup, bootstrap CLI, Steam-options, and installation Bash
  suites passed, as did systemd unit validation.
- ShellCheck, shfmt, Ruff, REUSE license checks, and `git diff --check` passed.
- A new clone of `5d37a2e`, with a new dependency cache, passed the synthetic
  clean-start acquisition/install/no-op/rollback comparison test.

## Complete bundle and repeat build

The complete bundle subsequently built twice with the corrected image. The
second run used the public `tools/build-bootstrap-bundle --clean` command,
fresh Git checkouts, and a fresh dependency directory. Both compilation phases
ran without network access. `cmp` confirmed byte-identical archives:

```text
size: 13107352 bytes
sha256: 1e0f258c8d981f25973ca76696ecec71e383bc4b808efdffd7d9a03191b90d13
builder: sha256:3a4274c5913404e366163e23437101f64e59ccbe1b00c74d9d57378f4ac82ae5
```

These are **local candidate identities, not published release coordinates**.
The production trust manifest must bind the final public registry reference.

The release inspector validated all five artifact records, metadata, source
revisions, command/environment provenance, and checksums. A separate offline
container verified `SHA256SUMS`, resolved shared-library dependencies for all
three Xodus executables, and successfully ran `xodus-cli --help`, including
the Invite, Join, and social-picker commands. No Microsoft login or game was
started by this smoke test.

## Release gate

The local repeated-build comparison passed. The release still requires a public immutable GHCR
builder reference, a finalized bundle trust manifest, verification through the
public download path, and live acceptance of the rebuilt artifacts. Passing
synthetic tests or compiling a DLL is not evidence that these gates passed.
