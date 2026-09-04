# SPDX-FileCopyrightText: 2026 Forza Motorsport Linux tools contributors
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
import tarfile
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from .model import (
    ArtifactSpec,
    BootstrapError,
    BootstrapManifest,
    BuilderSpec,
    DownloadSpec,
    ProtonUpSpec,
    SourceSpec,
    canonical_json,
)
from .safeio import open_owned_root, rename_noreplace

BASE_IMAGE = (
    "docker.io/library/archlinux:base-devel@sha256:"
    "694da1fce635e3a14d90751941b08f02c500e8724682f7a00768e7152251ec34"
)
ARCH_SNAPSHOT = "https://archive.archlinux.org/repos/2026/09/01/$repo/os/$arch"
SOURCE_DATE_EPOCH = 1_756_684_800
LLVM_MINGW_SHA256 = "cee8d2ce3da5145ce4dc882e70d0b0719a783d53a99752c60948fc0659975a65"
LLVM_MINGW_URL = (
    "https://github.com/mstorsjo/llvm-mingw/releases/download/20260826/"
    "llvm-mingw-20260826-ucrt-ubuntu-22.04-x86_64.tar.xz"
)
BUNDLE_ROOT = "forza-bootstrap-bundle-v1"
BUNDLE_ARCHIVE = f"{BUNDLE_ROOT}.tar.zst"
_DIGEST_IMAGE = re.compile(r"(?:[^\s]+@)?sha256:[0-9a-f]{64}\Z")
_REVISION = re.compile(r"[0-9a-f]{40}\Z")
_SAFE_SOURCE_NAME = re.compile(r"[a-z0-9][a-z0-9._-]*\Z")
_RUNNER = Callable[..., object]
_MAX_BUNDLE_SIZE = 2 * 1024 * 1024 * 1024
_BUILDER_CONTEXT = Path(__file__).resolve().parents[1] / "containers/bootstrap-builder"


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
    xodus_revision: str
    xgameruntime_revision: str
    build_root: Path


def _completed_stdout(result: object, label: str) -> str:
    stdout = getattr(result, "stdout", None)
    if not isinstance(stdout, str):
        raise BootstrapError(f"{label} returned invalid output")
    return stdout


def check_docker(runner: _RUNNER = subprocess.run) -> DockerCapability:
    """Require a reachable rootless Docker daemon on x86_64."""
    argv = ("docker", "info", "--format", "{{json .}}")
    try:
        result = runner(argv, check=True, capture_output=True, text=True)
    except (FileNotFoundError, subprocess.CalledProcessError, OSError):
        raise BootstrapError("Docker daemon is unavailable for source mode") from None
    try:
        payload = json.loads(_completed_stdout(result, "docker info"))
    except (json.JSONDecodeError, TypeError):
        raise BootstrapError("invalid Docker capability response") from None
    if not isinstance(payload, dict):
        raise BootstrapError("invalid Docker capability response")
    version = payload.get("ServerVersion")
    architecture = payload.get("Architecture")
    security = payload.get("SecurityOptions")
    if (
        not isinstance(version, str)
        or not version
        or not isinstance(architecture, str)
        or not isinstance(security, list)
        or not all(isinstance(option, str) for option in security)
    ):
        raise BootstrapError("invalid Docker capability response")
    normalized_arch = "x86_64" if architecture == "amd64" else architecture
    if normalized_arch != "x86_64":
        raise BootstrapError("source mode requires an x86_64 Docker daemon")
    rootless = any(
        option == "rootless" or option.startswith("name=rootless")
        for option in security
    )
    if not rootless:
        raise BootstrapError("source mode requires a rootless Docker daemon")
    return DockerCapability(version, normalized_arch, True)


def _run_git(
    argv: Sequence[str],
    *,
    runner: _RUNNER = subprocess.run,
    check: bool = True,
) -> object:
    try:
        return runner(
            tuple(argv),
            check=check,
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError, OSError) as error:
        raise BootstrapError("source Git operation failed") from error


def _absolute_directory(path: Path, label: str) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        raise BootstrapError(f"{label} must be absolute")
    try:
        info = candidate.lstat()
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        raise BootstrapError(f"{label} is unsafe") from None
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_uid != os.getuid()
        or resolved != candidate
    ):
        raise BootstrapError(f"{label} is unsafe")
    return candidate


