# XGameRuntime WineGDK Bridge — Preflight Amendment

> This amendment supersedes the source-base, protocol, build, and publication
> assumptions in `2026-08-31-xgameruntime-invite-bridge.md`. The original plan
> remains as historical design evidence and must not be executed.

## Why the base changed

Fresh 2026-09-01 inspection established:

- `xodus-gaming/xgameruntime` `oot-cpp` at
  `c5e6ac17b13996ad2338b1f9e3dbffc5e4d4d3b1` contains the substantive
  XAsync/XTaskQueue implementation but exposes only XThreading. It has no
  XGameUi/XGameInvite implementation and is a standalone CMake tree with no
  Wine Unixlib integration.
- Draft PR 19 adds only `xodusprovider.idl`; it contains no socket or Unixlib
  transport. Draft PR 18 is build preparation, not a Winsock transport.
- `xodus-gaming/wine` `bleeding-edge` at
  `b1dd32734a34472a28eb5be9922df06e07ac0834` embeds the unrelated Wine-style
  xgameruntime history at gitlink
  `64aebcabb8c66121eae25d3bf0ace4b582ebb0da`. Its PE Winsock does not implement
  `AF_UNIX`, and its xgameruntime XAsync/XTaskQueue layer is stubbed.
- `Weather-OS/WineGDK` `master` at
  `b03ba49c4f326c36aa6930fbe6cf72841ef3738c` is the only inspected base that
  already combines the substantive XThreading implementation, Wine Unixlib
  loading, Xodus main-socket transport, Wine build integration, and a test
  target. The prior game prototype was built from this family, but its dirty
  source has lifecycle, ABI, timeout, and test defects and is reference-only.

Therefore the installable Linux implementation will be re-derived on a clean
WineGDK base. It is an experimental user-facing branch, not a proposed
`xodus-gaming/xgameruntime` upstream PR. The latter project's maintainer
rejected generated code in PR 10; this AI-assisted work must not be
misrepresented as eligible upstream code.

## Fixed boundaries

- Xodus contracts start from the reviewed local branch at `207a721e6a1cc95be04ffaeedc67a4d673d45ca2` and must first receive the scoped mode extension in Task -1:
  - accepted `XDUI` requests use version/mode + acknowledgement + exactly one
    terminal status byte (`0` complete, `1` cancel, `2` fail/busy); rejected
    pre-ack requests return only `2`;
  - `XDSI` + repeated little-endian `u32 length + URI`, maximum 4096 bytes;
  - Xodus socket mode `0600` and same-UID peer credential authorization.
- The XDUI/XDSI social bridge never owns Xbox HTTP/auth behavior and never
  carries tokens, XUIDs, or raw user handles; existing XUser authentication
  remains outside this bridge's wire contract.
- Invite activation URIs are sensitive: validate, bound, never log, never put
  in public evidence.
- The user-supplied `xgameruntime.dll.threading` is an external legal
  prerequisite. Never copy, hash into public fixtures, modify, or distribute it.
- No GNOME Keyring dependency, compositor command, polling/RTA path, Microsoft
  binary, or package installation is added.
- Existing dirty `wine-forza`, live runtime, prefix, Steam options, service,
  and game remain untouched until the installed-artifact gate.

## Task -1 — Make XDUI semantics explicit in an isolated Xodus branch

The payload-free XDUI contract cannot truthfully implement both the
session-specific legacy invite API and the Multiplayer Activity API, and the
current full social overlay can report a Join action as a successful invite
UI. Reuse only the existing isolated Xodus worktree
`<integration-source>/.worktrees/xodus-social` on
`feature/forza-social-current`. Before edits, require a clean tree at exactly
`207a721e6a1cc95be04ffaeedc67a4d673d45ca2`; otherwise stop. Do not edit the
live checkout under `~/.local/share/forza-motorsport-linux/xodus`.

Change only the scoped files needed from this list:

- `crates/xodus-service/src/connection/router.rs`
- `crates/xodus-service/src/social_ui.rs`
- `crates/xodus-overlay/src/main.rs`
- `crates/xodus-overlay/src/lib.rs`
- `crates/xodus-overlay/src/action.rs`
- `crates/xodus-overlay/src/controller.rs`
- `crates/xodus-overlay/src/model.rs`
- `crates/xodus-overlay/src/ui.rs`
- `crates/xodus-overlay/tests/overlay.rs`
- `docs/forza-social.md`

Extend XDUI with this bounded, versioned request after the magic:

```text
u8 version = 1
u8 mode      # 0 full social, 1 Multiplayer Activity invite-only
```

