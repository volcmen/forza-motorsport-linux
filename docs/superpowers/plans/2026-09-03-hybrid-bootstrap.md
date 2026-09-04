# Hybrid Bootstrap Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a resumable, transaction-safe `./setup bootstrap` that prepares the reviewed Forza Motorsport Linux stack on Arch Linux x86_64, proves its bounded changes with private SHA-256 snapshots, and supports either a verified binary bundle or an isolated Docker source build.

**Architecture:** Keep `setup` as a thin Bash dispatcher and put new standard-library Python code in focused `forza_bootstrap` modules. The coordinator owns only bootstrap state, GE-Proton acquisition/publication, the licensed threading-DLL copy, and composition; existing runtime installation, exact-build patching, doctor, launch-option, and rollback tools remain the mutation owners for their current scopes. Every phase is manifest-bound, plan-digest-confirmed, journaled, resumable, and tested first against synthetic Steam roots before any read-only inspection of the live installation.

**Tech Stack:** Bash, Python 3.11+ standard library, TOML/JSON, pytest, Docker rootless, `uv` with a frozen ProtonUp environment, systemd user units, existing repository transaction tools.

**Spec:** `docs/superpowers/specs/2026-09-03-hybrid-bootstrap-design.md`

## Global Constraints

- Supported bootstrap host is exactly Arch Linux x86_64 with native per-user Steam and Forza AppID `2440510`.
- Compatibility base is exact release `GE-Proton11-3`; the published dedicated tool is `GE-Proton11-3-FM` and canonical GE-Proton is never modified.
- Runtime profile is exactly `legacy-v0.1`, Xodus revision `7b236772297b3475ea4f3cb830feb5b224f3064a`, and XGameRuntime revision `a1548b1cf57371715d10b608bc81a77a188e40d4`.
- ProtonUp is version `0.1.5`, executed through a repository-owned `uv run --frozen` environment; it receives a private cache directory, never Steam's `compatibilitytools.d`.
- Official x86_64 GE archive is `GE-Proton11-3.tar.gz`, size `532524366`, SHA-256 `861c2edc8d40d051fb1e7a692deb953be52bd339c46d90f2b7dde50ddad91266`.
- Docker builder base is x86_64 `docker.io/library/archlinux:base-devel@sha256:694da1fce635e3a14d90751941b08f02c500e8724682f7a00768e7152251ec34`; build dependencies come from the immutable Arch Linux Archive snapshot dated `2026/09/01`.
- The Microsoft threading runtime is supplied by the user, copied locally, never downloaded or distributed, and its digest remains only in mode-0600 private state.
- Never invoke `sudo`, install host packages, edit Steam VDF state, launch Steam/Forza automatically, install GNOME Keyring, or enable Xodus as a permanent service.
- Xodus continues to use KDE Wallet through `org.freedesktop.secrets` and starts only through the existing per-session launcher ownership protocol.
- No source-build container receives the user's home, Steam, D-Bus, KWallet, Xodus account state, Microsoft DLL, or Xbox credentials.
- Snapshot only the finite managed/safety path set from the spec; never hash the full Steam tree, game, prefix, shader cache, saves, or home.
- Existing exact supported state is a no-op/adoption candidate; unknown, mixed, symlinked, replaced, or incomplete state fails closed and is preserved.
- All production code changes follow RED-GREEN-REFACTOR, and every task ends in a focused green gate plus a small commit.

## File and responsibility map

| File | Responsibility |
| --- | --- |
| `forza_bootstrap/model.py` | Immutable data types, canonical JSON, SHA helpers, plan digest. |
| `forza_bootstrap/manifest.py` | Strict parsing and cross-checking of bootstrap and bundle manifests. |
| `forza_bootstrap/safeio.py` | No-follow reads, owner/mode checks, atomic private writes, locks, rename primitives. |
| `forza_bootstrap/state.py` | Bootstrap state machine, journals, one-unfinished-transaction selection. |
| `forza_bootstrap/snapshot.py` | Finite path capture, comparison policy, redacted human report. |
| `forza_bootstrap/artifacts.py` | Bounded download cache, digest verification, safe bundle/GE archive inspection and extraction. |
| `forza_bootstrap/proton.py` | Frozen ProtonUp invocation, dedicated tool identity, publication/adoption/rollback. |
| `forza_bootstrap/container_build.py` | Rootless Docker preflight and source/bundle build command construction. |
| `forza_bootstrap/licensed.py` | Private PE validation, copy/no-op/backup/restore transaction. |
| `forza_bootstrap/coordinator.py` | Phase A, Phase B, resume, composed rollback, child-tool orchestration. |
| `forza_bootstrap/cli.py` | Public argument parsing, confirmation input, exit codes, redacted output. |
| `scripts/forza-bootstrap` | Executable Python entry point used by `setup`. |
| `manifests/bootstrap-v1.toml` | Production trust root for GE, ProtonUp, bundle, sources, builder, and outputs. |
| `containers/bootstrap-builder/*` | Pinned builder image and deterministic component/bundle scripts. |
| `tools/build-bootstrap-builder` | Rebuilds the builder twice, compares its declared rootfs manifest, and optionally publishes one digest-qualified image. |
| `tools/build-bootstrap-bundle` | Host driver for two-stage dependency fetch and offline source build. |
| `tools/finalize-bootstrap-manifest` | Writes concrete bundle size/hash/member records into the release manifest. |
| `tools/protonup/{pyproject.toml,uv.lock}` | Frozen, repository-local ProtonUp environment. |
| `tests/test_bootstrap_*.py` | Focused Python behavior and adversarial filesystem tests. |
| `tests/test_bootstrap_cli.bash` | Black-box `setup` dispatch, confirmation, and child-command ordering. |
| `tests/fixtures/bootstrap/*` | Synthetic archives, manifests, Steam roots, Docker/ProtonUp stubs; no real binaries or identities. |

---

### Task 1: Strict bootstrap model, manifest parser, and CLI skeleton

**Files:**
- Create: `forza_bootstrap/__init__.py`
- Create: `forza_bootstrap/model.py`
- Create: `forza_bootstrap/manifest.py`
- Create: `forza_bootstrap/cli.py`
- Create: `scripts/forza-bootstrap`
- Create: `tests/test_bootstrap_manifest.py`
- Create: `tests/test_bootstrap_cli.py`
- Create: `tests/fixtures/bootstrap/valid-bootstrap.toml`
- Modify: `pyproject.toml:1-7`
- Modify: `justfile:1-20`

**Interfaces:**
- Consumes: Python 3.11 `tomllib`, exact constants from `runtime-profiles.toml` and this plan.
- Produces: `BootstrapManifest`, `ArtifactSpec`, `SourceSpec`, `BuilderSpec`, `canonical_json(value) -> bytes`, `sha256_bytes(data) -> str`, `plan_digest(plan) -> str`, and `cli.main(argv: Sequence[str] | None = None) -> int`.

- [ ] **Step 1: Write failing strict-schema and CLI tests**

```python
def test_valid_manifest_binds_exact_supported_stack():
    manifest = load_bootstrap_manifest(FIXTURES / "valid-bootstrap.toml")
    assert manifest.schema == 1
    assert manifest.app_id == "2440510"
    assert manifest.host == ("arch", "x86_64", "native-steam")
    assert manifest.protonup.version == "0.1.5"
    assert manifest.ge.release == "GE-Proton11-3"
    assert manifest.ge.size == 532_524_366
    assert manifest.ge.sha256 == "861c2edc8d40d051fb1e7a692deb953be52bd339c46d90f2b7dde50ddad91266"
    assert manifest.profile == "legacy-v0.1"


@pytest.mark.parametrize(
    "mutation,message",
    [
        (lambda value: value.update(schema=True), "schema must be integer 1"),
        (lambda value: value.update(extra="value"), "unknown manifest key"),
        (lambda value: value["ge"].update(sha256="A" * 64), "lowercase SHA-256"),
        (lambda value: value["sources"][0].update(revision="short"), "40-character revision"),
    ],
)
def test_manifest_rejects_ambiguous_or_unpinned_values(tmp_path, mutation, message):
    value = fixture_manifest_dict()
    mutation(value)
    path = write_toml_fixture(tmp_path, value)
    with pytest.raises(BootstrapError, match=message):
        load_bootstrap_manifest(path)


def test_cli_parses_bootstrap_modes_as_mutually_exclusive():
    with pytest.raises(SystemExit) as failure:
        parse_args(["bootstrap", "--bundle", "/tmp/b.tar.zst", "--build-from-source"])
    assert failure.value.code == 2
```

- [ ] **Step 2: Run the focused tests and confirm RED**

Run: `rtk uv run pytest tests/test_bootstrap_manifest.py tests/test_bootstrap_cli.py -q`

Expected: collection fails because `forza_bootstrap` does not exist.

- [ ] **Step 3: Implement immutable models, strict TOML parsing, and command parsing**

