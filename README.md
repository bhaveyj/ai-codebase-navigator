# AI Codebase Navigator

A local-first code explorer with a React Flow dependency graph, source viewer,
FastAPI backend, asynchronous repository analysis, and Gemini answers grounded in
MongoDB Atlas retrieval. JavaScript and TypeScript are the first analyzer; the
analysis protocol is designed for additional language adapters.

## Required configuration

Copy `.env.example` to `.env` and fill these two values locally. Do not paste
credentials into chat or commit `.env`.

| Variable | Required | Purpose |
| --- | --- | --- |
| `MONGODB_URI` | For full mode | Atlas connection string with a database user and password |
| `GEMINI_API_KEY` | For AI | Google AI Studio Gemini API key |
| `GITHUB_TOKEN` | Optional | Higher GitHub API limits for public repository fetching |

### MongoDB Atlas

1. Create or select an Atlas cluster with Search and Vector Search available.
2. Create a database user scoped to the `codebase_navigator` database. It needs
   read/write access and permission to create the search indexes used by the app.
3. In Network Access, allow the public outbound IP of this development machine.
4. In **Connect → Drivers → Python**, copy the `mongodb+srv://` connection string
   into `MONGODB_URI`. Replace the username/password placeholders and URL-encode
   special characters in the password. This is a database connection string, not
   an Atlas management API key.

The initialization command creates normal collection indexes plus
`chunks_vector_v1` (768-dimensional cosine vector) and `chunks_search_v1`.
Atlas builds search indexes asynchronously. The worker checks them before
enabling AI retrieval for an analysis.

### Gemini