def validate_source(
    source: Path,
    expected_revision: str,
    runner: _RUNNER = subprocess.run,
) -> Path:
    """Verify an owner-held, clean, detached Git tree at one exact commit."""
    if not isinstance(expected_revision, str) or not _REVISION.fullmatch(
        expected_revision
    ):
        raise BootstrapError("expected source revision is invalid")
    candidate = _absolute_directory(Path(source), "source tree")
    head = _completed_stdout(
        _run_git(
            ("git", "-C", str(candidate), "rev-parse", "--verify", "HEAD^{commit}"),
            runner=runner,
        ),
        "git rev-parse",
    ).strip()
    if head != expected_revision:
        raise BootstrapError("source revision mismatch")
    branch = _run_git(
        ("git", "-C", str(candidate), "symbolic-ref", "-q", "HEAD"),
        runner=runner,
        check=False,
    )
    returncode = getattr(branch, "returncode", None)
    if returncode not in (0, 1):
        raise BootstrapError("source Git operation failed")
    if returncode == 0:
        raise BootstrapError("source tree must use detached HEAD")
    status_output = _completed_stdout(
        _run_git(
            (
                "git",
                "-C",
                str(candidate),
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
            ),
            runner=runner,
        ),
        "git status",
    )
    if status_output:
        raise BootstrapError("source tree is not clean")
    return candidate


def _private_cache_root(cache: Path) -> Path:
    cache = Path(cache)
    if not cache.is_absolute():
        raise BootstrapError("source cache must be absolute")
    if not cache.exists():
        try:
            cache.mkdir(mode=0o700, parents=True)
        except OSError:
            raise BootstrapError("source cache is unavailable") from None
    candidate = _absolute_directory(cache, "source cache")
    if stat.S_IMODE(candidate.stat().st_mode) != 0o700:
        raise BootstrapError("source cache must have mode 0700")
    return candidate


def prepare_container_stage(private_root: Path, name: str) -> Path:
    """Create one container-writable stage below an owner-only anchor."""
    root = _absolute_directory(Path(private_root), "container staging parent")
    if stat.S_IMODE(root.stat().st_mode) != 0o700:
        raise BootstrapError("container staging parent must have mode 0700")
    if name not in {"dependencies", "output"}:
        raise BootstrapError("container staging name is invalid")
    stage = root / name
    root_fd = open_owned_root(root)
    stage_fd: int | None = None
    try:
        os.mkdir(name, mode=0o700, dir_fd=root_fd)
        stage_fd = os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=root_fd,
        )
        os.fchmod(stage_fd, 0o733)
        if not _directory_name_matches(root_fd, name, stage_fd):
            raise BootstrapError("container staging path changed")
    except FileExistsError:
        raise BootstrapError("container staging path already exists") from None
    except OSError:
        raise BootstrapError("container staging path is unavailable") from None
    finally:
        if stage_fd is not None:
            os.close(stage_fd)
        os.close(root_fd)
    return _absolute_directory(stage, "container staging path")