```python
# forza_bootstrap/model.py
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

class BootstrapError(RuntimeError):
    pass

def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")

def plan_digest(plan: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(plan)).hexdigest()

@dataclass(frozen=True)
class ArtifactSpec:
    logical_name: str
    relative_path: str
    size: int
    sha256: str
    mode: int
    private: bool = False

@dataclass(frozen=True)
class DownloadSpec:
    name: str
    url: str
    filename: str
    size: int
    sha256: str
    expected_root: str
    allowed_redirect_hosts: tuple[str, ...]

@dataclass(frozen=True)
class ProtonUpSpec:
    version: str
    project_relative: str

@dataclass(frozen=True)
class SourceSpec:
    name: str
    repository: str
    revision: str

@dataclass(frozen=True)
class BuilderSpec:
    image: str
    base_image: str
    arch_snapshot: str
    source_date_epoch: int

@dataclass(frozen=True)
class BootstrapManifest:
    schema: int
    app_id: str
    host: tuple[str, str, str]
    profile: str
    protonup: "ProtonUpSpec"
    ge: "DownloadSpec"
    bundle: "DownloadSpec"
    sources: tuple["SourceSpec", ...]
    builder: "BuilderSpec"
    artifacts: tuple[ArtifactSpec, ...]
    path: Path
    sha256: str
```

The parser must reject booleans where integers are expected, unknown/missing
keys at every level, non-HTTPS URLs, mutable source references, duplicate role
or path names, unsafe relative paths, non-lowercase digests, unsupported host
tuples, and any disagreement with `manifests/runtime-profiles.toml`.

```python
# scripts/forza-bootstrap
#!/usr/bin/env python3
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))
from forza_bootstrap.cli import main

raise SystemExit(main())
```

`cli.parse_args()` must expose `bootstrap`, `snapshot`, and `compare`;
`bootstrap` accepts mutually exclusive `--check` and `--rollback`. Command
handlers initially raise `BootstrapError("command is unavailable in this
build")` so parsing is testable without false success.

- [ ] **Step 4: Wire focused tests into repository gates and run GREEN**

Add `scripts/forza-bootstrap` and all `forza_bootstrap/*.py` files to Ruff,
ShellCheck only to executable shell files, and let the existing `uv run pytest
-q` discover the new Python tests.

Run: `rtk uv run pytest tests/test_bootstrap_manifest.py tests/test_bootstrap_cli.py -q`

Expected: all focused tests pass.

- [ ] **Step 5: Commit the manifest/CLI foundation**

```bash
rtk git add forza_bootstrap scripts/forza-bootstrap tests/test_bootstrap_manifest.py tests/test_bootstrap_cli.py tests/fixtures/bootstrap pyproject.toml justfile
rtk git commit -m "feat: add strict bootstrap manifest model"
```

### Task 2: No-follow private state and resumable journal

**Files:**
- Create: `forza_bootstrap/safeio.py`
- Create: `forza_bootstrap/state.py`
- Create: `tests/test_bootstrap_state.py`

**Interfaces:**
- Consumes: `BootstrapError`, `canonical_json()` from Task 1.
- Produces: `BootstrapState`, `BootstrapPhase`, `ensure_private_directory(path)`, `open_owned_root(path) -> int`, `write_all(fd, data)`, `atomic_write_private_json(parent_fd, name, value)`, `read_private_json(parent_fd, name)`, `rename_noreplace(source_fd, source, destination_fd, destination)`, `acquire_bootstrap_lock(runtime_dir) -> int`, `create_transaction(state_root, manifest_sha256) -> BootstrapState`, `load_unfinished_transaction(state_root) -> BootstrapState | None`, and `transition(state, expected, target, **updates) -> BootstrapState`.

- [ ] **Step 1: Write failing state, permissions, and interruption tests**

```python
def test_new_transaction_is_private_and_single(tmp_path):
    state = create_transaction(tmp_path, "1" * 64)
    root = tmp_path / state.transaction_id
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE((root / "state.json").stat().st_mode) == 0o600
    assert load_unfinished_transaction(tmp_path) == state
    with pytest.raises(BootstrapError, match="unfinished bootstrap transaction"):
        create_transaction(tmp_path, "1" * 64)


def test_state_reader_rejects_symlink_and_wrong_mode(tmp_path):
    outside = tmp_path / "outside"
    outside.write_text("{}")
    (tmp_path / "state.json").symlink_to(outside)
    with pytest.raises(BootstrapError, match="symlink"):
        read_private_json(os.open(tmp_path, DIRECTORY), "state.json")


def test_transition_requires_last_durable_phase(tmp_path):
    state = create_transaction(tmp_path, "2" * 64)
    with pytest.raises(BootstrapError, match="expected NEW"):
        transition(state, BootstrapPhase.PREPARING, BootstrapPhase.AWAITING_STEAM_PREFIX)
```

Add parameterized cases for corrupt JSON, schema mismatch, duplicate
transaction IDs, state-root mode not 0700, file mode not 0600, foreign UID via
mocked `fstat`, concurrent lock contention, and a crash immediately before and
after every temp-file rename.

- [ ] **Step 2: Run the focused tests and confirm RED**

Run: `rtk uv run pytest tests/test_bootstrap_state.py -q`

Expected: import failure for `forza_bootstrap.state`.

- [ ] **Step 3: Implement descriptor-relative safe I/O and state transitions**

```python
class BootstrapPhase(str, Enum):
    NEW = "NEW"
    PREPARING = "PREPARING"
    AWAITING_STEAM_PREFIX = "AWAITING_STEAM_PREFIX"
    PLANNED_FINISH = "PLANNED_FINISH"
    INSTALLING_FINISH = "INSTALLING_FINISH"
    READY_TO_ATTEMPT = "READY_TO_ATTEMPT"
    ACCEPTED = "ACCEPTED"
    ROLLING_BACK = "ROLLING_BACK"
    ROLLED_BACK = "ROLLED_BACK"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"

@dataclass(frozen=True)
class BootstrapState:
    version: int
    transaction_id: str
    manifest_sha256: str
    phase: BootstrapPhase
    completed_boundaries: tuple[str, ...]
    child_runtime_transaction: str | None
    patch_backup_manifest: str | None
    compatibility_tool_disposition: Literal["created", "adopted"] | None
```

Use `os.open(..., O_NOFOLLOW, dir_fd=...)`, require current UID, validate modes,
write a unique mode-0600 temp file, `fsync` the file, rename within the held
directory descriptor, and `fsync` the directory. The JSON must be reconstructed
into the dataclass only after exact key/type validation.

- [ ] **Step 4: Run focused and inherited transaction tests**

Run: `rtk uv run pytest tests/test_bootstrap_state.py tests/test_secure_user_files.py tests/test_runtime_components.py -q`

Expected: all tests pass with existing transaction behavior unchanged.

- [ ] **Step 5: Commit private bootstrap state**

```bash
rtk git add forza_bootstrap/safeio.py forza_bootstrap/state.py tests/test_bootstrap_state.py
rtk git commit -m "feat: add recoverable bootstrap state"
```

### Task 3: Narrow SHA-256 snapshots and deterministic comparison

**Files:**
- Create: `forza_bootstrap/snapshot.py`
- Create: `tests/test_bootstrap_snapshot.py`
- Modify: `forza_bootstrap/cli.py`

**Interfaces:**
- Consumes: manifest hash, transaction ID, resolved Steam/user roots, `ArtifactSpec`, safe I/O.
- Produces: `FileRecord`, `Snapshot`, `ChangeRule`, `Comparison`, `capture_snapshot(scope, roots) -> Snapshot`, `compare_snapshots(before, after, rules) -> Comparison`, `write_snapshot(path, snapshot)`, and `render_comparison(comparison) -> str`.

- [ ] **Step 1: Write failing canonical snapshot and policy tests**

```python
def test_compare_classifies_exact_expected_unchanged_and_unexpected(tmp_path):
    before = snapshot_fixture({"managed/a": b"old", "guard/b": b"same", "guard/c": b"old"})
    after = snapshot_fixture({"managed/a": b"new", "guard/b": b"same", "guard/c": b"surprise"})
    rules = (
        ChangeRule("managed/a", "expected", sha256_bytes(b"new"), 0o644),
        ChangeRule("guard/b", "unchanged", sha256_bytes(b"same"), 0o644),
        ChangeRule("guard/c", "unchanged", sha256_bytes(b"old"), 0o644),
    )
    result = compare_snapshots(before, after, rules)
    assert [item.logical_path for item in result.expected_changes] == ["managed/a"]
    assert [item.logical_path for item in result.required_unchanged] == ["guard/b"]
    assert [item.logical_path for item in result.unexpected_changes] == ["guard/c"]
    assert result.ok is False


def test_private_digest_never_enters_human_report():
    result = comparison_with_private_threading_record("f" * 64)
    report = render_comparison(result)
    assert "f" * 64 not in report
    assert "[private local digest verified]" in report
```

Also test absent-to-present, present-to-absent, wrong mode, non-regular target,
duplicate logical path, different manifest/root IDs, missing rule, extra record,
sorted canonical bytes, root ID derived from `(st_dev, st_ino)` rather than an
absolute path, stdout-only snapshot causing no writes, and mode-0600 explicit
output.

