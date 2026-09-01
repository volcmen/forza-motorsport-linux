# Forza Motorsport Linux Publication Roadmap

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Coordinate five independently testable implementation plans into one accurate public Forza Motorsport Linux release and upstream contribution set.

**Architecture:** The integration repository is the user-facing control plane; Wine and Xodus changes remain in their canonical component repositories, while the XGameRuntime bridge remains an explicitly experimental WineGDK branch. Public GitHub communication happens only after stable branches, test evidence, and privacy scans exist.

**Tech Stack:** Bash, Python 3 standard library, pytest via uv, systemd user units, Rust/Cargo, Wine C tests, GitHub CLI.

**Spec:** `docs/superpowers/specs/2026-08-31-forza-motorsport-linux-publication-design.md`

## Global Constraints

- Target Steam AppID `2440510` and the exact known `GE-Proton11-3-FM` build first.
- Never distribute Microsoft binaries, credentials, XUIDs, gamertags, invite URIs, or game assets.
- Use KDE Wallet/Secret Service; do not install or enable GNOME Keyring.
- Leave `xodus-forza.service` disabled and start it only for a Forza session.
- Reject unknown binary hashes before writing and preserve reversible backups.
- Keep failed incoming-RTA push work out of the supported release and initial upstream PRs.
- Use GitHub noreply author identity for all new public commits.
- Label automated, local end-to-end, experimental, and unresolved evidence separately.

## Plans and Order

1. `2026-08-31-integration-repository.md`
   - Produces the safe local launcher, doctor, installer, exact-build fallback patcher, documentation, and CI.
2. `2026-08-31-wine-compatibility-fixes.md`
   - Produces source-level controller identity and storage TRIM branches with tests.
3. `2026-08-31-xodus-social.md`
   - Ports the verified friends/invite/join and keyboard/controller overlay work onto current Xodus main without failed RTA push.
4. `2026-09-01-xgameruntime-winegdk-amendment.md`
   - Supersedes the original xgameruntime source/build/publication assumptions and re-derives the Linux bridge on an isolated WineGDK base; the original `2026-08-31-xgameruntime-invite-bridge.md` is historical only.
5. `2026-08-31-github-publication.md`
   - Publishes the integration repository and permitted component forks, opens only publication-boundary-compliant draft PRs/issues, posts the Proton compatibility update, and links the blog.

Plans 2 and 3 may execute after Plan 1's test harness and privacy policy are committed. Plan 4 first versions the reviewed Plan-3 XDUI contract on the same isolated Xodus feature line, records a new immutable Xodus SHA, and then consumes that exact SHA for the WineGDK bridge. Plan 5 is last because every external claim must link to immutable public evidence.

## Cross-plan Gates

- [ ] **Gate 1: Integration safety**

Run:

```bash
just verify
```

Expected: every integration test passes, `git diff --check` is clean, and the privacy scan reports zero findings.

- [ ] **Gate 2: Component source evidence**

Run the exact commands recorded by Plans 2–4 and store their command, revision, exit code, and summary in `docs/verification.md`.

Expected: Wine controller/storage tests, Xodus workspace tests, and xgameruntime GDK tests all exit `0`.

- [ ] **Gate 3: Installed artifact parity**

Record component Git revisions separately. Compare each reviewed built artifact's SHA-256 only with its dedicated compatibility-tool or user-local installed counterpart.

Expected: build and installed SHA-256 values match for every open-source artifact; the AppID prefix and user-supplied Microsoft threading DLL remain untouched, and the latter is recorded only as present, never copied or hashed into public logs.

- [ ] **Gate 4: Manual game matrix**

Record dated results for cold KWallet startup, online menu, controller navigation/driving, AP702 absence, Linux-to-Windows invite, and Linux join.

Expected: each row is `PASS`, `FAIL`, or `NOT RUN`; no blank or inferred result is allowed.

- [ ] **Gate 5: Public publication**

Execute Plan 5 only if Gates 1–4 contain no unexplained failure in a feature claimed as supported.

Expected: public repository, branches, draft PRs/issues, Proton comment, and blog links all point to the same tested revisions.