def fetch_source(
    spec: SourceSpec,
    cache: Path,
    runner: _RUNNER = subprocess.run,
) -> Path:
    """Fetch exactly one immutable revision into a private cache."""
    if (
        not isinstance(spec, SourceSpec)
        or not _SAFE_SOURCE_NAME.fullmatch(spec.name)
        or not _REVISION.fullmatch(spec.revision)
        or not isinstance(spec.repository, str)
        or not spec.repository
    ):
        raise BootstrapError("source specification is invalid")
    cache_root = _private_cache_root(cache)
    destination = cache_root / f"{spec.name}-{spec.revision}"
    if destination.exists() or destination.is_symlink():
        return validate_source(destination, spec.revision, runner)
    stage = Path(tempfile.mkdtemp(prefix=f".{spec.name}-", dir=cache_root))
    stage.rmdir()
    stage_name = stage.name
    parent_fd: int | None = None
    stage_fd: int | None = None
    try:
        _run_git(
            (
                "git",
                "clone",
                "--no-checkout",
                "--filter=blob:none",
                "--",
                spec.repository,
                str(stage),
            ),
            runner=runner,
        )
        _run_git(
            (
                "git",
                "-C",
                str(stage),
                "fetch",
                "--depth=1",
                "origin",
                spec.revision,
            ),
            runner=runner,
        )
        _run_git(
            (
                "git",
                "-C",
                str(stage),
                "checkout",
                "--detach",
                spec.revision,
            ),
            runner=runner,
        )
        parent_fd = open_owned_root(cache_root)
        try:
            stage_fd = os.open(
                stage_name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=parent_fd,
            )
        except OSError:
            raise BootstrapError("source tree changed before publication") from None
        held = os.fstat(stage_fd)
        validate_source(stage, spec.revision, runner)
        if not _directory_name_matches(parent_fd, stage_name, stage_fd, held):
            raise BootstrapError("source tree changed before publication")
        try:
            rename_noreplace(parent_fd, stage_name, parent_fd, destination.name)
        except FileExistsError:
            raise BootstrapError("source cache destination already exists") from None
        if not _directory_name_matches(parent_fd, destination.name, stage_fd, held):
            raise BootstrapError("source tree changed during publication")
        os.fsync(parent_fd)
        validate_source(destination, spec.revision, runner)
        if not _directory_name_matches(parent_fd, destination.name, stage_fd, held):
            raise BootstrapError("source tree changed during publication")
        return destination
    finally:
        if stage_fd is not None:
            os.close(stage_fd)
        if parent_fd is not None:
            os.close(parent_fd)


def _directory_name_matches(
    parent_fd: int,
    name: str,
    held_fd: int,
    held: os.stat_result | None = None,
) -> bool:
    try:
        named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        current = os.fstat(held_fd)
    except OSError:
        return False
    expected = current if held is None else held

    def identity(info: os.stat_result) -> tuple[int, ...]:
        return (
            info.st_dev,
            info.st_ino,
            info.st_mode,
            info.st_uid,
            info.st_gid,
        )

    return (
        stat.S_ISDIR(named.st_mode)
        and identity(named) == identity(current)
        and identity(current) == identity(expected)
    )


def _validate_image(image: str) -> str:
    if not isinstance(image, str) or not _DIGEST_IMAGE.fullmatch(image):
        raise BootstrapError("builder image must use an immutable digest")
    return image


def _mount_root(path: Path) -> Path:
    candidate = _absolute_directory(Path(path), "mount root")
    if "," in os.fsdecode(candidate) or any(
        ord(character) < 0x20 or ord(character) == 0x7F
        for character in os.fsdecode(candidate)
    ):
        raise BootstrapError("mount root contains a Docker mount delimiter")
    home = Path.home().resolve(strict=True)
    forbidden = (
        home,
        home / ".local/share/Steam",
        home / ".steam",
        home / ".local/share/kwalletd",
        home / ".config/kwalletrc",
    )
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if (
        candidate == home
        or candidate.is_relative_to(home)
        or any(
            candidate == root or candidate.is_relative_to(root) for root in forbidden
        )
    ):
        raise BootstrapError("container mount uses a forbidden host root")
    if runtime:
        runtime_path = Path(runtime)
        if runtime_path.is_absolute() and (
            candidate == runtime_path or candidate.is_relative_to(runtime_path)
        ):
            raise BootstrapError("container mount uses a forbidden host root")
    return candidate


def _controlled_mount_roots(
    inputs: BuildInputs, paths: Sequence[Path]
) -> tuple[Path, ...]:
    build_root = _mount_root(inputs.build_root)
    if stat.S_IMODE(build_root.stat().st_mode) != 0o700:
        raise BootstrapError("declared build root must have mode 0700")
    controlled = tuple(_mount_root(path) for path in paths)
    if any(
        path == build_root or not path.is_relative_to(build_root) for path in controlled
    ):
        raise BootstrapError("mount is outside the declared build root")
    return controlled


def _reject_overlaps(paths: Sequence[Path]) -> None:
    for index, left in enumerate(paths):
        for right in paths[index + 1 :]:
            try:
                common = Path(os.path.commonpath((left, right)))
            except ValueError:
                continue
            if common == left or common == right:
                raise BootstrapError("container mount roots overlap")


def _mount(source: Path, destination: str, *, readonly: bool) -> tuple[str, str]:
    value = f"type=bind,src={source},dst={destination}"
    if readonly:
        value += ",readonly"
    return "--mount", value