- [ ] **Step 2: Run the focused tests and confirm RED**

Run: `rtk uv run pytest tests/test_bootstrap_snapshot.py -q`

Expected: import failure for `forza_bootstrap.snapshot`.

- [ ] **Step 3: Implement finite-scope capture and comparison**

```python
@dataclass(frozen=True)
class FileRecord:
    logical_path: str
    state: Literal["absent", "regular"]
    mode: int | None
    size: int | None
    sha256: str | None
    private: bool = False

@dataclass(frozen=True)
class ChangeRule:
    logical_path: str
    policy: Literal["expected", "unchanged"]
    after_sha256: str | None
    after_mode: int | None

@dataclass(frozen=True)
class Snapshot:
    version: int
    transaction_id: str
    manifest_sha256: str
    steam_root_id: str
    records: tuple[FileRecord, ...]

@dataclass(frozen=True)
class ComparedRecord:
    logical_path: str
    before: FileRecord
    after: FileRecord
    policy: Literal["expected", "unchanged"]

@dataclass(frozen=True)
class Comparison:
    expected_changes: tuple[ComparedRecord, ...]
    required_unchanged: tuple[ComparedRecord, ...]
    unexpected_changes: tuple[ComparedRecord, ...]

    @property
    def ok(self) -> bool:
        return not self.unexpected_changes
```

Capture only paths explicitly supplied by `build_snapshot_scope()`. Read each
path without following links, stream SHA-256 in 1 MiB blocks, and reject
directories/special files where a regular/absent record is required. Canonical
JSON contains logical roots only. `snapshot` prints canonical JSON unless an
absolute `--output` is supplied; `compare` exits 0 only when `Comparison.ok`.

- [ ] **Step 4: Run snapshot, CLI, and privacy gates**

Run: `rtk uv run pytest tests/test_bootstrap_snapshot.py tests/test_bootstrap_cli.py tests/test_repository_policy.py -q`

Expected: all tests pass and no private digest/path fixture enters tracked text.

- [ ] **Step 5: Commit snapshot evidence support**

```bash
rtk git add forza_bootstrap/snapshot.py forza_bootstrap/cli.py tests/test_bootstrap_snapshot.py
rtk git commit -m "feat: compare bounded bootstrap snapshots"
```

### Task 4: Verified cache and safe archive extraction

**Files:**
- Create: `forza_bootstrap/artifacts.py`
- Create: `tests/test_bootstrap_artifacts.py`
- Create: `tools/verify-bootstrap-bundle`

**Interfaces:**
- Consumes: `DownloadSpec`, `ArtifactSpec`, safe I/O primitives.
- Produces: `verify_cached_file(path, expected_size, expected_sha256) -> bool` (false only when absent; mismatch raises), `download_once(spec, cache_root, opener) -> Path`, `inspect_tar(path, policy) -> ArchiveGraph`, `extract_bundle(path, destination, bundle_manifest)`, `extract_ge(path, destination, expected_root)`, and `verify_bundle_root(root, manifest) -> VerifiedBundle`.

```python
@dataclass(frozen=True)
class ArchiveMember:
    path: str
    kind: Literal["directory", "regular", "symlink", "hardlink"]
    size: int
    mode: int
    link_target: str | None

@dataclass(frozen=True)
class ArchiveGraph:
    root: str
    members: tuple[ArchiveMember, ...]
    total_regular_size: int

@dataclass(frozen=True)
class VerifiedBundle:
    root: Path
    manifest_sha256: str
    artifacts: tuple[ArtifactSpec, ...]
```

- [ ] **Step 1: Write failing download and hostile-archive tests**

```python
@pytest.mark.parametrize(
    "member",
    ["/absolute", "../escape", "root/../../escape", "root//duplicate"],
)
def test_bundle_rejects_unsafe_member_names(tmp_path, member):
    archive = make_tar_zst(tmp_path, [(member, b"payload", "file")])
    with pytest.raises(BootstrapError, match="unsafe archive member"):
        extract_bundle(archive, tmp_path / "out", bundle_manifest_for(archive))


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo", "device"])
def test_bundle_rejects_links_and_special_files(tmp_path, kind):
    archive = make_tar_zst(tmp_path, [("forza-bootstrap-bundle-v1/member", b"target", kind)])
    with pytest.raises(BootstrapError, match="regular files and directories"):
        extract_bundle(archive, tmp_path / "out", bundle_manifest_for(archive))


def test_failed_download_never_replaces_verified_cache(tmp_path):
    cached = write_verified_cache(tmp_path, b"accepted")
    with pytest.raises(BootstrapError, match="download digest mismatch"):
        download_once(spec_for(b"accepted"), tmp_path, opener_returning(b"wrong"))
    assert cached.read_bytes() == b"accepted"
```

Add cases for duplicate normalized members, too many members, declared and
expanded size caps, truncated zstd/tar streams, wrong bundle manifest hash,
wrong member mode/size/hash, cache symlink, HTTP redirect away from HTTPS, no
implicit retry, GE relative link within root accepted, and GE absolute,
escaping, dangling, cyclic, or undeclared link rejected.

- [ ] **Step 2: Run focused tests and confirm RED**

Run: `rtk uv run pytest tests/test_bootstrap_artifacts.py -q`

Expected: import failure for `forza_bootstrap.artifacts`.

- [ ] **Step 3: Implement streaming verification and two extraction policies**

```python
@dataclass(frozen=True)
class ArchivePolicy:
    expected_root: str
    max_members: int
    max_total_size: int
    allow_contained_links: bool

def download_once(spec: DownloadSpec, cache_root: Path, opener=urlopen) -> Path:
    ensure_private_directory(cache_root)
    parent_fd = open_owned_root(cache_root)
    try:
        final_name = spec.filename
        if verify_cached_file(cache_root / final_name, spec.size, spec.sha256):
            return cache_root / final_name
        temp_name = f".{final_name}.{secrets.token_hex(12)}"
        fd = os.open(temp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent_fd)
        total = 0
        digest = hashlib.sha256()
        try:
            with opener(spec.url) as response:
                final_url = urlparse(response.geturl())
                if final_url.scheme != "https" or final_url.hostname not in spec.allowed_redirect_hosts:
                    raise BootstrapError("download redirect is outside the HTTPS allowlist")
                while block := response.read(1024 * 1024):
                    total += len(block)
                    if total > spec.size:
                        raise BootstrapError("download exceeds pinned size")
                    digest.update(block)
                    write_all(fd, block)
            os.fsync(fd)
        except BaseException:
            os.close(fd)
            os.unlink(temp_name, dir_fd=parent_fd)
            raise
        os.close(fd)
        if total != spec.size or digest.hexdigest() != spec.sha256:
            os.unlink(temp_name, dir_fd=parent_fd)
            raise BootstrapError("download digest mismatch")
        rename_noreplace(parent_fd, temp_name, parent_fd, final_name)
        os.fsync(parent_fd)
        return cache_root / final_name
    finally:
        os.close(parent_fd)
```

Use `subprocess.Popen(["zstd", "-dc", "--", archive], stdout=PIPE)` without a
shell for `.tar.zst`, and `tarfile.open(..., mode="r|*")` for the stream. First
build and validate the complete normalized member graph; then reopen and
extract. The bundle policy accepts directories/regular files only. The
GE-Proton policy accepts relative links only when their fully normalized target
is a declared member inside the one expected root. Reject special files in both
policies. Publish extraction only after verifying all declared members.

- [ ] **Step 4: Run artifact and repository privacy tests**

Run: `rtk uv run pytest tests/test_bootstrap_artifacts.py tests/test_repository_policy.py -q`

Expected: all tests pass; synthetic PE-like bytes remain generated only under pytest temporary roots.

- [ ] **Step 5: Commit verified artifact handling**

```bash
rtk git add forza_bootstrap/artifacts.py tests/test_bootstrap_artifacts.py tools/verify-bootstrap-bundle
rtk git commit -m "feat: verify bootstrap artifacts safely"
```

### Task 5: Frozen ProtonUp acquisition and dedicated GE-Proton staging

**Files:**
- Create: `tools/protonup/pyproject.toml`
- Create: `tools/protonup/uv.lock`
- Create: `forza_bootstrap/proton.py`
- Create: `tests/test_bootstrap_proton.py`
- Modify: `.gitignore`

**Interfaces:**
- Consumes: verified GE `DownloadSpec`, safe GE extractor, private cache/stage roots.
- Produces: `protonup_argv(repo_root, cache_root) -> tuple[str, ...]`, `acquire_ge(manifest, roots, runner) -> Path`, `prepare_fm_tree(extracted_ge, stage) -> ToolIdentity`, `inspect_existing_fm(path, manifest) -> ToolDisposition`, `publish_fm(stage, destination)`, and `rollback_published_fm(record)`.

```python
class ToolDisposition(str, Enum):
    ABSENT = "absent"
    CREATED = "created"
    ADOPTED = "adopted"
    CONFLICT = "conflict"

@dataclass(frozen=True)
class ToolIdentity:
    name: str
    base_release: str
    vdf_sha256: str
    managed_tree_sha256: str
    marker_sha256: str | None
```