The service reads both request bytes under a bounded request-read deadline. It
validates mode/capacity/path, spawns the overlay with only a fixed non-secret
mode argument, then writes acknowledgement byte `0xa1`. After acknowledgement,
the socket has no total response deadline: the terminal byte is written only
after the user acts, the overlay exits, the client cancels/disconnects, or the
service shuts down. Pre-ack rejection writes `2` and closes without launching a
child. Mode `0` is full social; mode `1` hides/disables Join and may send
terminal status `0` only after Invite. Unknown versions/modes return `2`
without launching a child. Any acknowledgement-write failure terminates and
reaps the just-spawned child before returning.

Version 1 is an intentional breaking cutover, not an extension of the old
payload-free exchange. Test old-client/new-service failure (`XDUI` alone times
out and receives `2`) and new-client/old-service failure (a terminal `0/1/2`
is not acknowledgement `0xa1`, so the client closes and the old service reaps
its child). Add service protocol/launcher-argv tests and overlay
model/render/keyboard/controller tests proving Join is impossible in mode `1`.
Write those tests first and observe failure with:

```bash
cargo test -p xodus-service --offline
cargo test -p xodus-overlay --offline
```

After implementation, run:

```bash
cargo fmt --check
cargo clippy -p xodus-social-protocol -p xodus -p xodus-service -p xodus-overlay --all-targets --offline -- -D warnings
cargo test -p xodus-service --offline
cargo test -p xodus-overlay --offline
cargo test --workspace --offline -- --skip test_get_xbox_live_dev_token
cargo build --release -p xodus-service -p xodus-cli -p xodus-overlay --offline
! cargo tree -p xodus-overlay --offline | rg 'keyring|secret-service'
! rg -n 'use xodus::|xodus::' crates/xodus-overlay
git diff --check
```

Commit as `feat: version the invite-only social overlay request`, record the
resulting full SHA as `<xodus-v1-sha>` in `docs/verification.md`, and make every
later build/install/evidence step consume that SHA. Do not implement the legacy
session-specific GDK invite API through this protocol.

## Task 0 — Isolated base and reproducible baseline

Let `<integration-source>` be the absolute integration worktree and
`<isolated-root>` be its absolute, user-owned `.worktrees` directory. Create:

- WineGDK source worktree: `<winegdk-source>` = `<isolated-root>/winegdk-xgameruntime`
- branch: `feature/xodus-social-invite-bridge`
- exact base: `b03ba49c4f326c36aa6930fbe6cf72841ef3738c`
- fresh build directory: `<winegdk-build>` = `<isolated-root>/winegdk-xgameruntime-build`
- evidence: `<integration-source>/docs/evidence/xgameruntime-baseline.md`

The worktree command is owned by the clean Weather-OS/WineGDK checkout and uses
`git -C <winegdk-owner> worktree add`; all later Git commands use
`git -C <winegdk-source>`. Task 0 consumes the clean committed
`<xodus-v1-sha>`, not `207a721e6a1cc95be04ffaeedc67a4d673d45ca2`.

Record the full base SHA/remotes, compiler/configure versions, exact configure
command, current xgameruntime source list, generated-header dependencies,
existing Unix-call IDs `0..2`, expected artifact names, and baseline test
state. The current test only initializes COM, so its success is explicitly
vacuous. Before feature behavior, add focused prerequisite tests for
`QueryApiImpl` XThreading resolution, the open-source fallback XAsync lifecycle,
Unixlib loading failure, and current main-stream Ping framing. Configure a fresh
Win64 build and build only xgameruntime. Do not reuse controller/storage builds.

Commit evidence separately in the integration repository:

```text
docs: record the xgameruntime bridge base
```

## Task 0.5 — Repair and prove the prerequisite XUser/main-IPC lifetime

The base cannot safely support `requestingUser` or final runtime teardown as-is:
XUser query/add/result/duplicate/close are incomplete, and the main Xodus poll
action is discarded. Before social behavior, change and test only the required
parts of:

- `dlls/xgameruntime/main.c`
- `dlls/xgameruntime/private.h`
- `dlls/xgameruntime/GDKComponent/InitInternalGDKC.cpp`
- `dlls/xgameruntime/GDKComponent/System/XUser.cpp`
- `dlls/xgameruntime/GDKComponent/System/User/UserImpl.cpp`
- `dlls/xgameruntime/GDKComponent/System/User/UserImpl.h`
- `dlls/xgameruntime/GDKComponent/Xodus/IPCLayer.cpp`
- `dlls/xgameruntime/GDKComponent/Xodus/IPCLayer.h`
- `dlls/xgameruntime/GDKComponent/Xodus/Unix/socket.c`
- `dlls/xgameruntime/WineCoreUAP/Foundation/IWineAsync.cpp`
- `dlls/xgameruntime/WineCoreUAP/Foundation/IWineAsync.hpp`
- `dlls/xgameruntime/tests/xgameruntime.c`

