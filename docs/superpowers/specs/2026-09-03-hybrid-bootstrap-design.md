# Forza Motorsport Linux Hybrid Bootstrap Design

Date: 2026-09-03
Status: Proposed for implementation review

## Decision

Add a resumable `./setup bootstrap` workflow for the first supported host:
Arch Linux x86_64 with native Steam. The workflow prepares an isolated
`GE-Proton11-3-FM`, installs the reviewed open-source Xodus and XGameRuntime
components, copies one user-supplied licensed Microsoft threading runtime,
applies the exact-build controller and AP702 fixes, and leaves Steam selection
and game launch under the user's control.

The normal path downloads one digest-pinned bundle of open-source components.
An optional `--build-from-source` path produces the same bundle in a pinned
Docker builder. Docker is a reproducible build and test boundary only; it never
runs Steam, Proton, Forza, Xodus authentication, KDE Wallet, or the Microsoft
DLL.

Every managed installation mutation is covered by a private, digest-bound plan
and a narrow SHA-256 snapshot. A final comparison classifies monitored paths as
expected changes, required unchanged paths, or unexpected changes. An existing
installation whose managed files already match a supported state is verified
and retained instead of overwritten.

## Goals

- Reduce a clean Arch/native-Steam setup to one resumable public command plus
  the unavoidable Steam UI handoff and licensed DLL input.
- Acquire exact `GE-Proton11-3` through ProtonUp without allowing ProtonUp to
  modify Steam's compatibility-tool directory.
- Keep the Forza runtime independent of compatibility tools used by other
  games.
- Offer a verified open-source binary bundle by default and a reproducible
  Docker source-build path for auditors.
- Reuse the repository's exact-revision evidence, transaction, patch, launcher,
  doctor, and rollback owners rather than duplicate their safety logic.
- Prove the bounded filesystem result with deterministic SHA-256 manifests
  before and after setup.
- Preserve KDE Wallet as the Secret Service provider and keep Xodus scoped to
  the Forza process lifetime.
- Fail closed on unknown files, mixed patch states, interrupted transactions,
  incomplete prefixes, or unverifiable downloads.

## Non-goals

- Running Forza, Steam, or Proton inside Docker.
- Automating Steam's compatibility-tool selection or editing Steam VDF files.
- Launching the game automatically.
- Installing system packages, invoking `sudo`, or supporting a system Steam
  installation that is not the native x86_64 Arch layout in the first release.
- Installing GNOME Keyring, enabling Xodus permanently, or replacing KDE
  Wallet.
- Downloading, redistributing, identifying publicly, or publishing hashes of
  the Microsoft threading runtime.
- Downloading game content, moving Xbox credentials, or testing live Xbox
  endpoints in CI.
- Hashing the complete Steam library, Proton tree, game prefix, or home
  directory.
- Claiming gameplay, Xbox sign-in, controller, AP702, invite, or join success
  from a container or automated preflight.

## Supported boundary

The first bootstrap release supports all of these together:

| Area | Supported value |
| --- | --- |
| Host | Arch Linux, x86_64 |
| Steam | Native Steam, per-user data root |
| Game | Steam AppID `2440510` |
| Compatibility base | Exact upstream `GE-Proton11-3` release |
| Dedicated tool | `GE-Proton11-3-FM` |
| Runtime profile | Repository profile `legacy-v0.1` |
| Secret storage | `org.freedesktop.secrets`, including KDE Wallet's provider |
| Service lifetime | Disabled user unit, started only by `forza-linux` |

Bootstrap preflight verifies the distribution identity, architecture, Steam
layout, filesystem capacity, required host commands, user ownership, and
absence of active Forza/Xodus processes before offering mutation. Unsupported
hosts receive a read-only explanation and the existing manual documentation;
they are not silently treated as compatible.

## Public command surface

The existing guided and expert commands remain available. The new surface is:

