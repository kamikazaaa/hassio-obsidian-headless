# Pinned rmapi download path patch

Apply `rmapi-contained-paths.patch` only to ddvk/rmapi commit `0a69a608b22f5c11dff07254a7e30b45f2c041f7` (tag `v0.0.34`). The image build must run `git apply --check` before `git apply` and record this local modification. Patch SHA-256: `f126dbd371f7f31a73d2eec95d5c1cc4df47d403123ac198a5c9a0e0712a1212`.

Verified upstream source:

- `shell/mget.go`: incremental mtime comparison, retained local files unless `-d`, remote names joined into destinations, traversal continues after individual download errors.
- `model/node.go`: `Name` returns the document display name.
- `api/sync15/apictx.go`: creates a temporary ZIP, then copies it into the destination.
- `util/util.go`: previously opened destination with `os.Create`.

The patch validates every remote component before the addon’s `mget` joins or writes it. It rejects empty, absolute, slash/backslash, dot/parent, hidden and control-character names. Only the filetree’s synthetic root slash is excluded. It verifies that the final path is relative to the configured download root and uses descriptor-relative `openat`/`mkdirat` with `O_NOFOLLOW` for local directory creation and destination opening. Existing targets must be regular, single-link files before truncation. Copy writes propagate fsync/close failures. A rejected path emits a generic stderr failure marker, which the addon treats as acquisition failure without logging source names or raw stderr.

This preserves spaces and ordinary Unicode names. Unsupported names fail safely; they are not silently flattened or renamed. The patch targets the Linux addon acquisition path, rather than claiming a general audit of every interactive upstream command. The addon uses only noninteractive `ls` and `mget -i`, and never `-d`.

The patch contains standard-library-only regressions for escaping file/folder names, root/parent/final symlinks, hardlinks, special-file targets, normal nested Unicode names and a substituted final symlink between path preparation and copy. Run from the patched rmapi checkout:

```sh
go test util/contained_download.go util/util.go util/contained_download_test.go
```

The normal rmapi build and broader tests can also be run using its pinned module dependencies. The source patch complements the worker’s resource controls and private nonroot service ownership; it does not create a general sandbox for an arbitrary executable of the same UID.

## Dependency modernization patch

Apply `rmapi-dependencies.patch` **after** the containment patch. SHA-256: `81df98d4cdcf43e378bb4e2c46ce597f0b0d63f42e9dae469686da9331ee267c`. It updates the pinned dependency graph using official Go proxy/checksum services, moves the JWT import to the supported compatible `/v4` API, initializes optional standard claims, and keys two existing struct literals for the required vet gate. It includes malformed-token allocation and token compatibility regressions.

The resulting graph uses JWT `/v4` `v4.5.2`, `x/crypto v0.58.0`, `x/image v0.47.0`, `x/sys v0.49.0`, `x/text v0.43.0`, and `x/sync v0.24.0`. Selected transitive versions include `x/net v0.60.0`, `x/mod v0.41.0`, `x/tools v0.51.0`, and `x/term v0.47.0`. These meet the verified advisory fix floors. The updated module graph legitimately drops goldmark, previously required by old x/tools; no required vulnerable module was hidden with exclusions. The updated x modules require Go 1.26, matching the addon's pinned build toolchain.

The official source-level `govulncheck v1.9.0` scan reports no findings for the patched rmapi source. A broader OSV module-version query still matches `GO-2026-5932` on every x/crypto version because that record concerns its unmaintained OpenPGP subtree. OpenPGP is absent from the imported package graph and produced binary; x/crypto itself is needed only by a UniPDF dependency-test closure for PKCS#12. This package-level distinction is retained explicitly instead of claiming a zero-match module inventory or an exploitable OpenPGP path.

From the fully patched rmapi checkout, run `go mod verify`, `go test ./...`, `go vet ./...`, and builds with `GOOS=linux GOARCH=amd64` and `GOARCH=arm64`. Keep the dependency patch hash and hardened binary version in build provenance alongside the containment patch.