Expose the canonical XUser class/interface through `QueryApiImpl`; make
`XUserAddAsync/Result` run through the selected IXThreading backend and return a
heap-owned registered handle rather than a stack address; implement duplicate
and close with balanced ownership; and keep caller-visible handle addresses in
a locked registry so validation never dereferences an unregistered pointer.
Handles store the current initialization epoch and main-Xodus-connection
generation. They retain their IUser object, never expose/log its token, and are
counted as active runtime objects until the final duplicate closes.

Retain the `IAsyncAction` returned by the main `InitializeSocket` operation.
There is deliberately no within-epoch main-stream reconnect: a natural
disconnect atomically marks that generation failed, rejects later main/social
operations and handles from that generation, and requires balanced final
uninitialize/reinitialize for a fresh generation. Lock the main response
callback registry, reject new sends once stop/failure begins, and count pending
send operations as active objects. Lock this complete append-only Unix call
order before social work:

```text
0 conn_socket             # existing
1 poll_socket             # existing
2 send_frame              # existing
3 request_main_stop       # shutdown only
4 close_main_socket       # current owner closes exactly once
5 invite_connect
6 invite_poll
7 invite_close
8 social_ui_start
9 social_ui_wait
10 social_ui_cancel
```

Repair the base `AsyncAction::Create` null-dereference by separating
`AsyncInfo::Create` failure from cleanup that requires a non-null `info`. Add a
narrow internal gated-create helper with an explicit submitted-state contract:
failure before `SubmitThreadpoolWork` means no callback can run, and no
production fallible step follows submission. A test-only fault point immediately
after submission exercises cleanup for a hypothetical post-submit failure.

Also repair the base async ownership cycle, not only the main caller. Before
submission, `AsyncInfo::Start` takes one worker self-reference and one outer
operation reference. The callback owns both until a single all-path cleanup
epilogue: after result/status/handler completion it releases the outer reference
exactly once, then releases the worker self-reference as its final action.
`AsyncAction` and every instantiated `AsyncOperation<T>` destructor release
their owned `info` reference exactly once. `AsyncInfo` destruction releases its
invoker, retained handler/error information/result, events, and threadpool work
exactly once; no destructor waits on its own callback. Initialization failure,
normal success, error, and cancellation all converge on this ownership path, so
no outer/info cycle survives final runtime teardown.

Descriptor ownership is explicit on every path. The gated helper receives a
refcounted worker context with start, cancel, ready, and done events, a closed
start gate, and no descriptor; creator and reserved worker references are
tracked separately. On pre-submit failure, it releases both reserved references
and all events without waiting because no worker exists. Even if submitted work
runs inline/immediately, the worker cannot connect through the gate. After
successful Create, initialization retains the action, opens the gate, and
releases the creator reference. On injected post-submit failure, the helper
signals cancel, waits for done, releases the action/context safely, and only then
returns failure. Thus failed first initialization leaves no thread executing
unloadable DLL code.

The worker alone invokes a bounded, cancel-aware `conn_socket`. Replace the
base's blocking/leaking connect with a nonblocking connect plus bounded poll
slices that observe the main cancel flag; `request_main_stop` sets that flag and
performs `shutdown` to wake the poll. Store `-1`, never `0`, as the invalid
descriptor. Socket creation failure owns nothing; every later timeout, refusal,
or cancellation returns to the worker with ownership of the created descriptor,
which it closes once. A successful connect gives the worker sole descriptor
ownership. It signals a separate bounded ready/failure event before
initialization sends Ping. Any worker-side connect/setup failure before the
first `poll_socket`, plus every later shutdown/disconnect terminal path, invokes
`close_main_socket` itself when and only when it acquired a descriptor. The stop
operation performs `shutdown` only; the worker sets the stored descriptor
invalid under lock before its exactly-once close, drains/releases response
callbacks, completes the retained action, and becomes joinable.
Initialization/readiness/Ping failure opens/cancels the gate as needed and uses
the same stop/join path before Unixlib unload.

Tests first prove the base failures, then cover canonical XUser query,
AddAsync/Result, exact provider/result identity, duplicate/close, foreign and
closed handles, current/prior initialization epochs, main generation failure,
disconnect without reconnect, stop/unblock/join, Ping/init failure cleanup, and
injected immediate worker execution before Create returns, pre-submit
`CreateThreadpoolWork` failure, post-submit failure with cancel/done/context
release, bounded connect timeout/refusal, in-progress connect cancellation and
join, readiness and pre-first-poll failures, failed-connect exactly-once close,
balanced outer/info/invoker/handler references after success/error/cancel, and
threadpool/event/resource-baseline equality after final teardown and repeated
reinitialize. Commit this prerequisite separately:

```text
xgameruntime: own user handles and main IPC lifetime
```

## Task 1 — Harden and extend the Unix transport

Primary files:

- `dlls/xgameruntime/private.h`
- `dlls/xgameruntime/GDKComponent/Xodus/Unix/socket.c`
- `dlls/xgameruntime/GDKComponent/Xodus/IPCLayer.cpp`
- `dlls/xgameruntime/GDKComponent/InitInternalGDKC.cpp`
- small shared/native-testable transport helpers as required
- `dlls/xgameruntime/tests/` synthetic fixture/harness files

