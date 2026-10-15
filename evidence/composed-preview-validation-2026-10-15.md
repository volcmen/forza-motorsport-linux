# Composed preview qualification — 2026-10-15

The v0.3.0-preview.1 artifacts passed the installed-SteamOS checks below. This is
an experimental installation/runtime result, not a physical Steam Deck or Forza
gameplay result. Artifact identities and source/build provenance are in the
[build record](composed-preview-build-2026-10-15.md).

## Environment and boundaries

Used a disposable QEMU/KVM overlay of the installed Valve recovery image recorded
in [the initial investigation](steamos-2026-10-15.md): SteamOS 3.8.14,
build 20260707.10, kernel 6.16.12, Python 3.13.5, glibc 2.41 and Flatpak 1.16.6.
The VM had four virtual CPUs, 6 GiB RAM, a 64 GiB virtual disk, virtio VGA with
software rendering, and KDE Plasma X11. Installation and runtime commands ran
as the ordinary `deck` user with its real systemd/D-Bus session. SteamOS read-only
mode remained enabled. No host Proton installation, game prefix, service, or
credential store was replaced.

The release code tested in the final lifecycle was commit
`7f17241` on the composed-preview branch. Subsequent release documentation changes
do not change those executables. The new Xodus binaries came from
`850ec9477e00117960ca09ad38c6154acc56c16d`; the Flatpak deployment was
`3be904e7171998b0a30b8c54d820c96ff0c3c3dba914824418d403f72c48b49c`, with GNOME 50
runtime commit `0bd5091046708e64bc882e8349877cfbae713f6a1e2d0487a5f10af820a2d792`.

Steam library/game metadata and the private DLL were **synthetic test fixtures**.
The prefix was created by running the actual packaged Proton launcher directly;
it was not created by an authenticated Steam AppID launch. No game or Microsoft
account was available in the VM. The synthetic DLL is not shipped or a substitute
for the user's licensed runtime.

## Observed results

| Check | Result and limit |
| --- | --- |
| Prepare | Exact archive and separate user-bundle hashes verified; independently staged complete tool published to native Steam's compatibility directory. |
| Finish | Actual prefix module hashes verified; version 3 user-only child installed; phase `READY_TO_ATTEMPT`. |
| Doctor | Composed records and seven ordinary prerequisite checks passed; free disk space warned below 100 GiB. This was not a warning-free result. |
| Payload immutability | `composed status` accepted the complete payload after Finish and after both runtime test launches; no Python-cache exclusion was used. |
| Wine API regression | Through the installed `forza-linux` launcher, native Xodus service and packaged Proton: **26 tests, 0 failures, 1 optional invite-relay test skipped**, including trace-enabled async cleanup. |
| Storage regression | Through the same installed path: **18 checks, 0 failures**, covering the patched storage-property behavior. No physical SSD or microSD conclusion follows. |
| Microsoft login UI | The new Flatpak rendered the Microsoft sign-in page. No account was entered; token persistence, refresh and wallet cancellation were not tested. |
| Overlay UI | The new Flatpak with the real native service rendered “Waiting for Forza” and the empty friends/invitations state. No account, invitations or controller were exercised. |
| Service ownership | Launcher started its owned service and left it inactive after the child exited. Direct service restart was correctly refused without the launcher's ownership token. |
| Socket recreation | A new game/launcher session created socket inode 565 while the existing overlay still held inode 545. Closing/reopening the Flatpak bound inode 565. Overlay restart is required after service recreation. |
| Rollback | Phase `ROLLED_BACK`; launcher, service binary/unit, private DLL and public tool path absent; prefix and private extracted-tool recovery copies retained. Flatpak stayed at the tested commit; native service inactive; OS read-only still enabled. |

The final Prepare plan was
`dabbeff29eb657b5857de5ac64080eb639a63a2833adc4cfafa4342aa87992cc`;
the Finish plan was
`bc915aa51e0f22d0a407cdd06640873f382e98c20266d89500ff5e58942bff21`.
Plans bind this disposable VM's paths and inputs and must not be reused by users.
Private lifecycle logs and the VM overlay were retained locally for diagnosis.

## Source verification and security

- `just verify` at the tested code revision: **1002 Python tests passed**, one
  optional Docker test skipped; all shell suites, unit syntax, ShellCheck, shfmt,
  Ruff, REUSE and 20 repository-policy checks passed.
- Xodus source: **88 tests passed, 7 live-service tests ignored**, plus **11
  optimized GLib iterator tests passed**. Includes bounded OAuth transport,
  redirect refusal, real SOAP signing and tamper detection.
- The exact Xodus head passed its owned-fork CI before merge:
  [push](https://github.com/volcmen/xodus/actions/runs/38709286998) and
  [pull request](https://github.com/volcmen/xodus/actions/runs/38709280301).
- [Registry audit](composed-xodus-osv-2026-10-15.json): 683 Rust registry packages
  checked. The RSA timing advisory remains in the dependency graph; private
  signing is routed through OpenSSL. The old compile-time `proc-macro-error`
  maintenance advisory remains. The vendored GLib correction is a source
  backport, not a clean registry-version result. Read the
  [scoped security review](https://github.com/volcmen/xodus/blob/850ec9477e00117960ca09ad38c6154acc56c16d/SECURITY-DEPENDENCIES.md).
- The independent Python development audit found no known vulnerabilities in
  its 17 pinned packages. This does not certify every Proton/OS dependency.

The implementation retains explicit trust boundaries: trusted local interpreter,
hash-bound durable recovery helper, verified immutable tool, exact Flatpak app and
runtime, and existing Secret Service. No test account credentials were supplied
or published. The Flatpak's input
and socket permissions remain meaningful access; see the build record.

## Remaining acceptance work

An account-enabled machine must validate the real Steam prefix checkpoint and
pressure-vessel handoff, licensed DLL behavior, sign-in/wallet persistence and
cancellation, two game launches, online content, and explicit Invite/Join.
A physical Steam Deck must additionally validate Gaming Mode integration,
controller/Steam Input, internal storage and microSD behavior, suspend/resume,
graphics stability, and measured performance. Incoming invite notifications remain
unsupported. Containers and QEMU cannot establish those device/game results.

The v0.1 historical gameplay matrix remains a separate dated observation. Neither
that matrix nor `READY_TO_ATTEMPT` is relabeled as preview gameplay acceptance.