```text
./setup bootstrap [--bundle /absolute/path/to/bundle.tar.zst]
                  [--build-from-source]
                  [--threading-dll /absolute/path/to/xgameruntime.dll]
./setup snapshot [--output /absolute/path/to/snapshot.json]
./setup compare [--before /absolute/path/to/before.json]
                [--after /absolute/path/to/after.json]
```

`./setup bootstrap` resumes the one unfinished bootstrap transaction for the
current user. It does not need a separate `--resume` flag. Supplying both
`--bundle` and `--build-from-source` is an error. The default path downloads the
bundle named by the repository manifest. `--bundle` permits offline use of the
same exact bundle and never relaxes verification.

`--threading-dll` may be omitted during Prepare. It becomes mandatory during
Finish. A rerun with the option continues the same transaction.

`./setup snapshot` is read-only with respect to managed targets. By default it
prints canonical JSON to standard output. With `--output`, it creates a
no-follow, owner-only file using atomic publication. `./setup compare` compares
explicit snapshots or the last bootstrap transaction's automatic before/after
pair. It exits nonzero for a malformed manifest, a policy mismatch, a missing
required record, or any unexpected change.

There is no unattended `--yes` mode. Before each mutating phase, bootstrap
prints the complete canonical plan and requires the user to type its full
`PLAN_SHA256`.

## Repository trust manifests

`manifests/bootstrap-v1.toml` is the machine-readable trust root. A release
manifest pins:

- the bootstrap schema and supported host/profile;
- ProtonUp package version and the exact GE-Proton release identifier;
- GE-Proton archive URL, size, SHA-256, and safe extracted root name;
- open-source bundle URL, size, SHA-256, internal manifest SHA-256, and profile;
- exact Xodus and XGameRuntime source repositories and commits;
- every bundle artifact path, mode, size, and SHA-256;
- component licenses and corresponding source URLs;
- the Docker builder image by immutable digest;
- the source archive or Git-tree digest for each build input;
- build commands, environment values, and declared output paths; and
- the supported original and patched hashes already owned by
  `supported-builds.toml`.

The manifest is reviewed and committed before the release tag is created. The
release bundle is built first, hashed, and recorded in that commit; the tag is
then created at the commit and the exact prebuilt asset is uploaded. Moving a
tag or replacing an asset produces a digest failure.

Git history plus the committed SHA-256 values are the initial trust root. The
design does not claim a detached-signature system until signing keys,
distribution, rotation, and revocation have a documented owner.

## Open-source release bundle

The bundle contains only redistributable project inputs and outputs:

```text
forza-bootstrap-bundle-v1/
├── bundle-manifest.json
├── bin/
│   ├── xodus-service
│   ├── xodus-cli
│   └── xodus-overlay
├── runtime/
│   ├── xgameruntime.dll
│   └── xgameruntime.so
├── licenses/
├── provenance/
│   ├── sources.json
│   ├── build-environment.json
│   └── build-commands.txt
└── SHA256SUMS
```

The bundle never contains GE-Proton, Steam or game files, the Microsoft
threading DLL, credentials, account state, or live logs. Extraction rejects
absolute paths, `..` traversal, duplicate names, links, special files,
unexpected top-level entries, excessive file counts, and declared or expanded
sizes above fixed manifest limits. Every extracted regular file is verified
before it can become an installation source.

## Docker source-build boundary

`--build-from-source` uses Docker only after host preflight confirms a reachable
rootless-capable daemon. Absence of Docker blocks this optional mode but does
not block the default verified-bundle path.

The builder obeys these boundaries:

1. The base image is pinned by registry digest; package inputs are obtained
   from a pinned Arch archive snapshot or by exact package versions recorded in
   the build manifest.
2. Sources are fetched to a project-private host cache at the exact commits,
   checked for a clean tree, and converted to deterministic read-only build
   inputs.
3. The build container runs as a non-root UID with only the clean source trees
   mounted read-only and one empty output directory mounted read/write.
4. The build stage has no mount of the user's home, Steam, runtime directory,
   D-Bus socket, KDE Wallet, Xodus state, Microsoft DLL, or Xbox credentials.