Requirements:

1. Preserve the complete Task-0.5 Unix call order `0..10`; Task 1 implements
   only the already-reserved typed XDSI `5..7` and XDUI `8..10` operations.
   Use one shared enum/argument layout and never renumber the main operations.
2. `WINEGDK_XODUS_SOCKET`, when nonempty, is the full explicit socket path;
   otherwise resolve `$XDG_RUNTIME_DIR/xodus.sock`. Reject missing runtime,
   overlong paths, embedded truncation, non-sockets, non-owner UID, and socket
   permissions that expose group/other access. Test fixtures use a same-UID
   mode-`0600` Unix socket.
3. Use independent descriptors for the existing main protocol, XDSI, and each
   XDUI operation. Never share/interleave these streams.
4. Implement EINTR-safe `read_all`/`write_all`, explicit little-endian lengths,
   zero-byte disconnect handling, 4096-byte URI limit, `SOCK_CLOEXEC` with a
   tested `FD_CLOEXEC` fallback. XDSI/XDUI owners close their own descriptors on
   every error. A main-stream send error atomically marks that generation failed
   and requests shutdown; only the gated main worker invokes
   `close_main_socket`. Test send-error/cancel/poll races and exactly one main
   close. Bound XDUI/XDSI connect, request write, and acknowledgement read; the
   main connect bound/cancellation is already mandatory in Task 0.5. Reconnect
   attempts apply only to the persistent XDSI subscription while the main generation remains healthy;
   XDUI always starts a fresh descriptor, and a failed main stream atomically
   cancels/joins XDSI and remains failed until balanced
   uninitialize/reinitialize. No XDSI reconnect or callback is permitted after
   main-generation failure.
   Do not impose a total timeout on a human keeping the overlay open.
5. XDUI must be cancellable: an operation ID owns its descriptor; cancel
   atomically marks cancellation and performs `shutdown` only. The waiter is the
   sole closer after wakeup. A locked operation state prevents a late cancel
   from shutting down a reused descriptor number; test cancel/wait/close and
   descriptor-reuse races.
6. XDSI receives only the expected scheme/host/query shape before the PE side
   sees the URI. No path, URI, protocol body, or credential is logged. Remove
   existing body logging from the touched transport.
7. Build a hermetic native fixture/harness that exercises fragmented I/O,
   acknowledgement `0xa1`, terminal status `0/1/2`, old/new cutover, bad
   peer/path/mode, oversize, disconnect, pre-ack timeout, cancellation,
   descriptor isolation, and cleanup. It must not contact Xodus or Microsoft.
8. Add one writer mutex around the pre-existing main Xodus stream and a
   concurrent-frame fixture test; the social descriptors do not excuse
   interleaving on call IDs `0..2`.

Commit:

```text
xgameruntime: add bounded Xodus social transport
```

## Task 2 — Implement XGameUi with verified ABI and XAsync lifecycle

Import only the LGPL canonical XGameUi IDL/stub surface from exact commit
`64aebcabb8c66121eae25d3bf0ace4b582ebb0da`, preserving authorship, license,
and the reserved/unknown vtable slots required to reach the documented methods.
Use only `xodus-gaming/xgameruntime-docs` exact commit
`9383d4c3502122a727491e7a28532c4d43506a83` to preserve ABI layout; do not infer
semantics for reserved slots or copy the dirty prototype's
class-ID-as-interface shortcut. The task file list includes `xgameui.idl`,
`xgameui.c`, `Makefile.in`, `private.h`, `main.c`, and the narrowly required
`GDKComponent/System/XUser.cpp` plus `GDKComponent/System/User/UserImpl.*`
handle-registry/lifetime changes.

Implement only `XGameUiShowMultiplayerActivityGameInviteAsync/Result` through
XDUI invite-only mode. Keep `XGameUiShowSendGameInviteAsync/Result` explicitly
`E_NOTIMPL`; its session configuration/template/session ID contract is not
represented by Xodus Multiplayer Activity and must not be silently discarded.

The implementation uses one owned provider context and resolves/retains
IXThreading through this DLL's `QueryApiImpl(CLSID_XThreadingImpl, ...)`, so it
uses exactly the same fallback or external threading backend visible to the
game. Never directly mix the local `x_threading_impl` state with an external
`xgameruntime.dll.threading` backend:

- null/invalid arguments fail synchronously;
- `requestingUser` is accepted only when its pointer is present in the
  runtime-owned XUser handle registry for the current successful initialization
  epoch and current main-Xodus-connection generation. Validation checks registry
  membership before dereferencing, retains the handle for the provider lifetime,
  and rejects foreign, stale, closed, prior-epoch, or disconnected-generation handles with
  `E_GAMERUNTIME_INVALID_HANDLE`. The accepted handle was issued by this runtime
  through the same single-user Xodus service connection, so XDUI acts only as
  that service session's current title user; the social bridge sends no XUID,
  token, or handle value over the socket. Repair the base's XUser handle
  lifetime/duplicate/close registry only as needed to make this invariant real,
  and count issued handles as active runtime objects;
- Begin is nonblocking and schedules exactly once;
- DoWork starts XDUI version 1 invite-only mode, requires acknowledgement
  `0xa1` within the pre-ack deadline, then waits without a total deadline for
  exactly one terminal status;
- Cancel interrupts the Unix operation;
- status `0` → `S_OK`, `1` → `E_ABORT`, `2`/transport failure → `E_FAIL`;
- completion callback occurs exactly once;
- successful DoWork calls `XAsyncComplete(async, S_OK, 1)`. The provider's
  `XAsyncOp_GetResult` requires the exact static Multiplayer Activity provider
  identity, a non-null buffer of at least one byte, and writes a fixed internal
  sentinel. The public Result method allocates a one-byte local buffer and calls
  `XAsyncGetResult(async, exact_identity, 1, &sentinel, NULL)`, then verifies the
  sentinel. This preserves provider identity/state consumption although the
  public API exposes no payload. A prerequisite test must prove this exact
  Complete/GetResult lifecycle and double-result rejection on the fallback backend;
  the external backend remains a separate installed/manual gate because its
  binary is not redistributable.

Query tests must use the actual canonical class and interface IIDs and call the
real vtable slots. Add success, user cancel, operation cancel, missing service,
pre-ack timeout, double-result, exact-provider mismatch, short/null result
buffer, backend-resolution, valid retained requesting user, foreign/stale/closed
requesting user, and shutdown tests against the fixture. No real UI is launched
and no total human-interaction timeout is asserted.

Commit:

```text
xgameui: open the Xodus social picker
```

## Task 3 — Implement safe XGameInvite activation delivery

Import only the canonical LGPL XGameInvite IDL/stub surface from
`64aebcabb8c66121eae25d3bf0ace4b582ebb0da`; this is a deprecated compatibility
path for Forza, not a recommended general invite API. Build an owned dynamic
registration manager without the prototype-derived four-slot cap:

- monotonically increasing nonzero tokens and allocation-failure rollback;
- duplicate supplied task-queue handles and close them exactly once;
- one cancelable/joinable XDSI worker, started exactly once when needed,
  stopped when no registrations remain and during runtime unload;
- bounded reconnect with cancellation after an isolated XDSI disconnect only
  while the main generation is healthy (no busy loop). A service/main-stream
  restart fails the generation, cancels/joins XDSI, and delivers no later
  callback until balanced runtime reinitialization;
- snapshot/refcount callback targets outside the registry lock; never invoke
  client code while holding it;
- resolve all queue operations through the same retained IXThreading backend as
  the game. A null queue uses the process task queue or fails with
  `E_NO_TASK_QUEUE`; never invoke a null-queue callback synchronously on the
  socket worker;
- track in-flight callbacks and implement the documented unregister Boolean:
  `wait=FALSE` reports false while removal is pending and true when already
  drained. For self-unregister, remove the registration immediately, exclude
  the currently executing callback from `wait=TRUE`'s wait condition, defer only
  the runtime-owned registration record and duplicated queue-handle reclamation
  to the callback epilogue, and return true; no later callback may start. The
  opaque caller context is never freed or modified. Roll back all
  slot/token/queue ownership if startup fails;
- validate URI again before allocating/callback delivery; never log it.

Tests cover null arguments, multiple registrations/allocation failure, supplied
and process-default queue paths, missing process queue, fragmentation,
unregister-before-delivery, exact `wait=FALSE`/`wait=TRUE`, self-unregister,
caller-context preservation, callback race, isolated-XDSI reconnect,
main-generation failure with no reconnect/callback, zero-registration worker
stop, DLL/runtime shutdown, and post-unregister exclusion.

Add an explicit, locked runtime state machine in `main.c` with states
`STOPPED`, `STARTING`, `RUNNING`, and `STOPPING`, a successful-initialization
reference count, an initialization epoch, and an active-object count.

- Each successful `InitializeApiImpl*` call owns one reference. The first
  `0 -> 1` transition loads/retains Unixlib and the selected IXThreading backend,
  initializes the component, and only then commits `RUNNING`; any failure rolls
  back every acquired resource and leaves `STOPPED` with count zero. Later
  compatible initializes increment the count. An incompatible later options set
  returns `E_GAMERUNTIME_OPTIONS_MISMATCH` without changing state/count.
  Reinitialize after a complete final teardown starts a new epoch; stale XUser
  handles are rejected.