- [ ] **Step 1: Write failing argv, identity, adoption, and conflict tests**

```python
def test_protonup_download_is_frozen_and_cannot_target_steam(tmp_path):
    argv = protonup_argv(Path("/repo"), tmp_path / "cache")
    assert argv == (
        "uv", "run", "--frozen", "--project", "/repo/tools/protonup",
        "protonup", "--download", "-t", "GE-Proton11-3",
        "-o", str(tmp_path / "cache"), "-y",
    )
    assert "compatibilitytools.d" not in "\0".join(argv)


def test_identity_rewrite_changes_only_staged_compatibilitytool_vdf(tmp_path):
    source = extracted_ge_fixture(tmp_path)
    before = tree_manifest(source)
    identity = prepare_fm_tree(source, tmp_path / "stage")
    after = tree_manifest(tmp_path / "stage")
    assert identity.name == "GE-Proton11-3-FM"
    assert changed_paths(before, after) == {"compatibilitytool.vdf", ".forza-bootstrap.json"}


def test_unknown_existing_tool_is_preserved(tmp_path):
    destination = tmp_path / "GE-Proton11-3-FM"
    destination.mkdir()
    (destination / "unknown").write_bytes(b"user")
    before = tree_manifest(destination)
    with pytest.raises(BootstrapError, match="existing compatibility tool conflicts"):
        inspect_existing_fm(destination, fixture_manifest())
    assert tree_manifest(destination) == before
```

Also test verified cache reuse without invoking ProtonUp, wrong generated
archive rejected, exact managed state adopted without marker, canonical
`GE-Proton11-3` untouched, exact destination no-op, `RENAME_NOREPLACE`
conflict, identity parser refusing duplicate keys, and rollback preserving a
post-publication change.

- [ ] **Step 2: Run the focused tests and confirm RED**

Run: `rtk uv run pytest tests/test_bootstrap_proton.py -q`

Expected: import failure for `forza_bootstrap.proton`.

- [ ] **Step 3: Lock ProtonUp 0.1.5 and implement exact download invocation**

```toml
# tools/protonup/pyproject.toml
[project]
name = "forza-bootstrap-protonup-runner"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = ["protonup==0.1.5"]
```

Run `rtk uv lock --project tools/protonup`, review that the lock resolves
`protonup==0.1.5`, and commit the complete generated `uv.lock`. Execute only
the argv asserted in the test, with `shell=False`, an allowlisted environment,
and no retry. Independently verify resulting archive size and SHA-256 before
extracting it. Add `!tools/protonup/uv.lock` after the repository-wide
`uv.lock` ignore rule so this security lock is tracked intentionally.

- [ ] **Step 4: Implement staged identity rewrite, adoption, publication, and rollback**

Require the original VDF to contain one exact GE-Proton11-3 tool/name pair.
Write the FM VDF and `.forza-bootstrap.json` atomically inside the private
stage. Record a tree manifest that hashes regular files and link text. Publish
the complete directory with `renameat2(RENAME_NOREPLACE)`. Existing unmarked
trees may be adopted only when the VDF identity and every bootstrap-managed
runtime/patch target have supported hashes; adoption never writes a marker.

- [ ] **Step 5: Run Proton, artifact, and exact-build tests**

Run: `rtk uv run pytest tests/test_bootstrap_proton.py tests/test_bootstrap_artifacts.py tests/test_patcher.py -q`

Expected: all tests pass and canonical fixture GE remains byte-identical.

- [ ] **Step 6: Commit frozen ProtonUp and dedicated-tool logic**

```bash
rtk git add .gitignore tools/protonup forza_bootstrap/proton.py tests/test_bootstrap_proton.py
rtk git commit -m "feat: prepare dedicated Proton tool with ProtonUp"
```

### Task 6: Bundle-aware runtime-component transaction input

**Files:**
- Modify: `scripts/install-runtime-components:388-655,1510-end`
- Modify: `tests/test_runtime_components.py`

**Interfaces:**
- Consumes: a verified bundle root whose manifest binds exact profile and source revisions.
- Produces: `artifact_inputs_from_bundle(bundle_root, bundle_manifest)`, evidence schema version 3 with `input_kind`, and CLI alternative `--artifact-bundle-root ABS --bundle-manifest ABS`; existing source-build arguments remain supported.

- [ ] **Step 1: Write failing bundle-input compatibility tests**

```python
def test_bundle_input_locks_profile_and_artifacts_without_git_sources(tmp_path):
    fixture = setup_fixture(tmp_path, input_kind="bundle")
    run_tool(fixture, "lock-evidence")
    evidence = json.loads(fixture["evidence"].read_text())
    assert evidence["version"] == 3
    assert evidence["input_kind"] == "bundle"
    assert evidence["runtime_profile"] == "fixture"
    assert evidence["source_revisions"] == {
        "xgameruntime_git_sha": fixture["xgameruntime_revision"],
        "xodus_git_sha": fixture["xodus_revision"],
        "integration_git_sha": fixture["integration_revision"],
    }


def test_bundle_input_refuses_revision_or_artifact_drift_before_plan(tmp_path):
    fixture = setup_fixture(tmp_path, input_kind="bundle")
    mutate_bundle_artifact(fixture, "xodus-service")
    failed = run_tool(fixture, "lock-evidence", check=False)
    assert failed.returncode != 0
    assert "bundle artifact digest mismatch" in failed.stderr
    assert_runtime_destinations_unchanged(fixture)
```

Add cases for both input modes supplied, neither supplied, wrong profile,
changed bundle manifest after evidence lock, v2 source evidence remaining
accepted for existing journals, v3 malformed/unknown-key rejection, and a full
bundle plan/install/rollback round trip.

- [ ] **Step 2: Run the focused tests and confirm RED**

Run: `rtk uv run pytest tests/test_runtime_components.py -q`

Expected: new bundle cases fail because the CLI requires Git build directories.

- [ ] **Step 3: Add a mutually exclusive verified-bundle input mode**

```python
parser.add_argument("--artifact-bundle-root", type=absolute_argument("artifact bundle root"))
parser.add_argument("--bundle-manifest", type=absolute_argument("bundle manifest"))
parser.add_argument("--xgameruntime-build-dir", type=absolute_argument("XGameRuntime build directory"))
parser.add_argument("--xodus-build-dir", type=absolute_argument("Xodus build directory"))

bundle_mode = arguments.artifact_bundle_root is not None or arguments.bundle_manifest is not None
source_mode = arguments.xgameruntime_build_dir is not None or arguments.xodus_build_dir is not None
if bundle_mode == source_mode:
    parser.error("choose exactly one complete artifact input mode")
if bundle_mode and None in (arguments.artifact_bundle_root, arguments.bundle_manifest):
    parser.error("bundle mode requires root and manifest")
if source_mode and None in (arguments.xgameruntime_build_dir, arguments.xodus_build_dir):
    parser.error("source-build mode requires both build directories")
```

This keeps the historical pair of build directories valid without a new mode
flag. Bundle source paths map `bin/{xodus-service,xodus-cli,xodus-overlay}` and
`runtime/{xgameruntime.dll,xgameruntime.so}` directly to the five existing
roles and are re-hashed by the runtime installer before its own evidence lock.
Evidence v3 binds input kind, bundle manifest digest, profile, source revisions,
and artifacts. Journal v1/v2 recovery/status paths remain unchanged.

- [ ] **Step 4: Run all runtime transaction tests**

Run: `rtk uv run pytest tests/test_runtime_components.py tests/test_secure_user_files.py -q`

Expected: new bundle cases and all legacy/journal cases pass.

- [ ] **Step 5: Commit bundle input support**

```bash
rtk git add scripts/install-runtime-components tests/test_runtime_components.py
rtk git commit -m "feat: accept verified runtime bundles"
```

### Task 7: Private licensed threading-DLL transaction

**Files:**
- Create: `forza_bootstrap/licensed.py`
- Create: `tests/test_bootstrap_licensed.py`

**Interfaces:**
- Consumes: absolute source path, prefix root, bootstrap transaction directory.
- Produces: `validate_pe64(path) -> PrivateFileIdentity`, `plan_threading_copy(source, destination) -> LicensedAction`, `install_threading_copy(action, journal)`, and `rollback_threading_copy(journal)`.

```python
@dataclass(frozen=True)
class PrivateFileIdentity:
    size: int
    sha256: str

@dataclass(frozen=True)
class LicensedAction:
    destination_logical: str
    source_size: int
    source_sha256: str
    before: FileRecord
    disposition: Literal["install", "replace", "keep"]
```

- [ ] **Step 1: Write failing PE validation, redaction, no-op, and rollback tests**

