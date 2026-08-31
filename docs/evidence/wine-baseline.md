# Wine compatibility baseline

Date: 2026-08-31

## Canonical source and branch state

The canonical Wine remote is `https://github.com/xodus-gaming/wine.git`.  Both
local branches below track `xodus/bleeding-edge` at
`b1dd32734a34472a28eb5be9922df06e07ac0834`; each has zero commits ahead of or
behind that upstream.  They are planned, local, and unpublished.

| Purpose | Local branch | Source worktree | Dedicated build directory | Baseline state |
| --- | --- | --- | --- | --- |
| Physical controller identity | `fix/wgi-physical-nonroamable-id` | `.worktrees/wine-controller` | `.worktrees/wine-controller-build` | clean; no configure or build run |
| Storage TRIM capability | `fix/storage-trim-property` | `.worktrees/wine-storage` | `.worktrees/wine-storage-build` | clean; no configure or build run |

The build directories are distinct ignored out-of-tree directories.  No live
build artifact was reused.

## Source baseline

- `dlls/windows.gaming.input/provider.c` has a Steam virtual-controller
  special case for VID:PID `28de:11ff`; after that case,
  `wine_provider_get_NonRoamableId()` returns `E_NOTIMPL`.  Thus a physical,
  non-Steam controller still has no NonRoamableId implementation.
- `include/ntddstor.h` assigns `StorageDeviceTrimProperty` the value `0x8`.
  `dlls/mountmgr.sys/device.c` handles only Device and SeekPenalty properties
  in this query switch.  Property `0x8` reaches the default unsupported path
  and returns `STATUS_NOT_SUPPORTED`.

## Exact-build fallback provenance

| Fallback | Original SHA-256 | Patched SHA-256 | Verified edits |
| --- | --- | --- | --- |
| `windows.gaming.input.dll` controller identity | `57538166ba052dc763232880f18a4b48a07ab735a61b5b8fbf5a8f56a05f2ca5` | `f520d9b4d44d9cdc11e67396583a1ccc8c53eca60e0b3a69233f358556b59245` | `0x14932..0x14933`: `753a` to `9090`; `0x1493a..0x1493b`: `7532` to `9090` |
| `mountmgr.sys` storage TRIM | `764f72bad9e639aead6535045f953815d9013ecb39b24b3b0027a70b846712b6` | `61fd9e9bb22ec1e92a3156c55cbfe77e22df49186b2de8f42b26d71cae2349d2` | `0x6b3f..0x6b47`: `83f8070f85b0020000` to `83e80783f80177b690`; `0x6f84..0x6f8a`: `c7460800000000` to `803e080f944608` |

The hashes and byte ranges above are manifest metadata only; no binary content
or artifact is included here.

## Evidence status

| Check | Status | Result |
| --- | --- | --- |
| Deterministic source assertions | PASS | Baseline source behavior above was asserted without compiling. |
| Worktree status, upstream relationship, and whitespace checks | PASS | Both Wine worktrees are clean at the same canonical base; `git diff --check` passed. |
| Integration repository privacy test | PASS | The repository policy test found no prohibited binary or live-secret content. |
| Compiled Wine tests | NOT RUN | Dedicated build directories exist but were not configured or built in this baseline task. |
| Manual physical-controller matrix row | NOT RUN | No source artifact was installed. |
| Manual AP702 matrix row | NOT RUN | No source artifact was installed. |

No live prefix or compatibility tool was changed.
