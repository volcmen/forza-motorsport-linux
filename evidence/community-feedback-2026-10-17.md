# Community feedback review — 2026-10-17

Reviewed the project's public Reddit posts through the maintainer's browser with
Agent Browser over CDP, plus GitHub issues, PR conversations/reviews, and the
recent Forza discussion in ValveSoftware/Proton#7151. This is a source and
diagnostic review. It adds no new game-session or physical Steam Deck results.

## Who replied and what changed

The live Reddit profile and threads showed three comments on
[r/wine_gaming](https://www.reddit.com/r/wine_gaming/comments/1w7ifme/forza_motorsport_on_linux_working_personal_setup/),
and zero on the project's
[r/SteamPlay](https://www.reddit.com/r/SteamPlay/comments/1w7ingi/getting_forza_motorsport_working_on_linux/)
and [r/SideProject](https://www.reddit.com/r/SideProject/comments/1w7isic/forzamotorsportlinux_the_side_project_i_built/)
posts at review time. No replies or reactions were posted during this review.

| Contributor and source | Feedback | Validation and disposition |
| --- | --- | --- |
| [randomusernameonweb](https://www.reddit.com/r/wine_gaming/comments/1w7ifme/comment/p7vol0f/) | The libHttpClient XThreading port should remove the Microsoft DLL requirement. | Confirmed that upstream has native task-queue implementations and new tests. This does not qualify the shipped Forza runtime without the DLL; see the source comparison below. The requirement remains explicit. |
| [erickgps](https://www.reddit.com/r/wine_gaming/comments/1w7ifme/comment/pbhmgp2/) | Asked for a SteamOS tutorial. | The Wiki's Install and Steam Deck pages now describe the composed preview. Fixed current `platform-check` to check that workflow by default: native service libraries and Flatpak, without requiring native GTK/WebKit/SDL GUI packages. `--profile legacy` retains the old native prerequisites and fresh-bootstrap warning. |
| PhysicalIncrease3, in the wine_gaming thread above | Planned to try the project. | Acknowledged as interest, not a successful-installation report. |
| [FluffGlitter, issue #8](https://github.com/volcmen/forza-motorsport-linux/issues/8) and [PR #9](https://github.com/volcmen/forza-motorsport-linux/pull/9) | Fresh-install retries, runtime modes/transactions, mutable tool contents, and the wrong disk-space target. | PR #9 already fixes the retry/mode/child-transaction defects. PR #16 supplies a separate composed workflow with complete immutable tool publication and user-only Finish. Fixed the remaining Doctor disk check in this review: use the verified game directory for composed installs, or `steamapps` for legacy bind-mounted layouts. |
| [FluffGlitter, issue #7 follow-up](https://github.com/volcmen/forza-motorsport-linux/issues/7#issuecomment-6072891753) | Resolved the splash hang by removing a pair of DLL names differing only by case; reported AMD resets with ray tracing. | The current platform diagnostic and composed Prepare already detect top-level DLL name collisions without deleting files. The Wiki records this specific cause. The graphics report remains contributor evidence; it does not justify a global ray-tracing override. |
| [FluffGlitter, PR #9 validation](https://github.com/volcmen/forza-motorsport-linux/pull/9) | shfmt 3.14 rejected two files despite older formatting checks passing. | Reproduced with the official 3.14.0 binary: arithmetic negation in Doctor and a here-document in the setup test. Formatted both and pinned the same checksum-verified formatter in CI. |
| [Weather-OS, Wine fork PR #4](https://github.com/xodus-gaming/wine/pull/4#issuecomment-5576290488) | Asked for the physical-controller fix to go to Wine upstream first, with the fork as a possible fallback. | Current Wine still has the `NonRoamableId` stub, but its base differs from this Proton-derived patch. Corrected stale publication claims in the controller/storage records and documented the required upstream adaptation and conformance tests. Upstream submission remains open. |

The older hash/submodule comparison request in
[issue #7](https://github.com/volcmen/forza-motorsport-linux/issues/7#issuecomment-5985778977)
was followed by the contributor's successful diagnosis above. The project's
[current build identities](composed-preview-build-2026-10-15.md) and
[submodule list](composed-proton-submodules-2026-10-15.txt) are already public.
They identify the new preview; they are not a reconstructed hash list for the
deleted upstream build. Issues #7 and #8 remain open for the legacy path and
their remaining acceptance boundaries.

In the [upstream Proton thread](https://github.com/ValveSoftware/Proton/issues/7151),
[SoHiEarth](https://github.com/ValveSoftware/Proton/issues/7151#issuecomment-6013400174)
shared a working-build mirror and
[madcap0j](https://github.com/ValveSoftware/Proton/issues/7151#issuecomment-6063511155)
reported loading and driving with a manual Debian setup. Those are contributor
reports about other artifacts/environments. They do not change this project's
download pins or establish supported Debian/physical Deck behavior. The later
mirror packaging discussion also reinforces preserving Wine's symlinks; the
project's composed archive verifier already checks their targets.

The maintainer's [Wine controller PR #4](https://github.com/xodus-gaming/wine/pull/4)
and [storage PR #5](https://github.com/xodus-gaming/wine/pull/5) remain open at
`ddd302d` and `a7719bd`; the storage PR has no public replies or reviews.
[Xodus social issue #94](https://github.com/xodus-gaming/xodus/issues/94)
has no reply after the maintainer's proposal. The
[controller record](../patches/wine-controller/README.md#upstream-review-checked-2026-10-17)
links the current Wine source comparison. No external discussion was closed,
and no upstream submission or maintainer acceptance is claimed.

## Threading suggestion: what the source establishes

Upstream `xodus-gaming/xgameruntime` at
[`7a82701a53d316369b24156c21dbb8752abca264`](https://github.com/xodus-gaming/xgameruntime/tree/7a82701a53d316369b24156c21dbb8752abca264)
has implemented XThreading dispatch in
[`src/System/XThreading.cpp`](https://github.com/xodus-gaming/xgameruntime/blob/7a82701a53d316369b24156c21dbb8752abca264/src/System/XThreading.cpp)
and task-queue/async tests. The test job on
[PR #31](https://github.com/xodus-gaming/xgameruntime/pull/31) passed. Its
[workflow](https://github.com/xodus-gaming/xgameruntime/blob/7a82701a53d316369b24156c21dbb8752abca264/.github/workflows/test.yml)
runs on Windows and explicitly leaves Wine/Proton testing as future work.

The preview pins a different source tree: Wine
`3ecd0e8a9b8e7e0a754b09595d1aa70c9b1e1301`. Its
[`main.c`](https://github.com/volcmen/wine-forza-motorsport/blob/3ecd0e8a9b8e7e0a754b09595d1aa70c9b1e1301/dlls/xgameruntime/main.c)
loads `xgameruntime.dll.threading` and prefers its XThreading interface. A builtin
fallback exists, with a warning recommending the Microsoft implementation. The
recorded Forza sessions used the licensed DLL; no current DLL-free game session
has been qualified. Upstream source/tests support further investigation, not
removal of the installation requirement or an unreviewed runtime/protocol swap.

The [later preview 2 local test](preview-2-validation-2026-10-17.md#dll-free-comparison)
disabled native threading DLL loading in the actual game process without changing
the installed file. The builtin warning appeared, then the game exited before
the title screen. Its standalone API tests still passed. That follow-up is
separate from the initial source review above and does not qualify DLL-free
Forza or remove the licensed-DLL requirement.

## Diagnostic and verification changes

- Doctor measures the game filesystem using the game directory identity already
  recorded in a checksum-bound composed plan. If that directory was replaced or
  records are invalid, it reports failure instead of falling back to a different
  filesystem. A failed `df` cannot become a pass through partial numeric output.
- Legacy Doctor measures `steamapps`, so a game disk bind-mounted there is
  checked rather than the parent Steam directory. For other custom legacy mount
  layouts, use `platform-check --profile legacy`, which discovers the actual
  game directory from Steam metadata.
- The default platform diagnostic describes the experimental composed preview;
  it does not claim that the Flatpak deployment is installed or hash-verified.
  Exact app/runtime verification remains in composed Finish. Both profiles keep
  redacted output and read-only behavior.
- The native service's ELF dependencies were inspected without executing it.
  Its SHA-256 matched `c736faf121e9a6e8726245fec334a75b849d7c22dd32cd5a572b18bdacee90b5`
  in `composed-bundle-v1.toml`; it needs D-Bus/OpenSSL and base system libraries,
  without GTK/WebKit/SDL GUI dependencies. This is an ELF dependency observation,
  not a new SteamOS runtime test.
- Verify uses official shfmt 3.14.0 for Linux x86_64, SHA-256
  `fe42021c7272ef2d67ea36cbc3031683c625d0badec733ef3a57b567246a0b66`, checked before
  installation. Manual workflow dispatch is available. No trust pins for game
  runtimes, Proton, Xodus, or released artifacts changed.

## GitHub Actions findings

At the start of the review, main was `5db24f0` and both
[Verify](https://github.com/volcmen/forza-motorsport-linux/actions/runs/38892237650)
and [CodeQL](https://github.com/volcmen/forza-motorsport-linux/actions/runs/38892237693)
had passed. The latest failed Verify run returned by GitHub,
[34710530151](https://github.com/volcmen/forza-motorsport-linux/actions/runs/34710530151),
was a September dependency PR: its log failed at REUSE's generated license-text
scan. [PR #11](https://github.com/volcmen/forza-motorsport-linux/pull/11) already
corrected that failure before the dependency update merged. The newer permission
fixture repair in [PR #21](https://github.com/volcmen/forza-motorsport-linux/pull/21)
was also already on main. These historical failures were not current failures.

The formatter mismatch was freshly reproduced during this review and is fixed
by the source formatting and pinned CI tool described above.

## Local validation

`just verify` passed with **1,009 Python tests**, one opt-in Docker test skipped,
all six shell suites, systemd unit syntax, ShellCheck, shfmt 3.14.0, Ruff, REUSE,
21 repository-policy checks, and `git diff --check`. Regression coverage includes
game-versus-library filesystem selection, a replaced recorded game directory,
`df` failure despite numeric output, both diagnostic profiles, missing Flatpak
and native service dependencies, and read-only CLI operation without bytecode
writes. Wiki edits were checked for local link targets and balanced code fences.

These checks use temporary/synthetic installations. Existing managed game files,
runtime artifacts, account state, and released manifests were not changed.
