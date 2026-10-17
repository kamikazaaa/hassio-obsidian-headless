# Security boundaries and development status

This development profile reduces specific audited risks; it is not certified, formally verified, or guaranteed free of vulnerabilities. No real Home Assistant/cloud/restore acceptance run has yet been completed. The original maintainer does not endorse this fork.

## Assets and trust assumptions

Protected assets are source credentials, vault encryption material, original documents, generated output integrity, unpublished source state and Home Assistant availability. Untrusted inputs include note bytes/filenames, repository contents, remote document names and archives, OCR responses, MCP requests and network responses.

Trusted components include the host kernel, Home Assistant Supervisor, container root/bootstrap, the operator configuring options, and the pinned external client/toolchain code. An HA administrator or host/container root can read `/data` and all credentials. Permissions protect service boundaries; they do not encrypt local plaintext against the host. All services still share one container/kernel and its allocated resources.

The source workers have distinct private UIDs and may read the common publication group. MCP has one bearer credential authorizing all published namespaces; it does not provide per-user, per-folder or tenant-level access controls. Separate instances are required for material that must not share that reader trust boundary.

## Implemented controls

| Audited concern | Structural control |
|---|---|
| Unprotected token/pairing forms | Both web servers removed; HA-managed options and CLI stdin pairing |
| Broad writable share mount | Fixed private `/data` roots; no share/SSL mounts |
| Secret shell interpolation | No configuration `eval`; argument lists, private stdin and service-scoped environments |
| Wrong-vault relinking | Actual identity and pull-only mode verified; populated/mismatched setup fails closed |
| Destructive Git adoption | Disposable bare fetch/export at immutable commits; no reset/merge/push or personal-worktree adoption |
| Incorrect conversion caches / overwrites | Content/config/output hashes, conflict checks, private originals and durable recovery journal |
| Partial data becoming current | Bounded complete-generation copy, fsync sequence, atomic pointer replacement and reader hashes |
| Unbounded historical snapshots | Cooperative reader leases, recent-generation retention, unchanged-copy reuse and storage guards |
| MCP write access / weak global mode | Exactly three read tools; every HTTP route authenticated; TLS, Host/Origin and input limits |
| Root/session credential leakage | Fresh private worker exec after UID/GID/group drop; no Supervisor token export |
| Unsafe private-state path writes | Descriptor-relative no-follow writes through creation, ownership changes, replacement and fsync |
| Unbounded child output/lifetime | Bounded metadata capture, process timeouts/group cleanup and parent-death handling |
| Floating build dependencies | Verified image digests, fixed Debian snapshot inputs, npm/Python integrity locks and immutable patched downloader source |
| Automatic unreviewed publication | Read-only development CI; no upstream changes or automatic public image release |

Publisher and reader reject symlinks, multiply linked regular files, special files, escaping/hidden paths and oversized metadata. SHA-256 validates agreement with the manifest; it is not an authenticity signature against a compromised source producer. A source producer controls its own publication and can publish false content within that boundary.

## Explicit residual risks

- The official Obsidian client and third-party `rmapi`, Python SDK, Git, Node, Go, OS libraries and kernel remain dependencies. Pinning permits review and repeatability but also freezes vulnerabilities until reviewed updates occur.
- The Obsidian client may contact cloud services, store decrypted data and handle encrypted source state internally. This fork does not independently prove its cryptographic implementation or remote protocol semantics.
- reMarkable local retention is not an authoritative current cloud catalog. The provenance labels cloud presence as unverified; retained items may have been deleted remotely. Native pen-stroke OCR is unsupported.
- Disk/free-space/aggregate monitoring is sampled and shares storage with other processes. It can overshoot between scans and does not replace filesystem/cgroup quotas, independent storage, load testing or backups. Resource exhaustion from other services, storage failure and loss of power remain possible.
- A protected reader can still disclose notes to an authorized client. Client-side model/tool policy must resist prompt injection and govern external data transmission. Treat credentials accidentally written in ordinary visible Markdown as published content; filename filtering is not secret scanning.
- Recent snapshots expire. The original private archive has finite limits and stops at capacity; no automatic original-data pruning is enabled. An independent, tested backup is required.
- Atomic rename/fsync gives a local ordering contract. A post-replacement error may leave the new complete generation current. It does not implement an atomic transaction with remote cloud services or protect against a storage device that violates durability assumptions.
- Bearer tokens identify a shared reader capability, not a person. OAuth-only hosted clients, multi-user authorization and application-specific assistant compatibility are not implemented/validated in this first profile.
- HA-specific confinement, certificate lifecycle, real source accounts, ARM execution and backup/restore need the documented acceptance tests. A development test suite alone cannot establish these properties.

## Reporting a finding

Do not put tokens, vault encryption material, private notes or live account data in a public issue. Use GitHub's private vulnerability reporting if the repository owner has enabled it. Otherwise ask the owner for a private reporting route without publishing exploit material or sensitive data. There is no promised response SLA or supported production release yet.

Include the exact commit, affected component, minimal synthetic reproduction, expected and observed behavior, prerequisite trust/permissions and whether last-good publication/original bytes were affected. Distinguish a confirmed defect from a theoretical hardening opportunity. Tests in this repository use synthetic files and do not authorize testing any external deployment.
