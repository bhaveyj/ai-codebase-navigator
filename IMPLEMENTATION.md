# Implementation architecture and V1 plan

## Product boundary

Public GitHub repositories, JavaScript/TypeScript analysis, interactive static
dependencies, source browsing, graph-aware Atlas retrieval, and Gemini answers
with validated source references. Python, Rust, Go and Java are future language
adapters. Billing, users, teams, OAuth and private repositories are outside V1.

## Architecture decisions

Use FastAPI as the application backend and Python for ingestion, job
orchestration, retrieval and AI. Python keeps future AI tooling and language
adapters accessible. Use a small Node subprocess for the TypeScript Compiler API:
its module and symbol resolution are stronger than a syntax-only parser for
JavaScript/TypeScript. This does not require maintaining two application servers.

Language adapters emit the same files, symbols, source regions, unresolved
references and evidence-backed edges. Future adapters can begin with Tree-sitter
for declarations and language-specific import resolvers, then use compiler/LSP
information where it improves precision. Advertise capabilities per language;
unresolved calls must remain unresolved. Do not claim one parser resolves every
language's build system, macros, dynamic imports or runtime behavior.

MongoDB stores graph adjacency alongside source and chunks. Bounded traversals
are sufficient for V1; a separate graph database would add operational cost
without fixing static-analysis coverage. Use the official Gemini SDK and explicit
retrieval functions initially. An AI framework can be introduced when needed for
evaluation or more complex orchestration.

```text
React + React Flow + Monaco
       │ HTTP + job progress events
FastAPI ── MongoDB Atlas: source, graph, jobs, chunks, vector/lexical indexes
       │
Redis / Celery worker + reconciliation scheduler
       │ pinned archive → approved source manifest
       ├─ TypeScript Compiler API subprocess → language-neutral graph/regions
       └─ semantic chunks → Gemini embeddings → Atlas indexes → Gemini summary
```

## Project structure

| Path | Responsibility |
| --- | --- |
| `apps/web/src` | Repository workspace, graph, tree, source viewer, assistant |
| `backend/navigator/api` | HTTP validation, health, admission and progress APIs |
| `backend/navigator/ingestion.py` | Public GitHub metadata and safe archive reading |
| `backend/navigator/analysis.py` | Adapter subprocess, output validation, chunks, overview |
| `backend/navigator/graph.py` | Adjacency traversal and bounded graph projections |
| `backend/navigator/rag.py` | Embeddings, hybrid retrieval, graph expansion, grounded answers |
| `backend/navigator/jobs.py` | Leased background processing and resumable checkpoints |
| `backend/navigator/storage.py` | MongoDB and explicit local development persistence |
| `analyzers/typescript` | Pure static analysis of an in-memory source manifest |
| `contracts`, `CONTRACT.md` | OpenAPI, response schemas, analyzer protocol |
| `scripts` | Local development, setup checks, smoke and retrieval evaluation |
| `tests/fixtures`, `tests/evaluations` | Real source fixture and expected-path questions |
| `infra`, `compose*.yaml` | Container runtime and loopback-only development deployment |

## MongoDB representation

All source artifacts belong to an immutable analysis. Repository identity uses
GitHub's repository ID; analysis identity hashes repository, commit SHA and
analysis/embedding profile. A changed commit or incompatible profile produces a
new analysis rather than overwriting citations from an old snapshot.

| Collection | Main fields and indexes |
| --- | --- |
| `repositories` | `id`, owner/name/URL, default branch, latest ready analysis; unique ID/GitHub ID |
| `analyses` | repository/commit/profile, status, readiness, counts, overview/warnings; unique repository+commit+profile |
| `jobs` | analysis, phase/progress, checkpoint names, heartbeat, lease, cancellation, retry deadline; unique ID and status+heartbeat |
| `files` | analysis, file ID, path, language, redacted content, line count/hash; unique analysis+ID and analysis+path |
| `nodes` | repository/module/file/symbol/external, parent, file, source range, tags; unique analysis+ID and analysis+file |
| `edges` | source→dependency target, kind, resolution, file/line evidence; unique analysis+ID and analysis+source/target+kind |
| `regions` | semantic declaration spans and signatures; unique analysis+ID |
| `chunks` | source text, file/symbol/kind, line ranges, input hash, embedding/profile; unique analysis+ID and analysis+file |
| `embedding_cache` | content/profile hash and vector; unique ID, reusable across snapshots |
| `unresolved_references`, `diagnostics` | Explicit coverage limits and parse issues scoped to an analysis |

`chunks_vector_v1` uses a 768-dimensional cosine vector with filters for analysis,
repository, language and embedding profile. `chunks_search_v1` indexes text,
paths and symbol names and filters by analysis/profile. Index readiness includes
actual sample queries, not just collection existence.

## Parsing and graph architecture

Read a fixed GitHub archive at the resolved commit SHA. Do not install dependencies
or execute repository code. The analyzer's entire filesystem is an approved file
map. Parse JS, JSX, TS, TSX, MJS/CJS and MTS/CTS; use supplied tsconfig/jsconfig and
package manifests to resolve relative imports, aliases and workspace packages.

Extract functions, classes, methods, types, exports, components and supported
Express/Next handler patterns. Emit import/re-export edges, conservative resolved
references and direct calls, plus containment. File imports point from consumer
to dependency. External packages are explicit nodes; unknown local references
are diagnostics. Type-only and dynamic literal imports are marked.

Validate schema, completion counts, node uniqueness, edge endpoints and ranges
before storage. UTF-8 byte offsets are half-open; line ranges are inclusive and
1-based. Invalid files retain identity and diagnostics without invented symbols.

