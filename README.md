# AI Codebase Navigator

A local-first code explorer with a React Flow dependency graph, source viewer,
FastAPI backend, asynchronous repository analysis, and source-grounded answers
using MongoDB Atlas retrieval. JavaScript, TypeScript, Python, and Java repositories
are supported by static analysis adapters.

## Required configuration

Copy `.env.example` to `.env` and fill the values for your chosen providers locally. Do not paste
credentials into chat or commit `.env`.

| Variable | Required | Purpose |
| --- | --- | --- |
| `MONGODB_URI` | For full mode | Atlas connection string with a database user and password |
| `CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_API_TOKEN` | For Cloudflare embeddings | Workers AI account and API token |
| `GROQ_API_KEY` | For Groq answers | Groq API key |
| `GEMINI_API_KEY` | For Gemini embeddings or answers | Google AI Studio Gemini API key |
| `GITHUB_TOKEN` | Optional | Higher GitHub API limits for public repository fetching |
| `GITHUB_OAUTH_CLIENT_ID`, `GITHUB_OAUTH_CLIENT_SECRET` | For production sign-in | GitHub OAuth App credentials, kept on the API only |

### GitHub sign-in and free preview

Create a **GitHub OAuth App** under GitHub Settings → Developer settings → OAuth
apps. For the local Compose deployment, set its homepage to
`http://localhost:8080` and its callback URL to
`http://localhost:8080/api/v1/auth/github/callback`. Copy its client ID and
client secret into the corresponding `.env` values. Set
`GITHUB_OAUTH_REDIRECT_URI` to the exact callback URL. The login flow requests
no repository scopes; it reads only the authenticated GitHub user's public ID.
Production deployments must use HTTPS and set the redirect URI, allowed origins,
and allowed hosts to their public domain. Never put the client secret in a Vite
variable or commit it.

Signed-out visitors can explore the prebuilt Beacon Store example and its three
prepared questions. A browser-bound guest session can preview one public GitHub
repository and ask five live AI questions by default. The preview indexes at
most half of eligible chunks, capped by `PREVIEW_EMBEDDING_BYTES`; the complete
static map and source remain available. `FREE_QUESTIONS` changes the guest
allowance. Graph dependency questions and prepared example answers do not
consume it. If a preview fails before the static map is ready, removing it
recovers the guest repository slot. A completed preview can be restored without
granting a second slot.

GitHub sign-in unlocks full indexing for multiple public repositories and
removes the app-level AI question cap. Provider quotas and the configured
per-IP submission gate still apply. A guest preview is added to the signed-in
workspace on login; use **Analyze full repository** to index its remaining
chunks at the same pinned commit. Existing signed-in previews can be upgraded
the same way. Analyses and conversation history are private to the browser's
guest session or signed-in account. Old analyses created before this access
model have no owner grant and are hidden; they must be intentionally reassigned
or left as cached public-source data.
Local graph mode still skips sign-in for development.

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
`chunks_search_v1` and a cosine vector index for the selected embedding provider:
`chunks_vector_v1` (768 dimensions) for Gemini or `chunks_vector_v2` (1024 dimensions)
for Cloudflare Qwen. An existing Gemini index is retained when Cloudflare is selected.
Atlas builds search indexes asynchronously. The worker checks them before
enabling AI retrieval for an analysis.

### Gemini

