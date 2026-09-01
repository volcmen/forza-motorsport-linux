# XGameRuntime WineGDK Bridge — Preflight Amendment

> This amendment supersedes the source-base and build assumptions in
> `2026-08-31-xgameruntime-invite-bridge.md`. The protocol goals remain the
> same. The original plan remains as historical design evidence.

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
  `b03ba49c4f3` is the only inspected base that already combines the working
  XThreading implementation, Wine Unixlib loading, Xodus main-socket transport,
  Wine build integration, and a test target. The prior game prototype was built
  from this family, but its dirty source has lifecycle, ABI, timeout, and test
  defects and is reference-only.

Therefore the installable Linux implementation will be re-derived on a clean
WineGDK base. It is an experimental user-facing branch, not a proposed
`xodus-gaming/xgameruntime` upstream PR. The latter project's maintainer has an
explicit clean-room/human-authorship policy that this AI-assisted work must not
misrepresent as eligible upstream code.

## Fixed boundaries

- Xodus contracts are the reviewed local branch at `207a721e6a1cc95be04ffaeedc67a4d673d45ca2`:
  - `XDUI` + exactly one status byte (`0` complete, `1` cancel, `2` fail/busy);
  - `XDSI` + repeated little-endian `u32 length + URI`, maximum 4096 bytes;
  - Xodus socket mode `0600` and same-UID peer credential authorization.
- The runtime never owns Xbox HTTP/auth behavior and never receives tokens or
  XUIDs.
- Invite activation URIs are sensitive: validate, bound, never log, never put
  in public evidence.
- The user-supplied `xgameruntime.dll.threading` is an external legal
  prerequisite. Never copy, hash into public fixtures, modify, or distribute it.
- No GNOME Keyring dependency, compositor command, polling/RTA path, Microsoft
  binary, or package installation is added.
- Existing dirty `wine-forza`, live runtime, prefix, Steam options, service,
  and game remain untouched until the installed-artifact gate.

## Task 0 — Isolated base and reproducible baseline

Create:

- WineGDK worktree: `.worktrees/winegdk-xgameruntime`
- branch: `feature/xodus-social-invite-bridge`
- exact base: `b03ba49c4f3`
- fresh build directory: `.worktrees/winegdk-xgameruntime-build`
- evidence: `docs/evidence/xgameruntime-baseline.md`

Record the full base SHA/remotes, compiler/configure versions, current
xgameruntime source list, existing Unix-call IDs `0..2`, and baseline test
state. Configure a fresh Win64 build and build only xgameruntime before feature
edits. Do not reuse the controller/storage builds.

Commit evidence separately in the integration repository:

```text
docs: record the xgameruntime bridge base
```

## Task 1 — Harden and extend the Unix transport

Primary files:

- `dlls/xgameruntime/private.h`
- `dlls/xgameruntime/GDKComponent/Xodus/Unix/socket.c`
- small shared/native-testable transport helpers as required
- `dlls/xgameruntime/tests/` synthetic fixture/harness files

Requirements:

1. Preserve existing Unix call numbers `conn_socket`, `poll_socket`, and
   `send_frame`; append typed invite-connect/poll/close and social-UI
   start/wait/cancel operations. Use one shared enum/argument layout.
2. `WINEGDK_XODUS_SOCKET`, when nonempty, is the full explicit socket path;
   otherwise resolve `$XDG_RUNTIME_DIR/xodus.sock`. Reject missing runtime,
   overlong paths, embedded truncation, non-sockets, non-owner UID, and socket
   permissions that expose group/other access. Test fixtures use a same-UID
   mode-`0600` Unix socket.
3. Use independent descriptors for the existing main protocol, XDSI, and each
   XDUI operation. Never share/interleave these streams.
4. Implement EINTR-safe `read_all`/`write_all`, explicit little-endian lengths,
   zero-byte disconnect handling, 4096-byte URI limit, bounded poll deadlines,
   and close-on-every-error.
5. XDUI must be cancellable: an operation ID owns its descriptor; cancel uses
   `shutdown()`/close to wake the waiter; each descriptor is reaped exactly once.
6. XDSI receives only the expected scheme/host/query shape before the PE side
   sees the URI. No path, URI, protocol body, or credential is logged. Remove
   existing body logging from the touched transport.
