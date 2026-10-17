# Validation evidence and release gates

## Decision

**Development preview only. Production installation is not approved by this evidence.**

The hardened runtime has been implemented and tested against synthetic inputs. Native image construction, offline runtime smoke checks and a separate [Docker/S6 integration harness](container-integration.md) run in [GitHub CI](https://github.com/kamikazaaa/hassio-obsidian-headless/actions/workflows/verify.yaml); inspect the completed result and retained evidence for the exact commit being considered. A successful Docker/S6 run is limited to the synthetic scenarios documented below. No real Home Assistant Supervisor, Obsidian Sync, reMarkable, OCR or backup/restore acceptance run has been completed. Do not replace an existing production add-on in place.

Evidence date: **2026-10-17**. Reviewed upstream base: `3b856fa68892f3a8e1c4548cdc46b649b87de0e2`. The final integrated Python run below covered implementation commit **`18291bccc20bccbabcff5416453350ba7c75593e`**; this validation document was added afterwards without changing runtime behavior.

## Remote verification scope

The development source is available in [pull request #1](https://github.com/kamikazaaa/hassio-obsidian-headless/pull/1). The initial uploaded Git tree `6b3ee966248fea16723bec6f6d460c5b95ecd748` matched the reviewed local delivery exactly; subsequent build corrections are separate commits in that pull request.

The [initial native CI run](https://github.com/kamikazaaa/hassio-obsidian-headless/actions/runs/39035864431) passed all 124 Python tests without skips, plus Go race tests, vet, build and shell checks, on both amd64 and arm64. The image jobs exposed a missing system CA bundle in the slim Node stage. A copy from the untouched pinned Go base supplies HTTPS trust before APT. The [next run](https://github.com/kamikazaaa/hassio-obsidian-headless/actions/runs/39036549857) again passed both test jobs and exposed a required OCR package omitted from the Docker build-context allowlist. The allowlist now includes that package's Go sources. The actual image-build outcome for subsequent commits is recorded in their CI runs; the earlier failures are retained as evidence.

At commit `6fd1f0aa40daa877e12b2f0f9a54334883f1a2c1`, [PR CI run 39037037448](https://github.com/kamikazaaa/hassio-obsidian-headless/actions/runs/39037037448) completed successfully: both native Python/Go jobs, both complete image builds and both offline runtime smoke checks passed. Each architecture ran all 124 Python tests with no skips. A [parallel push run](https://github.com/kamikazaaa/hassio-obsidian-headless/actions/runs/39037034476) exposed a sporadic test-observer failure when procfs returned `ESRCH` during a process-status read. The test helpers now handle both process-disappearance errors explicitly, retaining live/zombie checks and propagating unrelated errors. The same race was removed from four runtime tests. This observer-only correction passed the complete local suite again (124 passed, no skips, two warnings, 8.11 s) and deterministic read-error injection; it changes no production code or image build input. Refer to subsequent commit checks for the final CI status.

The original image smoke checks run with no network, override the image entrypoint, and use service UIDs 10001, 10003 and 10004 for Python imports, CLI version commands and an in-memory SQLite query. Runs through reviewed head `b5601e8aefae1834e15c6f68686d6070adafdce3` therefore did not establish `/init`/S6 startup. Their successful results remain valid within that narrower scope.

## Docker/S6 integration gate

The image jobs now additionally invoke `tests/container/run.py` against the
same locally built image on native AMD64 and ARM64 runners. The tested
containers retain `/init` and the original S6 service definitions. They use
fresh synthetic `/data` volumes, no external network and no published ports.
Cloud CLI adapters are test-only files excluded from the production build;
Git, reMarkable conversion, publication and authenticated TLS/MCP run through
their installed implementations. See [the reproducible commands, fixture
boundaries and lifecycle expectations](container-integration.md).

The integration gate separately checks S6 process state and application
readiness, distinct service identities and filesystem/credential boundaries,
unavailable sources, preservation of complete prior generations, retries,
S6 restarts and controlled shutdown. Each architecture retains `evidence.json`,
container logs and CI provenance. A failed assertion fails the image job;
missing Docker/native execution is not recorded as a pass.

The first invocation at `d57589ac6cbc8a8704c16bda48ba8d388d77728e`
([PR run](https://github.com/kamikazaaa/hassio-obsidian-headless/actions/runs/39069488701),
[push run](https://github.com/kamikazaaa/hassio-obsidian-headless/actions/runs/39069486408))
exposed a fixture-provisioning error on both architectures: S6 started all four
services, but the copied options retained the host runner's ownership and the
runtime correctly rejected them. The harness now supplies explicit root-owned
archive entries before startup. The production options ownership check and
service definitions remain unchanged. This failed attempt is not a Docker/S6
pass.

At source commit `6f7bd61b23aaa96c5fc5352336d4430f47065c6a`,
[PR run 39070445191](https://github.com/kamikazaaa/hassio-obsidian-headless/actions/runs/39070445191)
and [push run 39070442042](https://github.com/kamikazaaa/hassio-obsidian-headless/actions/runs/39070442042)
each passed all four native matrix jobs, including the actual `/init`/S6
integration on AMD64 and ARM64. Each Python job reported 124 passed. The PR
checkout was merge commit `99d3c7212ef30b63b1a76f2e3100c482687bbba5`.
The downloaded PR artifact archives `12115262154` and `12114732818` matched
their recorded SHA-256 digests. Their shutdown records all reported container
exit status `0`, although the assertion then only rejected status `137`.
Those scenarios did not start a stopped container again on its populated data.

The follow-up requires exit status `0` and current-boot S6 stop logs. It also
adds a normal stop/start of the optional-disabled container through `/init`,
reusing the same image and named `/data` volume without recopying fixtures.
The second boot must show a fresh successful Obsidian acquisition even if its
PID is reused, correct disabled-service states, unchanged protected file
hashes/ownership/modes and a fresh Docker healthcheck, then stop successfully
again. These assertions require a completed CI run and matching artifacts for
the follow-up head; the prior passes do not establish this retained-data
stop/start result. An all-enabled container
restart, forced container kill, image replacement/update and
Supervisor/device/power-loss recovery remain outside this added scenario.

Only an actually completed integration step with its matching evidence closes
the synthetic Docker/S6 gate for that commit and platform. Earlier passing
unit tests or entrypoint-override smoke checks cannot substitute for it.
Even a complete Docker/S6 pass does not establish Supervisor integration,
real account synchronization, final operating-system vulnerability status,
graceful completion of every interrupted child operation, storage power-loss
durability or independent restore behavior.

## Executed local checks

| Check | Observed result | What the result establishes |
| --- | --- | --- |
| Combined Python suite using the final hash-locked dependencies | **124 passed, 0 skipped, 2 warnings**, 7.78 s | Local behavioral and integration regressions, including actual service UIDs, subprocess lifetime and real loopback TLS |
| Runtime/configuration | 29 tests within that total | Strict option schema, source identity, pull-only verification, scoped secrets, private writes, health/recovery and child cleanup |
| MCP reader | 48 tests within that total | Official SDK transport, all-route bearer authentication, Host/Origin checks, TLS, read/search bounds, path and manifest checks, generation leases |
| Publication | 24 tests within that total | Complete generation/pointer behavior, hash checking, unsafe-file rejection, retention, reader leases and controlled I/O/recovery failures |
| Git | 18 tests within that total | Disposable acquisition, immutable revisions, namespace validation, resource limits, command-group cleanup and interruption behavior |
| Acquisition storage | 3 tests within that total | Child file limit, aggregate completed-write rejection and low-space handling |
| Publisher/reader integration | 2 tests within that total | Real data/provenance contract; actual UID 10001 writes and UID 10004 reads while private reads/publication writes are denied |
| Go converter and support packages | `go test -race -count=1 ./...` and `go vet ./...` passed | Conversion/recovery, download supervision and OCR fixture behavior without a detected test-time data race |
| Go converter builds | Linux amd64 and arm64 builds passed | Both target binaries compile; this is not native ARM execution |
| Patched rmapi | Both patches applied; module verification, complete tests and vet passed; both target builds passed | Reviewed source modifications integrate with the pinned downloader; native binary reports `v0.0.34-hardened.2` |
| Service scripts | Eight scripts pass shell parsing; 28 permanent/normal-stop cases exit promptly as expected | Shell syntax and exit handling; no complete S6 boot was performed |
| npm runtime | Fresh scripts-disabled locked install, offline SQLite source compilation, SQL query and `ob --version` passed on Node 22.23.3 | Official CLI 0.0.14 and native SQLite load on the tested host toolchain |
| Python installation | All 29 runtime and six test distributions hash-verified for amd64 and arm64; fresh offline amd64 install and `pip check` passed | Available compatible locked wheels and coherent installed Python dependencies |
| Docker inputs | Docker BuildKit parser accepted the Dockerfile; Moby matcher passed required/excluded context cases | Syntax and selected build inputs, not successful image construction |

The Python command was run in an environment permitting real Linux service identities, a matching PID namespace and loopback sockets:

```sh
python3 -m pytest -q -rs tests
```

The actual test interpreter was Python 3.12 with packages installed from `requirements.lock` and `.github/requirements-test.lock`. The Docker runtime pins Python 3.12.15. The two unsuppressed warnings concern Starlette TestClient's deprecated HTTPX compatibility and the AnyIO BlockingPortal alias. They do not represent failed assertions. They should be addressed when updating the test transport, with compatibility tests retained.

The actual UID integration fixture copies the package into a readable, root-owned temporary installation. This is necessary because the development checkout may be below a private host directory. It exercises imports first reached after dropping privilege as well as repeated publication and retention. The container installs the same package on the standard Python `site-packages` path; CI additionally contains a non-root import smoke check from a different working directory.

## Findings resolved during review

The work included focused independent code reviews, followed by targeted regression checks. This is not an independent certification or an exhaustive audit of the operating system and every dependency.

Concrete issues reproduced and corrected during implementation included:

- Root-created service status could prevent a newly enabled worker from reading/writing its state. Fixed service ownership and tightly constrained descriptor-based recovery.
- Private status/TLS writes could be redirected between pathname-based operations. All components and subsequent mutations now remain anchored to open directory descriptors.
- Metadata capture, child lifetime and acquisition storage needed effective bounds during execution. Added file/output limits, aggregate monitoring, timeouts and process supervision; sampled monitoring is still not a disk quota.
- reMarkable PDF-to-notebook conversion could leave stale output, and a recovery deletion lacked a required directory sync. Added owned-output retirement, durable recovery and controlled-failure regressions.
- The pinned downloader accepted unsafe remote path components. The local source patch contains remote downloads and rejects links/special files before truncation.
- Retired publication cleanup after a crash could ignore an existing reader lease. Recovery now obtains the required exclusive lease and retains/counts busy generations.
- Unchanged publications could be reused despite a broken lease, timestamp/schema or access mode. Reuse now checks the reader contract and existing access permissions; invalid control state fails explicitly without replacing the current pointer.
- A rejected generations-directory mode leaked a file descriptor per retry. The validation now runs inside the descriptor cleanup scope; a repeated-rejection regression verifies stable descriptor count.
- The intended 30-second retry delay exceeded S6's default five-second finish timeout. Each service now supplies `timeout-finish` of 35000 ms and uses `exec sleep 30`; ordinary termination and permanent failures return promptly. See the [official S6 process/finish contract](https://skarnet.org/software/s6/s6-supervise.html). The Docker/S6 gate now exercises this policy in the installed container; consult its actual run evidence for the tested commit.

## Dependency advisory evidence

Build provenance, immutable inputs and patch digests are recorded in [build-provenance.md](build-provenance.md).

- The exact npm production lock contained 40 packages. `npm audit --omit=dev` returned zero known advisory matches at the time of the check.
- Earlier Python selections had known advisories. Starlette was updated to 1.3.1 and pydantic-settings to 2.14.2, their locks were regenerated, and the SDK/HTTP/TLS suite passed. A fresh lookup of all 29 exact runtime versions in PyPI vulnerability metadata returned zero remaining matches.
- Patched rmapi uses the updated JWT v4 API and reviewed Go dependency versions. Official `govulncheck` 1.9.0 scanned the rebuilt amd64 binary at symbol level and returned exit 0 with no findings. Its reported database metadata timestamp was **2026-10-08T22:31:09Z**.
- The broader selected Go module graph still matches **GO-2026-5932**, an all-version OpenPGP advisory. OpenPGP and the entire `golang.org/x/crypto` module are absent from the actual compiled rmapi inventory. This exception is documented as an uncompiled graph match, not silently described as a clean complete dependency graph.

These are dated checks against particular databases and artifacts. They do not cover unknown vulnerabilities, a final operating-system image/SBOM, all possible inputs, or future security updates. Locked versions require active maintenance.

## Required acceptance before production

1. **Build and run both images.** Require successful native AMD64 and ARM64 CI matrices including the actual `/init`/S6 integration step and retained per-architecture evidence. Inspect the resulting complete image and operating-system package inventory. The synthetic container test covers its recorded startup, health and restart scenarios; it does not replace the following environment-specific acceptance.
2. **Use a disposable Home Assistant instance.** Confirm Supervisor option provisioning, protection/confinement, UID access, port mapping, TLS certificate loading/renewal, disabled-to-enabled transitions, normal shutdown and restart. Verify the new slug does not replace an existing instance.
3. **Use disposable source accounts/data first.** Test fresh Obsidian setup and a repeated pull-only sync; reject a wrong vault selector and a changed identity. Verify exactly which downloaded files and deletions become published. Test Git authentication without exposing a token. Test reMarkable pairing, mixed document types, manual-output conflicts, retained local/cloud-deleted items and supported OCR behavior.
4. **Exercise recoverability.** Stop processes during acquisition, conversion, pointer replacement and cleanup. Test controlled disk-full/low-space conditions on the intended storage. After every case, verify original bytes, the current complete publication, hash agreement, health and recovery. A local fault-injection test does not prove power-loss behavior on the real filesystem/device.
5. **Restore an independent backup.** Restore private originals/configuration and publications to a fresh instance; verify source identity and access boundaries before restarting acquisition. Recent generations and finite original archives are not a backup policy.
6. **Validate the actual reader client and workload.** Check bearer/TLS compatibility, rejected unauthenticated/wrong-host requests, realistic CPU/memory/storage use, document freshness and client behavior around untrusted note content. An OAuth-only hosted client is not supported by this profile. Use separate instances for different confidentiality domains; the shared token exposes all namespaces in one instance.

Only after these gates have recorded evidence should a release decision be revisited. Keep automatic binary publication disabled until release and vendor redistribution requirements are assessed. No test result justifies describing the add-on as “perfect” or free from all security/data-integrity risk.