Create a key in [Google AI Studio](https://aistudio.google.com/apikey) and put it in
`GEMINI_API_KEY`. The project must have quota for both the configured generation
model and embedding model. Defaults are `gemini-3.5-flash` and
`gemini-embedding-2`; both can be changed in `.env`. Changing embedding models or
dimensions requires reindexing and a compatible vector index.

### Cloudflare Qwen embeddings and Groq answers

Set `CLOUDFLARE_ACCOUNT_ID` and `CLOUDFLARE_API_TOKEN` for Workers AI and
`GROQ_API_KEY` for Groq. With the default `EMBEDDING_PROVIDER=auto` and
`ANSWER_PROVIDER=auto`, the app selects Cloudflare Qwen3-Embedding-0.6B for
embeddings when both Cloudflare credentials are present, and Groq
`openai/gpt-oss-20b` for answers when the Groq key is present. Otherwise each
provider falls back independently to Gemini. You can explicitly set
`EMBEDDING_PROVIDER=cloudflare|gemini` and `ANSWER_PROVIDER=groq|gemini`.

The new chunk profile groups nearby small imports, declarations, and statements
into source-exact spans of at most 1,800 characters. This reduces embedding inputs
without losing citable line ranges. Cloudflare batches multiple chunks per
request and uses the same Qwen model for repository chunks and query embeddings.
Rate-limited jobs retain completed vectors and resume after a cooldown; a
Cloudflare daily-allocation response waits for the next UTC reset. The supported
Cloudflare Qwen model emits 1024-dimensional vectors. Submitting a repository
again under the new profile creates a new analysis; older Gemini analyses keep
their original vector space and remain queryable with a Gemini key. Groq answers
use a 10,000-character source-evidence cap, a 15,000-character combined
request-evidence cap, compact graph edges, strict JSON output for supported
GPT-OSS models, and a 3,072-token completion cap
by default to fit its free-tier token budget; the
corresponding `GROQ_*` values in `.env.example` can be adjusted.

## Full stack with Docker

Install/start Docker Desktop with Linux containers, then run from this directory:

```sh
docker compose build
docker compose up -d redis
docker compose run --rm api python -m navigator.doctor --services
docker compose run --rm api python -m navigator.doctor --initialize
docker compose up -d --build
docker compose exec -T api python -m navigator.prewarm_demo
```

Open **http://localhost:8080**. API docs are at
**http://localhost:8000/docs**. Redis is private to the Compose network. The web
and API ports bind to loopback by default.
The prewarm command waits for the example repository's embeddings and saves three
source-grounded answers. Run it once after provider credentials are configured,
and again if the embedding model, chunk profile, or answer style changes. Rebuild the API,
worker, and scheduler together after backend changes; otherwise queued jobs may
fail on an analysis profile that the older worker does not recognize.

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
and AI answers require full mode; graph dependency actions remain available.

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
     Python worker ── Cloudflare or Gemini embeddings; Groq or Gemini answers
          │
     Node TypeScript analyzer (isolated subprocess) + Python/Java static adapters
```

Each repository is pinned to a commit and analysis profile. The graph, chunks,
source viewer, and citations use the same analysis-scoped IDs. The analyzer uses
the TypeScript Compiler API against an approved in-memory file map. Python uses
the standard AST parser; Java uses structural source scanning. No adapter installs
or executes repository code. Import edges describe static dependencies;
call edges are emitted only for resolved targets. Unknown relationships remain
diagnostics.

The practical implementation plan is in [IMPLEMENTATION.md](IMPLEMENTATION.md).
The adapter contract is described in [CONTRACT.md](CONTRACT.md).
Completed live checks and their limits are recorded in [VALIDATION.md](VALIDATION.md).
Public response schemas generate [OpenAPI](contracts/openapi.json) and frontend
types with `python scripts/generate_contracts.py`. Go and Rust remain planned.

## Investigation workspace

- **Split / Map / Source / Ask** layouts let you focus a task. Drag the pane
  separators or use their arrow keys to resize; file and assistant widths are
  remembered in this browser. Compact screens use drawers and a panel switcher.
- **Impact** follows reverse static dependencies up to five steps and lists
  connected routes and test files. Symbol impact deliberately includes its
  containing file. These are potential effects, not runtime or test coverage claims.
- **Compare** compares two accessible snapshots of the same repository, including
  redacted source diffs and added/removed dependency relationships. Analyze another
  revision using a branch, tag, commit SHA, or `PR #123`. Only public repositories
  are supported. Renames appear as a removal and addition; identical overloaded
  symbol names are grouped when comparing relationships.
- **Paths** walks a recorded dependency path from an entry point or a file you
  choose. Each step can open source or its relationship evidence. Branch choices
  start another path; the tour is not an execution trace.
- **Save view / Save answer** keeps source ranges, graph focus, notes, and answer
  evidence in **Saved**. Investigations belong to the current account or browser
  guest identity and are migrated on sign-in. Local mode saves them to the data
  directory. Removing a repository from a workspace removes its saved investigations.
- The map offers a searchable **List view**, explicit **Explore inside** actions,
  a relationship legend, and bounded expansion to 1,000 nodes. Larger maps retain
  readable zoom; use pan, focus, or list view to navigate their contents.

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

For a configured Atlas/provider deployment, index the fixture through **Explore the
example repository** and run the retrieval evaluation against its analysis ID:

```sh
uv run --project backend python scripts/evaluate_retrieval.py ANALYSIS_ID
```

That command makes real embedding requests and reports recall@10 over 30
questions. It does not create substitute vectors or fabricated passing scores.
Absent-feature cases require separate grounded-answer review.

## End-to-end checks

With the stack running, exercise the real sample. The custom-repository smoke
command requires a signed-in session in production, so use the web UI for that
check:

```sh
python scripts/smoke.py --demo --rag
```

To run the same graph explorer without Atlas or AI providers:

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

The analyzers cover JS/TS, Python (including Django, FastAPI, Flask, and Starlette
patterns), and Java (including Spring Boot annotations). Python and Java analysis
uses structural imports and declarations; dynamic dispatch and build-time generated
source may remain unresolved. Go and Rust remain planned.
Graph dependencies are static and may miss dynamic behavior. Vector model/profile
changes require reindexing. The large Monaco source-viewer bundle loads on demand.
