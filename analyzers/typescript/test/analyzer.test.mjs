import test from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { analyze } from '../src/index.mjs';

function run(files) {
  return analyze({ protocolVersion: 1, analysisId: 'test', rootPath: '/untrusted/ignored', files: Object.entries(files).map(([path, content]) => ({ path, content })) });
}
function symbols(records, name) { return records.filter(r => r.type === 'node' && r.kind === 'symbol' && (!name || r.name === name)); }
function edges(records, kind) { return records.filter(r => r.type === 'edge' && (!kind || r.kind === kind)); }

test('resolves imports, reexports, declarations and direct calls from the same immutable file map', () => {
  const records = run({
    'src/auth.ts': 'export function authenticate(token: string) { return token === "valid"; }',
    'src/index.ts': 'export { authenticate } from "./auth.js";',
    'src/routes.ts': 'import { authenticate } from "./index";\nexport function login() { return authenticate("valid"); }',
  });
  assert.ok(edges(records, 'imports').some(e => e.source === 'file:src/routes.ts' && e.target === 'file:src/index.ts'));
  assert.ok(edges(records, 'reexports').some(e => e.target === 'file:src/auth.ts'));
  assert.ok(edges(records, 'calls').some(e => e.source === symbols(records, 'login')[0].id && e.target === symbols(records, 'authenticate')[0].id));
  const ids = new Set(records.filter(r => r.type === 'node').map(n => n.id));
  assert.ok(edges(records).every(e => ids.has(e.source) && ids.has(e.target)));
  assert.equal(records.at(-1).type, 'complete');
  assert.deepEqual(run({ 'a.ts': 'export const a = 1;' }), run({ 'a.ts': 'export const a = 1;' }));
});

test('resolves local tsconfig inheritance, aliases and named workspace package exports', () => {
  const records = run({
    'base.json': '{"compilerOptions":{"baseUrl":".","paths":{"@/*":["src/*"]}}}',
    'tsconfig.json': '{"extends":"./base.json"}',
    'src/lib.ts': 'export function lib() { return 1; }',
    'packages/util/package.json': '{"name":"@shop/util","exports":{".":{"types":"./src/index.ts"},"./*":"./src/*.ts"}}',
    'packages/util/src/index.ts': 'export const util = () => 1;',
    'packages/util/src/math.ts': 'export const twice = (x: number) => x * 2;',
    'src/main.ts': 'import {lib} from "@/lib"; import {util} from "@shop/util"; import {twice} from "@shop/util/math"; export function main() { return twice(util() + lib()); }',
  });
  const targets = edges(records, 'imports').filter(e => e.source === 'file:src/main.ts').map(e => e.target);
  assert.deepEqual(targets.sort(), ['file:src/lib.ts', 'file:packages/util/src/index.ts', 'file:packages/util/src/math.ts'].sort());
  assert.equal(edges(records, 'calls').length, 3);
});

test('retains unresolved local dependencies and distinguishes type-only, dynamic and external imports', () => {
  const records = run({
    'types.ts': 'export interface User { name: string; }',
    'consumer.ts': 'import type { User } from "./types"; import fs from "node:fs"; import "./missing"; const mod = import("./types"); import(variable);',
  });
  assert.ok(edges(records, 'imports').some(e => e.typeOnly && e.target === 'file:types.ts'));
  assert.ok(edges(records, 'imports').some(e => e.dynamic && e.target === 'file:types.ts'));
  assert.ok(records.some(r => r.type === 'node' && r.id === 'external:node:fs'));
  assert.equal(records.filter(r => r.type === 'unresolved').length, 2);
  assert.equal(edges(records, 'calls').length, 0);
});

test('literal CommonJS and import equals work, but shadowed require does not create an import', () => {
  const records = run({
    'a.cjs': 'module.exports = 1;',
    'b.cts': 'import a = require("./a.cjs"); const x = require("./a.cjs"); function f(require: (x: string) => string) { return require("./a.cjs"); }',
  });
  assert.equal(edges(records, 'imports').length, 2, 'import equals and global require are retained; shadowed require is excluded');
  assert.equal(records.filter(r => r.type === 'unresolved').length, 0);
});