- `QueryApiImpl` and new public work reject `STARTING`, `STOPPING`, `STOPPED`, or
  a failed main-connection generation with the appropriate initialized/failure
  HRESULT; they never observe half-published dependency pointers.
- `UninitializeApiImpl` at count zero returns
  `E_GAMERUNTIME_NOT_INITIALIZED`. At count greater than one it decrements and
  returns without touching shared state. At the final reference, any public
  XUser handle, provider, registration, queued/running callback, or
  registration-owned XDSI worker
  returns `E_GAMERUNTIME_UNINITIALIZE_ACTIVEOBJECTS` and preserves `RUNNING`
  and the last reference. Only with zero active objects may it transition to
  `STOPPING`. It requests main-socket shutdown, waits for the retained main poll
  action to close its descriptor and finish, drains/releases main response
  callbacks, verifies the XDSI/XDUI registries are empty, resets
  `InitInternalGDKC` component/config state, releases queues/threading, and only
  then unloads Unixlib, sets the count to zero, and returns to `STOPPED`.
  The always-internal main poll worker does not itself make uninitialize fail;
  final teardown owns its stop/join sequence.
- The first successful initialization permanently pins this DLL for the process
  with `GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS |
  GET_MODULE_HANDLE_EX_FLAG_PIN, ...)`. This deliberately has no unpin path;
  final uninitialize releases runtime resources but the code remains mapped
  until process exit, making an explicit later `FreeLibrary` harmless.
- `DllMain` only disables thread notifications on process attach. It never loads
  or frees the threading DLL/Unixlib and never joins, waits, drains, or acquires
  runtime locks under loader lock. Process termination relies on OS cleanup.
- `DllCanUnloadNow` returns `S_FALSE` while initialization references or active
  objects exist and `S_OK` after final resource teardown, independent of the
  deliberate process-lifetime module pin.

Tests cover two independent initialize owners, one-owner uninitialize, failed
first-init rollback, active-object refusal, balanced final uninitialize,
reinitialization/new epoch, stale-handle rejection, `DllCanUnloadNow`, explicit
`FreeLibrary` after pinning, main disconnect/join before Unixlib unload, and
process termination without loader-lock work.

Commit:

```text
xgameinvite: deliver Xodus invite activations
```

## Task 4 — Source integration and evidence

Use one hermetic fixture-owning command for PE integration tests:

```bash
uv run python <winegdk-source>/dlls/xgameruntime/tests/run_social_fixture.py -- \
  make -C <winegdk-build>/dlls/xgameruntime/tests test
```

The Python standard-library fixture creates a private temporary directory,
binds a same-UID mode-`0600` socket, waits for readiness before spawning the
test command, serves a deterministic per-test script, and always terminates the
server/removes the directory. It passes only `WINEGDK_XODUS_SOCKET` to the
child. Native transport helpers run through a separate standard-library
`unittest` command documented in their directory; no PyPI dependency or real
Xodus service is used.

Then run from the isolated build/source explicitly:

```bash
make -C <winegdk-build>/dlls/xgameruntime clean
make -C <winegdk-build>/dlls/xgameruntime
uv run python <winegdk-source>/dlls/xgameruntime/tests/run_social_fixture.py -- make -C <winegdk-build>/dlls/xgameruntime/tests test
git -C <winegdk-source> diff --check
```

Run the native transport fixture separately through `uv`. Record exact test
counts, build artifacts, SHA-256 values, source Git revisions, and any Wine
bootstrap limitation. Record the exact ten inputs that Task 4.5's
`lock-evidence` operation will bind; do not hand-write a private manifest. Add
`patches/xgameruntime/README.md` and update `docs/verification.md` without live
identities or URI values. Final source review must cover ABI, cancellation,
callback lifetime, Unix descriptor ownership, privacy, and clean worktrees.

## Task 4.5 — Add one recoverable runtime-component transaction

Implementation amendment after source and live-layout review: use one outer journal with all eight allowlisted user payloads and the updated user install manifest as ordered records, instead of introducing a nested `secure-user-files` upgrade receipt and second recovery state machine. The launcher is published first: before that rename no functional component is changed, and after it unfinished state is launch-blocking. Targeted tests run the existing secure `check` and `uninstall` operations against the accepted result. A missing manifest with existing allowlisted files requires explicit, digest-bound `--adopt-unmanaged` approval. This preserves ordinary ownership/uninstall behavior while giving every user payload, manifest, and Wine rename one recovery intent in the same journal. This amendment supersedes the nested `user_upgrade`/receipt design; all no-follow, staging, lock, drift, retention, and manual-acceptance requirements remain in force.

