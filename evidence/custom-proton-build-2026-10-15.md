# Custom Proton build and async regression — 2026-10-15

**Experimental build completed; not an approved installer artifact or gameplay result.**
The build is retained locally under the distinct name `GE-Proton11-3-FM-test`.
No installed compatibility tool or game prefix was replaced. No binary release or
new bootstrap trust hash was published.

## Inputs and build

| Input | Identity |
| --- | --- |
| GE-Proton source | [`GE-Proton11-3`, commit `8c8003f7f5473d883fbe1bc7ac070c79955754e8`](https://github.com/GloriousEggroll/proton-ge-custom/commit/8c8003f7f5473d883fbe1bc7ac070c79955754e8) |
| Custom Wine | [`a1548b1cf57371715d10b608bc81a77a188e40d4`](https://github.com/volcmen/wine-forza-motorsport/commit/a1548b1cf57371715d10b608bc81a77a188e40d4), replacing GE's Wine submodule |
| Wine fix | Remove the post-`Detach()` trace in `XAsyncSchedule`; implementation commit [`0246e64a28c`](https://github.com/volcmen/wine-forza-motorsport/commit/0246e64a28c), subsequently merged with regression coverage in [Wine fork PR #1](https://github.com/volcmen/wine-forza-motorsport/pull/1) |
| SDK | `registry.gitlab.steamos.cloud/proton/steamrt4/sdk/x86_64:4.0.20260331.220802-0` |
| SDK digest | `sha256:97526b794ce1a9bed5f891084462260b3a02399569f7438a3a57b5a253001db9` |
| Submodules | [Recursive revision snapshot](custom-proton-submodules-2026-10-15.txt) |

Initialized submodules, selected the custom Wine revision, applied the checked-in
GE `patches/protonprep-valve-staging.sh` sequence, then applied the async fix.
Used a separate source tree, build directory, and Cargo cache. The configured
SDK image was pinned by digest. The full build used rootless Docker with all
capabilities dropped, `no-new-privileges`, ten CPUs, an 18 GiB memory limit,
and a 4096-process limit. Existing application containers were left running.

Configured with `--build-name=GE-Proton11-3-FM-test --container-engine=docker`
and the SDK digest above; completed `make J=10 ENABLE_CCACHE=0 redist` with
`TAR_OPTIONS=--no-same-owner`. The target was `redist`, never `install`.
Dependency acquisition used the network; this full Proton build is not claimed
to be an offline build. Patch application and the later regression runs used
network-disabled containers.

The Debian download URL for `unzip_6.0-29.debian.tar.xz` returned 404. Retrieved
that exact version from Debian's snapshot service and checked the SHA-256 in
the official source descriptor before populating the isolated build cache:

- Debian patch archive: `14043e5ea351c02b3bc8676e1e6d20d79b9a690b6d7520e8138ac629cc048417` (25,876 bytes).
- Original source archive: `036d96991646d0449ed0aa952e4fbe21b476ce994abc276e49d30e686708bd37` (1,376,845 bytes).

The descriptor was retrieved over HTTPS; no claim of verifying its PGP signature
is made. Disabling tar owner restoration avoided a rootless extraction failure
without granting the build extra capabilities.

## Output identities

| Output | SHA-256 |
| --- | --- |
| `GE-Proton11-3-FM-test.tar.gz`, 535,081,181 bytes | `f623f1b033ec3f400fcaa19e3823f907c03187d69d949e74582db8dfa6ddbda4` |
| `files/lib/wine/x86_64-windows/xgameruntime.dll` | `8dc47671c8bb7b3b1eb746c13d8322637f25a3034e0fdae116864a9e285a1ef3` |
| `files/lib/wine/x86_64-unix/xgameruntime.so` | `d50e5057876e12ecdcf3e5a20604d7577bc94a798f1ca7fc614aa1e2883c4050` |

Steam registration uses the distinct internal/display name `GE-Proton11-3-FM-test`.
The tool manifest requires Steam Runtime AppID `4183110`. The version file is
`1792042791 GE-Proton11-3`.

This is a traceable experiment, **not a reproducibility result**. Build retries
used GE's default time-dependent `SOURCE_DATE_EPOCH`; no second clean build with
a fixed epoch and artifact comparison has been completed. These hashes identify
the experiment only and must not be substituted into production trust manifests.

## Actual regression results

The new Wine test calls `XAsyncRun`, manually dispatches work and completion,
and checks pending/final status and callback counts. It uses a separate prefix
without Microsoft binaries, accounts, a game, or a live Xodus service.

| Exact test setup | Result |
| --- | --- |
| Released v0.2 runtime pair, tracing disabled | 26 checks passed; one opt-in invite-relay test skipped |
| Same released runtime pair, `WINEDEBUG=-all,+gdkc,+seh` | Access violation `0xc0000005`, read address `0x78`, exit 5 |
| Patched SDK-built runtime pair, tracing enabled | 26 checks passed; same invite-relay skip |
| Entire new custom Proton tool, fresh test prefix, tracing enabled | 26 checks passed; same invite-relay skip |
| Entire new custom tool in the installed SteamOS 3.8.14 QEMU guest, fresh prefix, tracing enabled | 26 checks passed; same invite-relay skip |

The released DLL's fault at RVA `0x1270e` resolves to `XAsyncSchedule`,
`XAsync.cpp:810`: the TRACE accesses `state->queue` after `state.Detach()`
clears the pointer. Removing that trace fixes the observed regression without
changing queue ownership. This is distinct from the reported case-colliding DLL
splash-screen problem and does not establish removal of the licensed threading
runtime requirement for gameplay.

The component comparison used read-only container mounts to place each exact
DLL/Unix pair at Wine's built-in search location. An absolute `LoadLibrary` path
alone can still resolve to an existing built-in module, so it was not accepted
as evidence of testing the replacement bytes. The complete-tool run used the
new tool itself. No mount wrote to the existing working Proton installation.

The SteamOS run used the guest's unprivileged `deck` user, the read-only source/tool
share, and a new prefix under the guest home. It executed the raw Wine binary and
API test without system package changes, a container inside the guest, a Steam
client, account sign-in, or a proprietary threading DLL. This establishes that
the candidate can initialize that test prefix and pass this API regression on
the tested OS; it does not exercise the Steam Runtime handoff or game launch.

## Remaining acceptance work

Steam-driven prefix creation, transactional Prepare/Finish/rollback composition,
native Xodus GUI packaging, and physical Steam Deck gameplay are still pending.
The [SteamOS investigation](steamos-2026-10-15.md) records the current OS-level
blockers. Successful compilation and an async API regression do not make this
archive a supported replacement for the shipped manifest.
