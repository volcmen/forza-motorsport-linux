#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Forza Motorsport Linux tools contributors
# SPDX-License-Identifier: GPL-3.0-or-later

set -euo pipefail
umask 022

bundle_root=/tmp/forza-bootstrap-bundle-v1
archive=/output/forza-bootstrap-bundle-v1.tar.zst
xodus_revision=${FORZA_XODUS_REVISION:-1111111111111111111111111111111111111111}
xgameruntime_revision=${FORZA_XGAMERUNTIME_REVISION:-2222222222222222222222222222222222222222}

test "${SOURCE_DATE_EPOCH:-}" = 1756684800
test "${LC_ALL:-}" = C.UTF-8
test "${TZ:-}" = UTC
test -d /output
test ! -e "$archive"
test ! -e "$bundle_root"

mkdir -p "$bundle_root/bin" "$bundle_root/runtime" "$bundle_root/licenses" "$bundle_root/provenance"

if [ "${1:-}" = --fixture ]; then
    xodus_revision=1111111111111111111111111111111111111111
    xgameruntime_revision=2222222222222222222222222222222222222222
    builder_image=fixture
    printf '%s\n' 'fixture xodus service' > "$bundle_root/bin/xodus-service"
    printf '%s\n' 'fixture xodus cli' > "$bundle_root/bin/xodus-cli"
    printf '%s\n' 'fixture xodus overlay' > "$bundle_root/bin/xodus-overlay"
    printf '%s\n' 'fixture xgameruntime pe' > "$bundle_root/runtime/xgameruntime.dll"
    printf '%s\n' 'fixture xgameruntime unix' > "$bundle_root/runtime/xgameruntime.so"
    printf '%s\n' 'Synthetic fixture only; no distributed binary license.' > "$bundle_root/licenses/xodus.txt"
    printf '%s\n' 'Synthetic fixture only; no distributed binary license.' > "$bundle_root/licenses/xgameruntime.txt"
else
    test "$#" -eq 0
    builder_image=${FORZA_BUILDER_IMAGE:?}
    test -f /deps/cargo-config.toml
    test -d /deps/vendor
    test "$(git -C /src/xodus rev-parse --verify 'HEAD^{commit}')" = "$xodus_revision"
    test "$(git -C /src/xgameruntime rev-parse --verify 'HEAD^{commit}')" = "$xgameruntime_revision"

    CARGO_TARGET_DIR=/tmp/xodus-target \
        CARGO_HOME=/tmp/cargo-home \
        RUSTFLAGS='-C link-arg=-Wl,--build-id=none -C debuginfo=0 --remap-path-prefix=/src/xodus=/usr/src/xodus' \
        cargo build --manifest-path /src/xodus/Cargo.toml \
        --config /deps/cargo-config.toml --release --locked --offline \
        -p xodus-service -p xodus-cli -p xodus-overlay

    cp -a /src/xgameruntime /tmp/xgameruntime
    cd /tmp/xgameruntime
    autoreconf -f
    UV_CACHE_DIR=/tmp/uv-cache uv run --offline ./dlls/winevulkan/make_vulkan
    ./tools/make_specfiles
    ./tools/make_requests
    x86_64_CC=x86_64-w64-mingw32-clang \
        x86_64_CXX='x86_64-w64-mingw32-clang++ -std=gnu++17' \
        x86_64_CXXFLAGS=-D_LIBCPP_NO_VCRUNTIME \
        LDFLAGS=-Wl,--build-id=none \
        ./configure --enable-win64 --without-ffmpeg --without-opencl
    make -C dlls/xgameruntime -j2

    install -m 0755 /tmp/xodus-target/release/xodus-service "$bundle_root/bin/xodus-service"
    install -m 0755 /tmp/xodus-target/release/xodus-cli "$bundle_root/bin/xodus-cli"
    install -m 0755 /tmp/xodus-target/release/xodus-overlay "$bundle_root/bin/xodus-overlay"
    install -m 0644 dlls/xgameruntime/x86_64-windows/xgameruntime.dll "$bundle_root/runtime/xgameruntime.dll"
    install -m 0755 dlls/xgameruntime/xgameruntime.so "$bundle_root/runtime/xgameruntime.so"
    install -m 0644 /src/xodus/LICENSE "$bundle_root/licenses/xodus.txt"
    install -m 0644 /src/xgameruntime/LICENSE "$bundle_root/licenses/xgameruntime.txt"