def _sandbox_prefix(network: str) -> tuple[str, ...]:
    return (
        "docker",
        "run",
        "--rm",
        f"--network={network}",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--user=builder",
        "--tmpfs=/tmp:rw,nosuid,nodev,mode=1777",
    )


def dependency_fetch_argv(inputs: BuildInputs) -> tuple[str, ...]:
    """Build the only network-enabled container invocation."""
    image = _validate_image(inputs.builder_image)
    source, dependency_root = _controlled_mount_roots(
        inputs, (inputs.xodus_source, inputs.dependency_root)
    )
    validate_source(source, inputs.xodus_revision)
    _reject_overlaps((source, dependency_root))
    return (
        *_sandbox_prefix("bridge"),
        *_mount(source, "/src/xodus", readonly=True),
        *_mount(dependency_root, "/deps", readonly=False),
        image,
        "/usr/local/bin/fetch-xodus-deps",
    )


def offline_build_argv(inputs: BuildInputs, output: Path) -> tuple[str, ...]:
    """Build a network-disabled compiler invocation with four fixed mounts."""
    image = _validate_image(inputs.builder_image)
    xodus, xgameruntime, dependencies, output_root = _controlled_mount_roots(
        inputs,
        (
            inputs.xodus_source,
            inputs.xgameruntime_source,
            inputs.dependency_root,
            inputs.output_root,
        ),
    )
    requested_output = Path(output)
    if requested_output != output_root:
        raise BootstrapError("output must equal the declared output root")
    try:
        if any(output_root.iterdir()):
            raise BootstrapError("output directory is not empty")
    except OSError:
        raise BootstrapError("output directory is unsafe") from None
    _reject_overlaps((xodus, xgameruntime, dependencies, output_root))
    validate_source(xodus, inputs.xodus_revision)
    validate_source(xgameruntime, inputs.xgameruntime_revision)
    environment = (
        "--env",
        f"SOURCE_DATE_EPOCH={SOURCE_DATE_EPOCH}",
        "--env",
        "LC_ALL=C.UTF-8",
        "--env",
        "TZ=UTC",
        "--env",
        f"FORZA_BUILDER_IMAGE={image}",
        "--env",
        f"FORZA_XODUS_REVISION={inputs.xodus_revision}",
        "--env",
        f"FORZA_XGAMERUNTIME_REVISION={inputs.xgameruntime_revision}",
    )
    return (
        *_sandbox_prefix("none"),
        *environment,
        *_mount(xodus, "/src/xodus", readonly=True),
        *_mount(xgameruntime, "/src/xgameruntime", readonly=True),
        *_mount(dependencies, "/deps", readonly=True),
        *_mount(output_root, "/output", readonly=False),
        image,
        "/usr/local/bin/build-bundle",
    )


def validate_offline_build_argv(
    argv: Sequence[str],
    inputs: BuildInputs,
    output: Path,
    *,
    fixture: bool = False,
) -> tuple[str, ...]:
    """Fail closed if an offline compiler invocation was altered before run."""
    if type(argv) is not tuple or not all(type(item) is str for item in argv):
        raise BootstrapError("offline Docker invocation mismatch")
    base = offline_build_argv(inputs, output)
    expected = (*base, "--fixture") if fixture else base
    if argv != expected:
        raise BootstrapError("offline Docker invocation mismatch")
    return expected


def _source_head(source: Path) -> str:
    result = _run_git(
        ("git", "-C", str(source), "rev-parse", "--verify", "HEAD^{commit}")
    )
    head = _completed_stdout(result, "git rev-parse").strip()
    if not _REVISION.fullmatch(head):
        raise BootstrapError("source revision is invalid")
    return head


