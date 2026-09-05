# Repository security review — 2026-09-05

## Scope and conclusion

The review began at integration commit
`e54118dfd09753e68f0158a2bfb0e8d836849e19` and covered repository settings,
Actions workflows, Python dependency locks, Git history, and the downloader,
archive, file-transaction, runtime-launch, and container-build boundaries.
The accompanying hardening change fixes the confirmed development-dependency
and workflow findings. It does not certify the game or upstream binaries.

## Confirmed findings and changes

| Finding | Change |
| --- | --- |
| Development pytest 8.4.2 is affected by CVE-2025-71176 | Raised the requirement to `>=9.0.3,<10` and locked pytest 9.1.1. This affects the test environment, not the installed game runtime. |
| CI used movable action tags | Both checked-in workflows now use full upstream commit SHAs, with version comments for Dependabot. |
| Verify workflow had no explicit token-permission boundary and retained checkout credentials | Added `contents: read` and `persist-credentials: false`. Repository default permissions were already read-only. |
| Root development dependencies were not versioned as a lockfile | Added `uv.lock`; CI uses `uv sync --locked`. uv/just tool versions are also explicit. |
| No ongoing dependency/code scanning | Enabled Dependabot alerts and security updates, added weekly version-update configuration, and enabled CodeQL for Python and Actions with extended queries and the remote-and-local threat model. |
| No private vulnerability-reporting path | Enabled GitHub private reporting and added `.github/SECURITY.md`. |
| No protection for main or version tags | Main requires a PR, the GitHub Actions `verify` check against an up-to-date branch, and resolved conversations. Force pushes/deletion are blocked, including for admins. Version tags cannot be rewritten or deleted. |

The main policy requires zero approving reviews because this is a solo-maintainer
project. It still requires a PR and passing CI; it does not provide independent
second-person review. Version-tag protection does not restrict creating a new tag.

## Scan evidence

- **Gitleaks v8.30.1:** official release archive checksum verified; all 132 commits
  reachable from local refs scanned with redacted output; no secrets detected.
  GitHub secret scanning also returned no alerts. Push protection was already enabled.
- **Dependency advisory scan:** the fixed development environment's 16 applicable
  dependencies and ProtonUp's seven dependencies have no known vulnerabilities
  reported by `pip-audit` at review time. The development scan initially found
  pytest's [reviewed advisory](https://github.com/advisories/GHSA-6w46-j5rx-g56g).
- **Bandit:** reviewed 12,876 lines of Python modules/helpers plus 3,906 lines in
  explicitly named extensionless Python entrypoints. No scan errors. The three
  medium/high findings were the private container stage, an intentionally unsafe
  socket test, and the hermetic XML fixture described below.
- **CodeQL initial scan:** 51 alerts: four confirmed workflow configuration
  findings and 47 Python findings reviewed against their SARIF data flows and
  source validation. Each Python disposition has a recorded explanation in
  GitHub; no query rules were disabled or whole directories excluded.
- **Full local gate after hardening:** 885 Python tests passed, one opt-in real
  rootless-Docker test skipped; all 105 shell behavior tests passed. Unit syntax,
  ShellCheck, shfmt, Ruff, REUSE licensing, policy tests, and whitespace checks passed.

Raw scan output, the original repository-setting snapshot, and full per-alert
notes are retained in the maintainer's private local audit directory. They are
not application telemetry and contain no uploaded user logs.

## CodeQL disposition details

Alert numbers refer to this repository's first CodeQL run.

| Alerts | Review result |
| --- | --- |
| 1–4 | Confirmed Actions findings; corrected by this change. The next scan determines their resolved state. |
| 5–16 | Data flows start at the local build operator's output argument. Output is intentionally caller-selected; production staging/publication uses private roots, ownership checks, exact hashes, and no-follow/exclusive operations. Fixture reads are explicit test behavior. |
| 17, 22 | The source is the local integration-checkout override. Installation requires a trusted checkout and binds reviewed clean revisions and plan hashes. |
| 18–21, 28 | Private installer records and relative paths are schema-constrained. Dot/dot-dot/absolute components are rejected and directories are opened by descriptor without following symlinks. |
| 23–27 | Local CLI/XDG paths select the invoking user's Steam directories. Read helpers enforce ownership, bounded reads, no-follow opens, and identity rechecks; prerequisite exists/lstat checks do not authorize external requests. |
| 29–30, 37–38, 42 | Explicit test hooks or local hermetic socket fixtures. They are not network-facing production entrypoints. |
| 31–34 | Filesystem helpers intentionally accept operator-selected absolute roots, reject unsafe components, and traverse without following symlinks. No privilege elevation is performed. |
| 35–36 | Recovery directory names are a constant or a validated 24-character lowercase hexadecimal transaction ID; owner/mode checks apply. |
| 39–40 | Build/publication commands use fixed argv tuples with no shell evaluation. Tags and image identities are validated before the relevant Docker operation. |
| 41, 43 | An explicit executable override comes from the invoking user's environment. This is a supported test/local-operator seam, not a remote command source. The environment must be trusted. |
| 44 | The constant fullmatch expression has two digit runs separated by a mandatory colon, without ambiguous nested repetition. Malformed 10k/100k/1M-digit strings rejected in approximately 0.00014/0.00052/0.0052 seconds. The source is a private digest-bound plan. |
| 45–46 | Mode 0644 applies to a shareable synthetic bundle or runtime binary, not credentials. Exclusive no-follow creation and the relevant integrity/recovery checks still apply. |
| 47–51 | Negative tests deliberately create unsafe permissions or special files in temporary roots to verify rejection. Changing those masks would remove regression coverage. |

Bandit's mode-0733 container-stage finding is intentional: a mode-0700 parent
prevents unrelated host users from traversing to it, while the bind-mounted
child permits the rootless container's mapped build user to write. Existing tests
check that private anchor. The XML parser finding is in the local hermetic Xodus
fixture, not in a production network service.

## Limits

This review did not assess the downloaded Proton/Wine/Xodus/Microsoft binaries,
their complete transitive native dependency graphs, the Arch snapshot image's
package vulnerabilities, Microsoft account services, drivers, or the live game.
Pinned hashes and reproducible builds identify bytes; they are not evidence that
those bytes are free of vulnerabilities. The service's filesystem restrictions
are not a security boundary against a compromised host or a same-user process.

Scanner silence is not a proof of absence. Re-run checks after code/dependency
changes and keep reviewing CodeQL and Dependabot results. Current settings and
alerts can change; this record describes the dated review and its accompanying
change, not a permanent security rating.