Show module projections first. Drill into files and declarations. File/symbol
focus exposes one or two hops of dependencies/dependents. Cap each response at
100 nodes and 200 edges with omitted counts. Module edges preserve original edge
IDs as provenance. React Flow handles navigation and selection; ELK layout runs
in a browser worker. Source is read-only and syntax-highlighted; citations open
the matching snapshot and highlight the stored line range.

## RAG pipeline

1. Prefer declaration/class-member/component regions, then coherent uncovered
   top-level and documentation runs. Split oversized regions with bounded source
   ranges, retain signatures/paths and deduplicate overlaps.
2. Embed separate `Content` inputs in bounded batches; normalize vectors, verify
   dimensionality/counts and cache by exact input plus embedding profile.
3. Retrieve with analysis/profile-filtered vector search and lexical search.
   Combine ranks with reciprocal rank fusion and boost exact symbol/path matches.
4. Choose distinct file seeds and traverse stored graph relationships. Retrieve
   nearby chunks; diversify files so short symbols cannot crowd out other modules.
   Bound evidence to 24 chunks and 48,000 characters.
5. Send repository evidence and whitelisted static edges as untrusted data, with
   a trusted system instruction requiring fact/inference/unknown blocks and
   supplied citation/edge IDs. History is context, never source evidence.
6. Parse structured JSON, reject invented citation/edge IDs and uncited facts,
   and verify citation ranges against the same analysis and commit. Retry invalid
   output once. Return structured provider errors rather than substitute answers.

Dependency/dependent actions use deterministic graph queries and source evidence
without requiring the model. Generative citation validation verifies references;
it cannot prove that every natural-language interpretation is correct. Static
imports also do not prove runtime execution order.

## APIs and frontend

The complete endpoint contract is in `CONTRACT.md` and `contracts/openapi.json`.
Pydantic response schemas generate frontend types and remove internal DB/lease
fields. APIs cover repository submission/reuse/deletion, capabilities, analysis,
jobs/SSE/cancel/retry, lazy tree, source, search, graph projection and chat.

Frontend areas: repository intake and saved snapshots; progress with graph
availability before AI completion; searchable file tree and map; resizable source
viewer; cited conversational assistant; static overview and architecture summary;
analysis warnings and retry controls. All queries and citations include analysis
identity. No SaaS account flows are introduced.

## Background processing

Production uses one checkpointed Celery pipeline per analysis, with one worker
slot by default. Stages are fetching, parsing, building graph, creating chunks,
creating embeddings, preparing search, summary, ready. Persist progress and
artifacts between stages. An atomic job lease prevents duplicate consumers from
working concurrently. Heartbeats and a periodic reconciler recover abandoned
jobs. Preserve successful vectors, honor retry deadlines, and check cancellation
between bounded operations. A live worker acknowledges cancellation before source
deletion. Splitting every phase into separate tasks can be done when throughput
needs justify more scheduling complexity.

Local graph mode explicitly uses disk persistence and one background executor.
It does not pretend to provide Atlas retrieval. Celery runs in Linux containers
for Windows development. Redis is internal to Compose; web/API bind to loopback.

## Security and resource limits

Accept HTTPS `github.com/owner/repo` roots only. Verify public visibility and pin
the commit; download only from fixed GitHub hosts, without following arbitrary
redirects or forwarding credentials to the archive host. Read tar streams without
extraction; reject traversal, links, special entries and duplicate paths.

Ignore secret files, dependencies, build output, binaries and minified bundles.
Redact recognized secret-like literals while preserving lines. Redaction is
heuristic; review public repositories before intentionally indexing sensitive
material. Strip service credentials from the analyzer process environment.

Limits: 2,000 retained files, 25 MiB text, 512 KiB/file, 100 MiB compressed archive,
500 MiB expanded archive, 50,000 entries, 15,000 chunks, 300-second analyzer, 1 GiB
Node heap within a 2 GiB worker container, bounded output and chat concurrency.
Validate request bodies/origins/hosts, rate-limit mutations and cap history/input.
Render answers as React text, not executable HTML. Keep `.env` out of Git/images.

## Performance and validation

Reuse immutable analyses and exact embeddings. Query-vector caching reduces
repeat requests. Batching reduces embedding request pressure; provider limits
have backoff and resumable jobs. Graph responses are bounded; source viewer and
layout load independently. Atlas indexing can lag writes; keep graph browsing
available while search prepares. V1 limits are conservative and need large-repo
profiling before being increased.

Verify URL/archive boundaries, source ranges, graph direction/caps, semantic
chunk coverage, cross-analysis access, cancellation/recovery, schema compatibility
and grounding failures. Run real local and Atlas/Gemini smoke tests, a public
GitHub analysis, browser citation navigation, and the 30-question retrieval
evaluation. Fixture recall is a small-repository regression metric, not proof of
quality across arbitrary repositories or languages.

## Implementation phases

1. **Foundation:** pinned runtimes/dependencies, Docker, safe configuration checks,
   contracts and immutable storage model.
2. **Static exploration:** bounded ingestion, JS/TS adapter, graph/source APIs and
   module/file/symbol explorer.
3. **Grounded AI:** semantic chunks, batched embeddings, Atlas hybrid retrieval,
   graph expansion, validated Gemini output and architecture summary.
4. **Reliability:** checkpoints, leases, cancellation/retry, resource/security
   tests, live integration and browser validation.
5. **Future work:** language adapters with explicit capabilities, larger-repo
   performance, impact/flow evaluation, commit diffs, then separately authorized
   private-repository OAuth and hosted access controls.
