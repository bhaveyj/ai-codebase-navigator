import ts from 'typescript';
import path from 'node:path';
import { createHash } from 'node:crypto';

const p = path.posix;
const ROOT = '/snapshot';
const sourcePattern = /\.(?:[cm]?[jt]s|[jt]sx)$/i;
const baseOptions = {
  target: ts.ScriptTarget.Latest, module: ts.ModuleKind.NodeNext,
  moduleResolution: ts.ModuleResolutionKind.NodeNext, jsx: ts.JsxEmit.Preserve,
  allowJs: true, checkJs: false, noLib: true, noEmit: true, skipLibCheck: true,
  allowImportingTsExtensions: true, resolveJsonModule: true,
};

function languageOf(name) { return /\.(?:[cm]?ts|tsx)$/i.test(name) ? 'typescript' : 'javascript'; }
function packageName(specifier) { return specifier.startsWith('@') ? specifier.split('/').slice(0, 2).join('/') : specifier.split('/')[0]; }
function hash(value) { return createHash('sha256').update(value).digest('hex').slice(0, 24); }
function modifiersOf(node) { return ts.canHaveModifiers(node) ? (ts.getModifiers(node) || []) : []; }
function hasModifier(node, kind) { return modifiersOf(node).some(m => m.kind === kind); }
function isLiteral(node) { return node && (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node)); }

