# Forza Motorsport Linux GitHub Publication Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish the verified integration repository and component branches, request upstream review in the correct projects, and post an accurate compatibility update to Proton issue 7151.

**Architecture:** The public integration repository is the stable documentation hub. Component forks carry source changes under their original licenses; draft PRs remain small and dependency-ordered; Proton receives only a compatibility report linking to stable evidence.

**Tech Stack:** Git, GitHub CLI, GitHub Actions, GitHub issues/pull requests/releases, Astro blog.

**Spec:** `docs/superpowers/specs/2026-08-31-forza-motorsport-linux-publication-design.md`

## Global Constraints

- Publish no Microsoft binary, token, proof key, XUID, gamertag, invite URI, email address, or private path.
- Use `7078411+volcmen@users.noreply.github.com` for new public commits.
- Do not force-push a public reviewed branch.
- Do not claim automatic incoming Xbox invite notifications.
- Do not claim ordinary Proton support; name the exact custom stack and tested revisions.
- Open draft PRs before requesting merge and link overlapping Xodus issue 94/PR 105 and xgameruntime PR 19.
- Post to Proton issue 7151 only after all linked repositories and branches are public and immutable.

---

### Task 1: Final local publication audit

**Files:**
- Modify: `docs/verification.md`
- Create: `docs/publication-manifest.toml`
- Test: `tests/test_repository_policy.py`

**Interfaces:**
- Consumes: final integration, Wine, Xodus, and xgameruntime branch SHAs.
- Produces: one machine-readable manifest of every public claim and its evidence state.

- [ ] **Step 1: Write the failing manifest completeness test**

```python
def test_publication_manifest_has_no_unclassified_claims():
    data = tomllib.loads((ROOT / "docs/publication-manifest.toml").read_text())
    allowed = {"automated", "local-e2e", "experimental", "unresolved", "not-run"}
    assert data["claims"]
    assert all(claim["state"] in allowed for claim in data["claims"])
    assert all(claim["revision"] for claim in data["claims"] if claim["state"] != "unresolved")
```

- [ ] **Step 2: Run and verify failure**

Run:

```bash
uv run pytest tests/test_repository_policy.py::test_publication_manifest_has_no_unclassified_claims -q
```

Expected: failure because the manifest does not exist.

- [ ] **Step 3: Populate exact claims and revisions**

The manifest must classify at least:

```text
online menu/content
KWallet cold-session handoff
Xodus on-demand lifecycle
Xbox controller visibility and driving
AP702 absence
Linux-to-Windows invite
Linux join to Windows session
automatic incoming invite notification
```

The last item is `unresolved`; no blank state is allowed.

- [ ] **Step 4: Run every local gate freshly**

Run:

```bash
just verify
git fsck --full
git log --format='%H %an <%ae>' --all
git status --short --branch
```

Run each component's final command set from Plans 2–4 and confirm exact exit codes in `docs/verification.md`.

Expected: tests/lints/builds pass, commits use noreply identity, and all worktrees are clean except intentionally uncommitted evidence not yet added.

- [ ] **Step 5: Perform a full history privacy scan**

Scan commit blobs and messages, not only the working tree:

```bash
git rev-list --objects --all
git log -p --all -- . ':!docs/superpowers'
```

Search for actual local usernames, home paths, real friend identifiers, XBL authorization strings, Microsoft binary suffixes, and activation query values. Synthetic test identities and protocol names are permitted.

Expected: zero private or prohibited findings.

- [ ] **Step 6: Commit the publication manifest**

```bash
git add docs/publication-manifest.toml docs/verification.md tests/test_repository_policy.py
git commit -m "docs: lock publication evidence"
```

---

### Task 2: Create and validate the public integration repository

**Files:**
- Remote repository: `volcmen/forza-motorsport-linux`
- Modify: local `.git/config` through `gh repo create`

**Interfaces:**
- Consumes: clean local `main` and authenticated GitHub CLI account `volcmen`.
- Produces: public GitHub repository, `origin`, pushed `main`, topics, and passing CI.

- [ ] **Step 1: Confirm the name is still unused**

Run:

```bash
gh repo view volcmen/forza-motorsport-linux --json name,url,visibility
```

Expected: repository-not-found. If it exists, inspect ownership and contents; never overwrite it.

- [ ] **Step 2: Create the public repository and push main**

Run:

```bash
gh repo create volcmen/forza-motorsport-linux \
  --public \
  --source=. \
  --remote=origin \
  --push \
  --description "Reproducible experimental Forza Motorsport setup for Linux using Proton, Wine and Xodus"
```

Expected: the command prints the public repository URL and pushes `main` without rewriting history.

- [ ] **Step 3: Add discoverability metadata**

Run:

```bash
gh api --method PUT repos/volcmen/forza-motorsport-linux/topics \
  -f 'names[]=forza-motorsport' \
  -f 'names[]=linux-gaming' \
  -f 'names[]=proton' \
  -f 'names[]=wine' \
  -f 'names[]=xodus'
```

Expected: the response contains exactly those five topics.

- [ ] **Step 4: Wait for and inspect CI**

Run:

```bash
gh run list --repo volcmen/forza-motorsport-linux --limit 5
gh run watch --repo volcmen/forza-motorsport-linux --exit-status
```

Expected: the verification workflow concludes successfully. If it fails, inspect logs, fix locally through red-green verification, commit, push, and wait for the replacement run before continuing.

---

### Task 3: Publish component forks and branches

**Files:**
- Remote forks under `volcmen`: `xodus`, `xgameruntime`, and a Wine fork/reused existing fork
- Local Git remotes for each isolated worktree

**Interfaces:**
- Consumes: clean component branches from Plans 2–4.
- Produces: public immutable branch URLs and compare links.

- [ ] **Step 1: Inspect existing forks before creating anything**

Run:

```bash
gh repo view volcmen/xodus --json isFork,parent,url,defaultBranchRef
gh repo view volcmen/xgameruntime --json isFork,parent,url,defaultBranchRef
gh repo view volcmen/wine --json isFork,parent,url,defaultBranchRef
```

Expected: reuse forks whose parent is the intended canonical repository; create only missing forks.

- [ ] **Step 2: Create missing forks without cloning over local work**

Run only for missing repositories:

```bash
gh repo fork xodus-gaming/xodus --clone=false --remote=false
gh repo fork xodus-gaming/xgameruntime --clone=false --remote=false
gh repo fork xodus-gaming/wine --clone=false --remote=false
```

Expected: each fork is public and reports the correct parent.

- [ ] **Step 3: Add fork remotes and push named branches**

Push only these reviewed branches:

```text
xodus: forza-social-invite-join
xgameruntime: xodus-social-invite-bridge
wine: wgi-physical-nonroamable-id
wine: storage-trim-property
```

Use explicit refspecs:

```bash
git push volcmen HEAD:refs/heads/<exact-branch-name>
```

Expected: branch SHAs on GitHub match local `git rev-parse HEAD`.

- [ ] **Step 4: Update integration links and rerun CI**

Replace local-only provenance references with public commit URLs, run `just verify`, commit, push, and wait for green CI.

---

### Task 4: Open maintainer-coordinated issues and draft PRs

**Files:**
- GitHub issue 94 comment in `xodus-gaming/xodus`
- Draft PRs in Xodus/xgameruntime/Wine as appropriate
- Create: `docs/upstream.md` in the integration repository

**Interfaces:**
- Consumes: public branches and verified command summaries.
- Produces: review requests with one responsibility each and no unsupported claims.

- [ ] **Step 1: Coordinate Xodus overlap before requesting merge**

Post a concise issue 94 comment containing:

```markdown
I have a tested Forza Motorsport invite/join backend and a keyboard/controller GTK overlay on a current Xodus base. It deliberately excludes the currently non-working incoming RTA push path. The code overlaps the social scope here and the UI direction in #105.

Would maintainers prefer:
1. a backend-only PR followed by an overlay PR, or
2. one draft integration PR for initial review?

Branch, tests, protocol notes, and local end-to-end matrix: <public integration evidence URL>
```

Expected: comment URL is recorded in `docs/upstream.md`. Do not mention any real friend identity.

- [ ] **Step 2: Open the Xodus draft PR in the maintainer-requested shape**

Default split when no maintainer response is required to proceed:

```text
PR 1: feat: add Xbox Multiplayer Activity invite and join operations
PR 2: feat: add keyboard and controller social overlay
```

Each body includes base SHA, commit list, tests, protocol/security boundary, manual evidence, excluded RTA behavior, and links to issue 94/PR 105.

- [ ] **Step 3: Open the xgameruntime draft PR**

Title:

```text
xgameui: bridge social invites through Xodus
```

Target the maintainer branch used by PR 19. The body links its dependency on the Xodus protocol branch and lists XAsync, activation, fragmented-I/O, and installed-artifact tests.

- [ ] **Step 4: Open separate Wine review items**

Controller draft PR title:

```text
windows.gaming.input: identify physical XInput controllers
```

Storage item:

```text
mountmgr: report StorageDeviceTrimProperty
```

Open the controller as a draft PR to `xodus-gaming/wine`. For storage, open a maintainer-facing issue first if the implementation still reports TRIM unconditionally; include the tested patch branch and ask which device-backed capability source is acceptable. Do not submit unconditional behavior to canonical Wine as if its semantics were settled.

- [ ] **Step 5: Record and verify all upstream URLs**

Update `docs/upstream.md`, run `just verify`, commit, push, and wait for green CI.

