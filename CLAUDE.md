# Development guidance

The hardened source profile replaces the original project's runtime and distribution assumptions. Read `README.md`, `DOCS.md`, `SECURITY.md`, `docs/hardening-plan.md` and `docs/validation.md` before making changes.

Preserve separate source UIDs, fixed private paths, the shared read-only publication contract, strict options and literal command arguments. Never reintroduce the credential web UIs, configuration eval, a broad writable share mount, personal-worktree Git reset, reader mutations or implicit public exposure. Keep complete previous publications and original source data on failed acquisition.

Use meaningful behavioral regression tests for integrity and security changes. Test real filesystem behavior and the official MCP transport where applicable. `pytest` is needed for the complete Python suite; unittest discovery alone does not run the MCP tests. UID and loopback tests must run in a disposable environment that supports the actual permissions. Do not misreport skipped UID/ARM/HA/image tests as passed.

The Dockerfile builds from pinned inputs. Dependency updates require lock/hash/provenance review and renewed tests. CI has no publishing permissions or automatic upstream release. Retain the original BSD license and check each dependency's terms before binary redistribution. Never commit credentials, actual vault material or personal project context.

The source-project root is the local add-on build context. It is not a ready-made add-on-store repository. Follow the validated build/migration gates rather than assuming the original upstream distribution workflow still applies.
