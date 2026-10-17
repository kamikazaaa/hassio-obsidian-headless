# reMarkable acquisition and conversion

This Linux worker mirrors `.rmdoc` bundles with `rmapi` and converts them into a dedicated, generated Markdown/PDF tree. The hardened runtime performs a successful one-shot acquisition before publishing a separate immutable reader snapshot. It does not write into the live Obsidian vault.

## Managed configuration

The runtime supplies these environment variables to the reMarkable service only:

| Variable | Default / behavior |
| --- | --- |
| `VAULT_PATH` | `/data/remarkable/generated`; dedicated generated root |
| `REMARKABLE_OUTPUT_DIR` | `documents`; relative subdirectory; absolute, escaping and hidden paths rejected |
| `REMARKABLE_CACHE_DIR` | `/data/remarkable/private`; private mirror, state, recovery journal and worker lock |
| `RMAPI_CONFIG` | Runtime-managed private rmapi credential file; regular file with no group/other permissions |
| `SYNC_ONCE` | `true` forces one acquisition and process exit, including when `-continuous` is supplied |
| `REMARKABLE_SYNC_ENABLED` | `false` exits without starting listeners or acquisition |
| `REMARKABLE_TAG_PREFIX`, `REMARKABLE_EXTRA_TAGS` | Label prefix and comma-separated extra tags |
| `HA_OCR_URL`, `HA_OCR_TOKEN`, `HA_OCR_ENTITY` | Optional OCR for PNG/JPEG page images already embedded in a bundle |
| `REMARKABLE_OCR_POLICY` | `required` by default; `optional` explicitly permits incomplete OCR notes, visibly marked and retried on every acquisition |

Pairing and Obsidian credentials are managed through addon options. Both custom registration/authentication HTTP servers and their packages have been removed. The old server port environment variables and CLI flags are unsupported; obsolete configured ports cause an error. OCR tokens are accepted through the environment only and never printed in flag defaults. Native reMarkable pen-stroke rendering/transcription is unsupported; configuring an image OCR endpoint does not enable it.

## Integrity and recovery

Conversion cache keys include source SHA-256, conversion version, effective tag settings, OCR policy and a hash of the effective OCR settings. Every generated output's SHA-256 is validated before a cache hit or replacement. Missing outputs regenerate. Changed source bytes with unchanged mtime and configuration changes regenerate. An edited or unowned generated note/PDF is a conflict; the worker preserves it and exits unsuccessfully. Store personal annotations outside this generated tree. Unmanaged generated files also block publication rather than being incorporated into imported content.

The collection index is `documents/_remarkable_collection/index.md`, separate from documents such as `documents/index.md`. A source path occupying the reserved `_remarkable_collection` namespace is rejected. The index regenerates when missing, including a cache-only run. Tag values are quoted in YAML frontmatter.

A run plans all conversions and the collection index before changing outputs. Every replacement is staged and fsynced privately. A bounded private transaction journal records permitted output/state paths, old backups and old/new hashes before any target mutation. Each target uses an atomic rename followed by directory fsync. State is committed after generated files. An interruption before the durable commit marker rolls back on the next start; an interruption after it finishes cleanup. Recovery validates every target/backup first, so unexpected manual edits are preserved as an explicit conflict. Another interruption during recovery is safe to retry. A kernel file lock prevents overlapping workers from cleaning each other's active transaction.

Recovery fsyncs final files and their existing parent directories before removing the journal, including targets already restored by an earlier failed recovery. A postcommit cleanup error can return a nonzero exit with a completed new private candidate; startup finishes cleanup, while the runtime retains the prior published snapshot until a successful acquisition.

Original downloaded bundles are retained in a bounded private content-addressed archive in addition to rmapi’s mutable mirror. The archive is checked before and after acquisition, uses read-only files, and its directory is read-only while the nonroot downloader runs. Only generated Markdown/PDF and collection provenance files are eligible for publication. Conversion, required OCR, PDF, index, read, write, fsync and recovery failures propagate to a nonzero one-shot exit. The runtime must publish only after success. No partial required failure becomes a completed cache entry.

## Resource and filesystem policy

