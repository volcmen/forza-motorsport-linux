# Historical XGameRuntime Xodus Invite Bridge Plan

> **Superseded — do not execute.** Fresh source inspection invalidated this
> plan's source-base, protocol, build, and publication assumptions. Execute
> `2026-09-01-xgameruntime-winegdk-amendment.md` instead. This file is retained
> only as historical design evidence.

> Every task, command, checkbox, and expected result below is non-normative
> archival text. It must not be used as implementation guidance.

**Goal:** Implement GDK social UI and accepted-invite delivery in the canonical Xodus xgameruntime component using a tested Unix-socket bridge.

**Architecture:** The Windows-facing xgameruntime implementation stays responsible for COM/GDK ABI and XAsync semantics; a Wine Unixlib transport owns socket I/O; Xodus owns UI and Xbox service behavior. The bridge carries only four bounded frame types and treats invite activation URIs as sensitive values.

**Tech Stack:** Wine C, GDK-compatible IDL, Wine Unixlib, pthread/Unix sockets, Wine test framework, Python fixture service.

**Spec:** `docs/superpowers/specs/2026-08-31-forza-motorsport-linux-publication-design.md`

## Global Constraints

- Compare open xgameruntime PRs 18 and 19 and use the maintainer's active Unixlib base (`oot-cpp`) instead of duplicating transport scaffolding.
- Preserve existing xgameui/xgameinvite ABI and implement only the methods exercised by Forza.
- Use `XAsyncSchedule`, `XAsyncComplete`, and `XAsyncGetResult` through XThreading.
- Bound invite URIs to 4096 bytes and reject unexpected schemes before delivery.
- Serialize writes to any shared stream and handle fragmented reads/writes.
- Never log a complete invite URI, token, XUID, or connection string.
- Keep Microsoft binaries out of the repository and CI.

---

### Task 1: Establish the canonical transport base

**Files:**
- Create: isolated xgameruntime worktree through the worktree workflow
- Modify according to active base: `Makefile.in`
- Modify according to active base: `private.h`
- Create or modify according to PR 19: `GDKComponent/Xodus/Unix/socket.c`
- Create: `docs/evidence/xgameruntime-baseline.md` in the integration repository

**Interfaces:**
- Consumes: xgameruntime `oot-cpp`, PR 19's Unixlib conventions, and Xodus socket path override.
- Produces: `enum xodus_unix_func` and typed argument structures shared by Windows and Unix sides.

- [ ] **Step 1: Invoke worktree isolation and inspect PR 19 completely**

Use `superpowers:using-git-worktrees`. Record the base SHA and whether these existing operations are present:

```text
connect main Xodus socket
poll main protocol socket
send main protocol frame
WINEGDK_XODUS_SOCKET override
XDG_RUNTIME_DIR/xodus.sock default
```

- [ ] **Step 2: Define append-only Unix operation IDs**

Use one shared enum in `private.h` rather than repeating numeric literals:

```c
enum xodus_unix_func
{
    xodus_connect,
    xodus_poll,
    xodus_send,
    xodus_invite_connect,
    xodus_invite_poll,
    xodus_invite_close,
    xodus_show_social_ui,
};

enum xodus_social_status
{
    XODUS_SOCIAL_OK = 0,
    XODUS_SOCIAL_CANCELLED = 1,
    XODUS_SOCIAL_FAILED = 2,
};
```

Add compile-time assertions in both sides when the build supports them. Never renumber the first three operations supplied by the base.

- [ ] **Step 3: Build the untouched/normalized base**

Run in the configured Wine embedding tree:

```bash
make -C dlls/xgameruntime
git diff --check
```

Expected: xgameruntime builds before behavior changes; any required base-only normalization is isolated in one commit.

- [ ] **Step 4: Commit only required base adaptation**

```bash
git add Makefile.in private.h GDKComponent/Xodus/Unix/socket.c
git commit -m "xgameruntime: share Xodus Unix call definitions"
```

Skip this commit when PR 19 already provides an equivalent clean interface.

---

### Task 2: Add bounded Unix transport operations

**Files:**
- Modify: `GDKComponent/Xodus/Unix/socket.c`
- Modify: `private.h`
- Create: `tests/xodus_socket_fixture.py`
- Create: `tests/test_xodus_socket.py`

**Interfaces:**
- Consumes: `$WINEGDK_XODUS_SOCKET` or `$XDG_RUNTIME_DIR/xodus.sock`.
- Produces: `xodus_invite_connect`, `xodus_invite_poll`, `xodus_invite_close`, and `xodus_show_social_ui` Unix calls.

