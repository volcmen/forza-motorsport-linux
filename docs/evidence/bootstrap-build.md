# Hybrid bootstrap candidate verification

Status: **release candidate, not published**. These checks do not replace live
Forza acceptance. No installed game, Steam configuration, Microsoft/Xodus credentials, or
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

- Python suite: **883 passed, 1 skipped**, with no deselected tests. The
  production-manifest check now passes using real release artifact hashes.
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

## Registry-bound release candidate

The final builder recipe was rebuilt without cache and its declared package
and script manifest matched the first image. That exact image was uploaded to
GHCR and verified against its registry manifest. The publisher now checks both
classic Docker configuration IDs and containerd-backed OCI manifest IDs;
unrelated and malformed identities remain rejected by regression tests.

Two additional `--clean` builds used the GHCR digest reference, each with fresh
source and dependency directories. They produced identical archives:

```text
size: 13107039 bytes
sha256: 68d7fc9e70a665c575f34432a5136e22bc07c840a028a52ec83b06f889c2b9b1
builder: ghcr.io/volcmen/forza-motorsport-builder@sha256:3a4274c5913404e366163e23437101f64e59ccbe1b00c74d9d57378f4ac82ae5
```

The finalizer generated `manifests/bootstrap-v1.toml` from these verified
archives. The public bundle-verifier command accepted the result. The internal
artifact manifest remains identical to the earlier local candidate: all five
binary hashes are unchanged; the archive now records the registry-qualified
builder identity in its provenance.

## Release gate

The repeated-build comparison and trust-manifest finalization passed. The
release still requires public package visibility, verification through the
public download path, and live acceptance of the rebuilt artifacts. Passing
synthetic tests or compiling a DLL is not evidence that these gates passed.