Create a key in [Google AI Studio](https://aistudio.google.com/apikey) and put it in
`GEMINI_API_KEY`. The project must have quota for both the configured generation
model and embedding model. Defaults are `gemini-3.5-flash` and
`gemini-embedding-2`; both can be changed in `.env`. Changing embedding models or
dimensions requires reindexing and a compatible vector index.

## Full stack with Docker

Install/start Docker Desktop with Linux containers, then run from this directory:

```sh
docker compose build
docker compose up -d redis
docker compose run --rm api python -m navigator.doctor --services
docker compose run --rm api python -m navigator.doctor --initialize
docker compose up -d
```

Open **http://localhost:8080**. API docs are at
**http://localhost:8000/docs**. Redis is private to the Compose network. The web
and API ports bind to loopback by default.

```sh
docker compose ps
docker compose logs --tail=100 worker
docker compose down
```

The worker runs in Linux because Celery does not support native Windows.
MongoDB Atlas remains external. Source snapshots persist in Atlas; Redis and
worker state use named Docker volumes. `down` keeps those volumes.

## Host development and local graph mode

Requires Python 3.12+, Node 24, and pnpm 11.25.0. Dependency lockfiles are checked
in for the Python backend, frontend, and analyzer.

```sh
pnpm --dir analyzers/typescript install --frozen-lockfile
pnpm --dir apps/web install --frozen-lockfile
uv sync --project backend --extra test --frozen
uv run --project backend python scripts/dev.py
```

Or use a standard Python virtual environment:

```sh
python -m venv .venv
# Windows:
.venv/Scripts/python.exe -m pip install -e "backend[test]"
.venv/Scripts/python.exe scripts/dev.py
# macOS/Linux: use .venv/bin/python instead.
```

Open **http://127.0.0.1:5173**. `scripts/dev.py` explicitly selects local mode;
it uses disk persistence and a background executor. It can analyze real public
GitHub repositories and the bundled Beacon Store source fixture. Vector search
and Gemini answers require full mode; graph dependency actions remain available.

Use `NODE_BINARY` to select a specific Node executable. The development script
starts API and frontend together and stops both on Ctrl+C. API and worker
settings resolve `.env` and relative paths against the project root.

## Architecture

```text
React / React Flow / Monaco
          │ HTTP + progress events
        FastAPI ── MongoDB Atlas
          │            │
     Redis / Celery    Search + Vector Search
          │            │
     Python worker ── Gemini embeddings / generation
          │
     Node TypeScript analyzer (isolated subprocess)
```

Each repository is pinned to a commit and analysis profile. The graph, chunks,
source viewer, and citations use the same analysis-scoped IDs. The analyzer uses
the TypeScript Compiler API against an approved in-memory file map; it never
installs or executes repository code. Import edges describe static dependencies;
call edges are emitted only for resolved targets. Unknown relationships remain
diagnostics.

The practical implementation plan is in [IMPLEMENTATION.md](IMPLEMENTATION.md).
The adapter contract is described in [CONTRACT.md](CONTRACT.md).
Completed live checks and their limits are recorded in [VALIDATION.md](VALIDATION.md).
Public response schemas generate [OpenAPI](contracts/openapi.json) and frontend
types with `python scripts/generate_contracts.py`. Python, Go,
Rust, and Java are planned adapters, not currently advertised as analyzed languages.

## Verification

```sh
pnpm --dir analyzers/typescript test
pnpm --dir apps/web build
uv run --project backend pytest backend/tests
uv run --project backend python -m navigator.doctor
```

The bundled fixture contains React components, Express routes, authentication,
rate limiting, and checkout flow. It is intentionally fictional source, and its
graph is generated by the real analyzer.

For a configured Atlas/Gemini deployment, index the fixture through **Explore the
example repository** and run the retrieval evaluation against its analysis ID:

```sh
uv run --project backend python scripts/evaluate_retrieval.py ANALYSIS_ID
```

That command makes real embedding requests and reports recall@10 over 30
questions. It does not create substitute vectors or fabricated passing scores.
Absent-feature cases require separate grounded-answer review.

## End-to-end checks

With the stack running, exercise the real sample and a public repository:

```sh
python scripts/smoke.py --demo --rag
python scripts/smoke.py --url https://github.com/sindresorhus/p-limit --rag
```

To run the same graph explorer without Atlas/Gemini:

```sh
docker compose -f compose.yaml -f compose.local.yaml up -d api web
```

Stop the full-mode worker/scheduler first when switching to graph-only mode.
Return to full mode with `docker compose up -d --force-recreate`.

## Service troubleshooting

- Atlas TLS/timeouts: confirm the cluster is running and add the current outbound
  IP under Atlas Network Access. Docker and the host both need allowed access.
- Gemini capacity: completed embeddings are cached. API and worker share an
  atomic Redis gate for per-model minute requests, estimated input tokens, and
  daily application budgets. Indexing gets a smaller allocation so questions
  retain capacity. The defaults admit 70% of a 100 RPM / 30K TPM Embedding 2
  free-tier allocation. This project's AI Studio currently shows a 1,000 RPD
  embedding limit; the local deployment sets `GEMINI_EMBEDDING_RPD=1000` and
  keeps a 700-unit application budget (70% of the provider limit). Each source input and each
  token-count preflight consumes a unit. Check your own project's
  current values in AI Studio and set `GEMINI_EMBEDDING_RPM`,
  `GEMINI_EMBEDDING_TPM`, and `GEMINI_EMBEDDING_RPD` accordingly. Set
  `GEMINI_QUOTA_PROJECT` to the same identifier in every deployment sharing
  one Gemini project and Redis. A full budget queues indexing with a visible
  retry time, releases the worker, and resumes from saved vectors. Unknown 429s
  get a short cooldown; confirmed daily limits wait for midnight Pacific.
  Source batches default to eight inputs, trimmed to the safe token budget.
  Each new batch uses Gemini `countTokens` to obtain an exact input count;
  both that preflight call and the embedding call are admitted through Redis,
  and the count is cached so a resumed job does not repeat it. Disable this
  with `GEMINI_COUNT_EMBEDDING_TOKENS=false` if your embedding model does not
  support token counting. A rejected multi-input batch is halved to use any
  remaining daily capacity before a single-input request waits for reset.
  The worker limits each lease to 240 new vectors to stay below its task time
  limit. Concurrent use of the Gemini project outside this app can still cause
  429s. Provider usage metadata releases excess conservative token reservations
  after successful responses. The recovery scheduler checks due jobs every ten
  seconds; this does not increase the Gemini request budget.
- Gemini overload: choose another model available to your key with `GEMINI_MODEL`
  and recreate API/worker containers. This workspace uses `gemini-3.5-flash` after
  repeated overloads on 3.8 Flash. `GEMINI_THINKING_LEVEL=low` and a configurable
  120-second request timeout are used by default.
- Atlas search preparing: source and graph remain available. Retry indexing once
  both search indexes become queryable.

The V1 analyzer supports JS/TS; Python, Rust, Go and Java are planned adapters.
Graph dependencies are static and may miss dynamic behavior. Vector model/profile
changes require reindexing. The large Monaco source-viewer bundle loads on demand.