5. The compiler stage runs without network access once all pinned inputs are
   present.
6. Only declared regular-file outputs can leave the container. Their hashes,
   modes, architecture, and license/provenance records must match the bundle
   schema.
7. The locally built bundle passes the same verifier and installer path as a
   downloaded bundle. There is no privileged fast path for local output.

A clean rebuild is reproducible when each declared output hash matches the
published bundle. A mismatch is reported as a build result, never silently
accepted as a new release.

## ProtonUp acquisition and dedicated compatibility tool

Bootstrap uses an existing `protonup` command when its version matches the
manifest. Otherwise it runs the manifest-pinned Python package through
`uvx --from protonup==VERSION`. ProtonUp receives `--download`, the exact
release, and a project-private cache output. It does not receive Steam's
`compatibilitytools.d` as an install destination.

Bootstrap then:

1. verifies the downloaded archive's size and SHA-256;
2. safely extracts it to a same-filesystem private staging directory;
3. verifies the expected GE-Proton layout;
4. creates a complete independent `GE-Proton11-3-FM` staging tree;
5. rewrites only that staged tree's compatibility-tool identity to the
   dedicated name;
6. records the resulting identity and managed runtime target hashes; and
7. atomically publishes the staged directory only when the destination is
   absent.

Bootstrap never changes canonical `GE-Proton11-3`. An existing
`GE-Proton11-3-FM` is accepted only when the dedicated identity and every
managed target match a supported original, installed, or patched state. Exact
matches become no-op/adoption plan entries. Unknown or mixed state is a
conflict: it is preserved and bootstrap stops with a diagnostic. It is never
recursively overwritten.

## Two-phase resumable workflow

### Read-only inspection

Every invocation begins by resolving the supported Steam root and AppID,
taking the existing launcher/bootstrap lock, reading the private state if one
exists, and checking for active Forza or Xodus processes. Read-only inspection
does not start or stop either process.

### Phase A: Prepare

Prepare performs these ordered actions:

1. Complete supported-host, disk, dependency, source, cache, and destination
   preflight.
2. Print the canonical Prepare plan and require its full `PLAN_SHA256`.
3. Write the automatic private `before.json` snapshot before the first managed
   installation target changes.
4. Acquire and verify GE-Proton with ProtonUp.
5. Download and verify the open-source bundle, or build and verify it in Docker.
6. Stage and publish the dedicated `GE-Proton11-3-FM` compatibility tool.
7. Record a durable `AWAITING_STEAM_PREFIX` checkpoint.
8. Print exact manual instructions and stop successfully.

The instructions require the user to:

- fully restart Steam so it discovers the new compatibility tool;
- select `GE-Proton11-3-FM` in Forza's Compatibility properties;
- leave launch options empty for this first prefix creation;
- start Forza once, wait until the prefix exists, and close the game; and
- rerun the same bootstrap command, adding `--threading-dll`.

An expected Gaming Services error during this first unmodified prefix launch
does not upgrade or downgrade compatibility evidence. Bootstrap itself does
not click Steam or launch the game.

### Phase B: Finish

Finish starts only when Steam has created the AppID prefix and Forza/Xodus are
inactive. It then:

1. verifies the recorded Prepare checkpoint, compatibility-tool identity,
   bundle, and current managed-path snapshot;
2. validates `--threading-dll` as an absolute, user-owned, no-follow regular PE
   file and stages a private copy;
3. creates a fresh pre-Finish checkpoint and a single canonical plan covering
   the licensed copy, user integration, open-source runtime components,
   controller/AP702 patches, and final verification;
4. requires the complete `PLAN_SHA256` confirmation;
5. copies the licensed DLL to the exact `.threading` prefix destination using
   an adjacent backup/publication transaction;
6. delegates user integration and Xodus/XGameRuntime installation to the
   existing runtime-component transaction owner;