- [ ] **Step 1: Write failing fixture tests for fragmented I/O and status mapping**

```python
def test_social_ui_request_handles_fragmented_status(socket_fixture):
    socket_fixture.expect_magic(b"XDUI")
    socket_fixture.reply_fragments([b"\x00"])
    assert socket_fixture.run_client("show-social-ui") == 0


def test_invite_rejects_oversized_length(socket_fixture):
    socket_fixture.expect_magic(b"XDSI")
    socket_fixture.reply_u32(4097)
    assert socket_fixture.run_client("poll-invite") != 0
```

The fixture uses a temporary Unix socket and synthetic payloads only.

- [ ] **Step 2: Run the fixture tests and verify failure**

Run:

```bash
uv run pytest tests/test_xodus_socket.py -q
```

Expected: failure because the new client operations are absent.

- [ ] **Step 3: Implement robust connect/read/write helpers**

Required behavior:

```c
static int write_all(int fd, const void *buffer, size_t length);
static int read_all(int fd, void *buffer, size_t length);
static int connect_xodus_socket(const char *suffix);
```

Retry `EINTR`, reject Unix paths that do not fit `sockaddr_un.sun_path`, close on every error, treat zero-byte read/write as disconnect, and keep the invite descriptor independent from the existing protocol descriptor.

The social status byte is shared with Xodus:

```text
0 = completed successfully
1 = cancelled by the user
2 = failed to open or execute
```

- [ ] **Step 4: Verify transport tests and sanitizer-friendly build**

Run:

```bash
uv run pytest tests/test_xodus_socket.py -q
make -C dlls/xgameruntime
git diff --check
```

Expected: all commands exit `0`; fixture confirms fragmented I/O and every status value.

- [ ] **Step 5: Commit transport**

```bash
git add GDKComponent/Xodus/Unix/socket.c private.h tests/xodus_socket_fixture.py tests/test_xodus_socket.py
git commit -m "xgameruntime: add Xodus social transport"
```

---

### Task 3: Implement XGameUi asynchronous invite UI

**Files:**
- Modify: `xgameui.c`
- Modify: `tests/xgameruntime.c`

**Interfaces:**
- Consumes: `xodus_show_social_ui` and `XAsyncBlock`.
- Produces: working `XGameUiShowSendGameInviteAsync/Result` and `XGameUiShowMultiplayerActivityGameInviteAsync/Result`.

- [ ] **Step 1: Write failing XAsync lifecycle tests**

```c
hr = ui->lpVtbl->XGameUiShowMultiplayerActivityGameInviteAsync(ui, &async, user);
ok(hr == S_OK, "async start returned %#lx\n", hr);
ok(WaitForSingleObject(done, 5000) == WAIT_OBJECT_0, "callback timed out\n");
hr = ui->lpVtbl->XGameUiShowMultiplayerActivityGameInviteResult(ui, &async);
ok(hr == S_OK, "result returned %#lx\n", hr);
```

Add cases for null async (`E_POINTER`), Xodus cancelled (`E_ABORT`), Xodus unavailable (failure HRESULT), exactly one completion callback, and a nonblocking Begin path.

- [ ] **Step 2: Run the focused test and verify failure**

Run:

```bash
make -C dlls/xgameruntime/tests test
```

Expected: the two APIs return `E_NOTIMPL` or fail the completion checks.

- [ ] **Step 3: Implement one reusable XAsync provider**

Use this provider lifecycle:

```c
case XAsyncOp_Begin:
    return IXThreadingImpl_XAsyncSchedule(threading, data->async, 0);
case XAsyncOp_DoWork:
    status = __wine_unix_call(unixhandle, xodus_show_social_ui, NULL);
    result = status == XODUS_SOCIAL_OK ? S_OK :
             status == XODUS_SOCIAL_CANCELLED ? E_ABORT : E_FAIL;
    IXThreadingImpl_XAsyncComplete(threading, data->async, result, 0);
    return S_OK;
default:
    return S_OK;
```

Both public invite methods call the same `start_social_ui()` helper and both result methods call `XAsyncGetResult()`.

- [ ] **Step 4: Verify XAsync tests**

Run:

```bash
make -C dlls/xgameruntime/tests test
git diff --check
```

Expected: every success, cancellation, missing-service, and null-pointer case passes.

- [ ] **Step 5: Commit XGameUi implementation**

```bash
git add xgameui.c tests/xgameruntime.c
git commit -m "xgameui: open the Xodus social picker"
```

