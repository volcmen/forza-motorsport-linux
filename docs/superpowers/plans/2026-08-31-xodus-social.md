# Xodus Forza Social Invite and Join Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Port the verified Forza friends, invite, join, and keyboard/controller social overlay onto current canonical Xodus without claiming unsupported incoming push notifications.

**Architecture:** Xodus core owns Xbox authentication and Multiplayer Activity HTTP calls; xodus-service owns authenticated Unix-socket operations; a separate GTK4 overlay renders redacted view models and maps keyboard/SDL gamepad input to semantic actions. The initial branch intentionally omits RTA endpoint registration, background polling, and unsolicited incoming notification UI.

**Tech Stack:** Rust 2024, Tokio, reqwest, serde, GTK4/libadwaita, SDL3, Unix sockets, Cargo tests.

**Spec:** `docs/superpowers/specs/2026-08-31-forza-motorsport-linux-publication-design.md`

## Global Constraints

- Base the work on current `xodus-gaming/xodus` main and account for open PRs 105 and 108.
- Keep live Xbox endpoints out of CI; use synthetic fixtures and mock transports.
- Never log authorization headers, proof keys, XUIDs, connection strings, or invite activation URIs.
- Do not include RTA registration or automatic incoming notifications in the supported branch.
- Make keyboard and controller input first-class and semantically equivalent.
- Do not require Hyprland, Fuzzel, GNOME Keyring, or an always-running service.
- Preserve Xodus's GPL licensing and project style.

---

### Task 1: Establish a current upstream base and title-token prerequisite

**Files:**
- Create: isolated Xodus worktree through the worktree workflow
- Modify as required by current main: `crates/xodus-service/src/connection/proto.rs`
- Modify as required by current main: `crates/xodus/src/tokens.rs`
- Test in the corresponding existing Rust modules
- Create: `docs/evidence/xodus-baseline.md` in the integration repository

**Interfaces:**
- Consumes: current Xodus main and the intent of PR 108's title-claimed XSTS token IPC.
- Produces: a clean prerequisite branch where `ForzaSession::authenticate` can obtain one consistent device/title/user token bundle.

- [ ] **Step 1: Invoke worktree isolation and compare PR 108**

Use `superpowers:using-git-worktrees`, fetch current main, and inspect every commit/file in PR 108 against the new base.

Record whether current main already provides each required value:

```text
device token
SISU title token
user token
title-claimed XSTS token
per-token user hash
XUID
proof-key signer
```

- [ ] **Step 2: Write failing synthetic token-bundle tests for missing behavior**

The test contract is:

```rust
#[tokio::test]
async fn title_xsts_bundle_keeps_one_device_identity() {
    let bundle = fixture_bundle();
    let result = mint_title_xsts(&fixture_client(), &bundle, "rp://forza").await.unwrap();
    assert_eq!(result.xuid, "2810000000000000");
    assert_eq!(result.user_hash, "synthetic-uhs");
    assert!(result.authorization.starts_with("XBL3.0 x="));
}
```

If current main already passes an equivalent test, retain the upstream implementation and do not duplicate PR 108.

- [ ] **Step 3: Run the smallest prerequisite test**

Run:

```bash
cargo test -p xodus -p xodus-service title_xsts_bundle -- --nocapture
```

Expected: pass on existing upstream behavior, or a precise failure proving the missing prerequisite.

- [ ] **Step 4: Port only the missing prerequisite code**

Keep protocol request/response names aligned with current Xodus conventions. The returned Rust value must have this stable interface:

```rust
pub struct TitleAuthorization {
    pub authorization: String,
    pub user_hash: String,
    pub xuid: String,
    pub signer: RequestSigner,
}
```

Do not merge social UI code into this prerequisite commit.

- [ ] **Step 5: Verify and commit the prerequisite branch**

Run:

```bash
cargo fmt --check
cargo clippy -p xodus -p xodus-service --all-targets -- -D warnings
cargo test -p xodus -p xodus-service
```

Expected: all commands exit `0`.

Commit only if code was required:

```bash
git add crates/xodus crates/xodus-service
git commit -m "feat: expose title-scoped Xbox authorization"
```

---

### Task 2: Add Forza friends and Multiplayer Activity API

**Files:**
- Create: `crates/xodus/src/forza/mod.rs`
- Create: `crates/xodus/src/forza/model.rs`
- Create: `crates/xodus/src/forza/api.rs`
- Modify: `crates/xodus/src/lib.rs`
- Modify: `crates/xodus/Cargo.toml` only if current dependencies do not provide required serde/reqwest functionality

