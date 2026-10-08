# Implementation contract

All public JSON uses camelCase keys. API prefix `/api/v1`. Node IDs are unique within an analysis; every request and stored artifact is analysis scoped. `_id` is an internal Mongo key only, never the public ID.

## Analyzer CLI

`node analyzers/typescript/src/cli.mjs` accepts one JSON object on stdin: `{protocolVersion:1, analysisId:string, rootPath:string, files:[{path:string, content:string, language?:string}]}`. It must only read the supplied file map via a virtual compiler host (not execute repository code or load configuration from outside the supplied files).

It writes JSON Lines to stdout, one record per node, edge, unresolved reference, region, or diagnostic; errors/logs go to stderr. Final record `{type:'complete', protocolVersion:1, counts:{nodes,edges,regions}}` is required. Each record has a `type` plus the fields below (no data wrapper).

Node `{type:'node', id, kind:'repository'|'module'|'file'|'symbol'|'external', name, path?, fileId?, parentId?, language?, symbolKind?, startLine?, endLine?, startByte?, endByte?, signature?, exported?:boolean, tags?:string[]}`. Repository ID `repo`; module IDs `module:<directory>` (root module `module:.`); file IDs `file:<path>`; symbol IDs `symbol:<path>:<startByte>:<name>`; external IDs `external:<package>`.

Edge `{type:'edge', id, source, target, kind:'contains'|'imports'|'reexports'|'references'|'calls', fileId?, startLine?, endLine?, specifier?, resolution:'structural'|'resolved', typeOnly?:boolean, dynamic?:boolean}`. Dependencies point from consumer to dependency.

Unresolved `{type:'unresolved', fileId, specifier, startLine, endLine, reason}`.

Region `{type:'region', fileId, symbolId?, kind, startLine, endLine, startByte, endByte, signature?}`. Offsets UTF-8 half-open; lines 1-based inclusive.

Diagnostic `{type:'diagnostic', severity:'warning'|'error', message, path?, line?}`.

## HTTP types

Repository `{id, owner, name, url, defaultBranch, latestAnalysisId?, createdAt}`.

Analysis `{id, repositoryId, repositoryName, commitSha, status:'queued'|'running'|'ready'|'failed'|'cancelled', phase, graphReady:boolean, ragReady:boolean, jobId, counts:{files,symbols,edges,chunks}, languages:string[], createdAt, warnings:string[], overview?:{frameworks:string[], entryPoints:string[], routes:object[], integrations:string[], summary?:string}, error?:string}`.

Job `{id, analysisId, status, phase, completed, total, message?, error?, cancelRequested?:boolean}`.

File `{id, analysisId, path, language, content, lineCount, redacted?:boolean}`.

Chunk `{id,analysisId,fileId,path,symbolId?,symbol?,kind,startLine,endLine,text,inputHash,embedding?,embeddingConfig?}`.

Citations `{id,analysisId,fileId,path,commitSha,startLine,endLine,chunkId?,label?}`.

Chat response `{blocks:[{text,kind:'fact'|'inference'|'unknown',citationIds:string[]}],citations:[],relatedNodeIds:string[],mode:'rag'|'graph',warnings:string[]}`. No fictional AI answer fallback. Missing services => structured 503 with useful message.

## Endpoints

- GET `/capabilities` => `{languages:[{id,label,status:'supported'|'planned',capabilities:string[]}],services:{database:boolean,gemini:boolean,queue:boolean},mode:'local'|'production'}`.
- GET `/repositories` => `{repositories:[],analyses:[]}`.
- POST `/repositories` body `{url}` => `{repository,analysis,job}` (202 new/200 reuse).
- POST `/demo` => same as POST repositories, uses bundled local fixture; preview only, actual analyzer output, no mocked embeddings or AI.
- GET `/analyses/:id` => Analysis.
- GET `/jobs/:id` => Job. SSE `/jobs/:id/events` emits `event: progress` with Job JSON; reconnect sends current status.
  Job includes optional `nextAttemptAt` (UTC ISO timestamp) for a retry countdown.
  An embedding cooldown keeps `status:'running'`, uses `phase:'embedding_backoff'`,
  and retains completed/total counts. `graphReady` and `ragReady` are separate;
  the UI permits dependency actions while AI indexing is incomplete.
- POST `/jobs/:id/cancel` or `/retry` => Job.
  Retry accepts optional `{retryAt: <timezone-aware ISO timestamp>}` within the
  next two days. Deferred retries retain checkpoints/progress and are launched
  by the running scheduler after the deadline. Without a body, retry is immediate.
- GET `/analyses/:id/tree?parent=<path>` => `{entries:[{id,path,name,kind:'file'|'directory',language?,childCount?}]}`. Root default `parent=''`.
- GET `/analyses/:id/files/:fileId` => File. URI-encode the entire ID; server must support path-like IDs containing `/`.
- GET `/analyses/:id/search?q=` => `{results:[{id,kind,name,path,startLine?,symbolKind?}]}`.
- GET `/analyses/:id/graph?level=modules|files|symbols&focus=<nodeId>&direction=both|dependencies|dependents&depth=1&language=&kind=` => `{nodes:Node[],edges:Edge[],omittedNodes:number,omittedEdges:number}`. Defaults modules view. Contains edges can be omitted from focused dependency projections.
- POST `/analyses/:id/chat` body `{message,selectedNodeId?,history?:[{role:'user'|'assistant',content:string}],action?:'explain'|'dependencies'|'dependents'}` => Chat response JSON (V1 use progress indicator during fetch; no unvalidated token stream).
- DELETE `/repositories/:id` => 204.
- GET `/health/live` and `/health/ready`.

Errors `{error:{code,message,details?}}`. Unknown IDs 404. RAG unavailable 503. Incomplete analysis graph requests 409. All graph/citation references must be validated against analysis ID.

## Runtime

Python backend package `navigator` under `backend/`. `uvicorn navigator.api.main:app` from backend. Env `NAVIGATOR_MODE=local|production`, `NAVIGATOR_DATA_DIR`, `NAVIGATOR_DEMO_DIR`, `NAVIGATOR_ANALYZER`, `NODE_BINARY`, `MONGODB_URI`, `MONGODB_DATABASE`, `REDIS_URL`, `GEMINI_API_KEY`, `GEMINI_MODEL`, `GEMINI_EMBEDDING_MODEL`. Default production requires Mongo and Redis; explicit local mode uses disk persistence and a background executor and disables vector retrieval without Atlas. Keep public URL ingestion working in local mode through actual analyzer. Demo path is project `tests/fixtures/shop`; root owner creates fixture.