def builder_build_argv(tag: str, *, no_cache: bool = False) -> tuple[str, ...]:
    """Return the pinned command that constructs the project builder image."""
    if (
        not isinstance(tag, str)
        or not tag
        or any(character.isspace() for character in tag)
    ):
        raise BootstrapError("builder tag is invalid")
    cache = ("--no-cache",) if no_cache else ()
    return (
        "docker",
        "build",
        "--pull",
        *cache,
        "--platform=linux/amd64",
        "--build-arg",
        f"BASE_IMAGE={BASE_IMAGE}",
        "--build-arg",
        f"ARCH_SNAPSHOT={ARCH_SNAPSHOT}",
        "--build-arg",
        f"LLVM_MINGW_SHA256={LLVM_MINGW_SHA256}",
        "--build-arg",
        f"LLVM_MINGW_URL={LLVM_MINGW_URL}",
        "--build-arg",
        f"SOURCE_DATE_EPOCH={SOURCE_DATE_EPOCH}",
        "--target=builder",
        "--tag",
        tag,
        "--file",
        str(_BUILDER_CONTEXT / "Dockerfile"),
        str(_BUILDER_CONTEXT),
    )


def builder_manifest_argv(image: str) -> tuple[str, ...]:
    """Read the declared rootfs manifest without network or host mounts."""
    return (
        *_sandbox_prefix("none"),
        _validate_image(image),
        "/usr/bin/cat",
        "/usr/local/share/forza-builder/rootfs-manifest.txt",
    )


def _registry_repository(reference: str) -> str:
    if (
        not isinstance(reference, str)
        or not reference
        or reference.startswith("-")
        or "@" in reference
        or any(character.isspace() for character in reference)
    ):
        raise BootstrapError("registry tag is invalid")
    final = reference.rsplit("/", 1)[-1]
    if ":" not in final:
        return reference
    return reference[: -(len(final) - final.rfind(":"))]


def publish_builder_image(
    verified_image: str,
    registry_tag: str,
    runner: _RUNNER = subprocess.run,
) -> str:
    """Push only a captured local image ID and bind its registry manifest."""
    image = _validate_image(verified_image)
    repository = _registry_repository(registry_tag)

    def docker(argv: tuple[str, ...]) -> object:
        try:
            return runner(argv, check=True, capture_output=True, text=True)
        except (FileNotFoundError, subprocess.CalledProcessError, OSError) as error:
            raise BootstrapError("builder image publication failed") from error

    docker(("docker", "tag", image, registry_tag))
    docker(("docker", "push", registry_tag))
    tagged_id = _completed_stdout(
        docker(
            (
                "docker",
                "image",
                "inspect",
                "--format",
                "{{.Id}}",
                registry_tag,
            )
        ),
        "published image inspection",
    ).strip()
    if tagged_id != image:
        raise BootstrapError("published builder tag changed identity")
    raw_digests = _completed_stdout(
        docker(
            (
                "docker",
                "image",
                "inspect",
                "--format",
                "{{json .RepoDigests}}",
                image,
            )
        ),
        "published digest inspection",
    )
    try:
        digests = json.loads(raw_digests)
    except json.JSONDecodeError:
        raise BootstrapError("published builder has invalid registry digests") from None
    prefix = repository + "@sha256:"
    matches = (
        [item for item in digests if isinstance(item, str) and item.startswith(prefix)]
        if isinstance(digests, list)
        else []
    )
    if len(matches) != 1 or not _DIGEST_IMAGE.fullmatch(matches[0]):
        raise BootstrapError("published builder has no unique immutable digest")
    manifest = matches[0]
    raw_manifest = _completed_stdout(
        docker(("docker", "manifest", "inspect", manifest)),
        "published manifest inspection",
    )
    try:
        payload = json.loads(raw_manifest)
    except json.JSONDecodeError:
        raise BootstrapError("published builder manifest is invalid") from None
    config = payload.get("config") if isinstance(payload, dict) else None
    if not isinstance(config, dict) or config.get("digest") != image:
        raise BootstrapError("published builder digest has the wrong image identity")
    return manifest


