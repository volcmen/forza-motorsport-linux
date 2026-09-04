Thanks for the work on Xodus. I got Forza Motorsport's Xbox Invite and Join
working with a friend on Windows, using a friend picker that supports both
keyboard and controller input. I'd like to find a useful way to bring that
work back here without duplicating what's already underway.

The working v0.1 reproduction is the branch
[`forza-social-invite-join-v0.1`](https://github.com/volcmen/xodus/tree/forza-social-invite-join-v0.1)
at [`7b236772297b3475ea4f3cb830feb5b224f3064a`](https://github.com/volcmen/xodus/commit/7b236772297b3475ea4f3cb830feb5b224f3064a).
This is the older version I used for the successful two-launch game test.
I've shared it so others can reproduce that setup, not as a proposal to merge
the whole branch.

I also tried porting the work to the newer Xodus line in
[`xodus-social-invite-bridge`](https://github.com/volcmen/xodus/tree/xodus-social-invite-bridge)
at [`ee9db0f68122a9731b9b66cc24767693d1737f5c`](https://github.com/volcmen/xodus/commit/ee9db0f68122a9731b9b66cc24767693d1737f5c)
but that version, paired with the newer XGameRuntime, failed in the game.
Please don't use it as a replacement for the working setup. Its source tests
are available, but they aren't proof of gameplay.

When I prepared this work, #105 covered XGameUI and #108 covered title-claimed
XSTS token IPC. With that overlap in mind, would it be useful to split a future
PR into:

1. backend authorization, PeopleHub, Multiplayer Activity, and bounded IPC;
2. a separate keyboard/controller UI change built on #105; and
3. any title-token dependency rebased after #108?

Automatic incoming Xbox invite notifications are not supported by the tested
v0.1 workflow. The verified behavior is limited to explicit Invite and Join
actions after the user opens the social picker.

The [test record](https://github.com/volcmen/forza-motorsport-linux/blob/d46640d20c304c9329a99ba823052289ed1f06b6/docs/evidence/v0.1-live-validation.md)
lists what worked and the versions used, without account details.

For people trying the integration, there's now a separate
[v0.2.0 installer prerelease](https://github.com/volcmen/forza-motorsport-linux/releases/tag/v0.2.0).
Its open-source bundle builds reproducibly and downloads correctly, but its
rebuilt binaries still need live-game testing. It includes no Microsoft DLL.
That's a packaging update, not a claim that the newer Xodus experiment works.