7. Build a hermetic native fixture/harness that exercises fragmented I/O,
   status `0/1/2`, bad peer/path/mode, oversize, disconnect, timeout,
   cancellation, descriptor isolation, and cleanup. It must not contact Xodus
   or Microsoft.

Commit:

```text
xgameruntime: add bounded Xodus social transport
```

## Task 2 — Implement XGameUi with verified ABI and XAsync lifecycle

Import only the LGPL canonical XGameUi IDL/stub surface from the pinned
Wine-style xgameruntime history, preserving authorship and license. Do not copy
the dirty prototype's guessed padding or class-ID-as-interface shortcut.

Implement only:

- `XGameUiShowSendGameInviteAsync/Result`
- `XGameUiShowMultiplayerActivityGameInviteAsync/Result`

Both methods use one owned provider context and the existing open-source
XThreading implementation:

- null/invalid arguments fail synchronously;
- Begin is nonblocking and schedules exactly once;
- DoWork starts/waits for XDUI;
- Cancel interrupts the Unix operation;
- status `0` → `S_OK`, `1` → `E_ABORT`, `2`/transport failure → `E_FAIL`;
- completion callback occurs exactly once;
- Result validates the provider identity and returns the stored status without
  a payload.

Query tests must use the actual canonical class and interface IIDs and call the
real vtable slots. Add success, cancel, missing service, timeout, double-result,
and unload/shutdown tests against the fixture. No real UI is launched.

Commit:

```text
xgameui: open the Xodus social picker
```

## Task 3 — Implement safe XGameInvite activation delivery

Import only the canonical LGPL XGameInvite IDL/stub surface. Build an owned
registration manager:

- maximum four active registrations with monotonically increasing nonzero
  tokens;
- duplicate supplied task-queue handles and close them exactly once;
- one cancelable/joinable XDSI worker, started exactly once when needed,
  stopped when no registrations remain and during runtime unload;
- bounded reconnect with cancellation (no busy loop and no permanent dormant
  registration after service restart);
- snapshot/refcount callback targets outside the registry lock; never invoke
  client code while holding it;
- queue callbacks through the supplied completion port; use the documented
  no-queue behavior only where the ABI permits it;
- track in-flight callbacks so `wait=TRUE` drains them without self-unregister
  deadlock; rollback slot/token/queue ownership if startup fails;
- validate URI again before allocating/callback delivery; never log it.

Tests cover null arguments, four/fifth registration, queue and no-queue paths,
fragmentation, unregister-before-delivery, `wait=TRUE`, self-unregister,
callback race, reconnect, zero-registration worker stop, DLL/runtime shutdown,
and post-unregister exclusion.

Commit:

```text
xgameinvite: deliver Xodus invite activations
```

## Task 4 — Source integration and evidence

Run from the isolated build only:

```bash
make -C <build>/dlls/xgameruntime clean
make -C <build>/dlls/xgameruntime
make -C <build>/dlls/xgameruntime/tests test
git diff --check
```

Run the native transport fixture separately through `uv`. Record exact test
counts, build artifacts, SHAs, and any Wine bootstrap limitation. Add
`patches/xgameruntime/README.md` and update `docs/verification.md` without live
identities or URI values. Final source review must cover ABI, cancellation,
callback lifetime, Unix descriptor ownership, privacy, and clean worktrees.

## Task 5 — Installed-artifact and manual game gate

Only after source review:

1. stop the game and launcher-owned service;
2. back up only the current open-source xgameruntime DLL/Unixlib artifacts;
3. install the reviewed build only into the dedicated Forza runtime;
4. prove source/build/installed SHA-256 parity;
5. install the reviewed Xodus service/overlay sibling artifacts with parity;
6. keep the user-supplied threading DLL untouched;
7. start via the reviewed launcher, which owns service start/stop;
8. run keyboard and Xbox-controller UI, cancel, outbound invite, join to a
   compatible remote activity, service cleanup, and game exit;
9. record automatic incoming notification as `NOT SUPPORTED`.

Any failed live step triggers rollback to the backed-up open-source artifacts.
No public claim is made until the exact installed hashes pass the manual matrix.

## Publication ruling

The eventual public deliverable may be a clearly labeled AI-assisted
experimental WineGDK branch/repository plus reproducible evidence. Do not open
an upstream xodus-gaming/xgameruntime code PR or imply clean-room eligibility.
Upstream issue coordination, if any, must describe behavior/protocol evidence
and ask for architecture direction without offering the generated branch for
merge.