7. delegates controller and AP702 changes to the existing exact-build patcher;
8. runs transaction status and the read-only doctor;
9. writes `after.json`, compares it to `before.json`, and stores the canonical
   comparison report; and
10. prints the generated Steam launch-options line and marks the bootstrap
    `READY_TO_ATTEMPT`.

The user then pastes the line into Steam and launches Forza manually. A passing
Finish means ready to attempt, not gameplay verified. Existing acceptance and
live-validation steps remain explicit.

## State machine and persistence

Bootstrap stores only private coordinator state under:

```text
${XDG_STATE_HOME:-$HOME/.local/state}/forza-motorsport-linux/bootstrap/
└── TRANSACTION_ID/
    ├── state.json
    ├── plan.json
    ├── before.json
    ├── pre-finish.json
    ├── after.json
    ├── comparison.json
    └── recovery/
```

Directories are mode 0700 and regular files are mode 0600. Paths are opened
without following symlinks, state updates use temp-file/fsync/rename, and every
state transition names the exact previously durable state. One user may have
only one unfinished bootstrap transaction.

Normal durable states are:

```text
NEW
  -> PREPARING
  -> AWAITING_STEAM_PREFIX
  -> PLANNED_FINISH
  -> INSTALLING_FINISH
  -> READY_TO_ATTEMPT
  -> ACCEPTED
```

An interruption keeps the last durable state plus per-operation intent and
completion records. Resume derives reality from current/stage/backup hashes;
it never assumes that the previous process finished its last operation.
`ACCEPTED` can be reached only through the existing explicit acceptance path
after manual live validation.

## Licensed Microsoft threading runtime

The user supplies the Microsoft file from a licensed source through
`--threading-dll`. Bootstrap does not download it and the repository does not
publish it.

The coordinator validates and copies it locally because safe rollback requires
knowing whether source, stage, destination, and backup are byte-identical. Its
SHA-256 and size may appear only inside the owner-only transaction journal.
They are redacted from ordinary stdout, public diagnostics, test fixtures,
release evidence, support bundles, and Git. The original source path is not
persisted.

If the destination already contains the same bytes, the action is a no-op. If
it contains different bytes, Finish records and preserves an adjacent backup
before publication. Rollback restores that exact backup or removes a proven
new destination. An unknown replacement is preserved as recovery evidence and
causes a fail-closed stop.

## SHA-256 before/after evidence

Snapshots use canonical JSON with sorted UTF-8 path identifiers, explicit
state (`absent` or `regular`), mode, size, and SHA-256 for regular files. They
also bind the bootstrap manifest digest, AppID, resolved Steam root identifier,
dedicated tool identity, transaction ID, and snapshot schema. Absolute home
paths are represented by stable logical roots and are not printed publicly.

The monitored scope is deliberately finite:

- the compatibility-tool identity marker;
- the two open-source XGameRuntime destinations;
- the exact four controller/AP702 patch targets;
- the licensed `.threading` destination, with its hash private/redacted;
- the eight user-local integration payloads and their install manifest;
- the user service definition and enabled/disabled state; and
- the generated launch-options value as data, not Steam configuration.

The scope excludes the rest of GE-Proton, Steam configuration, shader caches,
game files, save data, the mutable prefix, account state, and unrelated user
files. This prevents a normal Steam restart or prefix creation from becoming a
false proof failure and avoids collecting unrelated private data.

The comparison policy is generated from the confirmed plan, not inferred after
installation:

- **Expected changes:** an allowlisted path moved from the recorded before
  state to the exact planned after hash/mode/state.
- **Required unchanged:** a monitored input or already-correct managed path
  retained its exact before hash/mode/state.
- **Unexpected changes:** any monitored path differs from both its required
  unchanged state and its exact planned after state, or a required record is
  missing.

Success requires every expected change to match, every required-unchanged path
to remain identical, and zero unexpected changes. The human report shows
logical paths and categories. The machine report retains private hashes. The
licensed DLL's digest is displayed only as `[private local digest verified]`.

