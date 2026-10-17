# Development build provenance

This is a source-build development profile. It has no automatic image publication or release workflow. Container execution and a real Home Assistant installation must pass before this profile is described as installation-ready.

## Platform and build contract

The Dockerfile uses the official multi-architecture Home Assistant Debian bookworm base, which supplies S6 overlay v3 and `/init`. The official Node and Python runtime stages also use bookworm. This keeps the copied executables and shared libraries on the same glibc release family. Go binaries are built with `CGO_ENABLED=0`.

The supported targets are Linux amd64 and arm64 (Home Assistant calls the latter `aarch64`). BuildKit selects the correct platform from each pinned image index. Home Assistant supplies `BUILD_ARCH` and `BUILD_VERSION`; the Dockerfile does not depend on the obsolete implicit `BUILD_FROM` or a separate `build.yaml`. The existing `init: false` setting is required for S6 to be PID 1. These details follow the [Home Assistant image guide](https://contributors.home-assistant.io/apps/guides/dockerfile/) and [configuration reference](https://contributors.home-assistant.io/apps/reference/configuration/), consulted on 2026-10-17.

For a native amd64 development build:

```sh
docker build --pull --platform linux/amd64 \
  --build-arg BUILD_ARCH=amd64 \
  --build-arg BUILD_VERSION=0.1.0-dev \
  --tag obsidian-hardened:dev .
```

For a native arm64 build, use `--platform linux/arm64` and `--build-arg BUILD_ARCH=aarch64`. A cross-platform build on a different host also requires a working emulation or remote builder setup; the Dockerfile does not install one.

Our Python package is copied into `/usr/local/lib/python3.12/site-packages/hardened`. The runtime can therefore re-exec a worker after changing directory and dropping privilege, without inheriting `PYTHONPATH`. The final image contains numeric identities 10001 (Obsidian), 10002 (Git), 10003 (reMarkable), and 10004 (MCP reader). Only acquisition identities join reader group 10004. Runtime bootstrap creates the managed `/data` directories with the agreed private/publication permissions.

## Immutable image inputs

The following indexes were fetched directly from the official OCI registries on 2026-10-17. For every entry, the SHA256 computed over the returned manifest bytes matched the registry's `Docker-Content-Digest`. The indexes were checked to contain both `linux/amd64` and `linux/arm64` manifests. The exact patch-version Node/Python tag names were also checked against the same digests.

| Input | Pinned index SHA256 |
| --- | --- |
| `ghcr.io/home-assistant/base-debian:bookworm` | `0bf0b64cc09b5490fc06cb6d71ca45f3949cd2fb0bee1abf2e422a49ddae1640` |
| `golang:1.26.9-bookworm` | `d9c68c2c51161e12fd77e4c6320687c9cd86e1af1e3ad6e6cd63ff970641453c` |
| `node:22.23.3-bookworm-slim` | `c3de60bf2f9dd0ac6370e6117950ff62d6e339527e7472301c9c78a017978392` |
| `python:3.12.15-slim-bookworm` | `34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258` |

The HA index contains amd64 manifest `5287b7865ae59f327d1705f4ffbed0169a769c820e75afe0eb1c5468f868f4bf` and arm64 manifest `f091359f651e26ecb5108b931397ff09aede824093692c2992b07343b826a446`. Other entries with platform `unknown` are registry attestation manifests, not runtime architectures.

The upstream source definitions for the verified runtime images are [Node revision 81f4191](https://github.com/nodejs/docker-node/tree/81f419144a1251854c6d9afb09eaa39928e724e8/22/bookworm-slim) and [Python revision 2a3b794](https://github.com/docker-library/python/tree/2a3b794c223ab067d122719541cdd54a068732a5/3.12/slim-bookworm).

## System packages

The slim Node builder obtains its system CA bundle before its first HTTPS APT update by copying `/etc/ssl/certs/ca-certificates.crt` from the untouched `go-base` stage. That stage uses the existing pinned official Go image above, whose amd64 and arm64 CA-install layers were checked to contain the same valid PEM bundle. The Go compiler stage derives separately from that base, so certificate bootstrap does not depend on Go builds or downloaded modules. No additional image or package input is introduced; HTTPS certificate verification remains enabled.

`build/debian.sources` fixes both Debian and Debian Security to `20261016T000000Z` snapshots. The following signed repository suites were checked to exist and include both amd64 and arm64:

- [bookworm](https://snapshot.debian.org/archive/debian/20261016T000000Z/dists/bookworm/Release)
- [bookworm-updates](https://snapshot.debian.org/archive/debian/20261016T000000Z/dists/bookworm-updates/Release)
- [bookworm-security](https://snapshot.debian.org/archive/debian-security/20261016T000000Z/dists/bookworm-security/Release)

APT keeps signature verification against the Debian archive keyring and verifies package hashes. `Check-Valid-Until: no` is restricted to these explicitly dated snapshots because an immutable archived Release file eventually expires. There is no `trusted=yes`, unsigned repository, remote shell setup script, or runtime package installation.

The snapshot makes dependency selection stable and must be advanced deliberately when incorporating security fixes. Pinning is not a vulnerability-free guarantee. The inputs are fixed; byte-for-byte identical complete images have not been demonstrated, and build timestamps/metadata can still vary.

## Application dependencies

### Official Obsidian CLI

`build/obsidian/package.json` selects exactly `obsidian-headless@0.0.14`. Its npm v3 lockfile includes 40 package entries, each with an exact version, an HTTPS npm registry tarball, and a SHA512 integrity value. The official package's direct dependencies are `commander@14.0.3` and `better-sqlite3@12.11.1`. The CLI tarball integrity from [official npm metadata](https://registry.npmjs.org/obsidian-headless/0.0.14) is:

```text
sha512-S1d/hxLKvCUG2g5tRyXFkzPqMs3Ntw1tDyzoF2yfHGRuB4B+Mi3X2vgT8LbfQKrkEEi3LfJRdXtYzAVHcbpccw==
```

The builder first runs `npm ci --ignore-scripts`. It then explicitly compiles the required SQLite extension with `npm rebuild better-sqlite3 --offline`, using the headers in the pinned Node image. This avoids fetching a native binary outside the npm integrity lock. `npm_config_build_from_source=true` directs the locked install script to its source-build path; no other dependency lifecycle scripts run. The builder verifies an in-memory SQLite query and `ob --version` without configuring an account. npm is not copied into the final image.

The Obsidian package is unchanged. Its own published metadata states `license: UNLICENSED`; the BSD license of this fork does not replace the vendor's terms. This repository does not claim redistribution rights for a public prebuilt image. Image publication remains disabled pending a separate review of the applicable vendor terms and release acceptance.

### Python reader

`requirements.lock` pins all 29 runtime packages and their SHA256 hashes, including official `mcp==1.30.0`, `starlette==1.3.1`, and `pydantic-settings==2.14.2`. Installation uses `--require-hashes --only-binary=:all:` against PyPI, then `pip check`. Missing wheels or mismatched hashes stop the build.

The first dependency advisory pass identified known issues in the earlier Starlette 0.52.1 and pydantic-settings 2.12.0 selections. The versions above incorporate the published fixes, including [Starlette GHSA-82w8-qh3p-5jfq](https://github.com/Kludex/starlette/security/advisories/GHSA-82w8-qh3p-5jfq) and [pydantic-settings GHSA-4xgf-cpjx-pc3j](https://github.com/pydantic/pydantic-settings/security/advisories/GHSA-4xgf-cpjx-pc3j). The entire reader SDK/authentication/HTTP/TLS suite passed on the regenerated lock without changes to the application. Rechecking all 29 exact versions against PyPI's vulnerability metadata found no remaining advisory matches on 2026-10-17. This is a database/version check and does not establish exploitability or the absence of unknown vulnerabilities.

CI additionally uses `.github/requirements-test.lock`, a complete six-package Linux/Python 3.12 test lock for pytest and PyYAML. Including `PyYAML==6.0.3` ensures configuration/schema parity tests run instead of skipping for a missing parser. Test tools are not copied into the image.

### reMarkable tools

`remarkable-sync` is compiled from this repository with Go 1.26.9, read-only module mode, no VCS stamping, and no implicit toolchain download. `rmapi` starts from [ddvk/rmapi commit 0a69a608b22f5c11dff07254a7e30b45f2c041f7](https://github.com/ddvk/rmapi/tree/0a69a608b22f5c11dff07254a7e30b45f2c041f7), the exact commit returned for `v0.0.34` by `git ls-remote`. Module versions and checksums come from that commit's `go.mod` and `go.sum`; `go mod verify` and `go build -mod=readonly` must succeed. Dependencies use the official Go module proxy and checksum database.

This fork applies two reviewed patches, in order, before compilation:

| Patch under `remarkable-sync/patches` | SHA256 |
| --- | --- |
| `rmapi-contained-paths.patch` | `f126dbd371f7f31a73d2eec95d5c1cc4df47d403123ac198a5c9a0e0712a1212` |
| `rmapi-dependencies.patch` | `81df98d4cdcf43e378bb4e2c46ce597f0b0d63f42e9dae469686da9331ee267c` |

The containment patch rejects unsafe remote filename components and uses descriptor-relative, no-follow directory/file operations for contained downloads. The dependency patch moves JWT to the patched v4 API, handles its claims initialization correctly, and raises selected Go module versions beyond the known fixed-version thresholds. Both patches include behavioral tests. The Dockerfile verifies each hash, runs `git apply --check`, applies each patch, and runs the patched rmapi tests and vet checks before compilation. The modified binary reports `v0.0.34-hardened.2`; both patches remain in `/usr/share/licenses/rmapi/` as `local-changes.patch` and `dependency-changes.patch`. It is not an unchanged upstream binary.

The compiled module inventory includes `github.com/golang-jwt/jwt/v4@v4.5.2`, `golang.org/x/image@v0.47.0`, `golang.org/x/sync@v0.24.0`, `golang.org/x/sys@v0.49.0`, and `golang.org/x/text@v0.43.0`. The selected build graph also updates `x/crypto`, `x/net`, and `x/mod`; obsolete goldmark leaves the graph after dependency tidying. Module metadata alone is broader than the shipped program: the updated graph still matches the all-version [OpenPGP advisory GO-2026-5932](https://vuln.go.dev/ID/GO-2026-5932.json), while `golang.org/x/crypto` and its OpenPGP package are absent from the actual compiled rmapi module/import inventory. The final binary and imported code must be assessed separately from unused build-graph entries.

## CI and publication boundaries

`.github/workflows/verify.yaml` runs on pushes, pull requests, and manual dispatch. Its entire permission set is `contents: read`; checkout disables persisted Git credentials. There are no registry logins, package-write permissions, release mutations, external repository checkouts with PATs, or upstream add-on repository writes. The previous five workflows are removed.

Native `ubuntu-24.04` and `ubuntu-24.04-arm` jobs run the Python suite, actual numeric-UID tests on synthetic data, Go race tests/vet/build, and shell syntax checks. Only after both test jobs succeed do native image jobs build the Dockerfile and run offline, unprivileged import/CLI/SQLite smoke checks. The workflow does not test real vendor accounts or a running Supervisor. [GitHub's runner reference](https://docs.github.com/en/actions/reference/runners/github-hosted-runners) documents these native runner labels and their administrative test capability.

Third-party action references are full commit SHAs, resolved from their official repositories on 2026-10-17:

| Action | Commit |
| --- | --- |
| `actions/checkout` v4 | `11d5960a326750d5838078e36cf38b85af677262` |
| `actions/setup-python` v5 | `a26af69be951a213d495a4c3e4e4022e16d87065` |
| `actions/setup-go` v5 | `40f1582b2485089dde7abd97c1529aa768e1baff` |

CI VM images and the Docker daemon supplied by GitHub are service-managed build infrastructure. They are not embedded in the final application image and are not claimed to be frozen by these action pins.

## Validation performed in the implementation environment

There was no Docker, Podman, or Docker daemon in the implementation environment. No complete image build, image start, S6 boot, or Home Assistant installation was claimed there.

Completed checks on 2026-10-17:

- Official OCI digest/platform verification and dated Debian repository availability checks described above.
- Exact npm lock generation, followed by a fresh `npm ci` using official Node 22.23.3, scripts disabled, then successful offline SQLite source compilation, an in-memory SQL query, and CLI version output `0.0.14`. The official Linux x64 Node tarball SHA256 matched `df450af89261115ef9f9e3830c3eeb2cc9213b63c720b1af623cb5dcbe2e02de` from the vendor's HTTPS `SHASUMS256.txt`. This was a host toolchain check, not an image test.
- The pinned upstream rmapi source's module checksums verified and both linux/amd64 and linux/arm64 baseline binaries compiled. After applying both verified patches, module verification and containment tests passed and both target binaries compiled again; the native final binary printed `v0.0.34-hardened.2`. Its actual compiled module inventory excludes `golang.org/x/crypto`; its imported production packages exclude OpenPGP.
- Official `govulncheck v1.9.0` scanned the actual rebuilt amd64 rmapi binary at symbol level, returned exit 0, and emitted no findings. The scanner reported database metadata timestamp `2026-10-08T22:31:09Z`. The separate module-graph OSV check on 2026-10-17 still reports the uncompiled OpenPGP match explained above; these checks are not described as a complete final-image SBOM scan.
- `npm audit --omit=dev --json` on the exact 40-package lock returned exit 0 and zero known advisories in npm's database at the time of the check. This is a dependency metadata check, not proof of runtime safety or a scan of the final operating-system image.
- All 29 updated Python runtime packages and all six test packages downloaded successfully with mandatory hash checking and binary-only selection for Debian-compatible amd64 and arm64 targets. A fresh local Python 3.12 environment installed offline from those verified amd64 wheels; `pip check` passed.
- All 48 reader tests passed with the updated security fixes, including real SDK transport over local HTTP and certificate-verified HTTPS. Two deprecation warnings concern Starlette's test-client HTTPX/AnyIO compatibility; they were not suppressed. Native arm64 wheels were validated, not executed locally.
- Dockerfile syntax parsed with Docker BuildKit's parser. The Docker context allowlist was checked with Moby's actual pattern matcher against required source files and excluded Git data, credentials, local node_modules, caches, and unrelated documents. This checks input selection, not image execution.
- Workflow YAML parsed. Actual native image-build and smoke-test results are recorded in [GitHub Actions](https://github.com/kamikazaaa/hassio-obsidian-headless/actions/workflows/verify.yaml), separately from these local checks. See [validation](validation.md) for the remote verification scope and remaining release gates.

## Updating the build

Update related runtime versions, image digests, and Debian snapshot together in a reviewed change. Resolve OCI digests directly from the official registries and verify returned bytes/platforms. Regenerate the npm lock with package scripts disabled; inspect dependency changes, then rerun the explicit offline native build. Regenerate Python locks with hashes and verify wheel availability for both target architectures. Recheck the pinned rmapi source and any local patches when its version changes.

Run both native CI matrices and a disposable HA instance before a release. Test fresh setup, disabled/enabled services, restart and health status, vendor acquisition, reader authentication, and recovery using synthetic/disposable data. Keep image publishing disabled until all required release gates have concrete evidence.
