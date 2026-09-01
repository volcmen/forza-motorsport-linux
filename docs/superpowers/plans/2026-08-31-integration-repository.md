# Forza Motorsport Linux Integration Repository Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a safe, reversible, user-local launcher and diagnostic toolkit for the exact tested Forza Motorsport Linux stack.

**Architecture:** Bash owns process lifecycle and user-local installation; one Python standard-library patcher owns exact-build binary transformations. A TOML manifest is the single source of truth for accepted hashes and byte changes, while tests run against temporary roots and fake systemd commands.

**Tech Stack:** Bash 5, Python 3.11+, `tomllib`, pytest through uv, systemd user units, ShellCheck, shfmt, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-08-31-forza-motorsport-linux-publication-design.md`

## Global Constraints

- Target Steam AppID `2440510` and `GE-Proton11-3-FM`; refuse unknown binary hashes.
- Do not download or distribute Microsoft DLLs.
- Do not invoke `sudo`, modify Steam VDF files, launch Steam, or enable Xodus from installer/doctor commands.
- Do not install GNOME Keyring; use the existing Secret Service provider.
- Start Xodus only for the game session and stop it only if this launcher started it.
- Preserve argv and the game exit code.
- Redact XUIDs, gamertags, authorization values, proof keys, and invite URIs from diagnostics.
- Use only explicit paths under a test root or documented user-local directories.

---

### Task 1: Repository policy and verification harness

**Files:**
- Create: `README.md`
- Create: `LICENSES/GPL-3.0-or-later.txt`
- Create: `LICENSES/LGPL-2.1-or-later.txt`
- Create: `REUSE.toml`
- Create: `pyproject.toml`
- Create: `justfile`
- Create: `.gitignore`
- Create: `tests/test_repository_policy.py`

**Interfaces:**
- Consumes: Python 3.11+ and repository root discovery through `Path(__file__).parents[1]`.
- Produces: `just test`, `just lint`, `just privacy`, and `just verify` as the common gates used by every later task.

- [ ] **Step 1: Write the failing repository-policy test**

```python
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[1]
FORBIDDEN_SUFFIXES = {".dll", ".exe", ".sys", ".so"}
FORBIDDEN_TEXT = ("authorization: " + "xbl3.0 x=", "proof_" + "private_key=")


def test_repository_contains_no_prohibited_binaries_or_live_secrets():
    names = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT,
    ).decode().split("\0")
    tracked = [ROOT / name for name in names if name]
    assert not [path for path in tracked if path.suffix.lower() in FORBIDDEN_SUFFIXES]
    findings = []
    for path in tracked:
        if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}:
            continue
        text = path.read_text(errors="ignore").lower()
        findings.extend((path, token) for token in FORBIDDEN_TEXT if token in text)
    assert findings == []


def test_declared_licenses_exist():
    assert (ROOT / "LICENSES/GPL-3.0-or-later.txt").is_file()
    assert (ROOT / "LICENSES/LGPL-2.1-or-later.txt").is_file()
```

- [ ] **Step 2: Run the policy test and verify it fails**

Run:

```bash
uv run pytest tests/test_repository_policy.py -q
```

Expected: failure because the license and repository policy files do not yet exist.

- [ ] **Step 3: Add repository metadata and exact verification commands**

Use this `pyproject.toml` dependency boundary:

```toml
[project]
name = "forza-motorsport-linux-tools"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = []

[dependency-groups]
dev = ["pytest>=8.4,<9", "ruff>=0.12,<1"]
```

Use this `justfile` contract:

```make
test:
    uv run pytest -q