---

### Task 5: Post the Proton issue 7151 compatibility update

**Files:**
- GitHub comment in `ValveSoftware/Proton#7151`
- Create: `docs/proton-comment.md`

**Interfaces:**
- Consumes: stable integration URL, component branch/PR URLs, and locked evidence manifest.
- Produces: one concise public compatibility report.

- [ ] **Step 1: Generate the comment from locked evidence**

Use this structure:

```markdown
## Tested update: online play, Xbox controller, AP702, and invite/join

I tested the Steam build on Linux with the custom GE-Proton11-3-FM + Xodus stack described earlier in this thread. This is experimental and is not ordinary upstream Proton support.

### Verified locally
- Game reaches online menus and downloads online content.
- Physical Xbox Series controller works after fixing Windows.Gaming.Input's missing NonRoamableId for physical XInput devices.
- AP702 disappears after mountmgr answers StorageDeviceTrimProperty instead of returning STATUS_NOT_SUPPORTED.
- Forza's Invite Friends button opens a Xodus social picker.
- Xbox invite from Linux to a Windows player and Linux join to a Windows player's published activity both completed successfully.

### Important limitation
Automatic incoming Xbox invite notifications are not working. The attempted RTA registration returns HTTP 400, so the published supported path does not claim push notifications.

Reproducible launcher, exact-build fallback checks, source branches, tests, rollback, and evidence:
<integration repository URL>

No Microsoft DLLs are distributed; the threading runtime requirement remains user-supplied from a licensed Windows installation.
```

- [ ] **Step 2: Validate links and privacy before posting**

Run the comment through the repository privacy test and open every Markdown link. Confirm the text contains no username, home path, friend identity, XUID, invite URI, or unstated claim.

- [ ] **Step 3: Post exactly once**

Run:

```bash
gh issue comment 7151 --repo ValveSoftware/Proton --body-file docs/proton-comment.md
```

Expected: GitHub returns one comment URL. Do not repost on slow UI feedback; query the issue comments first if the command response is ambiguous.

- [ ] **Step 4: Record the comment URL**

Add it to `docs/upstream.md`, run `just verify`, commit, push, and wait for green CI.

---

### Task 6: Update the blog and publish a tagged integration release

**Files:**
- Modify: external blog file `$BLOG_ROOT/src/content/blog/forza-motorsport-linux/index.mdx`
- Modify: `README.md`
- Modify: `docs/verification.md`
- Create: `docs/release-v0.1.0.md`
- GitHub release: `v0.1.0`

**Interfaces:**
- Consumes: public repository/PR/issue URLs and green CI.
- Produces: linked blog article and first source-only release.

- [ ] **Step 1: Add public links without changing the article's personal story**

Add a short `Source and reproducibility` section linking the integration repository, Proton comment, and component review items. Preserve the existing narrative and use only “a friend on Windows”; include no nickname.

- [ ] **Step 2: Verify the blog freshly**

Run:

```bash
cd "$BLOG_ROOT"
bun test
bun run check
bun run build
while IFS= read -r identifier; do
    [[ -z $identifier ]] && continue
    if rg -F -n -- "$identifier" src/content/blog/forza-motorsport-linux dist; then
        exit 1
    fi
done < "${FORZA_PRIVATE_DENYLIST:?set a local-only private identifier file}"
```

Expected: tests, check, and build exit `0`; nickname scan returns no matches.

- [ ] **Step 3: Commit and push only the Forza article changes**

Review dirty state first and stage explicit files only:

```bash
git add src/assets/forza-motorsport-linux.png \
  src/content/blog/forza-motorsport-linux/index.mdx \
  src/content/blog/forza-motorsport-linux/ap702.png
git commit -m "docs: link Forza Motorsport Linux source work"
git push origin main
```

Do not stage unrelated existing untracked blog files.

- [ ] **Step 4: Create the source-only release**

Tag the exact green integration commit:

Write `docs/release-v0.1.0.md` from the locked evidence manifest, then run:

```bash
git add docs/release-v0.1.0.md
git commit -m "docs: prepare v0.1.0 release notes"
git push origin main
git tag -a v0.1.0 -m "Forza Motorsport Linux integration v0.1.0"
git push origin v0.1.0
gh release create v0.1.0 --repo volcmen/forza-motorsport-linux \
  --title "Forza Motorsport Linux integration v0.1.0" \
  --notes-file docs/release-v0.1.0.md
```

Release contents are GitHub-generated source archives plus documentation/checksums only. Attach no DLL, SYS, EXE, game asset, credential, or system log.

- [ ] **Step 5: Run the final public consistency audit**

Verify that README, release, Proton comment, blog, branches, and PRs all point to the same revisions and use the same support/limitation language.

Expected: no broken links, no contradictory support claims, and green CI on the tagged commit.