def _fixture_files() -> dict[str, tuple[bytes, int]]:
    return {
        "bin/xodus-service": (b"fixture xodus service\n", 0o755),
        "bin/xodus-cli": (b"fixture xodus cli\n", 0o755),
        "bin/xodus-overlay": (b"fixture xodus overlay\n", 0o755),
        "runtime/xgameruntime.dll": (b"fixture xgameruntime pe\n", 0o644),
        "runtime/xgameruntime.so": (b"fixture xgameruntime unix\n", 0o755),
    }


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_fixture_tree(root: Path) -> None:
    artifacts = _fixture_files()
    artifact_names = {
        "bin/xodus-service": "xodus-service",
        "bin/xodus-cli": "xodus-cli",
        "bin/xodus-overlay": "xodus-overlay",
        "runtime/xgameruntime.dll": "xgameruntime-pe64",
        "runtime/xgameruntime.so": "xgameruntime-unix64",
    }
    ordered_paths = (
        "bin/xodus-service",
        "bin/xodus-cli",
        "bin/xodus-overlay",
        "runtime/xgameruntime.dll",
        "runtime/xgameruntime.so",
    )
    for relative, (data, mode) in artifacts.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(mode)
    metadata: dict[str, bytes] = {
        "licenses/xodus.txt": b"Synthetic fixture only; no distributed binary license.\n",
        "licenses/xgameruntime.txt": b"Synthetic fixture only; no distributed binary license.\n",
        "provenance/sources.json": canonical_json(
            {"xgameruntime": "2" * 40, "xodus": "1" * 40}
        ),
        "provenance/build-environment.json": canonical_json(
            {
                "arch_snapshot": ARCH_SNAPSHOT,
                "base_image": BASE_IMAGE,
                "fixture": True,
                "llvm_mingw_sha256": LLVM_MINGW_SHA256,
                "source_date_epoch": SOURCE_DATE_EPOCH,
            }
        ),
        "provenance/build-commands.txt": b"fixture: deterministic producer\n",
    }
    internal = canonical_json(
        {
            "artifacts": [
                {
                    "logical_name": artifact_names[path],
                    "mode": artifacts[path][1],
                    "private": False,
                    "relative_path": path,
                    "sha256": _digest(artifacts[path][0]),
                    "size": len(artifacts[path][0]),
                }
                for path in ordered_paths
            ],
            "profile": "legacy-v0.1",
            "schema": 1,
            "sources": {"xgameruntime": "2" * 40, "xodus": "1" * 40},
        }
    )
    metadata["bundle-manifest.json"] = internal
    for relative, data in metadata.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(0o644)
    checksums = b"".join(
        f"{_digest(path.read_bytes())}  {path.relative_to(root).as_posix()}\n".encode(
            "ascii"
        )
        for path in sorted(root.rglob("*"))
        if path.is_file()
    )
    (root / "SHA256SUMS").write_bytes(checksums)
    (root / "SHA256SUMS").chmod(0o644)


def _write_deterministic_tar(source: Path, raw_tar: Path) -> None:
    entries = [source, *sorted(source.rglob("*"))]
    with tarfile.open(raw_tar, "w", format=tarfile.USTAR_FORMAT) as archive:
        for path in entries:
            relative = path.relative_to(source.parent).as_posix()
            info = tarfile.TarInfo(relative)
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            info.mtime = 0
            if path.is_dir():
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                info.size = 0
                archive.addfile(info)
            elif path.is_file() and not path.is_symlink():
                info.type = tarfile.REGTYPE
                info.mode = stat.S_IMODE(path.stat().st_mode)
                info.size = path.stat().st_size
                with path.open("rb") as stream:
                    archive.addfile(info, stream)
            else:
                raise BootstrapError("fixture bundle accepts regular files only")


def build_fixture_bundle(output_root: Path) -> Path:
    """Produce a small deterministic bundle for hermetic boundary tests."""
    output = _absolute_directory(Path(output_root), "output directory")
    archive = output / BUNDLE_ARCHIVE
    if archive.exists() or archive.is_symlink():
        raise BootstrapError("bundle output already exists")
    try:
        existing = tuple(output.iterdir())
    except OSError:
        raise BootstrapError("output directory is unsafe") from None
    if existing:
        raise BootstrapError("output directory is not empty")
    with tempfile.TemporaryDirectory(prefix="forza-bundle-fixture-") as temporary:
        temporary_root = Path(temporary)
        source = temporary_root / BUNDLE_ROOT
        source.mkdir()
        _write_fixture_tree(source)
        raw_tar = temporary_root / "bundle.tar"
        _write_deterministic_tar(source, raw_tar)
        try:
            destination_fd = os.open(
                archive,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o644,
            )
        except FileExistsError:
            raise BootstrapError("bundle output already exists") from None
        try:
            with os.fdopen(destination_fd, "wb", closefd=False) as destination:
                subprocess.run(
                    ("zstd", "-q", "-19", "--threads=1", "-c", str(raw_tar)),
                    check=True,
                    stdout=destination,
                )
                destination.flush()
                os.fsync(destination_fd)
        except (FileNotFoundError, subprocess.CalledProcessError, OSError):
            os.close(destination_fd)
            archive.unlink(missing_ok=True)
            raise BootstrapError("deterministic zstd compression failed") from None
        else:
            os.close(destination_fd)
    return archive


