# TypeScript / JavaScript analyzer

This package implements protocol version 1 in the root `CONTRACT.md`. Run `pnpm install`, then send one JSON input object to `node src/cli.mjs`. The result is JSON Lines ending with a `complete` record. `pnpm test` runs the analyzer fixtures without a database or API key.

The analyzer uses the TypeScript Compiler API and a virtual compiler host. The supplied `files` map is its entire repository filesystem; `rootPath` is intentionally not used to read files. Include relevant `package.json`, `tsconfig.json`, inherited JSON configurations, and source files in the map. Repository scripts and dependencies are never executed or installed.

## Implemented analysis

- JS, JSX, TS, TSX, MJS, CJS, MTS, CTS, and local declaration files.
- File/module containment, declarations, classes, methods, interfaces, type aliases, enums, functions, variables, and inline function callbacks.
- ESM imports/reexports, import-equals, literal unshadowed CommonJS `require`, and literal dynamic imports. Type-only/dynamic flags remain distinct.
- Relative and TypeScript path resolution, local configuration inheritance, named workspace packages and basic conditional/wildcard exports.
- TypeScript-resolved symbol references and call edges only when a single implemented function/method target is known. Callback parameters, interface signatures, unresolved values and ambiguous overloads do not produce call edges.
- React components containing JSX, Next.js exported route handlers, and handlers in literal Express registrations whose receiver is created from an Express import.
- Source ranges use UTF-8 half-open byte offsets and inclusive one-based lines, including Unicode and CRLF.
- Semantic regions partition top-level statements and class members without duplicating class bodies. Large function declarations split at statement boundaries; the backend still enforces token limits.

## Explicit limits

No full runtime call graph is claimed. Reflection, computed imports, framework dependency injection, indirect callbacks, arbitrary bundler plugins, cross-language calls, external dependency implementations, and runtime export conditions may remain unresolved. The analyzer runs without standard-library or installed dependency declarations, which limits some type inference. A package containing parse errors keeps its file node, but that file's symbols, relationships, and semantic regions are omitted with diagnostics. Route detection is intentionally limited to source patterns with evidence; CommonJS Express factories and custom routers are not classified automatically.

IDs are deterministic inside an analysis. They are not stable across source edits that change a declaration's byte offset. Consumers must always scope IDs by analysis.