On the current already-working installation, a first standalone snapshot is
read-only. When bootstrap is later run against that host, supported identical
files are classified as required unchanged/no-op entries. Setup does not
rewrite them merely to reproduce the installation history.

## Transaction composition and rollback

Bootstrap is a coordinator, not a second low-level installer. Each existing
owner keeps its boundary:

- compatibility-tool publication: bootstrap's same-filesystem directory
  transaction;
- licensed DLL copy: bootstrap's single-target adjacent transaction;
- Xodus/user integration and XGameRuntime: `install-runtime-components`;
- controller/AP702 patching: `patch-known-build`;
- launch readiness: `forza-doctor`;
- launch options: `print-steam-options`.

The coordinator records the child transaction ID, plan digest, patch backup
manifest, and exact completed boundary before advancing. If Finish fails, it
reports the current state and preserves all evidence; it does not guess that an
automatic rollback is safe.

Explicit bootstrap rollback runs in reverse dependency order:

1. restore exact-build patches from their verified backup manifest;
2. roll back the unfinished runtime-component transaction;
3. restore or remove the locally copied Microsoft threading destination;
4. remove a newly published dedicated compatibility tool only when its full
   bootstrap identity and managed state are still proven; and
5. retain journals, backups, and displaced unexpected objects until recovery
   is verified.

Accepted runtime transactions continue to use the existing explicit
`restore-runtime` semantics. Rollback never removes a compatibility tool whose
contents changed after bootstrap or one adopted from a pre-existing live
installation.

## Steam handoff and runtime behavior

Steam remains manual because its UI/config state is mutable and not a stable
public installation interface. Bootstrap prints two exact handoffs:

1. after Prepare: restart Steam, select `GE-Proton11-3-FM`, create the prefix,
   close Forza;
2. after Finish: paste the generated launch-options line and launch manually.

The final options contain the existing launcher and Xodus socket exposure, and
do not add `PROTON_DISABLE_HIDRAW`, `PROTON_ENABLE_WAYLAND`,
`-SkipTargetHardwareProfiler`, or accumulated diagnostic flags. Steam Input
remains disabled for the currently verified physical-controller path.

At runtime, `forza-linux` starts the disabled Xodus user service only for the
owned game session and stops only that owned invocation. No bootstrap action
enables a login service. KDE Wallet continues to satisfy Secret Service through
`org.freedesktop.secrets`; bootstrap neither installs nor starts GNOME Keyring.

## Failure behavior

Bootstrap stops without mutation when it encounters:

- unsupported host, architecture, Steam layout, or AppID;
- an active game, Xodus service, or conflicting bootstrap lock;
- insufficient disk space;
- missing or wrong manifest, download, bundle member, source revision, builder
  image, or build output;
- unsafe archive structure or filesystem object type;
- a Steam prefix that is missing, incomplete, symlinked, or not owned by the
  current user;
- an absent or invalid licensed input during Finish;
- unknown or mixed compatibility-tool, runtime, or patch state;
- a plan digest confirmation mismatch;
- an unfinished child transaction that cannot be safely resumed;
- doctor failure; or
- a non-empty unexpected-change comparison.

Errors identify the failed phase, immutable transaction ID, last durable state,
and safe next command. They never print credentials, Xbox identities, the
licensed source path, or its digest. Network failures are not silently retried;
a verified complete cache entry may be reused on the next explicit invocation.

## Security and privacy

- All host writes are user-local and fixed by an allowlist rooted at resolved
  XDG/Steam locations.
- No command invokes `sudo`, package managers, Steam UI automation, or a game
  launch.
- Downloads have pinned size and SHA-256 and are published to cache only after
  verification.
- Archives and state reject symlinks, hard links, special files, traversal,
  ownership mismatches, and unsafe modes.
- Docker receives no home, Steam, runtime, D-Bus, keyring, credential, or
  proprietary-binary mount.
- Logs and public reports redact home paths, gamertags, XUIDs, tokens, invite
  URIs, proof keys, and the private licensed-file digest.