| Resource | Limit |
| --- | --- |
| One compressed bundle / one mirrored regular file | 64 MiB |
| Expanded bytes per ZIP | 128 MiB |
| ZIP entries | 2,048 |
| One `.content` or `.metadata` JSON | 1 MiB |
| One PDF | 64 MiB |
| One embedded image | 16 MiB |
| Embedded images per bundle | 256 |
| OCR response | 4 MiB |
| OCR text per document | 8 MiB |
| Mirrored entries, including directories | 20,000 |
| Private original bundle archive | 512 MiB / 20,000 distinct bundle hashes |
| Active plus locally retired source records | 10,000 |
| Acquisition workspace monitor threshold | 2 GiB / 65,536 entries |
| Free-space reserve during acquisition | 256 MiB |
| Acquisition monitoring interval | 25 ms plus directory traversal and scheduling latency |
| Converted documents per run | 10,000 |
| Total mirror, planned generated bytes, old journal backups | 512 MiB each |
| Journal entries / manifest | 30,002 / 16 MiB |

Reject ZIP traversal, absolute/backslash/hidden paths, duplicate or ambiguous metadata/PDF entries, symlinks, hardlinks, special files, malformed JSON and missing required embedded PDFs. Mirror checks run before invoking rmapi and after acquisition. File reads open every Linux path component with `O_NOFOLLOW`; bounded reads and ZIP CRC validation detect malformed data. Generated, mirror and state trees must be distinct and nonoverlapping. Private directories use `0700`, private files `0600`, retained original bundles `0400`, generated files `0640`. The original archive directory uses `0500` while the downloader runs. Publication permissions and reader group assignment belong to the runtime publisher.

The generated and private state trees must be on the same filesystem for atomic rename from private staging. This is satisfied by the managed `/data/remarkable` layout. A filesystem that cannot provide file/directory fsync is an acquisition error.

## Deliberate limits

The pinned downloader is ddvk/rmapi `v0.0.34`, commit `0a69a608b22f5c11dff07254a7e30b45f2c041f7`, with the checked containment and dependency patches documented in `patches/README.md`. The containment patch rejects unsafe remote file/folder components and opens download directories/files without following links before writing. Names containing slash, backslash, control characters, or a leading dot are unsupported; ordinary Unicode and spaces are preserved. These Linux addon patches are applied and checked against the exact pinned source during the image build. The dependency patch moves JWT parsing to the supported compatible v4 API and updates the affected Go module graph; its advisory reachability evidence is documented with the patches.

The acquisition command is deliberately incremental and never uses rmapi’s destructive `-d` option. Its successful completion does not establish current cloud membership or complete cloud deletion reconciliation. Every note and index explicitly uses **retained local mirror** semantics and marks cloud presence **unverified**; `_remarkable_collection/provenance.json` records active local bundle hashes and local disappearance tombstones. A source bundle removed from the private mirror retires only its unmodified, previously owned generated files in the same durable transaction. Original archive bytes, private tombstones and old published generations are retained. Reappearing bundles reactivate safely. Annotated generated files conflict and are preserved. Source-record or archive retention exhaustion stops acquisition until an administrator deliberately archives material; there is no automatic history deletion.

The downloader child has a hard 64 MiB per-file `RLIMIT_FSIZE`, a private HOME/TMPDIR/cache, a `0077` umask and its own process group. Its environment excludes the OCR token and unrelated service secrets. Timeout or a budget failure kills the whole group. Linux parent-death signaling also terminates the actual rmapi process if the converter dies abruptly; the launcher keeps its creating OS thread alive for that child’s lifetime. The ten-minute timeout also has an independent cancellation callback, so a slow directory scan does not leave the downloader running past cancellation. Before launch, free space must cover the remaining 2 GiB workspace budget plus a 256 MiB reserve. During execution and after exit, the worker checks workspace bytes/count and the free-space reserve.

The **2 GiB cumulative threshold is sampled, not a filesystem quota**. Writes between checks, directory traversal, scheduler delays and unrelated processes can consume additional space; no strict aggregate byte overshoot or guaranteed global free-space floor is claimed. The hard per-file limit still applies between checks. Network traffic and CPU are bounded by the process timeout rather than a strict transferred-byte quota. A dedicated filesystem quota would be required for a strict total storage ceiling. Failed downloads can leave private temporary files, which remain included in the next acquisition’s budget and may require administrative cleanup. No real account or device was used for validation.

An unknown state schema, corrupted recovery backup, manually modified generated file, or unrelated material in a journal is preserved as a conflict requiring the administrator to move or repair that material. Legacy mtime stamps are not trusted as ownership evidence. Migrate by preserving old generated content separately and starting with a dedicated empty generated tree; the worker does not adopt existing personal files.

## Local validation

Run `go test ./...`, `go test -race ./...`, `go vet ./...`, and `go build ./...` with Go 1.26. The tests use temporary directories, a no-op rmapi executable, in-memory HTTP transports and subprocess fixtures; they use no accounts or network listeners.
