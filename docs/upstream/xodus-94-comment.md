I have a source-only Forza Motorsport integration that extends Xodus with an
explicit friend picker and Microsoft/Xbox Invite and Join actions.

The working v0.1 reproduction is the branch
`forza-social-invite-join-v0.1` at
`7b236772297b3475ea4f3cb830feb5b224f3064a`. It is a privacy-clean single
commit over the reviewed upstream parent and is being published as reproduction
evidence, not as a merge proposal. That exact source produced the Xodus
artifacts used in a successful two-launch Forza test.

For maintainable upstream discussion, the separate branch
`xodus-social-invite-bridge` at
`ee9db0f68122a9731b9b66cc24767693d1737f5c` is based on the newer Xodus line.
Its source tests are useful, but its paired next-generation XGameRuntime
experiment was rejected in the live game, so I am not presenting it as a
working replacement.

This overlaps the direction of #105 for XGameUI and #108 for title-claimed
XSTS token IPC. Before opening a Xodus PR, would the maintainers prefer the work
split into:

1. backend authorization, PeopleHub, Multiplayer Activity, and bounded IPC;
2. a separate keyboard/controller UI change built on #105; and
3. any title-token dependency rebased after #108?

Automatic incoming Xbox invite notifications are not supported by the tested
v0.1 workflow. The verified behavior is limited to explicit Invite and Join
actions after the user opens the social picker.

No Microsoft binary, credential, account identifier, invite URI, or compiled
artifact will be attached. Verified public source links and the redacted test
record will be inserted after the immutable branches exist, before this comment
is posted.
