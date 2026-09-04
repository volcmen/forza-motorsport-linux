#!/bin/sh
# SPDX-FileCopyrightText: 2026 Forza Motorsport Linux tools contributors
# SPDX-License-Identifier: GPL-3.0-or-later

set -eu
umask 000

test -f /src/xodus/Cargo.lock
test -d /deps
test ! -e /deps/vendor
test ! -e /deps/cargo-config.toml
test ! -e /deps/cargo-home

mkdir /deps/vendor
mkdir /deps/cargo-home
cd /src/xodus
CARGO_HOME=/deps/cargo-home \
    cargo vendor --locked --versioned-dirs /deps/vendor >/deps/cargo-config.toml