```python
def test_valid_pe64_is_identified_without_persisting_source_path(tmp_path):
    source = make_synthetic_pe(tmp_path / "licensed.dll", machine=0x8664)
    identity = validate_pe64(source)
    record = plan_threading_copy(source, tmp_path / "prefix/xgameruntime.dll.threading")
    assert identity.size == source.stat().st_size
    assert record.source_sha256 == identity.sha256
    assert str(source) not in json.dumps(record.private_json())
    assert identity.sha256 not in record.public_summary()


@pytest.mark.parametrize("machine", [0x014C, 0xAA64])
def test_non_x86_64_pe_is_refused(tmp_path, machine):
    source = make_synthetic_pe(tmp_path / "wrong.dll", machine=machine)
    with pytest.raises(BootstrapError, match="x86_64 PE"):
        validate_pe64(source)


def test_install_then_rollback_restores_exact_previous_destination(tmp_path):
    source, destination = licensed_fixture(tmp_path, before=b"previous")
    action = plan_threading_copy(source, destination)
    install_threading_copy(action, tmp_path / "journal")
    rollback_threading_copy(tmp_path / "journal")
    assert destination.read_bytes() == b"previous"
```

Add source symlink, destination symlink, oversize, malformed DOS header,
out-of-range PE offset, missing PE signature, exact-existing no-op, absent
destination rollback removal, interruption at every rename, replacement after
install preserved as recovery evidence, file/directory fsync, mode 0600 state,
and stdout/stderr digest/path redaction.

- [ ] **Step 2: Run focused tests and confirm RED**

Run: `rtk uv run pytest tests/test_bootstrap_licensed.py -q`

Expected: import failure for `forza_bootstrap.licensed`.

- [ ] **Step 3: Implement local-only PE copy transaction**

Parse DOS `MZ`, little-endian `e_lfanew` at `0x3c`, `PE\0\0`, and machine
`0x8664`; cap input at 256 MiB. Read through an `O_NOFOLLOW` descriptor owned
by the current user. Store only size/digest and logical destination in private
state, never the source path. Stage adjacent to the destination, preserve an
existing regular file by rename, publish with no-replace/exchange semantics,
and derive resume/rollback from current/stage/backup hashes.

- [ ] **Step 4: Run licensed, state, snapshot, and privacy tests**

Run: `rtk uv run pytest tests/test_bootstrap_licensed.py tests/test_bootstrap_state.py tests/test_bootstrap_snapshot.py tests/test_repository_policy.py -q`

Expected: all tests pass with no tracked synthetic binary.

- [ ] **Step 5: Commit the licensed-input boundary**

```bash
rtk git add forza_bootstrap/licensed.py tests/test_bootstrap_licensed.py
rtk git commit -m "feat: transact licensed threading runtime privately"
```

### Task 8: Rootless Docker builder and deterministic bundle producer

**Files:**
- Create: `forza_bootstrap/container_build.py`
- Create: `containers/bootstrap-builder/Dockerfile`
- Create: `containers/bootstrap-builder/fetch-xodus-deps.sh`
- Create: `containers/bootstrap-builder/build-bundle.sh`
- Create: `tools/build-bootstrap-builder`
- Create: `tools/build-bootstrap-bundle`
- Create: `tests/test_bootstrap_container_build.py`
- Create: `tests/fixtures/bootstrap/docker-info-rootless.txt`

**Interfaces:**
- Consumes: exact source revisions, pinned builder digest, clean source/cache/output roots.
- Produces: `check_docker(runner) -> DockerCapability`, `fetch_source(spec, cache) -> Path`, `dependency_fetch_argv(inputs)`, `offline_build_argv(inputs, output)`, a verified builder-image driver, and a deterministic `forza-bootstrap-bundle-v1.tar.zst` plus `bundle-manifest.json`.

```python
@dataclass(frozen=True)
class DockerCapability:
    server_version: str
    architecture: str
    rootless: bool

@dataclass(frozen=True)
class BuildInputs:
    xodus_source: Path
    xgameruntime_source: Path
    dependency_root: Path
    output_root: Path
    builder_image: str
```

- [ ] **Step 1: Write failing Docker boundary and argv tests**

```python
def test_offline_build_has_only_declared_mounts_and_no_network(tmp_path):
    argv = offline_build_argv(build_inputs(tmp_path), tmp_path / "output")
    joined = "\0".join(argv)
    assert "--network=none" in argv
    assert "--read-only" in argv
    assert "--cap-drop=ALL" in argv
    assert "no-new-privileges" in joined
    assert str(Path.home()) not in joined
    assert "Steam" not in joined
    assert "xodus.sock" not in joined
    assert "xgameruntime.dll.threading" not in joined


def test_source_mode_rejects_dirty_or_wrong_revision(tmp_path):
    source = git_source_fixture(tmp_path, revision="1" * 40)
    (source / "dirty").write_text("change")
    with pytest.raises(BootstrapError, match="source tree is not clean"):
        validate_source(source, expected_revision="1" * 40)
```

Add rootless daemon accepted, unreachable/rootful-only daemon rejected for
source mode, image reference without digest rejected, network present on
offline build rejected, unknown mount rejected, output pre-existence rejected,
source fetch exact SHA and clean tree, dependency fetch limited to source/cache,
two fixture builds byte-identical, sorted tar metadata, and wrong output hash
reported rather than adopted.

- [ ] **Step 2: Run focused tests and confirm RED**

Run: `rtk uv run pytest tests/test_bootstrap_container_build.py -q`

Expected: import failure for `forza_bootstrap.container_build`.

- [ ] **Step 3: Implement the host driver and immutable builder**

Use the exact `archlinux:base-devel@sha256:694da1fce635e3a14d90751941b08f02c500e8724682f7a00768e7152251ec34` x86_64 manifest. Point
pacman at `https://archive.archlinux.org/repos/2026/09/01/$repo/os/$arch` before
installing the reviewed build package list. Create a non-root `builder` user in
the final image. Fetch and SHA-verify LLVM-MinGW 20260826 UCRT archive digest
`cee8d2ce3da5145ce4dc882e70d0b0719a783d53a99752c60948fc0659975a65`.

The dependency-fetch invocation may use network only to create an external
Cargo vendor/cache input. The compiler invocation uses `--network=none`, fixed
mount destinations `/src/xodus`, `/src/xgameruntime`, `/deps`, and `/output`,
fixed `SOURCE_DATE_EPOCH`, locale/timezone, single-threaded final zstd, sorted
tar names, mtime 0, uid/gid 0, and no build-id where supported.

```bash
cargo build --release --locked --offline -p xodus-service -p xodus-cli -p xodus-overlay
autoreconf -f
uv run --offline ./dlls/winevulkan/make_vulkan
./tools/make_specfiles
./tools/make_requests
./configure --enable-win64 --without-ffmpeg --without-opencl
make -C dlls/xgameruntime -j2
```

The container copies exactly the three Xodus binaries and the PE/Unix
XGameRuntime pair into the documented bundle layout, writes source/toolchain
provenance and license texts, then builds the deterministic archive.

- [ ] **Step 4: Run mocked boundary tests and one fixture container build**

Run: `rtk uv run pytest tests/test_bootstrap_container_build.py -q`

Run: `rtk ./tools/build-bootstrap-bundle --fixture --output "$(rtk mktemp -d)"`

Expected: tests pass; fixture command produces one verified bundle without any home/Steam mount.

- [ ] **Step 5: Commit the Docker source-build path**

```bash
rtk git add forza_bootstrap/container_build.py containers/bootstrap-builder tools/build-bootstrap-builder tools/build-bootstrap-bundle tests/test_bootstrap_container_build.py tests/fixtures/bootstrap/docker-info-rootless.txt
rtk git commit -m "feat: build bootstrap bundle in rootless Docker"
```

### Task 9: Phase A Prepare coordinator

**Files:**
- Create: `forza_bootstrap/coordinator.py`
- Create: `tests/test_bootstrap_prepare.py`
- Modify: `forza_bootstrap/cli.py`
- Modify: `scripts/forza-bootstrap`

**Interfaces:**
- Consumes: manifest, state, snapshot, artifacts, Proton/Docker sources, confirmation callback.
- Produces: `ResolvedHost`, `resolve_supported_host(env)`, `build_prepare_plan(context) -> dict`, and `prepare(context, confirm) -> BootstrapState` ending at `AWAITING_STEAM_PREFIX`.

```python
@dataclass(frozen=True)
class ResolvedHost:
    os_id: str
    architecture: str
    user_root: Path
    steam_root: Path
    app_manifest: Path
    compatibility_tools: Path
    state_root: Path
    cache_root: Path
    runtime_dir: Path
```

- [ ] **Step 1: Write failing host/preflight and Prepare-order tests**

