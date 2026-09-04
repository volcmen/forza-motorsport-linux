# SPDX-FileCopyrightText: 2026 Forza Motorsport Linux tools contributors
# SPDX-License-Identifier: GPL-3.0-or-later

# syntax=docker/dockerfile:1.7
ARG BASE_IMAGE=docker.io/library/archlinux:base-devel@sha256:694da1fce635e3a14d90751941b08f02c500e8724682f7a00768e7152251ec34
FROM ${BASE_IMAGE} AS fixture

ARG BASE_IMAGE
ARG SOURCE_DATE_EPOCH=1756684800
ENV SOURCE_DATE_EPOCH=${SOURCE_DATE_EPOCH} \
    LC_ALL=C.UTF-8 \
    TZ=UTC

RUN useradd --uid 1000 --create-home builder
COPY build-bundle.sh /usr/local/bin/build-bundle
RUN chmod 0755 /usr/local/bin/build-bundle
USER builder
WORKDIR /work

FROM fixture AS builder

USER root
ARG BASE_IMAGE
ARG ARCH_SNAPSHOT=https://archive.archlinux.org/repos/2026/09/01/$repo/os/$arch
ARG LLVM_MINGW_URL=https://github.com/mstorsjo/llvm-mingw/releases/download/20260826/llvm-mingw-20260826-ucrt-ubuntu-22.04-x86_64.tar.xz
ARG LLVM_MINGW_SHA256=cee8d2ce3da5145ce4dc882e70d0b0719a783d53a99752c60948fc0659975a65

RUN printf 'Server = %s\n' "${ARCH_SNAPSHOT}" > /etc/pacman.d/mirrorlist \
    && pacman -Syu --noconfirm --needed \
        autoconf automake bison clang cmake curl flex fontforge git gtk4 libadwaita \
        libtool libx11 libxcomposite libxcursor libxext libxfixes libxi \
        libxinerama libxkbcommon libxrandr libxrender libxcb make meson \
        ninja protobuf python rust sdl3 tar uv webkit2gtk-4.1 zstd \
    && pacman -Scc --noconfirm \
    && rm -rf /var/cache/pacman/pkg/* /var/lib/pacman/sync/*

RUN curl --fail --location --proto '=https' --tlsv1.2 \
        --output /tmp/llvm-mingw.tar.xz "${LLVM_MINGW_URL}" \
    && printf '%s  %s\n' "${LLVM_MINGW_SHA256}" /tmp/llvm-mingw.tar.xz \
        | sha256sum --check --strict - \
    && mkdir /opt/llvm-mingw \
    && tar --extract --xz --file /tmp/llvm-mingw.tar.xz \
        --directory /opt/llvm-mingw --strip-components=1 \
    && rm /tmp/llvm-mingw.tar.xz

RUN mkdir -p /opt/vulkan-registry \
    && curl --fail --location --proto '=https' --tlsv1.2 \
        https://raw.githubusercontent.com/KhronosGroup/Vulkan-Docs/v1.4.339/xml/vk.xml \
        -o /opt/vulkan-registry/vk.xml \
    && curl --fail --location --proto '=https' --tlsv1.2 \
        https://raw.githubusercontent.com/KhronosGroup/Vulkan-Docs/v1.4.339/xml/video.xml \
        -o /opt/vulkan-registry/video.xml \
    && printf '%s\n' \
        '5ebddf02358d937d8ce9bd997b20d2be9693e48320e1a060056c26b9cfe15438  /opt/vulkan-registry/vk.xml' \
        '4480ab87c8ee6d561376fa83820af1071f85ff7fa1a26b21f521717fa04d5809  /opt/vulkan-registry/video.xml' \
        | sha256sum --check --strict -

COPY fetch-xodus-deps.sh /usr/local/bin/fetch-xodus-deps
RUN chmod 0755 /usr/local/bin/fetch-xodus-deps \
    && mkdir -p /usr/local/share/forza-builder \
    && { \
        printf 'schema=1\nbase_image=%s\narch_snapshot=%s\nllvm_mingw_sha256=%s\n' \
            "${BASE_IMAGE}" "${ARCH_SNAPSHOT}" "${LLVM_MINGW_SHA256}"; \
        sha256sum /usr/local/bin/build-bundle /usr/local/bin/fetch-xodus-deps; \
        LC_ALL=C pacman -Q | LC_ALL=C sort; \
    } > /usr/local/share/forza-builder/rootfs-manifest.txt \
    && chmod 0644 /usr/local/share/forza-builder/rootfs-manifest.txt
ENV PATH=/opt/llvm-mingw/bin:${PATH} \
    FORZA_ARCH_SNAPSHOT=${ARCH_SNAPSHOT} \
    FORZA_BASE_IMAGE=${BASE_IMAGE} \
    FORZA_LLVM_MINGW_SHA256=${LLVM_MINGW_SHA256}
USER builder
WORKDIR /work