fi

chmod 0755 "$bundle_root/bin/xodus-service" "$bundle_root/bin/xodus-cli" "$bundle_root/bin/xodus-overlay"
chmod 0644 "$bundle_root/runtime/xgameruntime.dll"
chmod 0755 "$bundle_root/runtime/xgameruntime.so"

artifact_json() {
    logical_name=$1
    relative_path=$2
    mode=$3
    artifact=$bundle_root/$relative_path
    size=$(stat --format='%s' "$artifact")
    digest=$(sha256sum "$artifact" | cut -d ' ' -f 1)
    printf '{"logical_name":"%s","mode":%s,"private":false,"relative_path":"%s","sha256":"%s","size":%s}' \
        "$logical_name" "$mode" "$relative_path" "$digest" "$size"
}

{
    printf '{"artifacts":['
    artifact_json xodus-service bin/xodus-service 493
    printf ','
    artifact_json xodus-cli bin/xodus-cli 493
    printf ','
    artifact_json xodus-overlay bin/xodus-overlay 493
    printf ','
    artifact_json xgameruntime-pe64 runtime/xgameruntime.dll 420
    printf ','
    artifact_json xgameruntime-unix64 runtime/xgameruntime.so 493
    printf '],"profile":"legacy-v0.1","schema":1,"sources":{"xgameruntime":"%s","xodus":"%s"}}' \
        "$xgameruntime_revision" "$xodus_revision"
} > "$bundle_root/bundle-manifest.json"

printf '{"xgameruntime":"%s","xodus":"%s"}' \
    "$xgameruntime_revision" "$xodus_revision" \
    > "$bundle_root/provenance/sources.json"
printf '{"arch_snapshot":"%s","base_image":"%s","builder_image":"%s","llvm_mingw_sha256":"%s","source_date_epoch":%s}' \
    "${FORZA_ARCH_SNAPSHOT:-fixture}" "${FORZA_BASE_IMAGE:-fixture}" \
    "$builder_image" "${FORZA_LLVM_MINGW_SHA256:-fixture}" \
    "$SOURCE_DATE_EPOCH" > "$bundle_root/provenance/build-environment.json"
cat > "$bundle_root/provenance/build-commands.txt" <<'EOF'
cargo build --release --locked --offline -p xodus-service -p xodus-cli -p xodus-overlay
autoreconf -f
uv run --offline ./dlls/winevulkan/make_vulkan
./tools/make_specfiles
./tools/make_requests
./configure --enable-win64 --without-ffmpeg --without-opencl
make -C dlls/xgameruntime -j2
tar --sort=name --format=ustar --mtime=@0 --owner=0 --group=0 --numeric-owner
zstd --threads=1 -19
EOF

find "$bundle_root" -type f ! -name SHA256SUMS -printf '%P\0' \
    | sort -z \
    | while IFS= read -r -d '' relative; do
        digest=$(sha256sum "$bundle_root/$relative" | cut -d ' ' -f 1)
        printf '%s  %s\n' "$digest" "$relative"
    done > "$bundle_root/SHA256SUMS"

find "$bundle_root" -type f -exec touch -h -d @0 {} +
find "$bundle_root" -type d -exec touch -h -d @0 {} +
cd /tmp
tar --sort=name --format=ustar --mtime=@0 --owner=0 --group=0 --numeric-owner \
    --create --file=- forza-bootstrap-bundle-v1 \
    | zstd --quiet --threads=1 -19 -o "$archive"
chmod 0644 "$archive"
