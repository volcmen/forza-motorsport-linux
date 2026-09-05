# Security policy

## Report a vulnerability privately

Use [GitHub's private vulnerability report](https://github.com/volcmen/forza-motorsport-linux/security/advisories/new).
Do not open a public issue with an exploit, credentials, or account data.

Include the affected tag or commit, component, expected and observed behavior,
and a minimal reproduction using synthetic data where possible. Redact local
paths and account identifiers. Never attach Microsoft DLLs, game files, access
tokens, proof keys, XUIDs, gamertags, or invite/activation data.

For ordinary installation problems, use the
[troubleshooting guide](https://github.com/volcmen/forza-motorsport-linux/wiki/Troubleshooting)
and the bug report form.

## Versions and scope

Security fixes are developed on `main`. v0.2.0 is the experimental bootstrap
prerelease; v0.1.0 is a historical source-only release. Pinned releases and
installed components do not update automatically. Read the fix's release notes
before changing an installation and keep its recovery records.

This repository owns the bootstrap, launch integration, installers, patcher,
and associated workflows. Relevant reports include unsafe file replacement,
archive traversal, hash-verification bypass, credential disclosure, and
unexpected command execution. Upstream Proton, Wine, Xodus, Microsoft services,
and host packages have their own security boundaries.

The setup runs as your user and requires trusted source/manifests and a trusted
host. Hash checks identify approved artifacts; they do not prove that upstream
binaries are vulnerability-free. User-service hardening is not a sandbox
against a compromised host or another process running as the same user.

No bug bounty or response-time guarantee is offered. Please allow coordinated
handling before publicly disclosing a reproducible vulnerability.
