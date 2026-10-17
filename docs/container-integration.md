# Offline Docker and S6 integration

This check exercises the installed development image through its inherited
`/init` entrypoint. It is a Docker/S6 integration test with synthetic sources,
not a Home Assistant Supervisor or real cloud acceptance test. The release
decision and additional gates remain in [validation.md](validation.md).

## Run it

Use a disposable Linux Docker host with Python 3, OpenSSL, sufficient free disk space,
and permission to create containers and named volumes. Build inputs require
network access. The integration containers themselves use `--network none`;
loopback remains available inside each container. No host port is published.

From a reviewed checkout on an AMD64 host:

```sh
docker build --pull --platform linux/amd64 --build-arg BUILD_ARCH=amd64 \
  --tag obsidian-hardened:test .
python3 tests/container/run.py --image obsidian-hardened:test \
  --artifacts artifacts/container-amd64
```

On a native ARM64 host, build with `--platform linux/arm64` and
`--build-arg BUILD_ARCH=aarch64`, then run the same harness. The Home Assistant
manifest calls this architecture `aarch64`; Docker calls it `arm64`.

The harness requires a usable Docker daemon and fails if prerequisites or
assertions are unmet. Absence of Docker is not a passing or skipped integration
result. The host orchestrator uses the Python standard library; probes inside
the container use the image's already installed, locked dependencies.

## What is real and what is synthetic

The tested container does not override its entrypoint, Python package,
supervision scripts, service identities, managed-option parser, publication
implementation or MCP server. Test helpers and synthetic options are copied
into a newly created container before startup. Archive entries explicitly use
UID/GID 0, so an unprivileged host runner cannot change the required ownership
of the synthetic Supervisor options. Each scenario uses a new named data
volume. The harness removes its containers and volumes after collecting
evidence, including after assertion failures.

Only the external Obsidian and reMarkable CLI responses are substituted by
test-only adapters. The adapter files live under `tests/container`, outside the
production Docker build-context allowlist. No production configuration option
enables these adapters. They are copied into the disposable test container at
the normal CLI locations. The real reMarkable converter, Git client, publisher,
reader and S6 lifecycle remain in use.

Git acquires a small synthetic repository from an HTTPS server on container
loopback. A generated test certificate is trusted only inside the disposable
container; production TLS verification remains enabled. The fixture is not an
external repository or a real account. MCP uses a generated certificate and a
synthetic bearer token, with the official SDK exercising the actual TLS
endpoint. No personal credentials, notes, Supervisor tokens or cloud sessions
are required.

## Lifecycle contracts

The four S6 services are `obsidian-sync`, `vault-git-sync`, `remarkable-sync`
and `obsidian-mcp-server`. Their long-lived application identities are
respectively UID/GID 10001, 10002, 10003 and 10004. Root initializes the private
directories and scopes the managed options before each worker drops its
privileges and executes a fresh interpreter.

S6 process state and application health answer different questions. The
services do not expose an S6 `notification-fd`; successful process creation
does not prove a completed source acquisition or a listening MCP endpoint.
The harness observes S6 state, application status, publication content and an
actual TLS/MCP request separately. It starts with a delayed source and an
unavailable Git fixture to exercise this distinction.

The existing restart policy is deliberately tested at both levels:

| Condition | Expected behavior |
| --- | --- |
| Optional source or MCP disabled | Root records `disabled`; exit 0 and finish exit 125 leave the service down without a restart loop. |
| Invalid configuration or permanent source identity failure | Exit 78 and finish exit 125 require deliberate operator action. A running container alone is not proof of health. |
| Ordinary acquisition/publication failure | The source worker remains alive, records `error`, preserves the previous complete publication and retries after the configured interval, whose minimum is 30 seconds. |
| Unexpected worker/MCP process failure | S6 supervises the worker; unexpected failure uses the existing 30-second finish delay and 35000-millisecond finish timeout. |
| SIGTERM with the service still wanted up | Worker cleanup runs; the finish script permits S6 to restart the service. |
| S6 down request or container stop | Desired state is down; the worker must terminate and remain down while S6 completes shutdown. |

If shutdown begins while an unexpected failure's `finish` script is already
sleeping, S6 waits for that bounded backoff to complete. A stop can therefore
take the remainder of the 30-second delay. The harness uses an explicit Docker
stop budget large enough for this case and records its timing; Docker's shorter
default stop timeout must not be mistaken for that tested budget. Supervisor
acceptance must verify its configured stop budget against this behavior.

There is no producer-to-reader startup dependency: a reader may start before
the first source publication. It must reject an unavailable source without
inventing data and later read a complete generation once acquisition succeeds.
The aggregate health command must remain unhealthy while a required source is
unready or in error, even if MCP itself is reachable.

## Evidence and interpretation

The harness writes `evidence.json` and per-container diagnostics in the selected
artifact directory. Assertions cover the actual entrypoint, service state,
readiness, Linux identities and privileges, scoped credentials, denied private
reads and publication writes, authenticated read-only MCP, source failure and
recovery, restart behavior and controlled termination. Generated data and
retained publications are checked for content and hash integrity.

The existing workflow runs this harness after building and smoke-testing the
same local image on native `ubuntu-24.04` and `ubuntu-24.04-arm` runners. Each
architecture has its own required integration step and retained diagnostics.
An integration failure fails that image job; an unavailable runner or Docker
daemon does not turn into a successful architecture result. The workflow has
`contents: read` permission and does not push images to a registry.

A successful result applies to the commit, image, platform and scenarios
recorded by that run. Inspect the actual completed CI run and its artifacts;
adding the harness or passing the older entrypoint-override smoke tests is not
evidence that the integration test ran successfully.

Shutdown evidence distinguishes the outer Docker stop from child cleanup.
The current runtime intentionally kills outstanding acquisition/reader process
groups during cleanup. A bounded S6 shutdown without Docker's timeout killing
PID 1 therefore does not claim that every child receives a graceful protocol
shutdown or that interrupted cloud operations complete.

## Home Assistant acceptance remains separate

This test supplies `/data/options.json` itself and uses Docker's Linux process
and filesystem isolation. It does not reproduce Supervisor option delivery,
AppArmor/protection mode, Home Assistant networking/port mappings, add-on
install/update/remove behavior, HA backups, a production certificate lifecycle
or the storage characteristics of the intended device.

The synthetic CLI adapters also do not validate actual Obsidian authentication,
encrypted vault negotiation, cloud sync semantics, reMarkable pairing/current
cloud membership or a real OCR service. Those require the separately documented
disposable Supervisor/source-account acceptance. No Docker/S6 pass authorizes a
production installation, image publication or use of personal credentials.