test('UTF-8 offsets and inclusive line ranges match source through Unicode and CRLF', () => {
  const content = '// 😀 café\r\nexport function greet() {\r\n  return "你好";\r\n}\r\n';
  const records = run({ 'unicode.ts': content });
  const node = symbols(records, 'greet')[0];
  const extracted = Buffer.from(content).subarray(node.startByte, node.endByte).toString('utf8');
  assert.equal(extracted, 'export function greet() {\r\n  return "你好";\r\n}');
  assert.equal(node.startLine, 2); assert.equal(node.endLine, 4);
  assert.equal(node.startByte, Buffer.byteLength('// 😀 café\r\n'));
  const region = records.find(r => r.type === 'region');
  assert.equal(region.symbolId, node.id);
});

test('classes are partitioned into header and members without overlapping bodies', () => {
  const content = 'export class User {\n name = "Ada";\n greet() { return this.name; }\n}\n';
  const records = run({ 'user.ts': content });
  const user = symbols(records, 'User')[0]; const method = symbols(records, 'greet')[0];
  assert.equal(user.symbolKind, 'class'); assert.equal(method.parentId, user.id);
  const regions = records.filter(r => r.type === 'region');
  assert.equal(regions.length, 3);
  for (let i = 1; i < regions.length; i++) assert.ok(regions[i].startByte >= regions[i - 1].endByte);
});

test('JSX components and Next.js handlers are classified from source evidence', () => {
  const records = run({
    'src/Card.tsx': 'export const Card = () => <article>Hi</article>;',
    'app/api/users/route.ts': 'export async function GET() { return Response.json([]); }',
  });
  assert.ok(symbols(records, 'Card')[0].tags.includes('react-component'));
  assert.ok(symbols(records, 'GET')[0].tags.includes('api-handler'));
});

test('malformed files preserve file identity but produce no graph relationships or chunks', () => {
  const records = run({ 'broken.ts': 'import { f } from "./ok"; export function broken( {', 'ok.ts': 'export function f() {}' });
  assert.ok(records.some(r => r.type === 'node' && r.id === 'file:broken.ts'));
  assert.ok(records.some(r => r.type === 'diagnostic' && r.path === 'broken.ts' && r.severity === 'error'));
  assert.equal(edges(records).filter(e => e.fileId === 'file:broken.ts').length, 0);
  assert.equal(records.filter(r => r.type === 'region' && r.fileId === 'file:broken.ts').length, 0);
});

test('call edges require a known implementation; a callback parameter or interface method is not a runtime target', () => {
  const records = run({ 'dispatch.ts': 'export interface Service { run(): void; } export function execute(service: Service, callback: () => void) { service.run(); callback(); }' });
  assert.equal(edges(records, 'calls').length, 0);
});

test('exports and Express handlers are detected with evidence from imports and registrations', () => {
  const records = run({ 'api.ts': 'import express from "express"; const app = express(); function handler() { return 1; } export {handler}; app.get("/health", handler); app.post("/items", (req, res) => { res.send("ok"); });' });
  const handler = symbols(records, 'handler')[0];
  assert.equal(handler.exported, true);
  assert.ok(handler.tags.includes('route:/health'));
  assert.ok(symbols(records).some(n => n.name.startsWith('callback@') && n.tags?.includes('route:/items')));
});

test('virtual filesystem rejects traversal and does not resolve real host files', () => {
  assert.throws(() => run({ '../escape.ts': '' }), /Unsafe file path/);
  assert.throws(() => run({ 'C:\\escape.ts': '' }), /Unsafe file path/);
  const records = run({ 'index.ts': 'import "../../../../etc/passwd"; import "C:/Windows/System32/config";' });
  assert.ok(records.some(r => r.type === 'unresolved' && r.specifier.startsWith('../')));
  assert.equal(edges(records).some(e => e.resolution === 'resolved'), false);
});

test('CLI emits clean protocol JSONL and rejects invalid protocol', () => {
  const cli = fileURLToPath(new URL('../src/cli.mjs', import.meta.url));
  const result = spawnSync(process.execPath, [cli], { input: JSON.stringify({ protocolVersion: 1, analysisId: 'cli', files: [{ path: 'index.ts', content: 'export const result = 1;' }] }), encoding: 'utf8' });
  assert.equal(result.status, 0, result.stderr);
  const records = result.stdout.trim().split('\n').map(line => JSON.parse(line));
  assert.equal(records.at(-1).type, 'complete');
  const invalid = spawnSync(process.execPath, [cli], { input: '{"protocolVersion":2}', encoding: 'utf8' });
  assert.equal(invalid.status, 1); assert.equal(invalid.stdout, '');
});