def verify_fixture_archive(path: Path) -> Path:
    """Run a synthetic archive through the production bundle verifier."""
    from .artifacts import extract_bundle

    archive = Path(path)
    try:
        payload_size = archive.stat().st_size
        payload_sha256 = hashlib.sha256(archive.read_bytes()).hexdigest()
    except OSError:
        raise BootstrapError("fixture bundle is unavailable") from None
    files = _fixture_files()
    logical_names = {
        "bin/xodus-service": "xodus-service",
        "bin/xodus-cli": "xodus-cli",
        "bin/xodus-overlay": "xodus-overlay",
        "runtime/xgameruntime.dll": "xgameruntime-pe64",
        "runtime/xgameruntime.so": "xgameruntime-unix64",
    }
    order = (
        "bin/xodus-service",
        "bin/xodus-cli",
        "bin/xodus-overlay",
        "runtime/xgameruntime.dll",
        "runtime/xgameruntime.so",
    )
    manifest = BootstrapManifest(
        schema=1,
        app_id="2440510",
        host=("arch", "x86_64", "native-steam"),
        profile="legacy-v0.1",
        protonup=ProtonUpSpec("0.1.5", "tools/protonup"),
        ge=DownloadSpec(
            "fixture-ge",
            "fixture",
            "https://example.invalid/ge.tar.gz",
            "ge.tar.gz",
            1,
            "0" * 64,
            "fixture-ge",
            ("example.invalid",),
        ),
        bundle=DownloadSpec(
            "fixture-bundle",
            "fixture",
            "https://example.invalid/forza-bootstrap-bundle-v1.tar.zst",
            BUNDLE_ARCHIVE,
            payload_size,
            payload_sha256,
            BUNDLE_ROOT,
            ("example.invalid",),
        ),
        sources=(
            SourceSpec("xodus", "https://example.invalid/xodus", "1" * 40),
            SourceSpec(
                "xgameruntime", "https://example.invalid/xgameruntime", "2" * 40
            ),
        ),
        builder=BuilderSpec(
            "example.invalid/builder@sha256:" + "3" * 64,
            BASE_IMAGE,
            ARCH_SNAPSHOT,
            SOURCE_DATE_EPOCH,
        ),
        artifacts=tuple(
            ArtifactSpec(
                logical_names[relative],
                relative,
                len(files[relative][0]),
                _digest(files[relative][0]),
                files[relative][1],
            )
            for relative in order
        ),
        path=Path("fixture-bootstrap.toml"),
        sha256="4" * 64,
    )
    with tempfile.TemporaryDirectory(prefix="forza-fixture-verify-") as temporary:
        extract_bundle(archive, Path(temporary) / BUNDLE_ROOT, manifest)
    return archive


def verify_output_bundle(path: Path, expected_sha256: str) -> Path:
    """Verify an output in place without adopting, replacing, or deleting it."""
    if not isinstance(expected_sha256, str) or not re.fullmatch(
        r"[0-9a-f]{64}", expected_sha256
    ):
        raise BootstrapError("expected bundle SHA-256 is invalid")
    candidate = Path(path)
    try:
        info = candidate.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
        ):
            raise BootstrapError("bundle output is unsafe")
        digest = hashlib.sha256()
        with candidate.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
    except BootstrapError:
        raise
    except OSError:
        raise BootstrapError("bundle output is unsafe") from None
    if digest.hexdigest() != expected_sha256:
        raise BootstrapError("bundle output SHA-256 mismatch")
    return candidate