**Interfaces:**
- Consumes: `TitleAuthorization` and a signed reqwest client.
- Produces: `ForzaSession::{authenticate, friends, activity, invite, join_uri}` and public redacted models.

- [ ] **Step 1: Write failing model fixtures**

```rust
#[test]
fn parses_friends_without_exposing_transport_fields() {
    let friends = parse_friends(include_str!("fixtures/friends.json")).unwrap();
    assert_eq!(friends, vec![XboxFriend {
        xuid: Xuid::new("2810000000000001").unwrap(),
        display_name: "Driver One".into(),
        presence: Presence::Online { text: Some("Forza Motorsport".into()) },
    }]);
}

#[test]
fn builds_validated_invite_accept_uri() {
    let uri = build_invite_accept_uri(
        &Xuid::new("2810000000000000").unwrap(),
        &Xuid::new("2810000000000001").unwrap(),
        "synthetic-connection",
    ).unwrap();
    assert!(uri.expose_for_activation().starts_with("ms-xbl-multiplayer://inviteAccept?"));
    assert!(!format!("{uri:?}").contains("synthetic-connection"));
}
```

The actual URI type implements a redacted `Debug`; raw serialization is available only through an explicit `expose_for_activation()` method.

- [ ] **Step 2: Verify new tests fail**

Run:

```bash
cargo test -p xodus forza:: -- --nocapture
```

Expected: compilation failure because the Forza module and types do not exist.

- [ ] **Step 3: Implement validated domain types and HTTP operations**

Use newtypes rather than free-form strings at service boundaries:

```rust
pub struct Xuid(String);
pub struct InviteActivationUri(Url);

pub struct XboxFriend {
    pub xuid: Xuid,
    pub display_name: String,
    pub presence: Presence,
}
```

`Xuid` and `InviteActivationUri` use redacted `Debug` implementations; activation serialization requires the explicit `expose_for_activation()` method. `ForzaSession` keeps authorization, signer, and local XUID private. Errors expose the service name and HTTP status but never response bodies by default. `friends()` calls PeopleHub; `activity()` queries Multiplayer Activity; `invite()` requires a local published activity; `join_uri()` requires the selected friend's activity.

- [ ] **Step 4: Add mock HTTP success and failure tests**

Test these exact cases:

```text
empty friends response
friend presence absent
local activity missing
friend activity missing
invite HTTP 204 success
invite HTTP 401 redacted error
malformed JSON redacted error
invalid activation scheme rejected
```

- [ ] **Step 5: Verify and commit the API layer**

Run:

```bash
cargo fmt --check
cargo clippy -p xodus --all-targets -- -D warnings
cargo test -p xodus forza::
```

Expected: all commands exit `0` with no live network calls.

```bash
git add crates/xodus/src/forza crates/xodus/src/lib.rs crates/xodus/Cargo.toml Cargo.lock
git commit -m "feat: add Xbox Multiplayer Activity operations"
```

---

### Task 3: Add versioned social and activation IPC

**Files:**
- Create: `crates/xodus/src/forza/ipc.rs`
- Create: `crates/xodus-service/src/social_ipc.rs`
- Create: `crates/xodus-service/src/invite_activation.rs`
- Modify: `crates/xodus-service/src/connection/router.rs`
- Modify: `crates/xodus-service/src/main.rs`

**Interfaces:**
- Consumes: `ForzaSession` and same-user Unix peer credentials.
- Produces: framed protocol version `1`, `SocialRequest`, `SocialResponse`, an invite subscriber channel, and a validated activation publisher.

- [ ] **Step 1: Write failing codec and peer-boundary tests**

```rust
#[test]
fn social_frames_round_trip_with_version_one() {
    let request = SocialRequest::InviteFriend { friend: FriendHandle::test("friend-1") };
    let bytes = encode_frame(&request).unwrap();
    assert_eq!(&bytes[0..4], b"XDSO");
    assert_eq!(u16::from_le_bytes(bytes[4..6].try_into().unwrap()), 1);
    assert_eq!(decode_frame(&bytes).unwrap(), request);
}

#[tokio::test]
async fn rejects_oversized_frame_before_allocation() {
    let error = read_frame(&mut oversized_fixture()).await.unwrap_err();
    assert!(matches!(error, ProtocolError::FrameTooLarge));
}
```

Add a Unix credential test proving a peer UID mismatch is rejected before token or friend-list work.

- [ ] **Step 2: Run and confirm failure**

