# Xodus title-authorization baseline

Date: 2026-09-01

This evidence is revision-bound. It records the Xodus prerequisite used by the
Forza social implementation; it is not a claim that current upstream Xodus
already supports Forza invites or incoming notifications.

## Canonical source and reviewed references

The canonical source is `https://github.com/xodus-gaming/xodus.git`. The
isolated source branch started from current `xodus/main` at
`a92abacb0743f16c769279b9057c8c28435e5ef9` and was clean before the task.

PR 108 was inspected commit by commit and file by file at the locally fetched
head `b36cd0b96144a87894f3d2e5d3074f3c896ab396`:

- `7923cea7b50c7bf367e2a01f240eff5c8ff1df4b` returns the device token used by
  SISU. It is patch-equivalent to the already merged upstream commit
  `642dd25146ead1209d4b097fcb90c2b777d12c71`.
- `b36cd0b96144a87894f3d2e5d3074f3c896ab396` adds title-claimed XSTS IPC. Its
  changes to `xml.rs`, `common.proto`, and `xuser.rs` were absent from the base.
  The port uses the active XML handler, validates and bounds request fields,
  redacts diagnostic formatting, and keeps the proof-key signer in Xodus.

PR 105 was inspected as an overlap reference at fetched head
`aa30f528f00f6157f6ceb8979b3802a54e801681`. It diverges from current main and
its xgameui/UI work is not part of this prerequisite commit. No PR 105 code was
copied here.

## Required authorization values

| Value | Current-main baseline | Prerequisite result |
| --- | --- | --- |
| Device token | Present: `do_sisu()` returns the exact token authorized with SISU via merged commit `642dd25`. | Reused without minting a second device identity. |
| SISU title token | Present in `SisuRPSAuthorizationResponse`. | Passed with the matching device and user tokens. |
| User token | Present in `SisuRPSAuthorizationResponse`. | Passed from the same SISU response. |
| Title-claimed XSTS token | Missing from the active IPC path. | Minted for one validated relying party and exposed through XML message IDs 5/6. |
| Per-token user hash | Available inside XSTS display claims but not returned by the IPC baseline. | Extracted from the minted token and returned as `UserHash`. |
| XUID | Available inside XSTS display claims but not returned by the IPC baseline. | Extracted from the same claim set and returned as `Xuid`; missing values are errors. |
| Proof-key signer | Available from the authenticator, but discarded by the PR 108 response shape. | Returned in `TitleAuthorization` from the authenticator that minted the full chain. |

## Implemented mapping

The local source commit is
`baa811b1d6d08b0cdd7e73a6a202d9b9e6616aec` (`feat: expose title-scoped Xbox
authorization`). Review then found that its call to pinned XAL
`get_xsts_token()` inherited request/response debug middleware that could emit
the device, title, user, XSTS, UHS, and XUID values. Local source commit
`c4491c0ce8107b79de284062163191008772346c` (`fix: keep Xbox authorization
secrets out of logs`) replaces that exchange and hardens the preceding error
paths. Both commits are local and unpublished.

The plan's stable `TitleAuthorization` interface is implemented in the Xodus
core with public authorization, user-hash, XUID, and signer fields. Private raw
token and expiry state has read-only accessors solely for the existing XML IPC
response shape. A future in-process `ForzaSession` therefore receives the
matching signer directly. The IPC response carries only token, per-token UHS,
XUID, and expiry; it does not export private proof-key material.

`crates/xodus-service/src/connection/xml.rs` is the active service handler.
`connection/proto.rs` remains an unimplemented placeholder and was deliberately
left unchanged. No friends, invite, join, RTA, incoming-notification, or UI code
is included in this commit.

## Credential logging audit

The complete production path reached by `mint_title_xsts()` was reviewed:

| Reachable step | Credential handling and log result |
| --- | --- |
| `TokenManager` STS reads | Reads the stored user/device credentials without logging their values. |
| Xodus `exchange_device_token()` / `exchange_user_token()` | RST2 requests log fixed processing stages only, never SOAP request/response bodies. The former raw-Debug `unimplemented!` fallback was replaced by a fixed `UnexpectedResponse` error. Persistence warnings format only `TokenStoreError`. |
| XAL `get_device_token_rps()` | Sends the RPS ticket and proof key without request/response `.log()` middleware. Any returned XAL error is consumed at the fixed `Authentication` boundary. |
| XAL `sisu_authorize_rps()` | Sends the matching user/device claims without `.log()` middleware. The former `expect()` was replaced by ordinary error propagation so an error containing a response body cannot be panic-formatted. |
| Title XSTS exchange | Does not call XAL `get_xsts_token()`. A local signed reqwest transport constructs the same public XAL request model with the same device/title/user tokens, sandbox, correlation vector, and cloned signer. It logs only `POST`, the fixed `title-xsts` service label, and numeric response status; it parses through reqwest without logging either body. |
| Service boundary | Converts all authentication/exchange failures into fixed `TitleAuthorizationError` variants and the bounded XML response; raw transport errors are not logged or returned. |

Pinned XAL implements its working signer extension for reqwest 0.11 while the
workspace uses reqwest 0.13. The source therefore names XAL's already-resolved
0.11 version as `reqwest-xal`; it does not add another resolved release. Its
public `RequestSigning<http::Request<Vec<u8>>>` route was evaluated but is not a
correct substitute in this revision because it drops the generated Signature
header when rebuilding the request. No dependency was forked or patched, and
private proof-key material remains inaccessible.

## Verification

| Check | Status | Result |
| --- | --- | --- |
| Original required RED | PASS | The first `title_xsts_bundle` run failed because `TitleTokenBundle`, `mint_title_xsts_bundle_with`, and `TitleAuthorization` were absent. |
| Privacy-fix RED | PASS | `cargo test -p xodus safe_title_xsts_exchange_never_logs_credentials_and_preserves_identity -- --exact` failed to compile because `safe_title_xsts_exchange` did not exist. |
| Focused privacy and identity tests | PASS | Four named synthetic runs each passed 1 test with 18 filtered: safe transport/log capture, exact `MissingUserHash`, exact `MissingXuid`, and one-device/signer identity. The transport test used only a loopback HTTP server. |
| Safe Xodus/Xodus-service suite | PASS | `cargo test -p xodus -p xodus-service -- --skip test_get_xbox_live_dev_token`: 19 passed, 0 failed, 1 ignored, 1 filtered across 3 suites. |
| Formatting | PASS | `cargo fmt --check` exited 0. |
| Lints | PASS | `cargo clippy -p xodus -p xodus-service --all-targets -- -D warnings` exited 0. |
| Diff integrity | PASS | `git diff --check` exited 0 before commit. |
| Integration repository gate | PASS | `just verify` exited 0 with the repository's ignored local ShellCheck/shfmt tools on `PATH`: 45 Python, 27 launcher, 13 doctor, 3 Steam-options, 35 installation, and 7 policy tests passed; systemd, ShellCheck, shfmt, Ruff, REUSE, privacy, and diff checks passed. |

All tests added by these changes use fixed synthetic values. They do not read
Xodus credentials, call Xbox endpoints, start the service, or launch the game.
The privacy test passes sentinel device/title/user/XSTS/UHS/XUID values through
the signed loopback exchange and captures enabled Rust logs; none of those
values appears in the captured records. The returned signer has the same proof
key as the input signer, and the captured HTTP body contains the exact synthetic
device/title/user values, proving the same bundle was sent. Production code and
custom `Debug` output do not emit authorization headers, tokens, per-token
identity, or proof-key material.

During the first unfiltered workspace verification, the pre-existing upstream
`test_get_xbox_live_dev_token` test was unintentionally included. Source review
confirmed that it constructs `TokenManager::with_memory()`, provisions only an
anonymous device identity, and persists both its device license and STS token
only in process-local `MemoryBackend` storage. It does not load a user token or
write the keychain. Default Rust test output capture did not expose its returned
token. The final safe suite explicitly skipped that test; this was therefore not
a zero-network verification run, but it caused no user/account persistence or
live game/service mutation.

The live Xodus checkout remained clean at
`23da0ab8323a1631ba2aacb069a06af22b8a20ac`; no live service, game process,
installed artifact, credential store, or Xbox user state was modified by the
new code.
