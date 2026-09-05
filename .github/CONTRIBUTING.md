# Contributing

For installation help, start with the
[Wiki](https://github.com/volcmen/forza-motorsport-linux/wiki).
Report vulnerabilities through [the private security channel](SECURITY.md).

## Propose a change

Open an issue describing the problem or use a focused pull request. Include the
release/commit, reproduction steps, and what you tested. For compatibility
claims, distinguish automated checks from an actual game session. A new source
revision or passing test does not inherit another build's gameplay results.

Edit player guides in the GitHub Wiki. Keep exact artifact identities and raw
verification records with the code. Do not reintroduce a duplicate guide folder.

## Work on the code

Use Python 3.11 or newer, `uv`, `just`, ShellCheck, shfmt, ripgrep, and systemd's
unit verifier. From a development checkout:

```bash
uv sync --locked --group dev
just verify
```

Tests use synthetic inputs and temporary roots. The unit verifier needs an
executable at the unit's declared service path; CI provides a harmless stub in
its disposable home. Do not replace an installed service with a test stub on
your desktop. One real rootless-Docker test is opt-in; the normal gate does not
launch Forza or sign in to Microsoft.

Keep changes narrow. Add regression coverage for security-sensitive behavior,
preserve recovery semantics, and explain any change to the trusted inputs.
Do not silently update Proton/Xodus/runtime revisions or disable integrity
checks to make a test pass. Dependency updates are reviewed pull requests;
there is no automatic dependency merge.

## Pull requests

Describe the problem, resulting behavior, and validation. Changes to `main`
require the verification check and resolved review conversations. This is a
solo-maintainer project, so a second reviewer is not required by branch policy.

Never commit proprietary binaries, credentials, or raw account logs. Original
contributions use GPL-3.0-or-later, recorded through REUSE metadata. Include
appropriate license/provenance information for third-party material.
