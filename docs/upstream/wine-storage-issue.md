# mountmgr: define device-backed StorageDeviceTrimProperty semantics

I have a compatibility branch named `storage-trim-property` based on
`xodus-gaming/wine:bleeding-edge`.

Base: `b1dd32734a34472a28eb5be9922df06e07ac0834`

Tip: `a7719bd8d0719e5ea6061387db020fa6a6b39d27`

Forza Motorsport queries `IOCTL_STORAGE_QUERY_PROPERTY` with
`StorageDeviceTrimProperty` and rejects a volume as an HDD when the capability
is unavailable. The branch implements the descriptor and the short, header,
partial, full, padding, and unrelated-property buffer cases.

The current production behavior unconditionally reports `TrimEnabled = TRUE`.
That is useful in a dedicated compatibility build and the AP702 warning was
absent in the tested game, but it is not an honest device-backed implementation
for canonical Wine. I am therefore opening an issue rather than proposing that
behavior as a PR.

Which existing Wine boundary should be authoritative for a canonical result?
In particular, should mountmgr derive this from the backing Unix device,
filesystem/discard capability, an existing volume property, or another
maintainer-preferred abstraction? Once that contract is clear, I can reshape
the branch and tests around it.

The production module and kernel32 test executable compile and link with
Clang/LLD 22.1.8. The standard volume PE test remains **BLOCKED before
dispatch** because the old build tree cannot initialize its Wine test prefix
(`secur32.dll`, then `kernel32.dll` status `c0000135`); no dispatched assertion
failed. No binary is attached, and the exact source/evidence links will be
inserted after publication before this issue is opened.
