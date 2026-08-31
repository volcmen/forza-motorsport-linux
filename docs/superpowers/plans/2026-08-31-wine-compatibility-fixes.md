# Wine Controller and Storage Compatibility Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the two exact-build binary workarounds with reviewable Wine source changes and regression tests.

**Architecture:** Controller identity and storage capability are separate Wine branches because they affect unrelated subsystems and require independent review. The controller branch returns a stable opaque NonRoamableId for physical XInput-backed devices; the storage branch implements the documented TRIM descriptor while keeping unsupported property behavior unchanged.

**Tech Stack:** Wine C, Wine test framework, Unix build tools, Git worktrees, SHA-256 artifact verification.

**Spec:** `docs/superpowers/specs/2026-08-31-forza-motorsport-linux-publication-design.md`

## Global Constraints

- Start from the current `xodus-gaming/wine` canonical branch, not the stale local Forza branch.
- Keep controller and storage changes in separate commits and public branches.
- Preserve Steam virtual controller behavior and all unrelated storage properties.
- Add source-level tests before replacing the installed binary fallback.
- Do not publish Wine or Microsoft binaries in the integration repository.
- Treat local Forza behavior as one end-to-end data point, not proof of universal correctness.

---

### Task 1: Create clean Wine worktrees and record the baseline

**Files:**
- Create: `.worktrees/wine-controller/` through the worktree workflow
- Create: `.worktrees/wine-storage/` through the worktree workflow
- Create: `docs/evidence/wine-baseline.md` in the integration repository

**Interfaces:**
- Consumes: current `xodus-gaming/wine` `bleeding-edge` revision and the known binary hashes.
- Produces: branches `fix/wgi-physical-nonroamable-id` and `fix/storage-trim-property`, each based on a recorded upstream SHA.

- [ ] **Step 1: Invoke the worktree isolation workflow**

Run the `superpowers:using-git-worktrees` skill, then fetch without rewriting the existing local Forza tree.

Expected branch layout:

```text
fix/wgi-physical-nonroamable-id -> current xodus-gaming/wine/bleeding-edge
fix/storage-trim-property       -> current xodus-gaming/wine/bleeding-edge
```

- [ ] **Step 2: Record source and binary evidence**

Capture:

```text
upstream Wine SHA
provider.c still returns E_NOTIMPL for non-28de:11ff devices
mountmgr device.c still rejects property 0x8
controller original/patched SHA-256 and the two changed offsets
mountmgr original/patched SHA-256 and the two changed ranges
```

Do not include machine usernames, Steam paths, controller serials, or full logs.

- [ ] **Step 3: Verify both worktrees are clean**

Run:

```bash
git -C .worktrees/wine-controller status --short --branch
git -C .worktrees/wine-storage status --short --branch
```

Expected: each branch is clean and points to the same recorded upstream base.

- [ ] **Step 4: Commit baseline evidence in the integration repository**

```bash
git add docs/evidence/wine-baseline.md
git commit -m "docs: record Wine compatibility baselines"
```

---

### Task 2: Implement physical controller NonRoamableId

**Files:**
- Modify: `.worktrees/wine-controller/dlls/windows.gaming.input/provider.c`
- Modify: `.worktrees/wine-controller/dlls/windows.gaming.input/tests/input.c`

**Interfaces:**
- Consumes: `device_path`, hardware VID/PID, and the existing Steam virtual-controller metadata path.
- Produces: `provider_create_nonroamable_id(const WCHAR *device_path, UINT16 vid, UINT16 pid, HSTRING *value) -> HRESULT`.

- [ ] **Step 1: Extract a pure identity builder and write failing tests**

Add table-driven cases for:

```c
struct nrid_case
{
    const WCHAR *path;
    UINT16 vid;
    UINT16 pid;
    HRESULT expected_hr;
    const WCHAR *expected;
};

static const struct nrid_case cases[] =
{
    {L"\\\\?\\hid#vid_045e&pid_0b12&xi_00#synthetic&0&0000",
     0x045e, 0x0b12, S_OK,
     L"{wgi/nrid/:wine-045E&0B12&synthetic&0&0000}"},
    {L"malformed", 0x045e, 0x0b12, E_NOTIMPL, NULL},
};
```

The expected identifier is opaque, stable across process launches, contains the actual VID/PID, and excludes `GetCurrentProcessId()`. Keep the existing Steam `28de:11ff` test separate because Steam's native-hook emulation intentionally has its own format.

- [ ] **Step 2: Run the focused Wine test and confirm failure**

Run from the existing Wine build directory configured for this worktree:

```bash
make -C dlls/windows.gaming.input/tests test
```

Expected: new physical-controller cases fail because the helper still returns `E_NOTIMPL`.

- [ ] **Step 3: Implement minimal physical XInput path parsing**

Use the path form already observed by Wine:

```c
static HRESULT provider_create_nonroamable_id(const WCHAR *path, UINT16 vid,
                                               UINT16 pid, HSTRING *value)
{
    const WCHAR *xi = wcsstr(path, L"&xi_");
    const WCHAR *instance;
    WCHAR buffer[1024];
    int len;

    if (!xi || !(instance = wcschr(xi, '#')) || !instance[1]) return E_NOTIMPL;
    instance++;
    len = swprintf(buffer, ARRAY_SIZE(buffer),
                   L"{wgi/nrid/:wine-%04X&%04X&%s}", vid, pid, instance);
    if (len < 0 || len >= ARRAY_SIZE(buffer)) return E_BOUNDS;
    return WindowsCreateString(buffer, len, value);
}
```

Normalize only ASCII path markers needed for comparison; do not strip the instance portion that makes two identical controllers distinct. Call this helper for non-Steam devices after the existing Steam-specific block.

- [ ] **Step 4: Run focused and neighboring tests**

Run:

```bash
make -C dlls/windows.gaming.input/tests test
make -C dlls/dinput8/tests test
git diff --check
```

Expected: all tests exit `0`; existing Steam identity cases remain unchanged.

- [ ] **Step 5: Build the DLL and compare behavior**

Run:

```bash
make -C dlls/windows.gaming.input
sha256sum dlls/windows.gaming.input/windows.gaming.input.dll
```

Install only into the dedicated Forza compatibility tool/prefix, record the new source SHA and artifact SHA, and run the controller portion of the manual matrix.

Expected: Wine control panel reports the controller and Forza offers controller input with Steam Input disabled.

- [ ] **Step 6: Commit the controller branch**

```bash
git add dlls/windows.gaming.input/provider.c dlls/windows.gaming.input/tests/input.c
git commit -m "windows.gaming.input: identify physical XInput controllers"
```

---

### Task 3: Implement StorageDeviceTrimProperty

**Files:**
- Modify: `.worktrees/wine-storage/dlls/mountmgr.sys/device.c`
- Modify: `.worktrees/wine-storage/dlls/kernel32/tests/volume.c`

**Interfaces:**
- Consumes: `STORAGE_PROPERTY_QUERY` for property `StorageDeviceTrimProperty`.
- Produces: a correctly sized `DEVICE_TRIM_DESCRIPTOR` with `TrimEnabled = TRUE` for the current compatibility branch.

- [ ] **Step 1: Write failing descriptor and buffer tests**

Add a helper in the volume test that opens the Wine physical drive and sends `IOCTL_STORAGE_QUERY_PROPERTY`. Cover:

```c
query.PropertyId = StorageDeviceTrimProperty;
query.QueryType = PropertyStandardQuery;

ok(!DeviceIoControl(disk, IOCTL_STORAGE_QUERY_PROPERTY, &query, sizeof(query),
                    buffer, sizeof(STORAGE_DESCRIPTOR_HEADER) - 1, &ret, NULL),
   "short header unexpectedly succeeded\n");

ok(DeviceIoControl(disk, IOCTL_STORAGE_QUERY_PROPERTY, &query, sizeof(query),
                   &trim, sizeof(trim), &ret, NULL),
   "trim query failed, error %lu\n", GetLastError());
ok(trim.Version == sizeof(trim), "unexpected version %lu\n", trim.Version);
ok(trim.Size == sizeof(trim), "unexpected size %lu\n", trim.Size);
ok(trim.TrimEnabled, "expected compatibility TRIM support\n");
```

Also query an unrelated unsupported property and assert it remains unsupported.

- [ ] **Step 2: Run the focused test and verify failure**

Run:

```bash
make -C dlls/kernel32/tests volume.ok
```

Expected: the standard TRIM query fails with `ERROR_NOT_SUPPORTED` before implementation.

- [ ] **Step 3: Implement the documented descriptor**

Add a distinct switch case:

```c
case StorageDeviceTrimProperty:
{
    DEVICE_TRIM_DESCRIPTOR *d = irp->AssociatedIrp.SystemBuffer;

    if (irpsp->Parameters.DeviceIoControl.OutputBufferLength < sizeof(STORAGE_DESCRIPTOR_HEADER))
        status = STATUS_INVALID_PARAMETER;
    else
    {
        d->Version = d->Size = sizeof(*d);
        if (irpsp->Parameters.DeviceIoControl.OutputBufferLength < sizeof(*d))
            irp->IoStatus.Information = sizeof(STORAGE_DESCRIPTOR_HEADER);
        else
        {
            d->TrimEnabled = TRUE;
            irp->IoStatus.Information = sizeof(*d);
        }
        status = STATUS_SUCCESS;
    }
    break;
}
```

Zero the full descriptor before setting fields when the output buffer can hold it. State in the commit body that this branch provides a Wine compatibility capability value; canonical device-backed detection is a follow-up design question.

- [ ] **Step 4: Run focused and mount/volume tests**

Run:

```bash
make -C dlls/kernel32/tests volume.ok
make -C dlls/mountmgr.sys
git diff --check
```

Expected: tests and build exit `0`, including unsupported-property regression coverage.

- [ ] **Step 5: Install and run the AP702 manual A/B check**

Build and install only the new `mountmgr.sys` into the dedicated compatibility tool and AppID prefix. Record:

```text
before: property 0x8 unsupported and AP702 displayed
after: property 0x8 succeeds and AP702 absent
```

Expected: same game build and Steam options proceed without AP702.

- [ ] **Step 6: Commit the storage branch**

```bash
git add dlls/mountmgr.sys/device.c dlls/kernel32/tests/volume.c
git commit -m "mountmgr: report StorageDeviceTrimProperty"
```

---

### Task 4: Export reviewable source evidence

**Files:**
- Create: `patches/wine-controller/README.md` in the integration repository
- Create: `patches/wine-storage-trim/README.md` in the integration repository
- Modify: `docs/verification.md` in the integration repository

**Interfaces:**
- Consumes: final Wine branch SHAs, test outputs, and manual matrix rows.
- Produces: provenance records and links; the integration repository does not vendor compiled Wine artifacts.

- [ ] **Step 1: Write provenance documents**

Each file records:

```text
canonical base repository and SHA
public branch and commit SHA
files changed
exact test commands and exit codes
manual result and date
known semantic limitation
```

The storage document explicitly says that unconditional `TrimEnabled = TRUE` is suitable for the dedicated compatibility branch but needs maintainer agreement before canonical Wine inclusion.

- [ ] **Step 2: Run privacy and link checks**

Run:

```bash
just privacy
git diff --check
```

Expected: zero personal identifiers, tokens, invite URIs, binaries, or whitespace errors.

- [ ] **Step 3: Commit integration provenance**

```bash
git add patches/wine-controller/README.md patches/wine-storage-trim/README.md docs/verification.md
git commit -m "docs: link tested Wine compatibility branches"
```