/** Pure analysis: the supplied file map is the compiler's entire filesystem. */
export function analyze(input) {
  if (!input || input.protocolVersion !== 1 || typeof input.analysisId !== 'string' || !input.analysisId || !Array.isArray(input.files)) {
    throw new Error('Expected protocolVersion 1, an analysisId and a files array.');
  }
  const files = new Map();
  for (const file of input.files) {
    if (!file || typeof file.path !== 'string' || typeof file.content !== 'string') throw new Error('Each file needs a path and content.');
    const name = file.path.replaceAll('\\', '/');
    if (!name || name.startsWith('/') || /^[a-z]:/i.test(name) || name.split('/').some(s => s === '..' || s === '') || name.includes('\0')) {
      throw new Error(`Unsafe file path: ${file.path}`);
    }
    const normalized = p.normalize(name);
    if (files.has(normalized)) throw new Error(`Duplicate file path: ${normalized}`);
    files.set(normalized, file.content);
  }
  const output = [];
  const nodes = new Map();
  const edges = new Map();
  const regions = [];
  const unresolved = [];
  const addNode = node => { if (!nodes.has(node.id)) nodes.set(node.id, { type: 'node', ...node }); };
  const addEdge = edge => {
    const id = `edge:${hash(JSON.stringify([edge.source, edge.target, edge.kind, edge.fileId, edge.startLine, edge.specifier, edge.typeOnly, edge.dynamic]))}`;
    if (!edges.has(id)) edges.set(id, { type: 'edge', id, ...edge });
  };
  const diagnostic = (message, filePath, line, severity = 'warning') => output.push({ type: 'diagnostic', severity, message, ...(filePath ? { path: filePath } : {}), ...(line ? { line } : {}) });
  const canonical = full => p.normalize(full.replaceAll('\\', '/'));
  const relative = full => { const name = canonical(full); return name.startsWith(`${ROOT}/`) ? name.slice(ROOT.length + 1) : null; };
  const readFile = full => { const rel = relative(full); return rel === null ? undefined : files.get(rel); };
  const fileExists = full => readFile(full) !== undefined;
  const directories = new Set([ROOT]);
  for (const name of files.keys()) {
    let dir = p.dirname(`${ROOT}/${name}`);
    while (dir.startsWith(ROOT)) { directories.add(dir); if (dir === ROOT) break; dir = p.dirname(dir); }
  }
  const directoryExists = full => directories.has(canonical(full).replace(/\/$/, ''));
  const virtualHost = {
    readFile, fileExists, directoryExists,
    readDirectory: (dir, extensions, excludes, includes, depth) => {
      const prefix = `${canonical(dir).replace(/\/$/, '')}/`;
      return [...files.keys()].map(k => `${ROOT}/${k}`).filter(k => k.startsWith(prefix) && (!extensions || extensions.some(ext => k.endsWith(ext))) && (depth === undefined || k.slice(prefix.length).split('/').length <= depth + 1));
    },
    useCaseSensitiveFileNames: true,
    getCurrentDirectory: () => ROOT,
    realpath: canonical,
  };
  const configs = new Map();
  for (const [name, text] of files) {
    if (!/(^|\/)(?:tsconfig|jsconfig)(?:\.[^/]+)?\.json$/.test(name)) continue;
    const parsed = ts.parseConfigFileTextToJson(name, text);
    if (parsed.error) { diagnostic(ts.flattenDiagnosticMessageText(parsed.error.messageText, '\n'), name); continue; }
    let config;
    try { config = ts.parseJsonConfigFileContent(parsed.config, virtualHost, p.dirname(`${ROOT}/${name}`), baseOptions, `${ROOT}/${name}`); }
    catch { diagnostic('Unsupported or malformed TypeScript configuration; using default resolution.', name); continue; }
    for (const error of config.errors) if (![18002, 18003].includes(error.code)) diagnostic(ts.flattenDiagnosticMessageText(error.messageText, '\n'), name);
    configs.set(name, { ...config.options, noLib: true, noEmit: true, allowJs: true });
  }
  const optionsFor = full => {
    let dir = p.dirname(relative(full) || '.');
    while (true) {
      for (const file of ['tsconfig.json', 'jsconfig.json']) {
        const key = dir === '.' ? file : `${dir}/${file}`;
        if (configs.has(key)) return configs.get(key);
      }
      if (dir === '.') return baseOptions;
      dir = p.dirname(dir);
    }
  };
  const packages = new Map();
  for (const [name, text] of files) {
    if (p.basename(name) !== 'package.json') continue;
    try { const manifest = JSON.parse(text); if (typeof manifest.name === 'string') packages.set(manifest.name, { dir: p.dirname(name), manifest }); }
    catch { diagnostic('Invalid package.json; package resolution may be incomplete.', name); }
  }
  function localCandidate(target) {
    if (!target.startsWith(`${ROOT}/`)) return undefined;
    const candidates = [target];
    if (/\.(?:js|jsx|mjs|cjs)$/.test(target)) {
      const stem = target.replace(/\.(?:js|jsx|mjs|cjs)$/, '');
      const originalExt = p.extname(target);
      candidates.unshift(...(originalExt === '.mjs' ? ['.mts', '.d.mts'] : originalExt === '.cjs' ? ['.cts', '.d.cts'] : ['.ts', '.tsx', '.d.ts']).map(ext => stem + ext));
    }
    for (const ext of ['.ts', '.tsx', '.mts', '.cts', '.js', '.jsx', '.mjs', '.cjs', '.d.ts']) candidates.push(target + ext);
    for (const ext of ['.ts', '.tsx', '.js', '.jsx', '.mts', '.cts', '.mjs', '.cjs']) candidates.push(`${target}/index${ext}`);
    return candidates.find(file => fileExists(file) && sourcePattern.test(file));
  }
  function exportTarget(value) {
    if (typeof value === 'string') return value;
    if (value && typeof value === 'object' && !Array.isArray(value)) {
      for (const condition of ['types', 'import', 'default', 'require']) { const result = exportTarget(value[condition]); if (result) return result; }
    }
    return undefined;
  }
  function resolve(specifier, containingFile) {
    const options = optionsFor(containingFile);
    const result = ts.resolveModuleName(specifier, containingFile, options, virtualHost).resolvedModule;
    if (result && relative(result.resolvedFileName) !== null && files.has(relative(result.resolvedFileName)) && sourcePattern.test(result.resolvedFileName)) return result.resolvedFileName;
    if (specifier.startsWith('.')) return localCandidate(p.resolve(p.dirname(containingFile), specifier));
    const pkg = packages.get(packageName(specifier));
    if (pkg) {
      const subpath = specifier.slice(packageName(specifier).length);
      let target;
      if (pkg.manifest.exports) {
        const exports = pkg.manifest.exports;
        target = exportTarget(subpath ? exports[`./${subpath.slice(1)}`] : (exports['.'] ?? exports));
        if (!target && typeof exports === 'object') {
          for (const [key, value] of Object.entries(exports)) {
            if (!key.includes('*')) continue;
            const [prefix, suffix] = key.split('*'); const request = subpath ? `.${subpath}` : '.';
            if (request.startsWith(prefix) && request.endsWith(suffix)) { const match = request.slice(prefix.length, suffix ? -suffix.length : undefined); const item = exportTarget(value); if (item) target = item.replaceAll('*', match); }
          }
        }
        if (!target) return undefined;
      }
      target ||= subpath ? `.${subpath}` : (pkg.manifest.types || pkg.manifest.module || pkg.manifest.main || './index');
      return localCandidate(p.resolve(ROOT, pkg.dir, target));
    }
    return undefined;
  }
  const sourcePaths = [...files.keys()].filter(name => sourcePattern.test(name)).sort();
  const sourceFiles = new Map();
  const host = {
    ...virtualHost,
    getSourceFile: (fileName, version) => {
      const content = readFile(fileName);
      if (content === undefined) return undefined;
      if (!sourceFiles.has(fileName)) sourceFiles.set(fileName, ts.createSourceFile(fileName, content, version, true));
      return sourceFiles.get(fileName);
    },
    getDefaultLibFileName: () => `${ROOT}/__no_lib__.d.ts`,
    writeFile: () => { throw new Error('Analyzer does not write compiler output.'); },
    getCanonicalFileName: canonical,
    useCaseSensitiveFileNames: () => true,
    getNewLine: () => '\n',
    resolveModuleNameLiterals: (literals, containingFile) => literals.map(literal => {
      const target = resolve(literal.text, containingFile);
      return { resolvedModule: target ? { resolvedFileName: target, extension: ts.extensionFromPath(target), isExternalLibraryImport: false } : undefined };
    }),
  };
  const program = ts.createProgram(sourcePaths.map(name => `${ROOT}/${name}`), baseOptions, host);
  const checker = program.getTypeChecker();
  const badFiles = new Set();
  const astSymbols = new Map();
  const callableSymbols = new Set();
  const sourceRegions = new Map();
  const byteMaps = new Map();
  const byteOffset = (sf, offset) => {
    let map = byteMaps.get(sf.fileName);
    if (!map) {
      map = new Uint32Array(sf.text.length + 1); let bytes = 0;
      for (let i = 0; i < sf.text.length;) {
        map[i] = bytes;
        const cp = sf.text.codePointAt(i); const width = cp > 0xffff ? 2 : 1;
        if (width === 2) map[i + 1] = bytes;
        bytes += cp <= 0x7f ? 1 : cp <= 0x7ff ? 2 : cp <= 0xffff ? 3 : 4;
        i += width; map[i] = bytes;
      }
      byteMaps.set(sf.fileName, map);
    }
    return map[offset];
  };
  function location(node, sf, includeLeading = false) {
    const start = includeLeading ? node.getFullStart() : node.getStart(sf);
    const end = node.getEnd();
    return { startLine: sf.getLineAndCharacterOfPosition(start).line + 1, endLine: sf.getLineAndCharacterOfPosition(Math.max(start, end - 1)).line + 1, startByte: byteOffset(sf, start), endByte: byteOffset(sf, end) };
  }
  function addModule(dir) {
    const id = `module:${dir}`;
    if (nodes.has(id)) return id;
    const parentId = dir === '.' ? 'repo' : addModule(p.dirname(dir));
    addNode({ id, kind: 'module', name: dir === '.' ? 'Root' : p.basename(dir), path: dir, parentId });
    addEdge({ source: parentId, target: id, kind: 'contains', resolution: 'structural' });
    return id;
  }
  addNode({ id: 'repo', kind: 'repository', name: 'Repository' });
  for (const name of sourcePaths) {
    const sf = program.getSourceFile(`${ROOT}/${name}`); if (!sf) continue;
    const fileId = `file:${name}`; const parentId = addModule(p.dirname(name));
    addNode({ id: fileId, kind: 'file', name: p.basename(name), path: name, fileId, parentId, language: languageOf(name), startLine: 1, endLine: sf.getLineAndCharacterOfPosition(Math.max(0, sf.text.length - 1)).line + 1, startByte: 0, endByte: Buffer.byteLength(sf.text) });
    addEdge({ source: parentId, target: fileId, kind: 'contains', resolution: 'structural' });
    if (sf.parseDiagnostics.length) {
      badFiles.add(sf.fileName);
      for (const error of sf.parseDiagnostics.slice(0, 20)) diagnostic(ts.flattenDiagnosticMessageText(error.messageText, '\n'), name, sf.getLineAndCharacterOfPosition(error.start || 0).line + 1, 'error');
      diagnostic('Syntax errors: relationships and semantic chunks are omitted for this file.', name);
      continue;
    }
    function signature(node, label) {
      const bodyStart = node.body?.getStart(sf) ?? node.initializer?.body?.getStart(sf);
      const end = bodyStart ?? Math.min(node.getEnd(), node.getStart(sf) + 240);
      return sf.text.slice(node.getStart(sf), end).replace(/\s+/g, ' ').trim().slice(0, 400) || label;
    }
    const regionsForFile = [];
    function visit(node, parentSymbol) {
      let kind; let nameNode = node.name; let symbolName;
      if (ts.isFunctionDeclaration(node)) { kind = 'function'; symbolName = node.name?.text || 'default'; }
      else if (ts.isClassDeclaration(node)) { kind = 'class'; symbolName = node.name?.text || 'default'; }
      else if (ts.isMethodDeclaration(node) || ts.isMethodSignature(node)) { kind = 'method'; symbolName = node.name.getText(sf); }
      else if (ts.isConstructorDeclaration(node)) { kind = 'method'; symbolName = 'constructor'; }
      else if (ts.isGetAccessor(node) || ts.isSetAccessor(node)) { kind = 'method'; symbolName = `${ts.isGetAccessor(node) ? 'get' : 'set'} ${node.name.getText(sf)}`; }
      else if (ts.isInterfaceDeclaration(node)) { kind = 'interface'; symbolName = node.name.text; }
      else if (ts.isTypeAliasDeclaration(node)) { kind = 'type_alias'; symbolName = node.name.text; }
      else if (ts.isEnumDeclaration(node)) { kind = 'enum'; symbolName = node.name.text; }
      else if ((ts.isVariableDeclaration(node) || ts.isPropertyDeclaration(node)) && ts.isIdentifier(node.name)) {
        kind = node.initializer && (ts.isArrowFunction(node.initializer) || ts.isFunctionExpression(node.initializer)) ? (ts.isPropertyDeclaration(node) ? 'method' : 'function') : 'variable';
        symbolName = node.name.text;
      }
      else if ((ts.isArrowFunction(node) || ts.isFunctionExpression(node)) && !astSymbols.has(node)) {
        kind = 'function';
        symbolName = node.name?.text || (ts.isExportAssignment(node.parent) ? 'default' : `callback@${sf.getLineAndCharacterOfPosition(node.getStart(sf)).line + 1}`);
      }
      let nextParent = parentSymbol;
      if (kind && symbolName) {
        const loc = location(node, sf); const id = `symbol:${name}:${loc.startByte}:${symbolName}`;
        let exportContainer = node;
        if (ts.isVariableDeclaration(node)) exportContainer = node.parent.parent;
        const exported = hasModifier(exportContainer, ts.SyntaxKind.ExportKeyword) || hasModifier(exportContainer, ts.SyntaxKind.DefaultKeyword) || ts.isExportAssignment(node.parent);
        const tags = [];
        let containsJsx = false;
        const findJsx = current => { if (ts.isJsxElement(current) || ts.isJsxSelfClosingElement(current) || ts.isJsxFragment(current)) containsJsx = true; if (!containsJsx) ts.forEachChild(current, findJsx); };
        if (kind === 'function' && /^[A-Z]/.test(symbolName)) { findJsx(node); if (containsJsx) tags.push('react-component'); }
        if (/^(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)$/.test(symbolName) && /(^|\/)app\/(?:.*\/)?route\.[cm]?[jt]s$/.test(name) && exported) tags.push('api-handler', `http:${symbolName}`);
        if (/(^|\/)pages\/api\//.test(name) && (hasModifier(exportContainer, ts.SyntaxKind.DefaultKeyword) || symbolName === 'handler')) tags.push('api-handler');
        const descriptor = { id, kind: 'symbol', symbolKind: kind, name: symbolName, path: name, fileId, parentId: parentSymbol || fileId, language: languageOf(name), ...loc, signature: signature(node, symbolName), exported, ...(tags.length ? { tags } : {}) };
        addNode(descriptor); astSymbols.set(node, id); if (nameNode) astSymbols.set(nameNode, id);
        if ((kind === 'function' || kind === 'method') && (node.body || node.initializer?.body)) callableSymbols.add(id);
        if ((ts.isVariableDeclaration(node) || ts.isPropertyDeclaration(node)) && node.initializer && (ts.isArrowFunction(node.initializer) || ts.isFunctionExpression(node.initializer))) astSymbols.set(node.initializer, id);
        addEdge({ source: descriptor.parentId, target: id, kind: 'contains', resolution: 'structural' });
        regionsForFile.push({ node, id, kind, signature: descriptor.signature });
        nextParent = id;
      }
      ts.forEachChild(node, child => visit(child, nextParent));
    }
    visit(sf, null);
    const moduleSymbol = checker.getSymbolAtLocation(sf);
    if (moduleSymbol) {
      for (let exportedSymbol of checker.getExportsOfModule(moduleSymbol)) {
        if (exportedSymbol.flags & ts.SymbolFlags.Alias) { try { exportedSymbol = checker.getAliasedSymbol(exportedSymbol); } catch { continue; } }
        for (const declaration of exportedSymbol.declarations || []) {
          const descriptor = nodes.get(astSymbols.get(declaration));
          if (descriptor) descriptor.exported = true;
        }
      }
    }
    sourceRegions.set(sf.fileName, regionsForFile);
  }
  function owner(node, sf) {
    for (let current = node; current && current !== sf; current = current.parent) if (astSymbols.has(current)) return astSymbols.get(current);
    return `file:${relative(sf.fileName)}`;
  }
  function resolvedSymbol(node) {
    let symbol = checker.getSymbolAtLocation(node);
    if (!symbol) return undefined;
    if (symbol.flags & ts.SymbolFlags.Alias) { try { symbol = checker.getAliasedSymbol(symbol); } catch { return undefined; } }
    const ids = new Set((symbol.declarations || []).filter(d => !badFiles.has(d.getSourceFile().fileName)).map(d => astSymbols.get(d)).filter(Boolean));
    return ids.size === 1 ? [...ids][0] : undefined;
  }
  for (const name of sourcePaths) {
    const sf = program.getSourceFile(`${ROOT}/${name}`); if (!sf || badFiles.has(sf.fileName)) continue;
    const fileId = `file:${name}`;
    const edgeLocation = node => { const { startLine, endLine } = location(node, sf); return { fileId, startLine, endLine }; };
    function dependency(specifier, node, kind = 'imports', extra = {}) {
      if (typeof specifier !== 'string' || !specifier) return;
      const target = resolve(specifier, sf.fileName);
      const loc = edgeLocation(node);
      if (target) {
        addEdge({ source: fileId, target: `file:${relative(target)}`, kind, ...loc, specifier, resolution: 'resolved', ...extra }); return;
      }
      const options = optionsFor(sf.fileName);
      const matchesAlias = Object.keys(options.paths || {}).some(key => { const [a, b] = key.split('*'); return key.includes('*') ? specifier.startsWith(a) && specifier.endsWith(b) : key === specifier; });
      const isLocal = specifier.startsWith('.') || specifier.startsWith('/') || /^[a-z]:/i.test(specifier) || packages.has(packageName(specifier)) || matchesAlias || specifier.startsWith('#');
      if (isLocal) {
        unresolved.push({ type: 'unresolved', ...loc, specifier, reason: 'No supported source target resolved in the supplied snapshot.' }); return;
      }
      const pkg = packageName(specifier); const externalId = `external:${pkg}`;
      addNode({ id: externalId, kind: 'external', name: pkg });
      addEdge({ source: fileId, target: externalId, kind, ...loc, specifier, resolution: 'structural', ...extra });
    }
    function walk(node) {
      if (ts.isImportDeclaration(node) && isLiteral(node.moduleSpecifier)) dependency(node.moduleSpecifier.text, node, 'imports', { typeOnly: Boolean(node.importClause?.isTypeOnly || (node.importClause?.namedBindings && ts.isNamedImports(node.importClause.namedBindings) && node.importClause.namedBindings.elements.length > 0 && node.importClause.namedBindings.elements.every(e => e.isTypeOnly))) });
      if (ts.isExportDeclaration(node) && isLiteral(node.moduleSpecifier)) dependency(node.moduleSpecifier.text, node, 'reexports', { typeOnly: Boolean(node.isTypeOnly || (node.exportClause && ts.isNamedExports(node.exportClause) && node.exportClause.elements.length > 0 && node.exportClause.elements.every(e => e.isTypeOnly))) });
      if (ts.isImportEqualsDeclaration(node) && ts.isExternalModuleReference(node.moduleReference) && isLiteral(node.moduleReference.expression)) dependency(node.moduleReference.expression.text, node, 'imports', { typeOnly: Boolean(node.isTypeOnly) });
      if (ts.isCallExpression(node)) {
        const dynamic = node.expression.kind === ts.SyntaxKind.ImportKeyword;
        const require = ts.isIdentifier(node.expression) && node.expression.text === 'require' && !checker.getSymbolAtLocation(node.expression)?.declarations?.length;
        if (dynamic || require) {
          if (isLiteral(node.arguments[0])) dependency(node.arguments[0].text, node, 'imports', { dynamic });
          else unresolved.push({ type: 'unresolved', ...edgeLocation(node), specifier: node.arguments[0]?.getText(sf).slice(0, 200) || '', reason: 'Computed module specifiers cannot be resolved statically.' });
        } else {
          const callee = ts.isPropertyAccessExpression(node.expression) ? node.expression.name : node.expression;
          const target = resolvedSymbol(callee);
          if (target && callableSymbols.has(target)) addEdge({ source: owner(node, sf), target, kind: 'calls', ...edgeLocation(node), resolution: 'resolved' });
        }
        // Only classify route registrations when the receiver is created from an Express import.
        if (ts.isPropertyAccessExpression(node.expression) && /^(get|post|put|patch|delete|head|options|all|use)$/.test(node.expression.name.text) && isLiteral(node.arguments[0])) {
          let receiver = checker.getSymbolAtLocation(node.expression.expression);
          const declaration = receiver?.valueDeclaration;
          const init = declaration && ts.isVariableDeclaration(declaration) ? declaration.initializer : undefined;
          const receiverCall = init && ts.isCallExpression(init) ? init.expression : undefined;
          const receiverIdentifier = receiverCall && ts.isPropertyAccessExpression(receiverCall) ? receiverCall.expression : receiverCall;
          const importSymbol = receiverIdentifier && checker.getSymbolAtLocation(receiverIdentifier);
          const fromExpress = (importSymbol?.declarations || []).some(decl => { let parent = decl; while (parent && !ts.isImportDeclaration(parent)) parent = parent.parent; return parent && isLiteral(parent.moduleSpecifier) && parent.moduleSpecifier.text === 'express'; });
          if (fromExpress) {
            for (const handler of node.arguments.slice(1)) {
              const id = astSymbols.get(handler) || resolvedSymbol(handler); const descriptor = nodes.get(id);
              if (descriptor && callableSymbols.has(id)) descriptor.tags = [...new Set([...(descriptor.tags || []), node.expression.name.text === 'use' ? 'api-middleware' : 'api-handler', `http:${node.expression.name.text.toUpperCase()}`, `route:${node.arguments[0].text}`])];
              else if (descriptor && node.expression.name.text === 'use') descriptor.tags = [...new Set([...(descriptor.tags || []), 'router-mount', `route-prefix:${node.arguments[0].text}`])];
            }
          }
        }
      }
      if (ts.isIdentifier(node)) {
        const isDeclarationName = astSymbols.has(node);
        const inImport = ts.isImportSpecifier(node.parent) || ts.isImportClause(node.parent) || ts.isNamespaceImport(node.parent) || ts.isExportSpecifier(node.parent);
        if (!isDeclarationName && !inImport) {
          const target = resolvedSymbol(node); const source = owner(node, sf);
          if (target && source !== target) addEdge({ source, target, kind: 'references', ...edgeLocation(node), resolution: 'resolved' });
        }
      }
      ts.forEachChild(node, walk);
    }
    walk(sf);
    // Partition top-level statements. Classes use method/declaration boundaries instead of duplicating whole bodies.
    const regionEntries = sourceRegions.get(sf.fileName) || [];
    const emitRegion = (node, entry, start, end) => {
      const range = start === undefined ? location(node, sf) : { startLine: sf.getLineAndCharacterOfPosition(start).line + 1, endLine: sf.getLineAndCharacterOfPosition(Math.max(start, end - 1)).line + 1, startByte: byteOffset(sf, start), endByte: byteOffset(sf, end) };
      if (range.endByte <= range.startByte) return;
      regions.push({ type: 'region', fileId, ...(entry?.id ? { symbolId: entry.id } : {}), kind: entry?.kind || 'statement', ...range, ...(entry?.signature ? { signature: entry.signature } : {}) });
    };
    for (const statement of sf.statements) {
      const entry = regionEntries.find(e => e.node === statement || (ts.isVariableStatement(statement) && statement.declarationList.declarations.some(d => d === e.node)));
      if (ts.isClassDeclaration(statement) && statement.members.length) {
        emitRegion(statement, entry, statement.getStart(sf), statement.members[0].getStart(sf));
        for (const member of statement.members) emitRegion(member, regionEntries.find(e => e.node === member));
      } else if (ts.isVariableStatement(statement) && statement.declarationList.declarations.length > 1) {
        for (const declaration of statement.declarationList.declarations) emitRegion(declaration, regionEntries.find(e => e.node === declaration));
      } else if (statement.getWidth(sf) > 6500 && ts.isFunctionDeclaration(statement) && statement.body?.statements.length) {
        const blocks = statement.body.statements;
        let start = statement.getStart(sf); let end = start;
        for (const block of blocks) {
          if (end > start && block.end - start > 6500) { emitRegion(statement, entry, start, end); start = block.getStart(sf); }
          end = block.end;
        }
        emitRegion(statement, entry, start, statement.end);
      } else emitRegion(statement, entry);
    }
  }
  return [
    ...nodes.values(), ...edges.values(), ...unresolved, ...regions, ...output,
    { type: 'complete', protocolVersion: 1, counts: { nodes: nodes.size, edges: edges.size, regions: regions.length } },
  ];
}
