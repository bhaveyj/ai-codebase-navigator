# V1 validation

Verified on 8 October 2026 against the local Docker stack, MongoDB Atlas Search
and Vector Search, and real Gemini embedding and generation requests. This is a
development validation record, not a benchmark across arbitrary repositories.

## Automated checks

| Check | Result |
| --- | --- |
| FastAPI, ingestion, storage, graph, jobs, retrieval and grounding tests | 73 passed |
| TypeScript analyzer tests | 12 passed |
| Frontend TypeScript and production build | Passed |
| Docker backend and frontend builds | Passed |
| Runtime/service doctor | Python, Node, analyzer, Atlas, Redis and Gemini passed |
| Atlas search indexes | `chunks_vector_v1` and `chunks_search_v1` queryable |
| Final API readiness | Database, Gemini and queue ready |

Backend tests emit one Starlette/httpx deprecation warning. The frontend build
warns about the large Monaco editor bundle; it loads only when source is opened.

## Live repository pipeline

Both snapshots completed fetching/parsing, graph construction, embeddings,
retrieval readiness and architecture summaries. Source citations were checked
against the analysis, pinned commit and valid source line ranges.

| Repository | Pinned commit | Files | Symbols | Edges | Chunks |
| --- | --- | ---: | ---: | ---: | ---: |
| Bundled fictional Beacon Store | `a97e99a340fdbad635e591f005634ef63278fc93` | 20 | 69 | 265 | 87 |
| `sindresorhus/p-limit` | `a8a6fbec4e0e866d6d779b10889bb4f5567e70eb` | 8 | 341 | 836 | 126 |

The public snapshot includes test files and type declarations. Its symbol count
therefore describes the analyzed snapshot, not only the published runtime code.

```sh
python scripts/smoke.py --demo --rag
python scripts/smoke.py --url https://github.com/sindresorhus/p-limit --rag
```

Both checks passed with real Gemini answers and source citations. Saved analyses
are `d60f4fd457b1454f89f804e9` (sample) and `dc7e085788405da0f1e917a9` (public).

## Retrieval and grounded answers

The 30-question Beacon Store retrieval evaluation achieved **100% recall@10**
against its expected source files, exceeding the fixture target of 85%. This
measures retrieval on this small fixture; it does not establish answer accuracy
or performance on large repositories. The generated `retrieval-report.json` is a
local, ignored artifact and can be regenerated with:

```sh
python scripts/evaluate_retrieval.py d60f4fd457b1454f89f804e9
```

Four absent-feature questions were also reviewed against the fixture. Answers
correctly described the lack of password hashing, PostgreSQL persistence, Stripe
payment retries and refresh-token rotation, with evidence for the actual email,
in-memory store, mock payment and session implementations.

## Browser verification

Headless Chrome checks passed for the React Flow layout, file/symbol search,
Monaco source viewing, dependency exploration, clicking a source citation,
highlighting its line range, and a real Gemini rate-limiting answer. The checked
answer cited `src/server/middleware/rate-limit.ts`. No browser errors occurred.

The final sample overview renders six nodes and seven edges. Deeper directories
remain accessible through drill-down and search; every displayed edge references
a displayed node. Local preview images are ignored artifacts.

Indexing recovery checks cover saved vectors, provider retry classification,
cancellation, and durable worker handoff when capacity is unavailable. The
project-wide Redis gate was tested with concurrent workers, minute request and
token limits, daily budgets, a query reserve, and a per-IP submission allowance.
The backend suite passes 82 tests with Redis; the production frontend builds.
Browser checks also verified the countdown, disabled premature AI requests, and
working static dependency answers during indexing, with no errors.

SkillNexus (`bhaveyj/SkillNexus`, commit
`7b0759284e5c06836014246b66dba3ee60f699f5`) additionally exercised a larger snapshot:
109 files, 2,042 symbols, 6,524 edges and 1,059 chunks. The 704 saved embeddings
were retained through deployment of the shared quota gate. A guarded query
embedding succeeded, while larger source batches received daily quota errors;
batch halving saved five additional source vectors, reaching **709/1,059**.
AI Studio confirms a 1,000 RPD Embedding 2 limit, now configured locally;
the application daily budget remains 500 inputs. After restarting the backend,
the API reports ready and the job remains queued under `waiting_for_capacity`
for the Pacific reset on 9 October 2026 at 12:30 PM IST
(`2026-10-09T07:00:00+00:00`). Source and graph remain available;
the analysis is **not yet AI-ready**. Completion and end-to-end RAG must be
checked after capacity returns.

## Scope and practical limits

- V1 analyzes JavaScript and TypeScript. Python, Rust, Go and Java adapters are
  planned behind the shared analyzer contract.
- Relationships come from static analysis. Dynamic imports, runtime dispatch and
  configuration-dependent behavior can remain unresolved and appear as diagnostics.
- Atlas access depends on its network allowlist. Gemini requests depend on model
  availability and quota; resumable embedding jobs and provider retries are implemented.
- This workspace uses `gemini-3.5-flash` after repeated provider overloads on
  3.8 Flash, and `gemini-embedding-2` with 768-dimensional vectors.
- Services bind to localhost. Public deployment, private repositories and
  multi-user access are outside this V1.

Open the app at **http://localhost:8080** and the API docs at
**http://localhost:8000/docs**. Setup and rerun instructions are in [README.md](README.md).
