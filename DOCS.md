# Configuration and operation of the hardened development profile

This document describes the code in this source branch. A production installation is not recommended until the acceptance gates in [validation](docs/validation.md) have been completed.

## 1. Prepare an isolated pilot

Use a disposable remote Obsidian vault and a new local add-on instance. The slug is `obsidian_headless_hardened`; data lives inside that instance's `/data`. Do not point this version at an existing shared personal worktree. All long-lived source processes run under different UIDs; managed options are read by root and sent only to the intended worker through a fresh private process environment.

An Obsidian Sync account and configured remote vault are required by this first profile even if optional Git/reMarkable features are enabled. A standalone Git-only mode is not implemented. Use separate instances/accounts for sources that should not be readable by the same MCP bearer-token holder. The token grants reading across every enabled publication namespace; it does not provide individual user roles.

Complete the OS/image tests first, then start with MCP, Git and reMarkable disabled. Keep an independent, recoverable backup outside the add-on and its host. A restored add-on backup can contain account tokens, derived plaintext and encryption material; protect it accordingly.

## 2. Obtain the Obsidian token on a trusted computer

Use the official pinned CLI on your own trusted workstation with Node.js 22 or later:

```bash
npm install --global obsidian-headless@0.0.14
ob login
ob sync-list-remote
```

`ob login` prompts for account credentials and MFA when required. Use the interactive prompt rather than supplying a password in shell arguments. Version 0.0.14 does **not** offer the old documentation's `get-token` command.

After login, the official client stores its token in a private file:

| Workstation platform | Token file used by CLI 0.0.14 |
|---|---|
| Linux | `${XDG_CONFIG_HOME:-$HOME/.config}/obsidian-headless/auth_token` |
| macOS / Windows | `.obsidian-headless/auth_token` inside the user's home directory |

Open that file privately on the trusted computer and copy its value into the add-on's `obsidian_auth_token` option through your protected Home Assistant session. Do not paste the token into issues, chat, shell history or a public configuration example. This token-file guidance is based on the pinned CLI implementation, not a stable external token-export API. A future CLI update must recheck it.

Set `vault_name` to the exact name or unique ID returned by `ob sync-list-remote`. Set `vault_password` to the vault encryption password if needed; it is not the account password. Passwords ending in whitespace are rejected because the official noninteractive stdin prompt trims trailing whitespace. The add-on refuses to silently alter such a password.

During first setup the worker requires empty managed vault/configuration state, verifies the actual remote vault identity, sets and verifies `pull-only`, writes its identity binding, then runs one acquisition. A later change of vault selector, unexpected identity, or incomplete first setup stops the worker with fresh-instance guidance. The worker never automatically rebinds a populated vault.

## 3. Option reference

The complete authoritative field/type list is [config.yaml](config.yaml), mirrored by [the strict parser](hardened/config.py). Unknown legacy options fail closed.

| Option | Default / rule |
|---|---|
| `obsidian_auth_token` | Required private token |
| `vault_name` | Required exact remote name or ID |
| `vault_password` | Empty; provide when required by the remote vault |
| `obsidian_sync_interval` | 300 seconds; 30–86400 |
| `enable_git_sync` | `false` |
| `git_repos` | Multiline namespace/HTTPS repository/branch declarations |
| `git_auth_token` | Optional GitHub token; only sent to `https://github.com` on standard port |
| `git_sync_interval` | 900 seconds; 30–86400 |
| `enable_remarkable` | `false` |
| `remarkable_pairing_code` | One-time pairing code for a new instance |
| `remarkable_sync_interval` | 300 seconds; 30–86400 |
| `remarkable_tag_prefix`, `remarkable_extra_tags` | Optional generated-note tag configuration |
| `ha_ocr_url` | Empty disables OCR; if set, must be HTTPS |
| `ha_ocr_token`, `ha_ocr_entity` | Optional settings for the configured OCR service |
| `enable_mcp` | `false` |
| `mcp_auth_token` | Required with MCP; 32–512 bearer-safe characters; generate randomness, do not invent a memorable token |
| `mcp_tls_cert`, `mcp_tls_key` | PEM certificate chain and matching private key, required with MCP |
| `mcp_allowed_hosts` | Comma-separated exact host or host:port values; required with MCP |
| `mcp_allowed_origins` | Empty denies browser Origin headers; otherwise exact HTTPS origins |

### Git sources

```yaml
enable_git_sync: true
git_repos: |
  SmartHome|https://github.com/example/home-docs.git|main
  SharedProject|https://github.com/example/project-docs.git|main
git_auth_token: ""
```

The entries are examples, not repositories supplied by this project. Namespaces cannot overlap or collide after case/Unicode normalization. HTTPS credentials embedded in URLs, query strings, local file transports and unsafe branch/path syntax are rejected. A different remote or branch creates a different source identity.

Every cycle fetches fresh bare repositories, resolves commit IDs and exports their trees without hooks, submodules or LFS execution. `_source.md` inside each namespace records the exact remote, branch, identity and commit. That filename is reserved; collisions fail rather than overwrite a source note. All configured repositories must finish before a new aggregate Git publication becomes current. External personal worktrees are never used as mirrors or reset.

### reMarkable

Obtain the one-time device pairing code through the official reMarkable account flow on your own trusted computer, configure it in `remarkable_pairing_code`, enable the service and restart. The code is passed through stdin. After successful pairing the service reuses private credentials and will not automatically replace them if later authentication fails. Remove the expired pairing code from options after confirming success.

The pinned `rmapi` downloader is patched to reject unsafe remote names and confine writes to its managed download tree. Generated output and collection indexes have separate reserved locations. Conversion uses source/config/output hashes; missing outputs are regenerated, changed human annotations produce a conflict, and a durable journal can complete rollback after a process interruption. Keep manual edits in a separate human workspace, not generated files.

