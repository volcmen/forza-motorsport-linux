# Evidence and history

[Home](Home.md) · [Play and verify](Play.md) · [How it works](How-it-works.md)

Use this page to check what a claim means and find the record behind it.
The installation instructions live in [Install](Install.md); archived plans are
historical material, not steps to execute.

## What was tested

| Release or experiment | Evidence | What it establishes |
| --- | --- | --- |
| **v0.2.0 bootstrap** | [Maintainer confirmation](https://github.com/volcmen/forza-motorsport-linux/blob/bf12d7e7f22c8ebb910f68162af1caf0e00b0a5c/README.md) | Tested in-game on the maintainer's system. This is not a new exact-hash matrix for every v0.2 feature. |
| **v0.2.0 bundle** | [Build and download record](../evidence/bootstrap-build.md) | Reproducible compilation, public download verification, and the checks detailed in that record. Its pending-gameplay wording predates the later maintainer confirmation. |
| **v0.1 / legacy-v0.1** | [Live validation, 2026-09-02](../evidence/v0.1-live-validation.md) | Two launches without reboot, online profile/content, controller navigation/driving/hotplug, AP702 absence, explicit Xbox Invite/Join with Windows, keyboard/controller picker input, and clean shutdown on one system. |
| **Newer WineGDK/Xodus experiment** | [Historical verification](https://github.com/volcmen/forza-motorsport-linux/blob/bf12d7e7f22c8ebb910f68162af1caf0e00b0a5c/docs/verification.md#rejected-live-winegdkxodus-source-experiment) | Rejected live despite successful hermetic source tests. Not an installation alternative. |
| **Incoming invite notifications** | [Recorded limitation](../evidence/v0.1-live-validation.md) | Not supported. Explicit Invite/Join is a separate capability. |

No row guarantees compatibility on another machine or after an upstream update.
The v0.1 matrix must not be relabeled as a v0.2 result. Your installation still
needs its own [game-session check](Play.md#verify-your-first-session).

## Runtime versions

The installable profile is **`legacy-v0.1`**. The bootstrap release packages this
profile; it does not switch to the newer protocol experiment.

| Component | Reviewed source revision |
| --- | --- |
| Legacy Xodus | [`7b236772297b3475ea4f3cb830feb5b224f3064a`](https://github.com/volcmen/xodus/commit/7b236772297b3475ea4f3cb830feb5b224f3064a) |
| Legacy XGameRuntime | [`a1548b1cf57371715d10b608bc81a77a188e40d4`](https://github.com/volcmen/wine-forza-motorsport/commit/a1548b1cf57371715d10b608bc81a77a188e40d4) |
| Wine controller change | [`ddd302d97c6008d79ea4f3e3ad56014cb548e514`](https://github.com/volcmen/wine-forza-motorsport/commit/ddd302d97c6008d79ea4f3e3ad56014cb548e514) |
| Wine storage change | [`a7719bd8d0719e5ea6061387db020fa6a6b39d27`](https://github.com/volcmen/wine-forza-motorsport/commit/a7719bd8d0719e5ea6061387db020fa6a6b39d27) |

**REJECTED LIVE:** WineGDK `d96a768e25f632b04a457e4cb9f585e89ef5d095` with
Xodus `ee9db0f68122a9731b9b66cc24767693d1737f5c`. The observed combinations
produced protocol rejection, flicker, or exit after the logo. Do not install this
pair as an alternative. This verdict applies to those exact revisions.

Machine-readable authorities stay with the code:

- [Bootstrap trust manifest](../manifests/bootstrap-v1.toml): release download, bundle checksum, builder and source identities.
- [Runtime profiles](../manifests/runtime-profiles.toml): installer profile constraints.
- [Supported builds](../manifests/supported-builds.toml): accepted originals and exact patch bytes/hashes.
- [v0.1 publication manifest](../manifests/publication-v0.1.toml): v0.1 source and live-claim evidence; not a v0.2 acceptance record.

## Build and source records

These records retain their original dates and verdicts. A baseline or source-test
result is not a current installation recommendation.

| Record | Scope |
| --- | --- |
| [v0.1 Xodus](../evidence/v0.1-xodus.md) | Legacy source/build evidence |
| [v0.1 XGameRuntime](../evidence/v0.1-xgameruntime.md) | Legacy out-of-tree build evidence |
| [v0.1 live validation](../evidence/v0.1-live-validation.md) | Exact installed artifacts and dated observations |
| [Bootstrap build](../evidence/bootstrap-build.md) | v0.2 reproducibility and download evidence |
| [Wine baseline](../evidence/wine-baseline.md) | Historical source-test baseline |
| [Xodus baseline](../evidence/xodus-baseline.md) | Historical Xodus source-test baseline |
| [XGameRuntime baseline](../evidence/xgameruntime-baseline.md) | Historical newer-runtime hermetic tests |

## Verify repository changes

From a development checkout with the development tools installed:

```bash
uv sync --group dev
just verify
```

The gate runs Python and shell behavior tests, unit syntax verification,
ShellCheck, shfmt, Ruff, REUSE licensing, publication/privacy policy, and whitespace
checks. See [the checked-in gate](../justfile) and [CI setup](../.github/workflows/verify.yml)
for its host-tool requirements. These tests use synthetic or temporary roots;
they do not prove gameplay, account sign-in, networking, or invite delivery.

Documentation checks cover local links, heading anchors, evidence paths, and the
release boundaries above. Change guidance in this wiki; keep manifests and raw
evidence authoritative for exact identities. Do not rewrite old observations as
new validation results.

## Releases and archive

The documentation before this wiki is preserved at commit
`bf12d7e7f22c8ebb910f68162af1caf0e00b0a5c`. These links are pinned to that
snapshot, so moving the current documentation does not erase the record.

| Record | How to read it |
| --- | --- |
| [v0.2.0 release](https://github.com/volcmen/forza-motorsport-linux/releases/tag/v0.2.0) · [original local notes](https://github.com/volcmen/forza-motorsport-linux/blob/bf12d7e7f22c8ebb910f68162af1caf0e00b0a5c/docs/release-v0.2.0.md) | Bootstrap release and its historical pre-gameplay wording |
| [v0.1.0 release](https://github.com/volcmen/forza-motorsport-linux/releases/tag/v0.1.0) · [release evidence](https://github.com/volcmen/forza-motorsport-linux/blob/bf12d7e7f22c8ebb910f68162af1caf0e00b0a5c/docs/release-v0.1.0.md) | Source-only release with the dated accepted matrix |
| [Verification ledger](https://github.com/volcmen/forza-motorsport-linux/blob/bf12d7e7f22c8ebb910f68162af1caf0e00b0a5c/docs/verification.md) | Historical counts, source gates, and exact-build claims |
| [Upstream publication ledger](https://github.com/volcmen/forza-motorsport-linux/blob/bf12d7e7f22c8ebb910f68162af1caf0e00b0a5c/docs/upstream.md) | Recorded public branches, issue comments, and draft PR URLs |
| [Upstream draft texts](https://github.com/volcmen/forza-motorsport-linux/tree/bf12d7e7f22c8ebb910f68162af1caf0e00b0a5c/docs/upstream) | Historical text prepared for those publications |
| [Plans](https://github.com/volcmen/forza-motorsport-linux/tree/bf12d7e7f22c8ebb910f68162af1caf0e00b0a5c/docs/superpowers/plans) · [designs](https://github.com/volcmen/forza-motorsport-linux/tree/bf12d7e7f22c8ebb910f68162af1caf0e00b0a5c/docs/superpowers/specs) | Original proposals, superseded work, and design history; not current instructions |
| [Detailed architecture](https://github.com/volcmen/forza-motorsport-linux/blob/bf12d7e7f22c8ebb910f68162af1caf0e00b0a5c/docs/architecture.md) | Original transaction/protocol design; its older live-status wording is superseded by the table above |
| [Original documentation tree](https://github.com/volcmen/forza-motorsport-linux/tree/bf12d7e7f22c8ebb910f68162af1caf0e00b0a5c/docs) | Full pre-wiki archive, including old setup and troubleshooting pages |

The archive retains all original files and identities. The seven local evidence
records were moved from `docs/evidence`; only the Wine baseline's navigation link
was updated. Old immutable URLs continue to refer to the original paths.