def _file_identity(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_uid,
        info.st_gid,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _regular_name_matches(parent_fd: int, name: str, held_fd: int) -> bool:
    try:
        named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        held = os.fstat(held_fd)
    except OSError:
        return False
    return stat.S_ISREG(named.st_mode) and (named.st_dev, named.st_ino) == (
        held.st_dev,
        held.st_ino,
    )


def _copy_open_file_to_recovery(
    parent_fd: int, source_fd: int, destination_name: str
) -> None:
    token = secrets.token_hex(12)
    stage_name = f".{destination_name}.{token}.recovery-stage"
    recovery_name = f".{destination_name}.{token}.recovery"
    recovery_fd = os.open(
        stage_name,
        os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
        dir_fd=parent_fd,
    )
    try:
        os.lseek(source_fd, 0, os.SEEK_SET)
        while data := os.read(source_fd, 1024 * 1024):
            view = memoryview(data)
            while view:
                written = os.write(recovery_fd, view)
                if written <= 0:
                    raise OSError("short recovery write")
                view = view[written:]
        os.fchmod(recovery_fd, 0o644)
        os.fsync(recovery_fd)
        rename_noreplace(parent_fd, stage_name, parent_fd, recovery_name)
        if not _regular_name_matches(parent_fd, recovery_name, recovery_fd):
            raise BootstrapError("container output recovery changed")
        os.fsync(parent_fd)
    finally:
        os.close(recovery_fd)


def _preserve_failed_destination(
    parent_fd: int, destination_name: str, destination_fd: int
) -> None:
    if _regular_name_matches(parent_fd, destination_name, destination_fd):
        recovery_name = f".{destination_name}.{secrets.token_hex(12)}.failed-recovery"
        try:
            rename_noreplace(parent_fd, destination_name, parent_fd, recovery_name)
        except (FileNotFoundError, FileExistsError):
            pass
        else:
            if _regular_name_matches(parent_fd, recovery_name, destination_fd):
                os.fsync(parent_fd)
                return
    _copy_open_file_to_recovery(parent_fd, destination_fd, destination_name)


def publish_container_output(source: Path, output_root: Path) -> Path:
    """Copy one container-owned archive through held no-follow descriptors."""
    output = _absolute_directory(Path(output_root), "output directory")
    source_path = Path(source)
    if source_path.name != BUNDLE_ARCHIVE:
        raise BootstrapError("container bundle output is unsafe")
    stage = _absolute_directory(source_path.parent, "container output staging")
    private_parent = _absolute_directory(stage.parent, "container output parent")
    if (
        private_parent.stat().st_uid != os.getuid()
        or stat.S_IMODE(private_parent.stat().st_mode) != 0o700
        or stage.stat().st_uid != os.getuid()
        or stat.S_IMODE(stage.stat().st_mode) != 0o733
    ):
        raise BootstrapError("container bundle output is unsafe")
    try:
        source_fd = os.open(source_path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        raise BootstrapError("container bundle output is unsafe") from None
    destination = output / BUNDLE_ARCHIVE
    output_fd = open_owned_root(output)
    destination_fd: int | None = None
    destination_created = False
    published = False
    try:
        before = os.fstat(source_fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size <= 0
            or before.st_size > _MAX_BUNDLE_SIZE
            or stat.S_IMODE(before.st_mode) != 0o644
        ):
            raise BootstrapError("container bundle output is unsafe")
        try:
            destination_fd = os.open(
                destination.name,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=output_fd,
            )
            destination_created = True
        except FileExistsError:
            raise BootstrapError("bundle output already exists") from None
        while data := os.read(source_fd, 1024 * 1024):
            view = memoryview(data)
            while view:
                written = os.write(destination_fd, view)
                if written <= 0:
                    raise BootstrapError("container bundle output copy failed")
                view = view[written:]
        after = os.fstat(source_fd)
        if _file_identity(after) != _file_identity(before):
            raise BootstrapError("container bundle output changed during copy")
        os.fchmod(destination_fd, 0o644)
        os.fsync(destination_fd)
        if not _regular_name_matches(output_fd, destination.name, destination_fd):
            raise BootstrapError("destination changed during publication")
        os.fsync(output_fd)
        if not _regular_name_matches(output_fd, destination.name, destination_fd):
            raise BootstrapError("destination changed during publication")
        published = True
        return destination
    except OSError:
        raise BootstrapError("container bundle output copy failed") from None
    finally:
        os.close(source_fd)
        if destination_fd is not None:
            try:
                if destination_created and not published:
                    _preserve_failed_destination(
                        output_fd, destination.name, destination_fd
                    )
            except (BootstrapError, OSError):
                pass
            os.close(destination_fd)
        os.close(output_fd)