The download strategy preserves a **retained local mirror**. A successful command is not proof of complete current cloud membership; a remote-deleted document may remain as a retained local document. Notes/index/provenance mark cloud presence as unverified. Disappearance of a local owned bundle retires only unmodified owned generated outputs; originals/tombstones remain private and reappearance is handled. Read [the converter contract](remarkable-sync/README.md) for the exact lifecycle and limits.

OCR is limited to supported embedded raster images. Native reMarkable pen-stroke recognition is not implemented. If OCR is configured and required conversion fails, the acquisition is not marked complete. Configured OCR sends supported images to that HTTPS service; choose an endpoint whose data handling is appropriate for the material.

### MCP over TLS

Generate a separate reader secret on a trusted workstation:

```bash
openssl rand -hex 32
```

Configure `mcp_auth_token`, a valid PEM certificate/key pair, and `mcp_allowed_hosts` such as `notes.example.net:8420`. Set `mcp_allowed_origins` only if a known browser client actually requires it. An ordinary backend client may omit `Origin`. Supply a certificate trusted by the client and covering the actual hostname; do not disable client certificate validation. The certificate/key pair is validated before being written to private managed storage.

After the service is healthy, explicitly map container port 8420 in the add-on's Network configuration. The endpoint is `https://YOUR_HOST:8420/mcp`. Clients send `Authorization: Bearer YOUR_PRIVATE_TOKEN`. This instance neither opens ports 8421/8422 nor starts its own tunnel. A separately managed network route must preserve TLS/authentication and the configured Host policy. Proxies must verify the upstream certificate; forwarded headers are not trusted.

The service offers exactly three tools, each with `source` equal to `obsidian`, `git` or `remarkable`:

- `list_documents`: paginated paths, hashes and provenance.
- `read_document`: bounded UTF-8 text and provenance.
- `search_documents`: bounded literal text search with cursors, truncation/scope information and skipped attachment reasons.

There are no write, upload, shell, delete or credential tools. Attachments may be listed but text reads are limited to 1 MiB and valid UTF-8. Search examines at most 100 files / 8 MiB per call; an incomplete search must not be interpreted as proof that a fact is absent. Caller pagination across separate operations may see different generations after an update; each individual operation pins one generation. An expired/contended generation returns an error requiring a fresh request.

Markdown is untrusted data. The server returns it as text without executing it; a consuming AI must still treat note content as evidence rather than instructions. Read-only access does not prevent a client from sending the returned text to an external model.

## 4. Storage and health

| Area | Enforced or monitored limit |
|---|---|
| Published file | 32 MiB |
| Published generation | 512 MiB content; 10000 files, bounded paths/depth/entries |
| Publication retention per source | Three recent generations plus active reader leases; at most eight retained generations; 2 GiB logical stored bytes |
| Free-space reserve | Acquisition/publication stops when sampled available space falls below 256 MiB |
| Obsidian private acquisition | 2 GiB / 45000 entries monitored before, during and after commands; child hard per-file cap 32 MiB |
| Git/reMarkable acquisition | Additional byte, entry, archive, response and process limits; see implementation/build validation and reMarkable README |
| Reader request | 32 KiB body, bounded concurrency/time; bounded result sizes |

Free-space/aggregate checks are sampled guards, not storage quotas or a guarantee against competing host writers, filesystem overhead or hardware failure. Retained originals can eventually reach a limit; the service stops and requires planned maintenance instead of silently deleting originals. Start with small source datasets and measure CPU, RAM and disk use on the actual HA device. A separate host/storage volume provides a stronger availability boundary for large projects.

Inside the container, the health command is:

```bash
python3 -m hardened.runtime status
```

Configured services must have a live process and a recent successful acquisition/readiness state. An enabled service with an error or stale data is unhealthy; disabled services are reported separately. Private per-service `status.json` records the last successful acquisition. Reader provenance records the generation's original publication/acquisition time, which can be older when unchanged content was reused. Last-good documents can remain readable after an acquisition error; their continued readability does not imply source freshness.

## 5. Recovery, migration and backup

1. **Stop the add-on before backup/restore or manual maintenance.** Preserve the complete managed `/data`, including each source's credentials, identity/configuration, journals and originals; generated publications alone are insufficient for full recovery.
2. **Verify an independent backup before replacing any instance.** Use a disposable restore target, start with network/MCP disabled, check the expected vault identity and compare fixture hashes before reconnecting production accounts.
3. **For a source identity change, create fresh managed state.** Never edit a binding marker to bypass the guard and never point it at a populated personal vault. Restore the expected identity or provision a separate instance deliberately.
4. **For a conversion conflict, preserve both versions.** Export the annotated generated file into a human workspace and inspect the source/provenance. Do not delete its integrity journal or fake output hashes to suppress the error.
5. **For a failed publication, retain the last-good generation.** Failures before pointer replacement preserve it. A failure after replacement/fsync can leave the new complete generation current; the code does not claim distributed or hardware-level all-or-nothing rollback. Interrupted owned staging/retired data is recovered on a later successful run.
6. **For migration from the original add-on, build a new instance.** Retain an offline backup of the old data and token stores. Do not import old setup markers, shared mounts, OAuth/tunnel options or generated caches as trusted new state. Reacquire a disposable copy first; switch clients only after validation.

The project does not claim that an HA backup has been restored successfully until that test is run on real HA. Do not manually prune `.lease` files or generation directories while readers are active. Never expose `/data` as an unprotected web directory or mount the original broad `/share` into this profile.