Run:

```bash
cargo test -p xodus -p xodus-service social_ -- --nocapture
```

Expected: compilation failure because the new protocol is absent.

- [ ] **Step 3: Implement bounded framing and request handling**

Use this request surface only:

```rust
pub enum SocialRequest {
    Snapshot,
    RefreshFriends,
    InviteFriend { friend: FriendHandle },
    JoinFriend { friend: FriendHandle },
}

pub enum SocialResponse {
    Snapshot(SocialSnapshot),
    Success { message: String },
    Error { message: String },
}
```

`FriendHandle(Uuid)` is created by xodus-service and is the only friend identifier sent over IPC. The snapshot contains display labels and opaque per-process handles, never XUIDs or connection strings. Bound frames at 1 MiB, reject unknown versions, serialize writes behind one owner, and verify peer UID.

The invite activation channel retains the already tested roles:

```text
XDSI: game subscribes for accepted activation URIs
XDAP: trusted Xodus UI publishes one validated activation URI
XDUI: game asks Xodus to open its social UI
XDSO: overlay requests snapshots/actions from xodus-service
```

- [ ] **Step 4: Add fragmented, concurrent, and disconnect tests**

Verify one-byte fragmented frames, two concurrent request writers, subscriber disconnect, publisher-before-subscriber failure, invalid scheme rejection, and a successful synthetic activation relay.

- [ ] **Step 5: Verify and commit IPC**

Run:

```bash
cargo fmt --check
cargo clippy -p xodus -p xodus-service --all-targets -- -D warnings
cargo test -p xodus -p xodus-service social_ invite_activation
```

Expected: all commands exit `0`.

```bash
git add crates/xodus/src/forza/ipc.rs crates/xodus-service/src/social_ipc.rs crates/xodus-service/src/invite_activation.rs crates/xodus-service/src/connection/router.rs crates/xodus-service/src/main.rs
git commit -m "feat: add Forza social and activation IPC"
```

---

### Task 4: Add a desktop-neutral keyboard/controller overlay

**Files:**
- Create: `crates/xodus-overlay/Cargo.toml`
- Create: `crates/xodus-overlay/src/lib.rs`
- Create: `crates/xodus-overlay/src/main.rs`
- Create: `crates/xodus-overlay/src/action.rs`
- Create: `crates/xodus-overlay/src/client.rs`
- Create: `crates/xodus-overlay/src/controller.rs`
- Create: `crates/xodus-overlay/src/model.rs`
- Create: `crates/xodus-overlay/src/ui.rs`
- Create: `crates/xodus-overlay/src/style.css`
- Modify: workspace `Cargo.toml`

**Interfaces:**
- Consumes: `SocialRequest`/`SocialResponse` over `$XDG_RUNTIME_DIR/xodus.sock`.
- Produces: `InputMapper`, `ControllerListener`, GTK window, invite/join actions, and exit status `0` only for a completed or deliberately cancelled UI session.

- [ ] **Step 1: Write failing semantic input tests**

```rust
#[test]
fn keyboard_and_xbox_buttons_map_to_equal_actions() {
    assert_eq!(InputMapper::key(KeyInput::Enter), Some(UiAction::Activate));
    assert_eq!(InputMapper::default().gamepad(
        GamepadInput::ButtonPressed(GamepadButton::South)), Some(UiAction::Activate));
    assert_eq!(InputMapper::key(KeyInput::Escape), Some(UiAction::Back));
    assert_eq!(InputMapper::default().gamepad(
        GamepadInput::ButtonPressed(GamepadButton::East)), Some(UiAction::Back));
}

#[test]
fn stick_emits_once_per_dead_zone_crossing() {
    let mut mapper = InputMapper::default();
    assert_eq!(mapper.gamepad(GamepadInput::LeftY(-20_000)), Some(UiAction::MoveUp));
    assert_eq!(mapper.gamepad(GamepadInput::LeftY(-25_000)), None);
    assert_eq!(mapper.gamepad(GamepadInput::LeftY(0)), None);
}
```

- [ ] **Step 2: Run and confirm failure**

Run:

```bash
cargo test -p xodus-overlay --lib
```

Expected: Cargo reports the package is absent.

- [ ] **Step 3: Implement input and view-model layers before GTK**

Semantic actions are:

```rust
pub enum UiAction { MoveUp, MoveDown, Activate, Back, Invite, Join, Refresh }
```