```python
def test_prepare_from_clean_root_is_plan_bound_and_stops_for_steam(tmp_path):
    fixture = prepare_fixture(tmp_path)
    result = run_prepare(fixture, confirmations=[fixture.prepare_digest])
    assert result.phase is BootstrapPhase.AWAITING_STEAM_PREFIX
    assert fixture.actions == [
        "preflight", "print-prepare-plan", "confirm-prepare-plan",
        "write-before-snapshot", "acquire-ge", "acquire-bundle",
        "publish-fm-tool", "checkpoint-awaiting-prefix",
    ]
    assert "restart Steam" in fixture.stdout
    assert "select GE-Proton11-3-FM" in fixture.stdout
    assert "launch options empty" in fixture.stdout
    assert "Launch Forza yourself" in fixture.stdout


@pytest.mark.parametrize("os_id,arch,message", [
    ("ubuntu", "x86_64", "Arch Linux"),
    ("arch", "aarch64", "x86_64"),
])
def test_prepare_unsupported_host_is_read_only(tmp_path, os_id, arch, message):
    fixture = prepare_fixture(tmp_path, os_id=os_id, arch=arch)
    before = tree_manifest(tmp_path)
    with pytest.raises(BootstrapError, match=message):
        run_prepare(fixture)
    assert tree_manifest(tmp_path) == before
```

Add missing Steam/AppID, Flatpak Steam, wrong ownership, insufficient disk,
Forza process active, Xodus service active, unfinished runtime child,
confirmation mismatch, bundle/source mutual exclusion, Docker absent only in
source mode, exact existing tool adoption, and interruption after every
Prepare boundary.

- [ ] **Step 2: Run focused tests and confirm RED**

Run: `rtk uv run pytest tests/test_bootstrap_prepare.py -q`

Expected: import or missing-handler failure.

- [ ] **Step 3: Implement read-only host resolution and digest-bound Prepare**

Read `/etc/os-release`, `platform.machine()`, resolved native Steam candidates,
AppID library manifest, disk free space, launcher lock, systemd user service,
and process checks before state creation. Inject all external runners in tests.
The plan binds manifest SHA, roots by logical ID, acquisition mode, cache
objects, destination before-state, and exact after identity. Print and compare
the full lowercase digest before writing `before.json` or cache/stage state.

After successful publication, persist the bundle/extracted roots by logical
private-state reference, checkpoint `AWAITING_STEAM_PREFIX`, and exit 0 with
manual Steam instructions. Never call `steam`, `xdg-open`, Proton, or a game
executable.

- [ ] **Step 4: Run Prepare, state, Proton, and artifact suites**

Run: `rtk uv run pytest tests/test_bootstrap_prepare.py tests/test_bootstrap_state.py tests/test_bootstrap_proton.py tests/test_bootstrap_artifacts.py -q`

Expected: all tests pass.

- [ ] **Step 5: Commit Phase A**

```bash
rtk git add forza_bootstrap/coordinator.py forza_bootstrap/cli.py scripts/forza-bootstrap tests/test_bootstrap_prepare.py
rtk git commit -m "feat: prepare resumable Forza bootstrap"
```

### Task 10: Phase B unified plan and child transaction composition

**Files:**
- Modify: `forza_bootstrap/coordinator.py`
- Create: `tests/test_bootstrap_finish.py`
- Modify: `forza_bootstrap/cli.py`

**Interfaces:**
- Consumes: `AWAITING_STEAM_PREFIX` state, prefix, licensed input, verified bundle, runtime installer, patcher, doctor, options generator.
- Produces: `validate_prefix(context)`, `build_finish_plan(context) -> FinishPlan`, `finish(context, confirm) -> BootstrapState`, child transaction/patch backup references, `after.json`, and `comparison.json`.

```python
@dataclass(frozen=True)
class FinishPlan:
    document: dict[str, object]
    sha256: str
    runtime_plan_sha256: str
    patch_after_sha256: tuple[str, ...]
    snapshot_rules: tuple[ChangeRule, ...]
```

- [ ] **Step 1: Write failing Phase B command-order and digest-binding tests**

```python
def test_finish_binds_and_executes_all_children_in_order(tmp_path):
    fixture = finish_fixture(tmp_path)
    state = run_finish(fixture, confirmations=[fixture.finish_digest])
    assert state.phase is BootstrapPhase.READY_TO_ATTEMPT
    assert fixture.actions == [
        "validate-prefix", "validate-threading-input", "lock-runtime-evidence",
        "runtime-plan", "patch-inspect", "print-finish-plan", "confirm-finish-plan",
        "install-threading", "runtime-install", "runtime-status", "patch-apply",
        "doctor", "write-after-snapshot", "compare-snapshots", "steam-options",
    ]
    assert fixture.finish_plan["runtime_plan_sha256"] == fixture.runtime_plan_sha256
    assert fixture.finish_plan["patch_after_sha256"] == fixture.supported_patched_hashes
    assert "READY TO ATTEMPT" in fixture.stdout


def test_finish_doctor_failure_retains_recovery_and_never_claims_ready(tmp_path):
    fixture = finish_fixture(tmp_path, doctor_status=1)
    with pytest.raises(BootstrapError, match="doctor found blockers"):
        run_finish(fixture, confirmations=[fixture.finish_digest])
    assert load_state(fixture).phase is BootstrapPhase.INSTALLING_FINISH
    assert fixture.recovery_material_is_present()
    assert "READY TO ATTEMPT" not in fixture.stdout
```

Add missing/incomplete prefix, symlinked prefix component, active game/service,
missing licensed option, wrong top digest, runtime plan changing after top plan,
runtime child failure, mixed patch state, patch failure, snapshot unexpected
change, launch-options failure, licensed digest redaction, and successful exact
no-op/adoption Finish.

- [ ] **Step 2: Run focused tests and confirm RED**

Run: `rtk uv run pytest tests/test_bootstrap_finish.py -q`

Expected: missing Finish behavior.

- [ ] **Step 3: Implement exact prefix and unified-plan validation**

Require `steamapps/compatdata/2440510/pfx/drive_c/windows/system32` beneath the
resolved Steam root with current-user-owned, no-follow directory traversal.
Before confirmation, create only private evidence needed to obtain the child
runtime plan. The top plan includes the child plan digest, licensed
source/destination identities with public redaction, patch states and exact
after hashes, snapshot rules, and command versions.

After confirmation, revalidate every plan input. Pass the exact child digest to
`install-runtime-components install`, capture its immutable transaction ID,
capture the exact `patch-known-build apply-forza` backup-manifest path, and
persist each completed boundary before starting the next. Invoke child tools as
argument arrays with `shell=False` and a minimal environment.

- [ ] **Step 4: Implement final snapshot/report and readiness output**

Run doctor only after installation and patching. Capture `after.json`, compare
against the policy created before mutation, write `comparison.json`, and refuse
`READY_TO_ATTEMPT` if any unexpected entry exists. On success print the human
comparison summary and exact output of `scripts/print-steam-options`; never edit
Steam or launch the game.

- [ ] **Step 5: Run Finish and all delegated-owner suites**

Run: `rtk uv run pytest tests/test_bootstrap_finish.py tests/test_runtime_components.py tests/test_patcher.py tests/test_secure_user_files.py -q`

Expected: all tests pass.

- [ ] **Step 6: Commit Phase B**

```bash
rtk git add forza_bootstrap/coordinator.py forza_bootstrap/cli.py tests/test_bootstrap_finish.py
rtk git commit -m "feat: finish bootstrap with one verified plan"
```

### Task 11: Resume, reverse rollback, and exact-state no-op behavior

**Files:**
- Modify: `forza_bootstrap/coordinator.py`
- Modify: `forza_bootstrap/state.py`
- Create: `tests/test_bootstrap_recovery.py`

**Interfaces:**
- Consumes: durable coordinator/child/patch/licensed/compatibility records.
- Produces: `resume(context) -> BootstrapState`, `rollback(context, confirm) -> BootstrapState`, and deterministic recovery routing for every durable state.

- [ ] **Step 1: Write failing interruption-matrix and rollback-order tests**

```python
@pytest.mark.parametrize("boundary", ALL_BOOTSTRAP_BOUNDARIES)
def test_resume_after_every_boundary_is_idempotent(tmp_path, boundary):
    fixture = crash_fixture(tmp_path, boundary)
    fixture.run_until_crash()
    before_resume = fixture.managed_snapshot()
    state = fixture.resume()
    assert state.phase in {BootstrapPhase.AWAITING_STEAM_PREFIX, BootstrapPhase.READY_TO_ATTEMPT}
    assert fixture.no_duplicate_publications()
    assert fixture.unexpected_replacements_are_preserved()


def test_rollback_is_reverse_dependency_order(tmp_path):
    fixture = completed_finish_fixture(tmp_path)
    state = fixture.rollback(confirm="ROLLBACK")
    assert fixture.actions[-4:] == [
        "patch-restore", "runtime-rollback", "threading-rollback", "compat-tool-rollback",
    ]
    assert state.phase is BootstrapPhase.ROLLED_BACK
    assert fixture.managed_snapshot() == fixture.original_snapshot
```

Add declined confirmation no-op, accepted transaction refusing ordinary
rollback, changed patch target, child recovery-required, changed licensed
destination, created FM tool changed after publication, adopted FM tool never
deleted, missing backup, crash during each rollback boundary, second rollback
idempotence, and one unfinished coordinator maximum.

- [ ] **Step 2: Run focused tests and confirm RED**

Run: `rtk uv run pytest tests/test_bootstrap_recovery.py -q`

Expected: missing resume/rollback handlers.

