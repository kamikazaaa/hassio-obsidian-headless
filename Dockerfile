# Immutable multi-architecture indexes verified against official registries.
# BuildKit selects amd64/arm64 automatically; see docs/build-provenance.md.
FROM golang:1.26.9-bookworm@sha256:d9c68c2c51161e12fd77e4c6320687c9cd86e1af1e3ad6e6cd63ff970641453c AS go-base

FROM go-base AS go-builder

ENV CGO_ENABLED=0 GOTOOLCHAIN=local GOFLAGS=-mod=readonly GOPROXY=https://proxy.golang.org GOSUMDB=sum.golang.org
WORKDIR /build/remarkable-sync
COPY remarkable-sync/ .
RUN go mod verify \
    && go build -mod=readonly -buildvcs=false -trimpath -ldflags="-s -w" -o /out/remarkable-sync .

# A release tag alone can be moved. Fetch and verify the reviewed commit.
WORKDIR /build/rmapi
COPY remarkable-sync/patches/rmapi-contained-paths.patch /build/patches/rmapi-contained-paths.patch
COPY remarkable-sync/patches/rmapi-dependencies.patch /build/patches/rmapi-dependencies.patch
RUN git init . \
    && git remote add origin https://github.com/ddvk/rmapi.git \
    && git -c core.hooksPath=/dev/null fetch --depth=1 origin 0a69a608b22f5c11dff07254a7e30b45f2c041f7 \
    && git -c core.hooksPath=/dev/null checkout --detach FETCH_HEAD \
    && test "$(git rev-parse HEAD)" = "0a69a608b22f5c11dff07254a7e30b45f2c041f7" \
    && printf '%s\n' 'f126dbd371f7f31a73d2eec95d5c1cc4df47d403123ac198a5c9a0e0712a1212  /build/patches/rmapi-contained-paths.patch' | sha256sum -c - \
    && git apply --check /build/patches/rmapi-contained-paths.patch \
    && git apply /build/patches/rmapi-contained-paths.patch \
    && printf '%s\n' '81df98d4cdcf43e378bb4e2c46ce597f0b0d63f42e9dae469686da9331ee267c  /build/patches/rmapi-dependencies.patch' | sha256sum -c - \
    && git apply --check /build/patches/rmapi-dependencies.patch \
    && git apply /build/patches/rmapi-dependencies.patch \
    && git diff --check \
    && go test util/contained_download.go util/util.go util/contained_download_test.go \
    && go mod download \
    && go mod verify \
    && go test ./... \
    && go vet ./... \
    && go build -mod=readonly -buildvcs=false -trimpath \
        -ldflags="-s -w -X github.com/juruen/rmapi/version.Version=v0.0.34-hardened.2" \
        -o /out/rmapi .

FROM node:22.23.3-bookworm-slim@sha256:c3de60bf2f9dd0ac6370e6117950ff62d6e339527e7472301c9c78a017978392 AS node-builder

# Bootstrap HTTPS trust from the untouched pinned base; Node slim has no CA bundle.
# Keep this independent of the Go build and its downloaded dependencies.
COPY --from=go-base /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt

# Freeze signed Debian package inputs. The dated snapshot disables expiry
# checking; APT signature and package hash verification stay enabled.
COPY build/debian.sources /tmp/debian.sources
RUN rm -f /etc/apt/sources.list /etc/apt/sources.list.d/* \
    && install -m 0644 /tmp/debian.sources /etc/apt/sources.list.d/debian.sources \
    && rm /tmp/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends g++ make python3 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/obsidian
COPY build/obsidian/package.json build/obsidian/package-lock.json ./
# Skip lifecycle scripts and unpinned native prebuild downloads. Compile only
# the required SQLite extension from locked source using the pinned headers.
RUN npm ci --ignore-scripts --omit=dev --engine-strict --no-audit --no-fund \
    && npm_config_nodedir=/usr/local npm_config_build_from_source=true \
        npm rebuild better-sqlite3 --offline --no-audit --no-fund \
    && node -e 'const D = require("better-sqlite3"); const d = new D(":memory:"); if (d.prepare("SELECT 42 AS v").get().v !== 42) process.exit(1); d.close();' \
    && node node_modules/obsidian-headless/cli.js --version

FROM python:3.12.15-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258 AS python-builder

COPY requirements.lock /build/requirements.lock
RUN python3 -m pip install --no-cache-dir --disable-pip-version-check \
        --require-hashes --only-binary=:all: --index-url=https://pypi.org/simple \
        -r /build/requirements.lock \
    && python3 -m pip check

# The HA base provides S6 overlay v3 and /init. All runtime stages use Debian
# bookworm so copied binaries do not cross glibc release generations.
FROM ghcr.io/home-assistant/base-debian:bookworm@sha256:0bf0b64cc09b5490fc06cb6d71ca45f3949cd2fb0bee1abf2e422a49ddae1640

ARG BUILD_ARCH
ARG BUILD_VERSION=0.1.0-dev
LABEL io.hass.name="Obsidian Headless Hardened" \
    io.hass.description="Isolated acquisition and read-only authenticated MCP" \
    io.hass.arch="${BUILD_ARCH}" \
    io.hass.type="app" \
    io.hass.version="${BUILD_VERSION}" \
    org.opencontainers.image.source="https://github.com/kamikazaaa/hassio-obsidian-headless"

COPY build/debian.sources /tmp/debian.sources
RUN rm -f /etc/apt/sources.list /etc/apt/sources.list.d/* \
    && install -m 0644 /tmp/debian.sources /etc/apt/sources.list.d/debian.sources \
    && rm /tmp/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates git passwd \
        libatomic1 libstdc++6 libssl3 libffi8 zlib1g libbz2-1.0 liblzma5 \
        libsqlite3-0 libexpat1 libreadline8 libncursesw6 libgdbm6 libgdbm-compat4 libuuid1 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 obsidian \
    && groupadd --gid 10002 git-sync \
    && groupadd --gid 10003 remarkable \
    && groupadd --gid 10004 mcp-reader \
    && useradd --uid 10001 --gid 10001 --groups 10004 --no-create-home --home-dir /data/obsidian --shell /usr/sbin/nologin obsidian \
    && useradd --uid 10002 --gid 10002 --groups 10004 --no-create-home --home-dir /data/git --shell /usr/sbin/nologin git-sync \
    && useradd --uid 10003 --gid 10003 --groups 10004 --no-create-home --home-dir /data/remarkable --shell /usr/sbin/nologin remarkable \
    && useradd --uid 10004 --gid 10004 --no-create-home --home-dir /data/mcp --shell /usr/sbin/nologin mcp-reader

COPY --from=node-builder /usr/local/bin/node /usr/local/bin/node
COPY --from=node-builder /opt/obsidian /opt/obsidian
COPY --from=python-builder /usr/local/bin/python3.12 /usr/local/bin/python3.12
COPY --from=python-builder /usr/local/lib/python3.12 /usr/local/lib/python3.12
COPY --from=python-builder /usr/local/lib/libpython3.12.so* /usr/local/lib/
COPY --from=go-builder /out/remarkable-sync /usr/local/bin/remarkable-sync
COPY --from=go-builder /out/rmapi /usr/local/bin/rmapi
COPY --from=go-builder /build/rmapi/LICENSE /usr/share/licenses/rmapi/LICENSE
COPY --from=go-builder /build/patches/rmapi-contained-paths.patch /usr/share/licenses/rmapi/local-changes.patch
COPY --from=go-builder /build/patches/rmapi-dependencies.patch /usr/share/licenses/rmapi/dependency-changes.patch
COPY LICENSE /usr/share/licenses/obsidian-headless-hardened/LICENSE

# Workers re-exec after dropping privilege and changing directory. Install on
# Python's normal path; no inherited PYTHONPATH or mutable working tree needed.
COPY hardened /usr/local/lib/python3.12/site-packages/hardened
COPY rootfs /
RUN ldconfig \
    && ln -sf /usr/local/bin/python3.12 /usr/local/bin/python3 \
    && ln -s /opt/obsidian/node_modules/obsidian-headless/cli.js /usr/local/bin/ob \
    && chmod 0755 \
        /etc/s6-overlay/s6-rc.d/obsidian-sync/run \
        /etc/s6-overlay/s6-rc.d/obsidian-sync/finish \
        /etc/s6-overlay/s6-rc.d/obsidian-mcp-server/run \
        /etc/s6-overlay/s6-rc.d/obsidian-mcp-server/finish \
        /etc/s6-overlay/s6-rc.d/remarkable-sync/run \
        /etc/s6-overlay/s6-rc.d/remarkable-sync/finish \
        /etc/s6-overlay/s6-rc.d/vault-git-sync/run \
        /etc/s6-overlay/s6-rc.d/vault-git-sync/finish \
    && python3 -c 'import ssl, sqlite3, ctypes, bz2, lzma; import hardened.runtime, hardened.reader, hardened.snapshots, hardened.git_sync' \
    && ob --version \
    && rmapi version

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
EXPOSE 8420
HEALTHCHECK --interval=30s --timeout=10s --start-period=120s --retries=3 \
    CMD ["/usr/local/bin/python3", "-m", "hardened.runtime", "status"]