---

### Task 4: Implement accepted-invite registration and delivery

**Files:**
- Modify: `xgameinvite.c`
- Modify: `tests/xgameruntime.c`

**Interfaces:**
- Consumes: `XDSI` activation stream and `XTaskQueueHandle` registrations.
- Produces: `XGameInviteRegisterForEvent`, `XGameInviteUnregisterForEvent`, and callback delivery on the requested completion queue.

- [ ] **Step 1: Write failing registration tests**

```c
hr = invite->lpVtbl->RegisterForEvent(invite, queue, &state, invite_callback, &token);
ok(hr == S_OK, "register returned %#lx\n", hr);
ok(token.token != 0, "registration token is zero\n");
publish_synthetic_activation("ms-xbl-multiplayer://inviteAccept?invitedXuid=1");
ok(WaitForSingleObject(state.event, 5000) == WAIT_OBJECT_0, "invite timed out\n");
ok(!strcmp(state.scheme, "ms-xbl-multiplayer"), "unexpected scheme %s\n", state.scheme);
```

Add null callback/token, four registrations, fifth-registration refusal, unregister-before-delivery, disconnect/reconnect, and `wait=TRUE` cases. Store only a redacted scheme in test output.

- [ ] **Step 2: Run and verify expected failure**

Run:

```bash
make -C dlls/xgameruntime/tests test
```

Expected: `XGameInviteRegisterForEvent` returns `E_NOTIMPL` before implementation.

- [ ] **Step 3: Implement synchronized registration and worker lifetime**

Use an SRW lock around a fixed registration table, monotonically increasing nonzero tokens, one subscriber worker, and one allocated callback payload per delivery. If a queue exists, dispatch with `XTaskQueueSubmitCallback`; otherwise invoke synchronously only where the GDK contract permits it.

Validate incoming data before callback:

```c
if (!length || length > MAX_INVITE_URI) reject;
static const char invite_prefix[] = "ms-xbl-multiplayer://inviteAccept?";
if (strncmp(uri, invite_prefix, sizeof(invite_prefix) - 1)) reject;
if (memchr(uri, '\0', length) != NULL) reject;
```

Do not print `uri` in TRACE/WARN output.

- [ ] **Step 4: Verify registration and full GDK tests**

Run:

```bash
make -C dlls/xgameruntime/tests test
git diff --check
```

Expected: registration, cancellation, reconnection, queue dispatch, and existing interface-query tests pass.

- [ ] **Step 5: Commit invite delivery**

```bash
git add xgameinvite.c tests/xgameruntime.c
git commit -m "xgameinvite: deliver Xodus invite activations"
```

---

### Task 5: Integrate, install, and verify the runtime

**Files:**
- Modify: `Makefile.in` only if new sources/tests are not already declared
- Modify: `docs/verification.md` in the integration repository
- Create: `patches/xgameruntime/README.md` in the integration repository

**Interfaces:**
- Consumes: Tasks 1–4 and the Xodus social protocol branch.
- Produces: a built xgameruntime DLL/Unixlib pair, installed-artifact parity, manual invite/join evidence, and a reviewable public branch.

- [ ] **Step 1: Run the complete source gate**

Run:

```bash
make -C dlls/xgameruntime clean
make -C dlls/xgameruntime
make -C dlls/xgameruntime/tests test
git diff --check
```

Expected: every command exits `0` and the Wine test summary reports zero failures.

- [ ] **Step 2: Install only into the dedicated Forza runtime**

Back up current open-source artifacts, install the new `xgameruntime.dll` and Unix side, and compare SHA-256 values between source build and installed locations.

Expected: every source/installed open-source artifact matches. Do not touch or publish the user-supplied `xgameruntime.dll.threading`.

- [ ] **Step 3: Run the manual social matrix**

Verify:

```text
Invite Friends opens Xodus overlay
keyboard selects and cancels
Xbox controller selects and cancels
invite reaches a Windows player from a published lobby
Linux joins a Windows player's published activity
game exits and launcher-owned Xodus stops
automatic incoming notification remains NOT SUPPORTED
```

- [ ] **Step 4: Write provenance and limitation notes**

Record base SHA, branch SHA, build commands, test counts, installed hashes, manual results, protocol version, and relationship to xgameruntime PR 19. Include no private identity or raw activation value.

- [ ] **Step 5: Commit integration evidence**

```bash
git add patches/xgameruntime/README.md docs/verification.md
git commit -m "docs: record xgameruntime invite verification"
```