- Tests use synthetic paths, identities, payloads, and binaries.
- The repository and release policy continue to reject Microsoft, Steam,
  Proton, and game binaries.

## Verification strategy

Implementation follows test-driven development. Each behavior begins with a
failing test, the smallest implementation, and a passing focused test before
the next behavior.

### Unit and policy tests

- CLI parsing, mutually exclusive modes, resume selection, and help output.
- Arch/x86_64/native-Steam preflight using synthetic roots.
- Canonical manifest parsing, plan hashing, state transitions, and mode 0600
  state publication.
- ProtonUp argv construction that cannot target Steam directly.
- GE archive and bundle verification, including wrong size/hash.
- Archive traversal, absolute path, link, device, duplicate, count, and
  decompression-limit rejection.
- Dedicated compatibility identity rewrite and conflict-preserving publication.
- Microsoft input validation, private digest redaction, exact no-op copy, and
  restore/remove behavior.
- Snapshot canonicalization and all three comparison categories.
- Existing supported installation detection and no-op adoption.
- Wrong, mixed, partial, interrupted, and post-validation-changed states.
- Reverse-order rollback and preservation of unexpected replacements.
- Repository privacy, license, forbidden-binary, shell, Python, and formatting
  gates already run by `just verify`.

### Hermetic clean-start integration tests

A disposable fake home/Steam tree verifies both phases from an empty supported
layout:

1. fixture ProtonUp download enters only the private cache;
2. Prepare publishes a dedicated tool and stops at the Steam handoff;
3. a synthetic first-launch fixture creates the minimum prefix;
4. Finish consumes synthetic licensed input and exact open-source fixtures;
5. all expected targets match their manifest hashes;
6. no unrelated fake-Steam sentinel changes;
7. comparison reports zero unexpected changes;
8. a second bootstrap run performs no managed writes; and
9. rollback restores the exact baseline hashes.

The same scenario runs with a downloaded fixture bundle and with a locally
built Docker fixture bundle.

### Docker reproducibility gates

- Builder reference contains an immutable digest.
- Build runs as non-root with only declared mounts.
- Compiler stage uses no network.
- Source trees match exact commits and are clean.
- Two clean builds produce identical declared output hashes.
- Outputs match the release bundle and pass architecture/license/provenance
  validation.
- A clean host without Docker receives a clear source-mode failure while the
  default bundle verifier remains usable.

### Controlled live validation

Container success is not a game test. After automated gates pass, live work is
separate and explicit:

1. take a read-only snapshot of the current known-working installation;
2. run bootstrap first in inspection/no-op mode against the current host;
3. verify that the snapshot comparison proposes no rewrite of identical files;
4. only after reviewing that plan, exercise the clean-start path in an isolated
   disposable Steam fixture or a separately authorized host layout;
5. perform the manual Steam selection and launch steps;
6. rerun the dated live matrix for launch, online content, controller/hotplug,
   AP702, Invite, Join, and clean Xodus shutdown; and
7. accept the transaction only after that manual evidence passes.

Development and container tests never mutate the currently working Forza
installation. A live snapshot is taken before any separately approved live
mutation.

## Release and documentation sequence

1. Land the coordinator, parsers, fixtures, and tests without a public binary
   asset.
2. Pin and review the Docker builder and exact component sources.
3. Build the open-source bundle twice from clean inputs and confirm identical
   hashes.
4. Run the complete repository and hermetic clean-start matrices.
5. Inspect the current live install read-only and confirm no-op behavior.
6. Perform the controlled live matrix without weakening current rollback
   evidence.
7. Commit the final bundle metadata, create the immutable release tag, and
   upload the exact pre-hashed open-source asset.
8. Verify the public download path from a fresh clone and empty cache.
9. Update README/install/architecture/troubleshooting/release evidence with the
   exact supported boundary and remaining manual steps.

No publication statement may describe Docker, doctor, SHA comparison, or CI as
proof that Forza itself works. Gameplay and Xbox claims remain tied to dated
manual evidence on the tested host.
