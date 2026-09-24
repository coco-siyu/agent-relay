# Agent Relay

Agent Relay is a small FastAPI service for registering agents, delivering one
task at a time, and recording results. PostgreSQL persists the queue and
attempts in the Compose deployment, while workers execute tasks on their own
machines. The included worker deterministically returns `input.upper()`.

## Run with PostgreSQL and Docker Compose

```bash
docker compose up --build
```

Open <http://127.0.0.1:8000/>. The API connects to the Compose service named
`postgres`; its data is kept in the `postgres_data` volume. Stop the services
with `docker compose down`. Add `-v` only when you intentionally want to delete
the database volume as well.

## Run on a local kind cluster

Build the application image, create a cluster, load the image into its node,
and apply the Kubernetes manifests:

```bash
docker build -t agent-relay:local .
kind create cluster --name agent-relay \
  --image kindest/node:v1.31.9@sha256:b94a3a6c06198d17f59cca8c6f486236fa05e2fb359cbd75dabbfc348a10b211
kind load docker-image agent-relay:local --name agent-relay
kubectl --context kind-agent-relay apply -f k8s/
kubectl --context kind-agent-relay wait \
  --for=condition=available deployment/postgres --timeout=5m
kubectl --context kind-agent-relay wait \
  --for=condition=available deployment/agent-relay --timeout=5m
kubectl --context kind-agent-relay port-forward service/agent-relay 8000:8000
```

Then open <http://127.0.0.1:8000/>. The `postgres-data` persistent volume
claim retains database files across PostgreSQL pod restarts. The credentials in
`k8s/secret.yaml` are local-development defaults and must be replaced before
using these manifests in a shared environment.

## Run it

```bash
uv sync
uv run uvicorn main:app --reload
```

Open <http://127.0.0.1:8000/> for the token-based local dashboard. Direct local
runs retain a `./agent-relay.db` SQLite fallback for fast development and unit
tests; set `RELAY_DATABASE_URL` to use PostgreSQL or another SQLite file.
`GET /health` is a liveness check and `GET /ready` verifies database
connectivity and schema (it queries the real tables, so a wiped volume
reports not-ready instead of passing with zero tables).

Register two identities and send a task:

```bash
alice=$(curl -sS -X POST http://127.0.0.1:8000/api/v1/agents \
  -H 'content-type: application/json' -d '{"name":"alice"}')
bob=$(curl -sS -X POST http://127.0.0.1:8000/api/v1/agents \
  -H 'content-type: application/json' -d '{"name":"uppercase"}')
```

The response contains each agent's secret `token` once. Keep it outside source
control. Use `Authorization: Bearer <token>` for all subsequent API calls;
registration is the only unauthenticated endpoint. For a shared installation,
set `RELAY_ENROLLMENT_SECRET` and send it as `X-Enrollment-Secret` when
registering.

## Run the deterministic worker

The worker can register itself and save credentials in a mode-0600 JSON file:

```bash
uv run python main.py worker \
  --base-url http://127.0.0.1:8000 \
  --name uppercase \
  --credentials ./uppercase-credentials.json \
  --worker-id laptop-1
```

For failure/redelivery demonstrations, make local execution intentionally slow
and stop the process after one completion:

```bash
uv run python main.py worker --credentials ./uppercase-credentials.json \
  --slow-seconds 75 --worker-id slow-laptop
```

The worker heartbeats during long work. Killing it leaves the claim leased;
after the 60-second lease expires, another worker can claim the task with a new
token and incremented attempt number. `RELAY_LEASE_SECONDS` and
`RELAY_MAX_ATTEMPTS` are configurable server settings.

An existing credential can also be supplied explicitly (the token is not
written to disk):

```bash
uv run python main.py worker --agent-id agent_123 --token agt_… --worker-id laptop-2
```

## Storage and delivery behavior

`database.py` contains SQLAlchemy models and database transaction setup.
`storage.py` contains task/claim/recovery operations; routes and request models
are kept in `main.py` and `schemas.py`. PostgreSQL claims use
`FOR UPDATE SKIP LOCKED`, allowing workers to lock different queued tasks
concurrently. The SQLite test fallback uses `BEGIN IMMEDIATE` because SQLite
does not provide row-level locking.

Claims are at-least-once and leased for 60 seconds by default. Heartbeats extend
an active lease. A completion or failure must include the recipient's bearer
token and claim token. Repeating the exact terminal request with that claim
token is idempotent; a stale token or different result receives `409`.

## Verify

The test suite covers the main protocol, sender/recipient access boundaries,
hashed claim-token behavior, idempotent terminal retries, concurrent claims,
lease expiry before and after recovery, pagination/error shape, and dashboard
asset serving:

```bash
uv run pytest -q
```

Tests default to a scratch database at `/tmp/agent-relay-test.db` so they
don't reset your dev server's `./agent-relay.db`. The fixture drops and
recreates all tables on whatever `RELAY_DATABASE_URL` points at, so stop
the dev server first or set `RELAY_DATABASE_URL` to a scratch file before
running tests against another database.

The project intentionally does not include an external message broker or an
LLM. Those are outside the relay protocol.

## Run CI locally with act

The workflow tests the application against a temporary PostgreSQL container,
builds a uniquely tagged image, loads it into the `agent-relay` kind cluster,
and waits for the Kubernetes rollout. Run it from the repository root:

```bash
docker_host="$(docker context inspect desktop-linux --format '{{.Endpoints.docker.Host}}')"
DOCKER_HOST="$docker_host" act push \
  -W .github/workflows/ci.yml \
  -P ubuntu-latest=-self-hosted \
  --pull=false \
  --env ACT_PROJECT_DIR="$PWD" \
  --env DOCKER_HOST
```

The self-hosted act runner intentionally uses the host's Docker daemon,
kind cluster, and kubeconfig, so only run trusted workflow changes this way.
