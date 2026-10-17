<!--
SPDX-FileCopyrightText: 2026 Forza Motorsport Linux tools contributors
SPDX-License-Identifier: GPL-3.0-or-later
-->

# Wine physical-controller identity source branch

## Provenance and publication status

| Field | Value |
| --- | --- |
| Canonical repository | <https://github.com/xodus-gaming/wine.git> |
| Canonical branch | `bleeding-edge` |
| Canonical base | `b1dd32734a34472a28eb5be9922df06e07ac0834` |
| Local compatibility branch | `fix/wgi-physical-nonroamable-id` |
| Final local commit | `ddd302d97c6008d79ea4f3e3ad56014cb548e514` |
| Evidence date | 2026-09-02 |

At the 2026-09-02 evidence date, this compatibility branch was local and
unpublished. It was subsequently published as
[xodus-gaming/wine PR #4](https://github.com/xodus-gaming/wine/pull/4), still open
at the same final commit when checked on 2026-10-17. The build and runtime
observations below remain historical; publication does not change their results.

The cumulative local commit sequence is:

1. `0b463bb633db147d8ba9381ab34e74540182cb5b` —
   `windows.gaming.input: identify physical XInput controllers`
2. `33553bb4195578aba162c9039ed7849aa2921830` —
   `windows.gaming.input: validate physical XInput identity paths`
3. `ddd302d97c6008d79ea4f3e3ad56014cb548e514` —
   `windows.gaming.input: keep NRID helper private`

No Wine binary is stored in this repository.

### Upstream review, checked 2026-10-17

[Weather-OS requested upstream Wine submission first](https://github.com/xodus-gaming/wine/pull/4#issuecomment-5576290488),
with the Xodus fork as a possible fallback if upstream declines. This is a routing
request, not approval or a request to remove the physical-controller fix.

Wine's [current provider implementation](https://github.com/wine-mirror/wine/blob/7298a818b79446bae153b8d5aa27235ab3703bf9/dlls/windows.gaming.input/provider.c#L144)
still returns `E_NOTIMPL` for `NonRoamableId`. Unlike the Xodus/Proton base of this
PR, it has no Steam virtual-controller implementation in that method. Upstream
submission therefore needs adaptation to that base, review of identity semantics,
and Wine/Windows conformance tests. The private-helper harness and historical
Forza fallback results below do not replace those tests. No Wine upstream merge
or new physical-controller validation is claimed.

## Source change

The final range from the canonical base changes exactly these files:

- `configure.ac`
- `dlls/windows.gaming.input/Makefile.in`
- `dlls/windows.gaming.input/nrid/Makefile.in`
- `dlls/windows.gaming.input/nrid/nrid.c`
- `dlls/windows.gaming.input/provider.c`
- `dlls/windows.gaming.input/provider_private.h`
- `dlls/windows.gaming.input/tests/Makefile.in`
- `dlls/windows.gaming.input/tests/input.c`

The implementation derives a stable `NonRoamableId` for a physical XInput
provider from its VID/PID and full device-instance discriminator. The path
parser accepts a case-insensitive, boundary-anchored `&XI_nn#` segment with
exactly two decimal slot digits. It preserves the existing Steam virtual
controller reservation for VID:PID `28de:11ff` and does not include a process
ID in the physical identity.

The production DLL and Wine test executable link the same private
`libwgi_private.a` implementation. The helper is not exported and the archive
is absent from the generated `install`, `install-dev`, and `uninstall` graphs.

## Verification evidence

The commands below use path variables deliberately; they do not identify a
specific workstation. `controller_src` names the local source worktree,
`controller_build` its dedicated out-of-tree build, and `tool_bin` the local
MinGW binutils directory.

The dedicated build was configured with:

```bash
(
    cd "$controller_build"
    PATH="$tool_bin:$PATH" "$controller_src/configure" --enable-win64 --without-ffmpeg
)
```

Exit status: `0`. The subsequent complete build also exited `0`:

```bash
PATH="$tool_bin:$PATH" make -C "$controller_build" -j8
```

The final focused compile/link gate was:

```bash
PATH="$tool_bin:$PATH" make -C "$controller_build" \
    dlls/windows.gaming.input/x86_64-windows/windows.gaming.input.dll \
    dlls/windows.gaming.input/tests/x86_64-windows/windows.gaming.input_test.exe \
    dlls/dinput/tests/x86_64-windows/dinput_test.exe
```

Exit status: `0`. Both final Windows.Gaming.Input link commands included
`dlls/windows.gaming.input/nrid/x86_64-windows/libwgi_private.a`. A
supplemental local harness compiled the actual `nrid.c` implementation and
reported zero failures across valid, malformed, bounds, distinct-instance,
case, and Steam-reservation cases. That ignored harness was not retained as a
public artifact and is not represented as Wine PE runtime evidence.

The final source-range check was:

```bash
git -C "$controller_src" diff --check \
    b1dd32734a34472a28eb5be9922df06e07ac0834..ddd302d97c6008d79ea4f3e3ad56014cb548e514
```

Exit status: `0`.

The focused Wine runtime command used an explicit build-local prefix:

```bash
PATH="$tool_bin:$PATH" \
WINEPREFIX="$controller_build/test-prefix-final" \
make -C "$controller_build" \
    dlls/windows.gaming.input/tests/x86_64-windows/input.ok
```

Exit status: `2`. Wine failed while starting `wineboot.exe`, before the test
DLL was dispatched, with `secur32.dll` initialization failure followed by
`kernel32.dll` status `c0000135`. The PE runtime result is therefore
**BLOCKED**, not a passing or failing controller assertion.

## Manual status and limitation

| Check on 2026-08-31 | Status |
| --- | --- |
| Source compile and link | **GREEN** |
| Supplemental production-source behavior harness | **GREEN** |
| Wine PE runtime test | **BLOCKED before dispatch** |
| Installation into the live compatibility tool or prefix | **NOT RUN** |
| Forza physical-controller matrix | **NOT RUN** |

No source or artifact from this branch was installed into the live
compatibility tool or game prefix. No Steam option was changed for this source
branch.

The parser intentionally covers the observed physical XInput device-path
grammar; it is not a claim about every HID, DirectInput, virtual-controller, or
future device-path form. Actual game/controller behavior still requires the
manual matrix on a separately authorized installed build.

## 2026-09-02 release revalidation

The clean source worktree remained at
`ddd302d97c6008d79ea4f3e3ad56014cb548e514`; its merge base with the configured
upstream branch remained
`b1dd32734a34472a28eb5be9922df06e07ac0834`. The source-range whitespace gate
exited 0.

The actual changed sources were touched only to force recompilation without
changing their bytes. These exact root targets then compiled and linked with
Clang and LLD 22.1.8 and exited 0:

```bash
make -C "$controller_build" \
    dlls/windows.gaming.input/x86_64-windows/windows.gaming.input.dll
make -C "$controller_build" \
    dlls/windows.gaming.input/tests/x86_64-windows/windows.gaming.input_test.exe
make -C "$controller_build" \
    dlls/dinput/tests/x86_64-windows/dinput_test.exe
```

The resulting review-only hashes were:

```text
befe134a5a45fddf8b61f966f61fea359e3c4c6da5ad772cd285d34f4e8975ff  windows.gaming.input.dll
cb3e13cd571a684a56467a4c7d911cf7b51932c0428ef6c4ce731245d30ee618  windows.gaming.input_test.exe
780af7f065b4e2b374e6ccca73146f9b78ea0466bff5fe35d2d42085ff490694  dinput_test.exe
```

Both runtime suites were retried with separate temporary `WINEPREFIX` values:

```bash
WINEPREFIX="$temporary_prefix" \
    make -C "$controller_build/dlls/windows.gaming.input/tests" test
WINEPREFIX="$temporary_prefix" \
    make -C "$controller_build/dlls/dinput/tests" test
```

Each exited 2 during `wineboot.exe`, with `secur32.dll` initialization failure
and `kernel32.dll` status `c0000135`, before either test executable dispatched.
The classification therefore remains **BLOCKED before dispatch**. The temporary
prefixes were removed and no default Wine prefix or Wine process remained.

The separately observed Forza controller and hotplug matrix passed through the
reviewed exact-build fallback recorded in
[`v0.1-live-validation.md`](../../evidence/v0.1-live-validation.md). That
game result does not claim that this standalone source branch was installed.