lint:
    shellcheck bin/* scripts/* tests/*.bash
    shfmt -d -i 4 -ci bin/* scripts/* tests/*.bash
    uv run ruff check scripts tests

privacy:
    uv run pytest tests/test_repository_policy.py -q

verify: test lint privacy
    git diff --check
```

The README at this stage states only the experimental status, legal boundary, exact supported AppID, and that installation is not ready until the remaining tasks land.

- [ ] **Step 4: Run the repository gate**

Run:

```bash
uv run pytest tests/test_repository_policy.py -q
```

Expected: `2 passed`.

- [ ] **Step 5: Commit the policy foundation**

```bash
git add README.md LICENSES REUSE.toml pyproject.toml justfile .gitignore tests/test_repository_policy.py
git commit -m "chore: establish repository policy and test gates"
```

---

### Task 2: Exact-build patch manifest and reversible patcher

**Files:**
- Create: `manifests/supported-builds.toml`
- Create: `scripts/patch-known-build`
- Create: `tests/test_patcher.py`

**Interfaces:**
- Consumes: `PatchSpec(name, original_sha256, patched_sha256, edits)` parsed from TOML.
- Produces: `inspect_target(path, spec) -> PatchState`, `apply_target(path, spec, backup_root) -> PatchResult`, `restore_target(path, backup, spec) -> PatchResult`, and `apply-forza --steam-root PATH` for the four exact AppID/tool targets.

- [ ] **Step 1: Write failing patch-state and mutation tests**

```python
def test_known_original_is_patched_and_backed_up(tmp_path, controller_spec):
    target = tmp_path / "windows.gaming.input.dll"
    target.write_bytes(controller_spec.synthetic_original())
    result = apply_target(target, controller_spec, tmp_path / "backups")
    assert result.before == controller_spec.original_sha256
    assert result.after == controller_spec.patched_sha256
    assert result.backup.read_bytes() == controller_spec.synthetic_original()


def test_unknown_hash_is_refused_without_writing(tmp_path, controller_spec):
    target = tmp_path / "windows.gaming.input.dll"
    target.write_bytes(b"unknown build")
    before = target.read_bytes()
    with pytest.raises(UnknownBuild):
        apply_target(target, controller_spec, tmp_path / "backups")
    assert target.read_bytes() == before


def test_restore_requires_matching_patched_and_original_hashes(tmp_path, controller_spec):
    target, backup = make_patched_pair(tmp_path, controller_spec)
    restore_target(target, backup, controller_spec)
    assert sha256(target) == controller_spec.original_sha256


def test_forza_scope_resolves_only_known_64_bit_targets(tmp_path, supported_builds):
    targets = resolve_forza_targets(tmp_path)
    assert [target.relative_to(tmp_path).as_posix() for target in targets] == [
        "compatibilitytools.d/GE-Proton11-3-FM/files/lib/wine/x86_64-windows/windows.gaming.input.dll",
        "steamapps/compatdata/2440510/pfx/drive_c/windows/system32/windows.gaming.input.dll",
        "compatibilitytools.d/GE-Proton11-3-FM/files/lib/wine/x86_64-windows/mountmgr.sys",
        "steamapps/compatdata/2440510/pfx/drive_c/windows/system32/drivers/mountmgr.sys",
    ]
```

Synthetic fixtures must generate only minimal byte arrays large enough to contain the offsets; tests must not copy Wine or Microsoft binaries into the repository.

- [ ] **Step 2: Run the focused tests and verify they fail**

Run:

```bash
uv run pytest tests/test_patcher.py -q
```

Expected: import failure because `scripts/patch-known-build` has not been implemented.

- [ ] **Step 3: Add the known-build manifest**

Encode these exact facts:

```toml
[[patches]]
name = "wgi-physical-xinput-nonroamable-id"
file = "windows.gaming.input.dll"
original_sha256 = "57538166ba052dc763232880f18a4b48a07ab735a61b5b8fbf5a8f56a05f2ca5"
patched_sha256 = "f520d9b4d44d9cdc11e67396583a1ccc8c53eca60e0b3a69233f358556b59245"
edits = [
  { offset = 0x14932, before = "753a", after = "9090" },
  { offset = 0x1493a, before = "7532", after = "9090" },
]

[[patches]]
name = "mountmgr-storage-trim"
file = "mountmgr.sys"
original_sha256 = "764f72bad9e639aead6535045f953815d9013ecb39b24b3b0027a70b846712b6"
patched_sha256 = "61fd9e9bb22ec1e92a3156c55cbfe77e22df49186b2de8f42b26d71cae2349d2"
edits = [
  { offset = 0x6b3f, before = "83f8070f85b0020000", after = "83e80783f80177b690" },
  { offset = 0x6f84, before = "c7460800000000", after = "803e080f944608" },
]
```

- [ ] **Step 4: Implement fail-closed patching**

The patcher must:

```python
class PatchState(Enum):
    ORIGINAL = "original"
    PATCHED = "patched"
    UNKNOWN = "unknown"


def inspect_target(path: Path, spec: PatchSpec) -> PatchState:
    digest = sha256(path)
    if digest == spec.original_sha256:
        return PatchState.ORIGINAL
    if digest == spec.patched_sha256:
        return PatchState.PATCHED
    return PatchState.UNKNOWN
```

Open the target only after state validation, write through a same-directory temporary file, `fsync`, preserve mode, replace atomically, and verify the final digest. Backups live under an explicit `--backup-root` and use a UTC timestamp plus the original hash.

`apply-forza` resolves exactly the dedicated compatibility tool and AppID `2440510` paths shown by the scope test. It first inspects all four targets, aborts the whole transaction if any target is unknown, then patches. `restore-forza` consumes the recorded backup manifest and verifies every current/backup hash before restoring any file.

- [ ] **Step 5: Verify red-green and CLI behavior**

Run:

```bash
uv run pytest tests/test_patcher.py -q
scripts/patch-known-build inspect --manifest manifests/supported-builds.toml --target /path/that/does/not/exist
```

Expected: all pytest cases pass; the CLI exits nonzero with `target does not exist` and performs no write.

- [ ] **Step 6: Commit the patcher**

```bash
git add manifests/supported-builds.toml scripts/patch-known-build tests/test_patcher.py
git commit -m "feat: add fail-closed compatibility patcher"
```

---

### Task 3: On-demand Xodus launcher and user service

**Files:**
- Create: `bin/forza-linux`
- Create: `config/xodus-forza.service`
- Create: `tests/test_launcher.bash`

**Interfaces:**
- Consumes: game command as `"$@"`, `XDG_RUNTIME_DIR`, optional `FORZA_SYSTEMCTL`, and installed `xodus-service` path.
- Produces: the game exit code; starts/stops `xodus-forza.service` with ownership tracking; waits for `$XDG_RUNTIME_DIR/xodus.sock`.

- [ ] **Step 1: Write failing lifecycle tests with a fake systemctl**

```bash
test_starts_and_stops_service_it_owns() {
    export FAKE_SERVICE_ACTIVE=0
    run_launcher /usr/bin/true
    assert_log_contains "start xodus-forza.service"
    assert_log_contains "stop xodus-forza.service"
}

test_preserves_preexisting_service() {
    export FAKE_SERVICE_ACTIVE=1
    run_launcher /usr/bin/true
    assert_log_not_contains "start xodus-forza.service"
    assert_log_not_contains "stop xodus-forza.service"
}

test_returns_game_exit_status_after_cleanup() {
    run_launcher /usr/bin/bash -c 'exit 42'
    assert_eq "$status" 42
    assert_log_contains "stop xodus-forza.service"
}
```

The harness supplies a temporary runtime directory and creates the socket marker only after the fake start command.

- [ ] **Step 2: Run tests and verify they fail**

Run:

```bash
bash tests/test_launcher.bash
```

Expected: failure because `bin/forza-linux` is absent.

- [ ] **Step 3: Implement the launcher ownership state machine**

Use these invariants:

```bash
started_here=0
game_status=0

cleanup() {
    if ((started_here)); then
        "$systemctl_bin" --user stop xodus-forza.service || true
    fi
}
trap cleanup EXIT INT TERM
```

Validate a nonempty command, complete `plasma-kwallet-pam.service` only when `PAM_KWALLET5_LOGIN` names an existing socket, start Xodus only when inactive, wait at most five seconds for the real Unix socket, run `"$@"`, save `$?`, clean up, and exit with the saved status.

The unit uses `%h/.local/libexec/xodus-forza/xodus-service`, keeps the current user's existing `%t` runtime directory writable so the pre-start token rename and socket creation can occur after namespace setup, has `UMask=0077`, and keeps home/system paths protected. It has no `[Install] WantedBy=` entry so accidental enablement is not offered.

- [ ] **Step 4: Verify lifecycle and systemd syntax**

Run:

```bash
bash tests/test_launcher.bash
systemd-analyze --user verify config/xodus-forza.service
```

Expected: every lifecycle case passes and systemd reports no unit error.

- [ ] **Step 5: Commit launcher and service**

```bash
git add bin/forza-linux config/xodus-forza.service tests/test_launcher.bash
git commit -m "feat: launch Xodus only for Forza sessions"
```

---

### Task 4: Read-only doctor and Steam option generator

**Files:**
- Create: `bin/forza-doctor`
- Create: `scripts/print-steam-options`
- Create: `tests/test_doctor.bash`
- Create: `tests/test_steam_options.bash`

**Interfaces:**
- Consumes: `FORZA_TEST_ROOT` for tests; otherwise XDG, Steam, and systemd user paths.
- Produces: one `PASS`, `WARN`, or `FAIL` line per check and a single shell-safe Steam launch-option line.

- [ ] **Step 1: Write failing read-only and UID tests**

```bash
test_doctor_does_not_start_services() {
    run_doctor
    assert_log_not_contains "start"
    assert_log_not_contains "enable"
}

test_options_use_runtime_uid_and_launcher_path() {
    export FORZA_TEST_UID=1234
    output=$(scripts/print-steam-options)
    assert_contains "$output" "PRESSURE_VESSEL_FILESYSTEMS_RW=/run/user/1234/xodus.sock"
    assert_contains "$output" "WINEDLLOVERRIDES=xgameruntime=b"
    assert_not_contains "$output" "PROTON_DISABLE_HIDRAW"
    assert_not_contains "$output" "SkipTargetHardwareProfiler"
}
```

- [ ] **Step 2: Run and confirm the expected failures**

Run:

```bash
bash tests/test_doctor.bash
bash tests/test_steam_options.bash
```

Expected: both fail because the commands are absent.

- [ ] **Step 3: Implement deterministic checks**

The doctor checks, without mutation:

```text
Steam AppID manifest
GE-Proton11-3-FM directory
Xodus service binary
user-supplied xgameruntime.dll.threading presence
Secret Service D-Bus owner or activatable KDE provider
xodus-forza.service disabled state
controller and mountmgr known-build states
current free disk space
```

The Steam option generator resolves `id -u`, its own installed launcher path, and emits exactly one line. It must quote the launcher with Bash `%q` and leave `%command%` literal.

- [ ] **Step 4: Verify read-only behavior and output**

Run:

```bash
bash tests/test_doctor.bash
bash tests/test_steam_options.bash
```

Expected: all cases pass; filesystem snapshots before and after `forza-doctor` are identical.

- [ ] **Step 5: Commit diagnostics**

```bash
git add bin/forza-doctor scripts/print-steam-options tests/test_doctor.bash tests/test_steam_options.bash
git commit -m "feat: add read-only Forza diagnostics"
```

---

### Task 5: Scoped install and uninstall

**Files:**
- Create: `scripts/install-user`
- Create: `scripts/uninstall-user`
- Create: `tests/test_installation.bash`

**Interfaces:**
- Consumes: repository files, `--xodus-build-dir PATH`, and optional `FORZA_INSTALL_ROOT` for isolated tests.
- Produces: `$XDG_STATE_HOME/forza-motorsport-linux/install-manifest`, user-local executables, and `xodus-forza.service`; uninstall consumes only that manifest.

- [ ] **Step 1: Write failing round-trip tests**

```bash
test_round_trip_touches_only_manifested_paths() {
    make_fake_home
    scripts/install-user --root "$FAKE_ROOT"
    assert_installed_files_match_manifest
    scripts/uninstall-user --root "$FAKE_ROOT"
    assert_manifested_files_absent
    assert_file_exists "$FAKE_ROOT/keep-me"
}

test_conflicting_existing_file_is_preserved() {
    make_conflicting_launcher
    before=$(sha256sum "$CONFLICT")
    assert_command_fails scripts/install-user --root "$FAKE_ROOT"
    assert_eq "$(sha256sum "$CONFLICT")" "$before"
}

test_fresh_install_requires_complete_xodus_build() {
    make_xodus_build_with xodus-service xodus-cli
    assert_command_fails scripts/install-user --root "$FAKE_ROOT" --xodus-build-dir "$BUILD"
    assert_path_absent "$FAKE_ROOT/.local/libexec/xodus-forza"
}
```

- [ ] **Step 2: Run and confirm failure**

Run:

```bash
bash tests/test_installation.bash
```

Expected: failure because install/uninstall commands are absent.

- [ ] **Step 3: Implement a manifest-owned transaction**

Install to these paths only:

```text
~/.local/bin/forza-linux
~/.local/bin/forza-doctor
~/.local/libexec/forza-motorsport-linux/patch-known-build
~/.local/share/forza-motorsport-linux/supported-builds.toml
~/.local/libexec/xodus-forza/xodus-service
~/.local/libexec/xodus-forza/xodus-cli
~/.local/libexec/xodus-forza/xodus-overlay
~/.config/systemd/user/xodus-forza.service
~/.local/state/forza-motorsport-linux/install-manifest
```

Require all three executable Xodus release artifacts from `--xodus-build-dir` for a fresh installation and record their hashes. Copy into a staging directory first. Refuse conflicting nonidentical files, atomically replace accepted destinations, write the manifest last, and call only `systemctl --user daemon-reload`. Do not enable or start the service.

Uninstall validates every manifest path against the allowed prefixes before removing it, preserves modified installed files with a warning, removes empty project-owned directories only, and reloads the user daemon.

- [ ] **Step 4: Verify round-trip and idempotency**

Run:

```bash
bash tests/test_installation.bash
```

Expected: initial install, identical reinstall, and uninstall pass; conflict and tampered-manifest cases fail closed.

- [ ] **Step 5: Commit installer**

```bash
git add scripts/install-user scripts/uninstall-user tests/test_installation.bash
git commit -m "feat: add reversible user-local installation"
```

---

### Task 6: User documentation, CI, and complete gate

**Files:**
- Modify: `README.md`
- Create: `docs/architecture.md`
- Create: `docs/troubleshooting.md`
- Create: `docs/verification.md`
- Create: `.github/workflows/verify.yml`
- Modify: `tests/test_repository_policy.py`

**Interfaces:**
- Consumes: every command and behavior from Tasks 1–5.
- Produces: a clean-checkout setup guide, evidence matrix, CI status, rollback instructions, and public privacy gate.

- [ ] **Step 1: Extend policy tests for documentation promises**

```python
def test_readme_names_supported_and_unsupported_boundaries():
    readme = (ROOT / "README.md").read_text()
    required = [
        "Experimental",
        "AppID 2440510",
        "KDE Wallet",
        "Microsoft binaries are not distributed",
        "Incoming invite notifications are not supported",
        "Uninstall",
    ]
    assert all(item in readme for item in required)
```

- [ ] **Step 2: Run the documentation test and verify failure**

Run:

```bash
uv run pytest tests/test_repository_policy.py::test_readme_names_supported_and_unsupported_boundaries -q
```

Expected: failure listing missing boundary text.

- [ ] **Step 3: Write exact user workflows and CI**

README order:

```text
Status and tested matrix
What this project does
Legal prerequisites
Read-only preflight
User-local install
Steam configuration
Known-build patch fallback
Launch and logs
Invite/join workflow
Known limitations
Restore and uninstall
Upstream branches and provenance
```

The CI job installs `shellcheck` and `shfmt`, installs uv, runs `just verify`, and uploads no runtime logs or artifacts containing system information.

- [ ] **Step 4: Run the fresh complete verification gate**

Run:

```bash
just verify
git status --short
```

Expected: all test/lint/privacy commands exit `0`; status contains only the intended Task 6 files.

- [ ] **Step 5: Commit the complete local MVP**

```bash
git add README.md docs/architecture.md docs/troubleshooting.md docs/verification.md .github/workflows/verify.yml tests/test_repository_policy.py
git commit -m "docs: publish the verified Forza Linux workflow"
```