Map arrows/W/S, Enter/Space, Escape/Backspace, I, J, R/F5 and Xbox D-pad, left stick, A, B, X, Y. SDL device add/remove must be hotplug safe. UI model tests cover empty, loading, friends available, no joinable activity, service error, and cancelled states.

- [ ] **Step 4: Implement GTK4 UI without compositor-specific commands**

Use a normal `gtk::ApplicationWindow`, one selectable friend list, a status line, and explicit Invite/Join buttons. Do not call `hyprctl`, require layer-shell, or execute Fuzzel. Render only `SocialSnapshot`; never expose raw transport models to widgets.

- [ ] **Step 5: Verify input, model, and headless-safe tests**

Run:

```bash
cargo fmt --check
cargo clippy -p xodus-overlay --all-targets -- -D warnings
cargo test -p xodus-overlay
```

Expected: all non-display tests exit `0`; display-dependent construction is behind a small manual smoke test, not run in headless CI.

- [ ] **Step 6: Commit the overlay**

```bash
git add Cargo.toml Cargo.lock crates/xodus-overlay
git commit -m "feat: add keyboard and controller social overlay"
```

---

### Task 5: Launch the overlay from xodus-service

**Files:**
- Create: `crates/xodus-service/src/social_ui.rs`
- Modify: `crates/xodus-service/src/connection/router.rs`
- Modify: `crates/xodus-service/src/main.rs`
- Modify: `crates/xodus-service/Cargo.toml`

**Interfaces:**
- Consumes: `XDUI` request from xgameruntime.
- Produces: one-byte completion status after launching the sibling `xodus-overlay` executable: `0` completed, `1` cancelled, `2` failed.

- [ ] **Step 1: Write failing launcher tests**

```rust
#[tokio::test]
async fn successful_overlay_returns_zero_status() {
    assert_eq!(run_overlay(Path::new("/usr/bin/true")).await.unwrap(), 0);
}

#[tokio::test]
async fn failed_overlay_returns_nonzero_status() {
    assert_eq!(run_overlay(Path::new("/usr/bin/false")).await.unwrap(), 2);
}
```

- [ ] **Step 2: Run and confirm failure**

Run:

```bash
cargo test -p xodus-service social_ui
```

Expected: compilation failure because `social_ui` is absent.

- [ ] **Step 3: Implement sibling binary discovery and bounded execution**

Resolve `XODUS_SOCIAL_UI` only when explicitly set; otherwise use `current_exe().parent()/xodus-overlay`. Pass no token, XUID, or URI in argv/environment. Allow only one overlay process at a time. Map a deliberate UI cancellation to status `1` and process/startup failure to status `2`; neither condition crashes xodus-service.

- [ ] **Step 4: Verify service tests and full workspace**

Run:

```bash
cargo fmt --check
cargo clippy --workspace --all-targets -- -D warnings
cargo test --workspace
cargo build --release --workspace
```

Expected: every command exits `0`.

- [ ] **Step 5: Commit service integration**

```bash
git add crates/xodus-service Cargo.lock
git commit -m "feat: launch the Xodus social overlay"
```

---

### Task 6: Validate and prepare scoped Xodus review

**Files:**
- Create: `docs/forza-social.md` in the Xodus branch
- Modify: `docs/verification.md` in the integration repository

**Interfaces:**
- Consumes: Tasks 1–5 and the xgameruntime bridge plan.
- Produces: reviewable commits, exact local verification, and an upstream coordination note for issue 94/PR 105.

- [ ] **Step 1: Run synthetic protocol and privacy gates**

Run:

```bash
cargo test --workspace
cargo clippy --workspace --all-targets -- -D warnings
rg -n 'Authorization:|XBL3\.0|ms-xbl-multiplayer://inviteAccept\?.+' target/test-logs crates || true
git diff --check
```

Expected: tests/clippy pass and the privacy search finds no live value.

- [ ] **Step 2: Run the manual overlay matrix**

Record keyboard navigation, Xbox controller navigation, invite from a published Private Multiplayer lobby, join to a Windows player's activity, cancellation, and service cleanup. Record automatic incoming notification as `NOT SUPPORTED`, not `PASS`.

- [ ] **Step 3: Write maintainer-facing documentation**

Describe protocol frames, trust boundary, API ownership, UI dependencies, test commands, relation to issue 94/PR 105, and why RTA work is excluded.

- [ ] **Step 4: Commit documentation**

```bash
git add docs/forza-social.md
git commit -m "docs: describe Forza social integration"
```

Update and commit integration evidence separately:

```bash
git add docs/verification.md
git commit -m "docs: record Xodus social verification"
```