- [ ] **Step 3: Implement state-derived resume and composed rollback**

For each boundary, inspect current/stage/backup hashes before deciding whether
the prior action completed. Never infer completion solely from the state enum.
Resume may repeat verification but not publication. Rollback confirms the word
`ROLLBACK`, checkpoints `ROLLING_BACK`, calls patch restore, runtime rollback,
licensed restore/remove, then compatibility-tool removal only for a proven
unchanged tool created by this transaction. On any ambiguity set
`RECOVERY_REQUIRED`, print exact private journal location, and retain evidence.

- [ ] **Step 4: Run recovery plus inherited interruption tests**

Run: `rtk uv run pytest tests/test_bootstrap_recovery.py tests/test_runtime_components.py tests/test_patcher.py tests/test_secure_user_files.py -q`

Expected: all tests pass.

- [ ] **Step 5: Commit resumability and rollback**

```bash
rtk git add forza_bootstrap/coordinator.py forza_bootstrap/state.py tests/test_bootstrap_recovery.py
rtk git commit -m "feat: resume and roll back bootstrap safely"
```

### Task 12: Public `setup` integration and hermetic clean-start matrix

**Files:**
- Modify: `setup:8-15,183-end`
- Create: `tests/test_bootstrap_cli.bash`
- Create: `tests/test_bootstrap_clean_start.py`
- Modify: `tests/test_setup.bash`
- Modify: `justfile`

**Interfaces:**
- Consumes: `scripts/forza-bootstrap` command surface and all coordinator modes.
- Produces: `./setup bootstrap`, `./setup snapshot`, `./setup compare`, and `./setup rollback` routing without changing the historical default guided workflow.

- [ ] **Step 1: Write failing black-box dispatcher tests**

```bash
test_setup_preserves_bootstrap_argv_exactly() {
    output=$(run_setup bootstrap --bundle "$BUNDLE" --threading-dll "$DLL") || return 1
    assert_contains "$ACTION_LOG" "bootstrap --bundle $BUNDLE --threading-dll $DLL"
}

test_setup_help_separates_bootstrap_from_legacy_guided_setup() {
    output=$(run_setup --help) || return 1
    assert_contains "$output" './setup bootstrap'
    assert_contains "$output" './setup snapshot'
    assert_contains "$output" './setup compare'
    assert_contains "$output" 'does not edit Steam or launch the game'
}
```

The Python clean-start test constructs an empty fake native-Steam root, runs
Prepare with fixture Proton/bundle sources, creates the minimum synthetic AppID
prefix, runs Finish with a synthetic PE, asserts exact final hashes and zero
unexpected changes, reruns bootstrap and asserts zero inode/mtime/content
changes, then rolls back and compares the exact original snapshot.

- [ ] **Step 2: Run the black-box tests and confirm RED**

Run: `rtk bash tests/test_bootstrap_cli.bash`

Run: `rtk uv run pytest tests/test_bootstrap_clean_start.py -q`

Expected: dispatcher rejects the new multi-argument commands and clean-start command is unavailable.

- [ ] **Step 3: Implement exact argument-preserving dispatch**

Replace the global one-argument limit with a command-aware case. Keep existing
zero-argument guided setup and single-argument legacy commands unchanged.

```bash
case "${1:-}" in
    bootstrap | snapshot | compare)
        command=$1
        shift
        exec "$ROOT/scripts/forza-bootstrap" "$command" "$@"
        ;;
    rollback)
        if (($# == 1)); then run_rollback; else usage; fi
        ;;
    # existing status/check/steam-options/uninstall/help branches remain
esac
```

Expose bootstrap rollback only as `./setup bootstrap --rollback`, so it cannot
collide with the existing runtime-only `./setup rollback`. The Python CLI uses
the `bootstrap` subcommand plus the same mutually exclusive flag.

- [ ] **Step 4: Complete both hermetic acquisition modes**

Run clean-start once with a preverified local fixture bundle and once with
`--build-from-source` using the fixture Docker builder. Assert both feed the
same bundle verifier/runtime installer path and produce identical managed
after-snapshots. Add a sentinel under fake Steam config and assert its inode,
mode, mtime, and digest are unchanged.

- [ ] **Step 5: Run all setup and clean-start tests**

Run: `rtk bash tests/test_setup.bash`

Run: `rtk bash tests/test_bootstrap_cli.bash`

Run: `rtk uv run pytest tests/test_bootstrap_clean_start.py -q`

Expected: all legacy and new setup tests pass.

- [ ] **Step 6: Commit the public entry point**

```bash
rtk git add setup justfile tests/test_setup.bash tests/test_bootstrap_cli.bash tests/test_bootstrap_clean_start.py
rtk git commit -m "feat: expose one-command hybrid bootstrap"
```

### Task 13: Produce and bind the real open-source release bundle

**Files:**
- Create: `manifests/bootstrap-v1.toml`
- Create: `tools/finalize-bootstrap-manifest`
- Create: `tests/test_bootstrap_release_manifest.py`
- Create: `.github/workflows/build-bundle.yml`
- Modify: `docs/publication-manifest.toml`
- Modify: `tests/test_repository_policy.py`

**Interfaces:**
- Consumes: two clean Docker builds from exact public component commits.
- Produces: concrete v0.2.0 bundle metadata, public asset name `forza-bootstrap-bundle-v1.tar.zst`, release URL, exact size/SHA/member records, and CI reproduction evidence without committing binaries.

- [ ] **Step 1: Write failing production-manifest and policy tests**

```python
def test_production_bootstrap_manifest_has_no_mutable_or_empty_release_fields():
    manifest = load_bootstrap_manifest(ROOT / "manifests/bootstrap-v1.toml")
    assert manifest.bundle.url == (
        "https://github.com/volcmen/forza-motorsport-linux/releases/download/"
        "v0.2.0/forza-bootstrap-bundle-v1.tar.zst"
    )
    assert manifest.bundle.size > 0
    assert len(manifest.bundle.sha256) == 64
    assert manifest.builder.base_image == (
        "docker.io/library/archlinux:base-devel@sha256:"
        "694da1fce635e3a14d90751941b08f02c500e8724682f7a00768e7152251ec34"
    )
    assert re.fullmatch(
        r"ghcr\.io/volcmen/forza-motorsport-builder@sha256:[0-9a-f]{64}",
        manifest.builder.image,
    )
    assert {source.revision for source in manifest.sources} == {
        "7b236772297b3475ea4f3cb830feb5b224f3064a",
        "a1548b1cf57371715d10b608bc81a77a188e40d4",
    }


def test_release_bundle_is_not_tracked_by_git():
    tracked = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).decode().split("\0")
    assert not [name for name in tracked if name.endswith((".dll", ".so", ".tar.zst"))]
```

Also require all five open-source artifact records, license/provenance members,
exact GE size/digest, ProtonUp 0.1.5, HTTPS URLs, release `v0.2.0`, and CI that
never accesses secrets or live Xbox endpoints.

- [ ] **Step 2: Run manifest/policy tests and confirm RED**

Run: `rtk uv run pytest tests/test_bootstrap_release_manifest.py tests/test_repository_policy.py -q`

Expected: production bootstrap manifest and workflow are absent.

- [ ] **Step 3: Build and publish the immutable builder image**

Build `containers/bootstrap-builder/Dockerfile` twice from the pinned base and
Arch snapshot, compare declared filesystem/package manifests, then publish the
reviewed image to `ghcr.io/volcmen/forza-motorsport-builder` under a candidate
tag. Resolve the registry's immutable `sha256:` manifest digest and use only the
digest-qualified reference for subsequent builds. The candidate tag is not a
trust input and may move; the digest may not.

```bash
rtk mkdir -p runtime/release
rtk ./tools/build-bootstrap-builder \
  --verify-twice \
  --push ghcr.io/volcmen/forza-motorsport-builder:v0.2.0-builder-rc | \
  rtk tee runtime/release/builder-ref.txt
rtk rg -x 'ghcr\.io/volcmen/forza-motorsport-builder@sha256:[0-9a-f]{64}' \
  runtime/release/builder-ref.txt
```

- [ ] **Step 4: Build twice from clean public sources and compare**

```bash
rtk ./tools/build-bootstrap-bundle --clean --output /tmp/forza-bundle-build-a
rtk ./tools/build-bootstrap-bundle --clean --output /tmp/forza-bundle-build-b
rtk sha256sum /tmp/forza-bundle-build-a/forza-bootstrap-bundle-v1.tar.zst
rtk sha256sum /tmp/forza-bundle-build-b/forza-bootstrap-bundle-v1.tar.zst
rtk cmp /tmp/forza-bundle-build-a/forza-bootstrap-bundle-v1.tar.zst /tmp/forza-bundle-build-b/forza-bootstrap-bundle-v1.tar.zst
```

Expected: `cmp` exits 0 and both SHA-256 lines are identical. If not, keep the
manifest absent, record which declared member differs, fix determinism with a
new failing regression, and repeat both clean builds.

- [ ] **Step 5: Generate the concrete production trust manifest**