Before any live installation, implement and test
`<integration-source>/scripts/install-runtime-components` with standard-library
Python tests in `<integration-source>/tests/test_runtime_components.py`. Extend
`scripts/secure-user-files`, `scripts/install-user`,
`tests/test_installation.bash`, the launcher's exclusive lock/recovery gate,
`tests/test_launcher.bash`, and `justfile` rather than bypassing the existing
user-install ownership manifest. Its CLI
accepts only absolute `--compat-tool-root`, `--user-root`,
`--xgameruntime-build-dir`, `--xodus-build-dir`, and
`--artifact-evidence-manifest` inputs. It has `lock-evidence`, `plan`, `install`,
`status`, `rollback`, `accept`, and `restore-runtime` operations; `plan` is
read-only and `install` requires the exact plan digest. `lock-evidence` verifies
the integration, WineGDK, and Xodus worktrees are clean, with WineGDK/Xodus at
the exact reviewed SHAs, opens all ten artifacts no-follow, rejects unsafe modes, and writes the private
mode-`0600` evidence manifest below the mode-`0700` transaction state root. It
contains revisions, role-relative source paths, fixed target modes, and artifact
hashes but no credential or invite value. The private evidence manifest, not
untrusted CLI revision strings, locks the inputs used by `plan`.

The only logical artifact roles and destinations are:

| Role | Source below reviewed build | Destination below approved root |
| --- | --- | --- |
| `launcher-gate` | `bin/forza-linux` | `<user-root>/.local/bin/forza-linux` |
| `doctor` | `bin/forza-doctor` | `<user-root>/.local/bin/forza-doctor` |
| `known-build-patcher` | `scripts/patch-known-build` | `<user-root>/.local/libexec/forza-motorsport-linux/patch-known-build` |
| `supported-builds` | `manifests/supported-builds.toml` | `<user-root>/.local/share/forza-motorsport-linux/supported-builds.toml` |
| `xgameruntime-pe64` | `dlls/xgameruntime/x86_64-windows/xgameruntime.dll` | `<compat-tool-root>/files/lib/wine/x86_64-windows/xgameruntime.dll` |
| `xgameruntime-unix64` | `dlls/xgameruntime/xgameruntime.so` | `<compat-tool-root>/files/lib/wine/x86_64-unix/xgameruntime.so` |
| `xodus-service` | `xodus-service` | `<user-root>/.local/libexec/xodus-forza/xodus-service` |
| `xodus-cli` | `xodus-cli` | `<user-root>/.local/libexec/xodus-forza/xodus-cli` |
| `xodus-overlay` | `xodus-overlay` | `<user-root>/.local/libexec/xodus-forza/xodus-overlay` |
| `systemd-unit` | `config/xodus-forza.service` | `<user-root>/.config/systemd/user/xodus-forza.service` |

Executable roles have fixed installed mode `0755`; `supported-builds` and
`systemd-unit` have fixed mode `0644`. The private evidence manifest and plan
digest include each target mode beside the artifact hash. Reject sources or
plans with setuid, setgid, sticky, group-write, or other-write bits, reject any
target-mode drift, and record/verify installed mode as well as hash.

No prefix DLL, default-prefix file, 32-bit artifact, or
`xgameruntime.dll.threading` is in the allowlist. The tool refuses a symlink,
non-regular source/target, wrong owner, unsafe parent, wrong build revision,
unknown role/path, active launcher lock, active `xodus-forza.service`, or any
changed plan input. It walks roots/parents with directory file descriptors and
`O_DIRECTORY|O_NOFOLLOW`, opens files with `O_NOFOLLOW`, and never authorizes a
destination from an unresolved string or glob.

All eight user destinations remain owned by the existing
`forza-motorsport-linux-install-v1` manifest. The outer journal records those
eight payloads, the manifest, and both WineGDK files directly. It requires an
exact valid existing manifest or empty destinations; a reviewed legacy layout
without a manifest is accepted only when both `plan` and `install` use
`--adopt-unmanaged`. The decision and every legacy before-state are bound by the
plan digest. The resulting accepted install must pass ordinary secure `check`
and `uninstall` operations. This is one recoverable umbrella transaction, not a
claim of cross-root atomicity.

`install`, `rollback`, and `restore-runtime` acquire the same exclusive
`$XDG_RUNTIME_DIR/forza-linux.lock` used by the launcher before service/process
checks and hold it through journal and filesystem work. After acquiring it,
each mutating operation verifies the game is absent and
`xodus-forza.service` inactive immediately before its first mutation; otherwise
it releases the lock unchanged. They never stop a process or service themselves.
The launcher performs one read-only transaction state check after taking that
lock and refuses to start while any journal is
`prepared`, `installing`, `rolling_back`, or `recovery_required`; this is an
event-time safety gate, not polling.

The private mode-`0600` JSON journal lives below a mode-`0700`
`<user-root>/.local/state/forza-motorsport-linux/runtime-transactions/<id>/` and
has this versioned logical schema:

```text
version, transaction_id, state, plan_sha256, evidence_manifest_sha256
source_revisions: {integration_git_sha, winegdk_git_sha, xodus_git_sha}
artifacts[]:
  role, source_sha256, target_mode, destination_root, destination_relative,
  before: {present, type, mode, uid, gid, sha256},
  stage_name, backup_name, installed_mode, installed_sha256, phase
```

Record Git source revisions as revision strings only. SHA-256 parity means each
built artifact's bytes equal its installed counterpart; never compare a Git SHA
to an artifact SHA-256.

All ten reviewed sources and destinations are validated and copied to exclusive
same-directory staging files before the first destination changes. Each
staging file receives its fixed mode, is fsynced, and has its mode/hash
rechecked.
For each of the eleven user-payload, manifest, and Wine records in fixed order, atomically rename an existing
destination to a unique adjacent backup, fsync/journal, atomically rename the
stage into place, fsync/journal, then verify the installed hash. Thus every
rename stays on one filesystem. The umbrella journal records intent before and
completion after every phase, so an interruption is
recoverable; no cross-root atomicity is claimed.

On any failure, rollback walks every selected record in reverse. It restores only when
the current destination still matches the journaled installed hash. Unexpected
material is moved to an adjacent transaction recovery name, never overwritten
or deleted. A previously absent destination is restored to absence by moving
the matching installed file into recovery. Restored type/mode/uid/gid/hash are
verified. Adjacent backups and the private journal remain until a separate,
explicit later cleanup after manual acceptance; cleanup is outside this plan.
Any unresolved artifact or ownership-manifest mismatch marks
`recovery_required` and blocks live launch. `accept` marks the tested transaction
accepted but retains backups. A later `restore-runtime` restores the original
WineGDK pair after exact-hash checks while leaving the accepted user files under
the normal install manifest; `uninstall-user` then handles those files normally.
Full pre-accept `rollback` restores all prior user files, their manifest, and the
WineGDK pair. Publishing or restoring the systemd unit requires a successful
user-manager reload before the recovery-aware launcher can be removed.

Write the tests first and observe RED with:

```bash
uv run pytest tests/test_runtime_components.py -q
bash tests/test_installation.bash
bash tests/test_launcher.bash
```

After implementation, run those commands again, then `just verify`. Add the new
extensionless Python script to the existing Ruff command in `justfile`; add no
new package or network dependency.

Tests cover allowlist enforcement, no-follow traversal,
source/destination swaps, plan-digest binding, all-before-any preflight,
same-filesystem staging, every interruption boundary, reverse rollback,
unexpected destination preservation, absent-original rollback, mode/hash
restoration, idempotent recovery, launcher-lock exclusion, launcher refusal for
unfinished/recovery-required state, evidence-manifest binding,
upgrade→status→install-user-check→uninstall, rollback→old-manifest restoration,
accepted WineGDK `restore-runtime`, concurrent-launch refusal during restore,
source/plan/installed mode drift, and source-revision versus artifact-hash
separation. Commit the reviewed tool separately as:

```text
feat: add recoverable runtime component installation
```

## Task 5 — Installed-artifact and manual game gate

Only after source review and Task 4.5 tests/review, use that tool's single
private transaction covering the WineGDK DLL/Unixlib pair and the exact
`<xodus-v1-sha>` service/CLI/overlay artifacts:

1. keep the game closed and require the launcher-owned service inactive;
2. run `plan`, review the ten resolved source roles, source revisions,
   before-state metadata, and plan digest, then run `install` with that digest;
3. prove each build-artifact SHA-256 equals its installed counterpart and record
   the WineGDK/Xodus Git revisions separately;
4. confirm the prefix and user-supplied threading DLL were untouched;
5. start via the reviewed launcher, which owns service start/stop;
6. run keyboard and Xbox-controller UI, cancel, outbound invite, join to a
   compatible remote activity, service cleanup, and game exit;
7. record automatic incoming notification as `NOT SUPPORTED`;
8. only after every claimed row passes, run `accept`; keep its verified backups
   and journal for later `restore-runtime`/recovery.

After any failed live step, first request normal game exit and let the reviewed
launcher perform its owned service cleanup. Verify the game is gone, the shared
launcher lock can be acquired, and `xodus-forza.service` is inactive; only then
run rollback and verify all eleven restored records. If orderly shutdown cannot complete, preserve the journal, perform no
filesystem mutation, mark/block the next launch, and report manual recovery
required. A partial rollback or `recovery_required` is a failure, not success.
No public claim is made until exact installed hashes and the manual matrix pass.

## Publication ruling

The eventual public deliverable may be a clearly labeled AI-assisted
experimental WineGDK branch/repository plus reproducible evidence. The
maintainer's PR 10 rejection of generated code is the reason for this project's
no-PR decision; do not open an upstream xodus-gaming/xgameruntime code PR or
imply clean-room eligibility.
Upstream issue coordination, if any, must describe behavior/protocol evidence
and ask for architecture direction without offering the generated branch for
merge.
