# Maintenance and security review — 2026-10-15

Scope: incoming changes to this repository, its Python dependency locks, CI
permissions, and the separately tested async fix in the maintainer's Wine fork.
This supplements the [September audit](security-audit-2026-09-05.md).

## Merged changes and checks

- [PR #9](https://github.com/volcmen/forza-motorsport-linux/pull/9): reviewed the extraction, retry, service-state, runtime-mode, and recovery changes and their tests. The original contributor head passed 900 Python tests and the repository shell/lint/license/policy checks locally. The final synchronized head passed GitHub verification and both CodeQL language analyses before approval and merge. No agent instructions, prompt-injection text, new acquisition endpoints, or trust-pin changes were found in the reviewed diff. This is a scoped manual review, not a proof of absence of all vulnerabilities.
- [PR #10](https://github.com/volcmen/forza-motorsport-linux/pull/10): urllib3 2.8.0 addresses [CVE-2026-97689](https://github.com/advisories/GHSA-vxq7-64xx-v4gw) (unbounded chunk-size buffering) and [CVE-2026-97688](https://github.com/advisories/GHSA-gh4c-6fx4-qh6g) (chunked Deflate infinite loop). Package artifact identities were checked against PyPI.
- [PR #3](https://github.com/volcmen/forza-motorsport-linux/pull/3): reviewed Action updates against their official release commits; full commit SHA pins and `persist-credentials: false` are retained.
- [PR #6](https://github.com/volcmen/forza-motorsport-linux/pull/6), preceded by [PR #11](https://github.com/volcmen/forza-motorsport-linux/pull/11): upgraded REUSE/Ruff and corrected REUSE 6 scanning of generated license text without removing the generated license or the source license.
- [PR #12](https://github.com/volcmen/forza-motorsport-linux/pull/12) and [PR #13](https://github.com/volcmen/forza-motorsport-linux/pull/13): moved CodeQL to a pinned advanced workflow that covers contributor PRs. The temporary repository-variable upload gate was removed because fork workflows do not receive it. Actual fork result uploads and the required aggregate CodeQL check passed.
- [PR #14](https://github.com/volcmen/forza-motorsport-linux/pull/14): contained the known incomplete fresh Proton acquisition and added read-only platform diagnostics. Full verification passed: **916 Python tests**, one opt-in Docker test skipped, plus shell suites, lint, unit syntax, REUSE 6, policy, and whitespace checks. Main-branch CI passed after merge.
- [Wine fork PR #1](https://github.com/volcmen/wine-forza-motorsport/pull/1): fixed the trace-enabled async null dereference. See the [build and regression record](custom-proton-build-2026-10-15.md) for the reproduced failure and 26 passing checks after the fix.

## Security controls and remaining scope

The updated registry dependency audits reported zero known vulnerabilities for
17 development packages and seven ProtonUp packages. GitHub's open Dependabot
and CodeQL alert queries both returned zero during the review. Advisory databases
change; these are dated results, not a guarantee of future absence of CVEs.

Branch protection still requires strict, up-to-date `verify` (GitHub Actions app)
and `CodeQL` (GitHub Code Scanning app) results. The workflow uses `pull_request`,
not privileged `pull_request_target`, with minimal permissions. No branch
protection or scanner check was bypassed to merge the contributor work.

This review does not constitute an exhaustive vulnerability audit of Wine,
Proton, all native/transitive dependencies, the SDK base image, or proprietary
Microsoft binaries. Later work on the same date qualified an experimental composed
preview; its [build](composed-preview-build-2026-10-15.md) and
[validation record](composed-preview-validation-2026-10-15.md) supersede the initial
artifact-readiness status. Real account/game and physical-device checks remain open.

The preview also includes the owned-fork
[Xodus dependency fixes](https://github.com/volcmen/xodus/pull/1).
The [683-package Rust registry audit](composed-xodus-osv-2026-10-15.json) retains
the RSA timing advisory and a compile-time maintenance advisory. Private RSA
signing now uses OpenSSL; the RSA crate itself is not claimed patched. GLib's
affected string iterator has a reviewed source backport with optimized regression
tests. These results must not be confused with the zero-findings Python audit.

The fresh-download and fingerprint issues remain tracked in
[#7](https://github.com/volcmen/forza-motorsport-linux/issues/7) and
[#8](https://github.com/volcmen/forza-motorsport-linux/issues/8). No public issue or
PR comments were posted during this maintenance work.