`tools/finalize-bootstrap-manifest` takes the verified bundle path and resolved
builder-image digest, parses the bundle's
internal manifest, verifies the five exact component outputs and provenance,
and atomically writes TOML containing the concrete bundle size/SHA. It hardcodes
the approved immutable release URL, GE values, source commits, ProtonUp version,
builder digest, and Arch snapshot; it refuses a dirty repository or a bundle
that differs from the second build.

Run:

```bash
builder_ref=$(rtk sed -n '1p' runtime/release/builder-ref.txt)
rtk ./tools/finalize-bootstrap-manifest \
  --bundle /tmp/forza-bundle-build-a/forza-bootstrap-bundle-v1.tar.zst \
  --matching-bundle /tmp/forza-bundle-build-b/forza-bootstrap-bundle-v1.tar.zst \
  --builder-image "$builder_ref" \
  --output manifests/bootstrap-v1.toml
rtk ./tools/verify-bootstrap-bundle \
  --manifest manifests/bootstrap-v1.toml \
  /tmp/forza-bundle-build-a/forza-bootstrap-bundle-v1.tar.zst
```

Expected: manifest has concrete nonzero values and verifier exits 0.

- [ ] **Step 6: Add source-only CI reproduction workflow**

The workflow checks out exact source revisions into isolated directories,
builds the bundle through the pinned builder twice, compares outputs, verifies
the result against `bootstrap-v1.toml`, uploads it only as a workflow artifact,
and runs no Steam/game/account step. GitHub release publication remains an
explicit tag-time action.

- [ ] **Step 7: Run manifest and privacy gates**

Run: `rtk uv run pytest tests/test_bootstrap_release_manifest.py tests/test_repository_policy.py -q`

Run: `rtk uv run reuse lint`

Expected: all tests pass; no binary or live/private data is tracked.

- [ ] **Step 8: Commit concrete release inputs**

```bash
rtk git add manifests/bootstrap-v1.toml tools/finalize-bootstrap-manifest .github/workflows/build-bundle.yml docs/publication-manifest.toml tests/test_bootstrap_release_manifest.py tests/test_repository_policy.py
rtk git commit -m "build: bind reproducible bootstrap bundle"
```

### Task 14: Documentation, full verification, and live read-only SHA baseline

**Files:**
- Modify: `README.md`
- Modify: `docs/install.md`
- Modify: `docs/architecture.md`
- Modify: `docs/troubleshooting.md`
- Modify: `docs/verification.md`
- Create: `docs/release-v0.2.0.md`
- Modify: `docs/publication-manifest.toml`

**Interfaces:**
- Consumes: final command/help output, verified bundle manifest, clean-start results.
- Produces: user onboarding, exact limitations, recovery guide, release evidence, and a private current-host baseline snapshot.

- [ ] **Step 1: Write failing documentation policy assertions**

```python
def test_readme_documents_bootstrap_without_overclaiming():
    readme = (ROOT / "README.md").read_text()
    required = {
        "./setup bootstrap",
        "Arch Linux x86_64",
        "GE-Proton11-3-FM",
        "--build-from-source",
        "--threading-dll",
        "KDE Wallet",
        "Do not install GNOME Keyring",
        "does not edit Steam",
        "does not launch the game",
        "READY TO ATTEMPT",
        "incoming invite notifications are not supported",
    }
    missing = {value for value in required if value not in readme}
    assert not missing
```

Require the release document to distinguish automated, clean-start synthetic,
current-host read-only, and manual gameplay evidence; require exact rollback
ordering and SHA-report redaction language.

- [ ] **Step 2: Run documentation policy tests and confirm RED**

Run: `rtk uv run pytest tests/test_repository_policy.py -q`

Expected: new bootstrap documentation assertions fail.

- [ ] **Step 3: Rewrite onboarding around the resumable command**

Document the normal path:

```bash
git clone https://github.com/volcmen/forza-motorsport-linux.git
cd forza-motorsport-linux
./setup bootstrap
# restart Steam, select GE-Proton11-3-FM, first-launch and close Forza
./setup bootstrap --threading-dll /absolute/licensed/path/xgameruntime.dll
```

Also document offline `--bundle`, optional Docker source build, snapshot and
compare commands, exact manual Steam steps, clean launch options, Steam Input
boundary, KWallet/Secret Service, Xodus session lifecycle, no incoming invite
notifications, interruption/resume, and reverse rollback. State plainly that
automatic checks cannot prove game or Xbox functionality.

- [ ] **Step 4: Run the complete automated gate from the isolated worktree**

Run:

```bash
rtk uv sync --group dev
rtk env PATH=/home/volc/.local/share/forza-motorsport-linux/build-tools/verification:/home/volc/.local/share/forza-motorsport-linux/build-tools/verification/downloads/shellcheck-v0.11.0:$PATH just verify
rtk git diff --check
```

Expected: every Python, Bash, systemd, format, license, privacy, clean-start,
and repository-policy gate exits 0.

- [ ] **Step 5: Verify from a fresh clone and empty caches with synthetic Steam**

Clone the candidate commit into a new temporary directory, create new empty
cache/state/fake-Steam roots, run both fixture acquisition modes, compare their
after snapshots, rerun for no-op, and roll back to the exact baseline. Record
command output and commit SHA in `docs/release-v0.2.0.md`; do not reuse developer
caches as evidence.

- [ ] **Step 6: Capture the live installation read-only baseline**

First prove Forza and Xodus are inactive. Then run only:

```bash
rtk ./setup snapshot --output /home/volc/.local/state/forza-motorsport-linux/bootstrap-live-before.json
rtk ./setup compare \
  --before /home/volc/.local/state/forza-motorsport-linux/bootstrap-live-before.json \
  --after /home/volc/.local/state/forza-motorsport-linux/bootstrap-live-before.json
rtk ./setup bootstrap --check
```

Expected: snapshot file is current-user mode 0600, self-comparison has zero
unexpected changes, and bootstrap inspection proposes adoption/no-op for exact
supported current state. Do not confirm a mutating plan and do not launch or
stop the game/service in this step.

- [ ] **Step 7: Commit final documentation and evidence**

```bash
rtk git add README.md docs/install.md docs/architecture.md docs/troubleshooting.md docs/verification.md docs/release-v0.2.0.md docs/publication-manifest.toml tests/test_repository_policy.py
rtk git commit -m "docs: publish hybrid bootstrap workflow"
```

### Task 15: Review, live acceptance, branch completion, and public artifact verification

**Files:**
- Review only unless verification finds a defect.

**Interfaces:**
- Consumes: complete feature branch, two identical local bundles, all green gates, private live baseline.
- Produces: review findings resolved, an exact-candidate live verdict, integration choice, and after integration an immutable v0.2.0 tag/release whose downloaded asset matches the committed manifest.

- [ ] **Step 1: Run a requirements and security review**

Use `superpowers:requesting-code-review`. The review must compare every spec
section to an implementation/test, inspect no-follow and recovery boundaries,
verify child-plan digest composition, audit Docker mounts/network/user, and
confirm no Microsoft binary/private identity/hash entered Git or release data.

- [ ] **Step 2: Re-run verification after every review fix**

Run the focused failing test first, then:

```bash
rtk uv run pytest -q
rtk env PATH=/home/volc/.local/share/forza-motorsport-linux/build-tools/verification:/home/volc/.local/share/forza-motorsport-linux/build-tools/verification/downloads/shellcheck-v0.11.0:$PATH just verify
rtk git diff --check
rtk git status --short --branch
```

Expected: all gates exit 0 and worktree is clean.

- [ ] **Step 3: Run the exact-candidate manual live matrix before publication**

With the user present, compare the saved live before snapshot to the proposed
plan, explicitly confirm any mutation, install from the exact local release
bundle, and capture the after snapshot. Validate launch, online profile and
content, controller navigation/driving/hotplug, AP702 absence, Invite, Join,
keyboard/controller social UI, Xodus cleanup, and second launch without reboot.
Record the exact candidate commit, bundle SHA-256, before/after comparison, and
date. A failure keeps the candidate unpublished and recovery material intact.

- [ ] **Step 4: Finish the branch without silently publishing**

Use `superpowers:finishing-a-development-branch`, present exact merge/PR/keep
choices, and perform the user's selection. Do not tag or upload from an
unreviewed, dirty, or live-failed branch.

- [ ] **Step 5: Create and verify the public release after integration**

After the reviewed commit is on public `main`, create immutable tag `v0.2.0`,
upload the already-hashed `forza-bootstrap-bundle-v1.tar.zst`, download that
asset into a fresh empty cache, and run:

```bash
rtk ./tools/verify-bootstrap-bundle \
  --manifest manifests/bootstrap-v1.toml \
  /absolute/fresh-download/forza-bootstrap-bundle-v1.tar.zst
rtk sha256sum /absolute/fresh-download/forza-bootstrap-bundle-v1.tar.zst
```

Expected: the verifier and SHA-256 match the committed production manifest.
Update no hashes after tagging; a mismatch invalidates the release and requires
a new version rather than moving the tag.
