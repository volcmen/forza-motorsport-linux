<!--
SPDX-FileCopyrightText: 2026 Forza Motorsport Linux tools contributors
SPDX-License-Identifier: GPL-3.0-or-later
-->

# Wine StorageDeviceTrimProperty source branch

## Provenance and publication status

| Field | Value |
| --- | --- |
| Canonical repository | <https://github.com/xodus-gaming/wine.git> |
| Canonical branch | `bleeding-edge` |
| Canonical base | `b1dd32734a34472a28eb5be9922df06e07ac0834` |
| Local compatibility branch | `fix/storage-trim-property` |
| Production commit | `943f12e4a30786c3a99213057a9e6aefeb22ff0a` |
| Final local commit | `a7719bd8d0719e5ea6061387db020fa6a6b39d27` |
| Evidence date | 2026-08-31 |

The compatibility branch is planned and local. It has not been pushed or
published, so the branch name and commit identifiers are provenance records,
not public download or fetch locations.

The local commits are:

1. `943f12e4a30786c3a99213057a9e6aefeb22ff0a` —
   `mountmgr: report StorageDeviceTrimProperty`
2. `a7719bd8d0719e5ea6061387db020fa6a6b39d27` —
   `kernel32/tests: cover partial TRIM descriptors`

No Wine binary is stored in this repository.

## Source change

The final range from the canonical base changes exactly:

- `dlls/mountmgr.sys/device.c`
- `dlls/kernel32/tests/volume.c`

### Upstream lineage

The production behavior is a backport/adaptation of wine-mirror commit
[`5b5e95719dd7dd4d219cad447f8fb219259cb162`](https://github.com/wine-mirror/wine/commit/5b5e95719dd7dd4d219cad447f8fb219259cb162),
`mountmgr.sys: Stub StorageDeviceTrimProperty query.` It was not applied
verbatim to this older Xodus base. The adaptation differs in three documented
ways:

- This base lacks the public `DEVICE_TRIM_DESCRIPTOR` declaration in
  `include/ntddstor.h`. The two task-scoped source files instead use an
  equivalent private 12-byte layout protected by
  `C_ASSERT(sizeof(struct trim_descriptor) == 12)`.
- The full descriptor is zero-filled before its fields are assigned, making
  the three ABI padding bytes after `TrimEnabled` deterministic.
- The added tests cover short-header, header-only, 9-, 10-, and 11-byte
  partial, full-descriptor, zero-padding, and unrelated-property cases. Wine's
  compatibility expectations remain strict while `broken()` allowances record
  accepted or possible native-Windows alternatives without weakening the Wine
  checks. Native Windows was not run for this task.

The modified Wine source remains under its existing LGPL-2.1-or-later source
headers. The GPL-3.0-or-later SPDX declaration at the top of this file applies
to this integration-repository evidence document; no Wine source or binary is
redistributed here.

For a standard `StorageDeviceTrimProperty` query, the compatibility branch
returns a correctly sized descriptor with `TrimEnabled = TRUE`. A buffer
shorter than `STORAGE_DESCRIPTOR_HEADER` is rejected; header and partial
buffers report the full descriptor metadata without writing beyond the
returned header; a full descriptor is zero-filled before its fields are set.
An unrelated out-of-range property remains unsupported.

This is an unconditional game-compatibility policy, not host-device or
filesystem TRIM detection. It is suitable for this dedicated compatibility
branch, but it needs maintainer agreement and a canonical device-detection
design before inclusion in canonical Wine.

## Verification evidence

The commands below use path variables deliberately. `storage_src` names the
local source worktree, `storage_build` its dedicated out-of-tree build, and
`tool_bin` the local MinGW binutils directory.

After initializing the source snapshot's pinned `dlls/xgameruntime` submodule,
the dedicated build was configured with:

```bash
(
    cd "$storage_build"
    PATH="$tool_bin:$PATH" "$storage_src/configure" --enable-win64 --without-ffmpeg
)
```

Exit status: `0`. The complete build with the implementation present was:

```bash
PATH="$tool_bin:$PATH" make -C "$storage_build" -j8
```

Exit status: `0`, ending with `Wine build complete.` Production and test
compile/link checks were:

```bash
PATH="$tool_bin:$PATH" make -C "$storage_build/dlls/mountmgr.sys"
PATH="$tool_bin:$PATH" make -C "$storage_build" \
    dlls/kernel32/tests/x86_64-windows/kernel32_test.exe
```

Each command exited `0` in the recorded clean build sequence. The review-only
test hardening changed no production source; the final production object was
also confirmed current with:

```bash
PATH="$tool_bin:$PATH" make -C "$storage_build" \
    dlls/mountmgr.sys/x86_64-windows/device.o
```

Exit status: `0`.

The final source-range check was:

```bash
git -C "$storage_src" diff --check \
    b1dd32734a34472a28eb5be9922df06e07ac0834..a7719bd8d0719e5ea6061387db020fa6a6b39d27
```

Exit status: `0`.

The final focused Wine runtime command used a fresh explicit build-local
prefix:

```bash
PATH="$tool_bin:$PATH" \
WINEPREFIX="$storage_build/test-prefix-green-final" \
make -C "$storage_build/dlls/kernel32/tests" x86_64-windows/volume.ok
```

Exit status: `2`. Wine failed during `wineboot.exe` startup, before the volume
test was dispatched, with `secur32.dll` initialization failure followed by
`kernel32.dll` status `c0000135`. The runtime result is **BLOCKED**, not a
passing or failing TRIM assertion.

## Manual status and limitation

| Check on 2026-08-31 | Status |
| --- | --- |
| Source compile and link | **GREEN** |
| Incremental whole-Wine build | **GREEN** |
| Wine PE runtime test | **BLOCKED before dispatch** |
| Native Windows test | **NOT RUN** |
| Installation into the live compatibility tool or prefix | **NOT RUN** |
| Forza/AP702 before-and-after matrix | **NOT RUN** |

No source or artifact from this branch was installed into the live
compatibility tool or game prefix. AP702 disappearance is not verified by this
source task. The behavior remains a compatibility answer until maintainers
agree whether canonical Wine should report a fixed capability or derive the
value from the backing device.
