# Upstream publication ledger

`published` means GitHub returned the repository/branch and its remote commit
was checked against the recorded revision. `prepared` means that the local
evidence and public text passed the repository policy gate but the external
review action has not been performed. The coordination actions and their exact
URLs are recorded below. Every action in this ledger is now published at the
returned URL shown in its row.

| Action | State | Exact target | Result URL |
| --- | --- | --- | --- |
| Integration repository creation | `published` | Public source-only repository | [repository](https://github.com/volcmen/forza-motorsport-linux) |
| Xodus fork and legacy/current branches | `published` | Parent `xodus-gaming/xodus`; branches `forza-social-invite-join-v0.1` and `xodus-social-invite-bridge` | [legacy branch](https://github.com/volcmen/xodus/tree/forza-social-invite-join-v0.1) · [legacy commit](https://github.com/volcmen/xodus/commit/7b236772297b3475ea4f3cb830feb5b224f3064a) · [current branch](https://github.com/volcmen/xodus/tree/xodus-social-invite-bridge) · [current commit](https://github.com/volcmen/xodus/commit/ee9db0f68122a9731b9b66cc24767693d1737f5c) |
| wine-forza-motorsport fork and reconstructed branch | `published` | Parent `AllanVester/wine-forza-motorsport`; branch `forza-xgameui-invite-join-v0.1` | [branch](https://github.com/volcmen/wine-forza-motorsport/tree/forza-xgameui-invite-join-v0.1) · [commit](https://github.com/volcmen/wine-forza-motorsport/commit/a1548b1cf57371715d10b608bc81a77a188e40d4) |
| Wine fork and controller/storage branches | `published` | `xodus-gaming/wine` is in the same GitHub fork network, so both branches live in the existing `volcmen/wine-forza-motorsport` fork | [controller branch](https://github.com/volcmen/wine-forza-motorsport/tree/wgi-physical-nonroamable-id) · [controller commit](https://github.com/volcmen/wine-forza-motorsport/commit/ddd302d97c6008d79ea4f3e3ad56014cb548e514) · [storage branch](https://github.com/volcmen/wine-forza-motorsport/tree/storage-trim-property) · [storage commit](https://github.com/volcmen/wine-forza-motorsport/commit/a7719bd8d0719e5ea6061387db020fa6a6b39d27) |
| WineGDK fork and experimental branch | `published` | Parent `Weather-OS/WineGDK`; branch `xodus-social-invite-bridge-experimental` | [branch](https://github.com/volcmen/WineGDK/tree/xodus-social-invite-bridge-experimental) · [commit](https://github.com/volcmen/WineGDK/commit/d96a768e25f632b04a457e4cb9f585e89ef5d095) |
| Xodus issue 94 comment | `published` | `xodus-gaming/xodus#94` | [comment](https://github.com/xodus-gaming/xodus/issues/94#issuecomment-5526430802) |
| Wine controller draft PR | `published` | `xodus-gaming/wine:bleeding-edge` | [draft PR](https://github.com/xodus-gaming/wine/pull/4) |
| Wine storage draft RFC PR | `published` | `xodus-gaming/wine:bleeding-edge` | [draft RFC PR](https://github.com/xodus-gaming/wine/pull/5) |
| Proton issue 7151 comment | `published` | `ValveSoftware/Proton#7151` | [comment](https://github.com/ValveSoftware/Proton/issues/7151#issuecomment-5526450265) |
| v0.1.0 tag and source-only release | `published` | Integration repository | [release](https://github.com/volcmen/forza-motorsport-linux/releases/tag/v0.1.0) |

## Immutable public source identities

| Branch | Revision |
| --- | --- |
| [`forza-social-invite-join-v0.1`](https://github.com/volcmen/xodus/tree/forza-social-invite-join-v0.1) | [`7b236772297b3475ea4f3cb830feb5b224f3064a`](https://github.com/volcmen/xodus/commit/7b236772297b3475ea4f3cb830feb5b224f3064a) |
| [`xodus-social-invite-bridge`](https://github.com/volcmen/xodus/tree/xodus-social-invite-bridge) | [`ee9db0f68122a9731b9b66cc24767693d1737f5c`](https://github.com/volcmen/xodus/commit/ee9db0f68122a9731b9b66cc24767693d1737f5c) |
| [`forza-xgameui-invite-join-v0.1`](https://github.com/volcmen/wine-forza-motorsport/tree/forza-xgameui-invite-join-v0.1) | [`a1548b1cf57371715d10b608bc81a77a188e40d4`](https://github.com/volcmen/wine-forza-motorsport/commit/a1548b1cf57371715d10b608bc81a77a188e40d4) |
| [`wgi-physical-nonroamable-id`](https://github.com/volcmen/wine-forza-motorsport/tree/wgi-physical-nonroamable-id) | [`ddd302d97c6008d79ea4f3e3ad56014cb548e514`](https://github.com/volcmen/wine-forza-motorsport/commit/ddd302d97c6008d79ea4f3e3ad56014cb548e514) |
| [`storage-trim-property`](https://github.com/volcmen/wine-forza-motorsport/tree/storage-trim-property) | [`a7719bd8d0719e5ea6061387db020fa6a6b39d27`](https://github.com/volcmen/wine-forza-motorsport/commit/a7719bd8d0719e5ea6061387db020fa6a6b39d27) |
| [`xodus-social-invite-bridge-experimental`](https://github.com/volcmen/WineGDK/tree/xodus-social-invite-bridge-experimental) | [`d96a768e25f632b04a457e4cb9f585e89ef5d095`](https://github.com/volcmen/WineGDK/commit/d96a768e25f632b04a457e4cb9f585e89ef5d095) |

The legacy Xodus branch is a single privacy-clean commit over its reviewed
upstream parent. Internal planning documents and private evidence refs are not
publication sources and must never be pushed.
