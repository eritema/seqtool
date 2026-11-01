#!/usr/bin/env node
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import {fileURLToPath, pathToFileURL} from 'node:url';
import {spawnSync} from 'node:child_process';
import {parse} from './vendor/acorn.mjs';

const FUNCTIONS = new Set(['FunctionDeclaration', 'FunctionExpression', 'ArrowFunctionExpression']);
const DECISIONS = new Set(['IfStatement', 'ConditionalExpression', 'ForStatement', 'ForInStatement',
  'ForOfStatement', 'WhileStatement', 'DoWhileStatement', 'CatchClause', 'LogicalExpression', 'AssignmentPattern']);
const SKIP_DIRS = new Set(['node_modules', '.git', '.swarmforge']);
const EXTENSIONS = new Set(['.js', '.mjs', '.cjs']);
const hash = text => crypto.createHash('sha256').update(text).digest('hex');
const inside = (root, target) => target === root || target.startsWith(root + path.sep);

export function crapScore(complexity, coverage) {
  if (!Number.isInteger(complexity) || complexity < 1 || !Number.isFinite(coverage) || coverage < 0 || coverage > 1) {
    throw new Error('CRAP requires complexity >= 1 and coverage between 0 and 1.');
  }
  return complexity ** 2 * (1 - coverage) ** 3 + complexity;
}

function children(node) {
  return Object.entries(node).filter(([key]) => key !== 'loc').flatMap(([, value]) =>
    Array.isArray(value) ? value.filter(item => item && typeof item.type === 'string') :
      value && typeof value.type === 'string' ? [value] : []);
}

function functionName(node, parent) {
  if (node.id) return node.id.name;
  if (parent?.type === 'VariableDeclarator' && parent.id.type === 'Identifier') return parent.id.name;
  if (parent?.type === 'AssignmentExpression' && parent.right === node) {
    const target = parent.left;
    if (target.type === 'Identifier') return target.name;
    if (target.type === 'MemberExpression') {
      if (!target.computed) return target.property.name;
      if (target.property.type === 'Literal' && typeof target.property.value === 'string') return target.property.value;
    }
  }
  if (['Property', 'MethodDefinition'].includes(parent?.type) && !parent.computed) return String(parent.key.name ?? parent.key.value);
  return `<anonymous@${node.loc.start.line}:${node.loc.start.column + 1}>`;
}

export function parseUnits(source, filename = 'source.mjs') {
  let ast;
  try { ast = parse(source, {ecmaVersion: 2022, sourceType: 'module', locations: true, allowHashBang: true}); }
  catch (moduleError) {
    try { ast = parse(source, {ecmaVersion: 2022, sourceType: 'script', locations: true, allowHashBang: true}); }
    catch { throw new Error(`${filename}: JavaScript parsing failed: ${moduleError.message}`); }
  }
  const units = [];
  function register(node, parent) {
    if (['PropertyDefinition', 'StaticBlock'].includes(node.type)) {
      throw new Error(`${filename}:${node.loc.start.line}: class fields/static blocks are not supported; no partial CRAP report produced.`);
    }
    if (node.type === 'Program' || FUNCTIONS.has(node.type)) {
      const program = node.type === 'Program';
      let complexity = 1;
      const points = new Set();
      function inspect(current) {
        if (current !== node && FUNCTIONS.has(current.type)) return;
        if (DECISIONS.has(current.type) || (current.type === 'SwitchCase' && current.test) ||
            (current.type === 'AssignmentExpression' && ['&&=', '||=', '??='].includes(current.operator)) ||
            (['MemberExpression', 'CallExpression'].includes(current.type) && current.optional)) complexity++;
        // Explicit coverage convention: statements plus branch-expression entry points.
        if ((current.type.endsWith('Statement') && !['BlockStatement', 'EmptyStatement'].includes(current.type) && !current.directive) ||
            ['VariableDeclaration', 'ClassDeclaration'].includes(current.type)) points.add(current.start);
        if (current.type === 'ConditionalExpression') {
          points.add(current.consequent.start); points.add(current.alternate.start);
        }
        if (current.type === 'LogicalExpression') points.add(current.right.start);
        if (current.type === 'AssignmentPattern') points.add(current.right.start);
        for (const child of children(current)) inspect(child);
      }
      inspect(node);
      if (!program && node.body.type !== 'BlockStatement') points.add(node.body.start);
      if (!program && points.size === 0) points.add(node.body.start);
      if (!program || points.size) units.push({
        name: program ? '<module>' : functionName(node, parent),
        kind: program ? 'module' : 'function', line: node.loc.start.line, column: node.loc.start.column + 1,
        start: node.start, end: node.end, bodyStart: program ? 0 : node.body.start,
        rangeStart: ['Property', 'MethodDefinition'].includes(parent?.type) ? parent.start : node.start,
        complexity, points: [...points].sort((a, b) => a - b)
      });
    }
    for (const child of children(node)) register(child, node);
  }
  register(ast, null);
  return units;
}

function executionCount(record, offset) {
  let selected = record.ranges[0];
  for (const range of record.ranges) {
    if (range.startOffset <= offset && offset < range.endOffset &&
        range.endOffset - range.startOffset <= selected.endOffset - selected.startOffset) selected = range;
  }
  return selected.count;
}

export function coverageForUnit(unit, scripts) {
  const hits = new Set();
  let matched = false;
  for (const script of scripts) {
    const records = script.functions.filter(record => {
      const root = record.ranges[0];
      return unit.kind === 'module' ? root.startOffset === 0 && root.endOffset === unit.end :
        root.startOffset >= unit.rangeStart && root.startOffset <= unit.bodyStart && root.endOffset === unit.end;
    });
    for (const record of records) {
      matched = true;
      if (record.ranges[0].count > 0 && record.isBlockCoverage !== true) {
        throw new Error(`Precise block coverage unavailable for ${unit.name}:${unit.line}.`);
      }
      for (const point of unit.points) {
        const root = record.ranges[0];
        if (point < root.startOffset || point >= root.endOffset) throw new Error(`V8 range does not contain an execution point in ${unit.name}:${unit.line}.`);
        if (executionCount(record, point) > 0) hits.add(point);
      }
    }
  }
  if (scripts.length && !matched) {
    throw new Error(`Cannot map V8 offsets to ${unit.name}:${unit.line}; source transformations or unsupported coverage shape.`);
  }
  return {covered: hits.size, total: unit.points.length, fraction: hits.size / unit.points.length,
    evidence: scripts.length ? 'measured' : 'source-not-loaded'};
}

export function buildReport(sources, coverage, maximum = null) {
  const entries = [];
  for (const source of sources) {
    for (const unit of source.units) {
      const measured = coverageForUnit(unit, coverage.get(source.file) ?? []);
      const score = crapScore(unit.complexity, measured.fraction);
      entries.push({file: source.relative, name: unit.name, kind: unit.kind,
        line: unit.line, column: unit.column, complexity: unit.complexity,
        coverage: measured, crap: score, overLimit: maximum !== null && score > maximum});
    }
  }
  entries.sort((a, b) => b.crap - a.crap || a.file.localeCompare(b.file) || a.line - b.line || a.column - b.column);
  return {schemaVersion: 1, formula: 'CC^2 * (1 - coverage)^3 + CC',
    complexityConvention: 'classic branches plus defaults, short-circuit and optional-chain branches; nested functions excluded',
    coverageConvention: 'fraction of own AST statements and branch-expression entry points executed; union across V8 snapshots',
    maximum, sources: sources.map(source => ({file: source.relative, sha256: source.sha256})), entries};
}

function readSources(root, inputs) {
  const selected = new Set();
  function visit(input) {
    const absolute = path.resolve(root, input);
    if (!inside(root, absolute) || fs.lstatSync(absolute).isSymbolicLink()) {
      throw new Error(`Source must be inside the current worktree without symlinks: ${input}`);
    }
    const real = fs.realpathSync(absolute);
    if (!inside(root, real)) throw new Error(`Source escapes the current worktree: ${input}`);
    if (fs.statSync(real).isDirectory()) {
      for (const name of fs.readdirSync(real).sort()) if (!SKIP_DIRS.has(name)) visit(path.join(real, name));
    } else if (EXTENSIONS.has(path.extname(real))) selected.add(real);
  }
  for (const input of inputs) visit(input);
  if (!selected.size) throw new Error('No .js, .mjs or .cjs source files selected.');
  return [...selected].sort().map(file => {
    const bytes = fs.readFileSync(file);
    const source = bytes.toString('utf8');
    const relative = path.relative(root, file);
    return {file, relative, sha256: hash(bytes), length: source.length, units: parseUnits(source, relative)};
  });
}

function readCoverage(directory, sources) {
  const selected = new Map(sources.map(source => [source.file, source]));
  const coverage = new Map();
  const names = fs.readdirSync(directory).filter(name => name.endsWith('.json'));
  if (!names.length) throw new Error('Test command produced no V8 coverage. Use a Node process with NODE_V8_COVERAGE enabled.');
  for (const name of names) {
    const snapshot = JSON.parse(fs.readFileSync(path.join(directory, name), 'utf8'));
    if (!Array.isArray(snapshot.result)) throw new Error(`Invalid V8 coverage snapshot: ${name}`);
    for (const script of snapshot.result) {
      if (!script.url?.startsWith('file:')) continue;
      const file = path.resolve(fileURLToPath(script.url));
      const source = selected.get(file);
      if (!source) continue;
      if (snapshot['source-map-cache']?.[script.url]) throw new Error(`Source maps are not supported: ${source.relative}`);
      if (!Array.isArray(script.functions) || !script.functions.length) throw new Error(`Missing function coverage: ${source.relative}`);
      for (const fn of script.functions) {
        if (!Array.isArray(fn.ranges) || !fn.ranges.length) throw new Error(`Missing ranges: ${source.relative}`);
        const root = fn.ranges[0];
        for (const range of fn.ranges) {
          if (![range.startOffset, range.endOffset, range.count].every(Number.isSafeInteger) ||
              range.startOffset < 0 || range.endOffset <= range.startOffset || range.endOffset > source.length || range.count < 0 ||
              range.startOffset < root.startOffset || range.endOffset > root.endOffset) {
            throw new Error(`Invalid or transformed V8 source offsets: ${source.relative}`);
          }
        }
      }
      if (!coverage.has(file)) coverage.set(file, []);
      coverage.get(file).push(script);
    }
  }
  return coverage;
}

function ensureReportDirectory(root, output) {
  const directory = path.resolve(root, output);
  if (!inside(root, directory)) throw new Error('Report directory must be inside the current worktree.');
  let current = root;
  for (const segment of path.relative(root, directory).split(path.sep).filter(Boolean)) {
    current = path.join(current, segment);
    if (fs.existsSync(current)) {
      if (fs.lstatSync(current).isSymbolicLink() || !fs.statSync(current).isDirectory()) throw new Error(`Unsafe report directory: ${current}`);
    } else fs.mkdirSync(current);
  }
  return directory;
}

export function parseArguments(args) {
  const separator = args.indexOf('--');
  const options = separator < 0 ? args : args.slice(0, separator);
  const command = separator < 0 ? [] : args.slice(separator + 1);
  const result = {sources: [], output: '.swarmforge/reports/crap-js', maximum: null, command};
  if (options.includes('--help') || options.includes('-h')) return {...result, help: true};
  for (let i = 0; i < options.length; i++) {
    const option = options[i];
    if (!['--source', '--output', '--max-crap'].includes(option) || i + 1 >= options.length || options[i + 1].startsWith('--')) {
      throw new Error(`Unknown or incomplete option: ${option}`);
    }
    const value = options[++i];
    if (option === '--source') result.sources.push(value);
    if (option === '--output') result.output = value;
    if (option === '--max-crap') {
      if (!/^(?:\d+\.?\d*|\.\d+)$/.test(value) || !Number.isFinite(Number(value))) throw new Error('Invalid --max-crap threshold.');
      result.maximum = Number(value);
    }
  }
  if (!result.sources.length || !command.length) throw new Error('Provide --source PATH and a test command after --.');
  return result;
}

export function testEnvironment(environment, coverageDirectory) {
  const child = {...environment, NODE_V8_COVERAGE: coverageDirectory};
  // This is an independent test command, not a worker of the caller's runner.
  // Inheriting NODE_TEST_CONTEXT makes nested `node --test` skip its files.
  delete child.NODE_TEST_CONTEXT;
  return child;
}

export function main(args = process.argv.slice(2)) {
  try {
    const options = parseArguments(args);
    if (options.help) {
      console.log('Usage: crap-js --source PATH [--source PATH] [--output DIR] [--max-crap N] -- node --test TESTS...');
      console.log('Collects fresh V8 coverage; writes an immutable report.json in a new run directory. No downloads.');
      return 0;
    }
    if (Number(process.versions.node.split('.')[0]) < 22) throw new Error('crap-js requires Node 22 or newer.');
    const root = fs.realpathSync(process.cwd());
    const sources = readSources(root, options.sources);
    const output = ensureReportDirectory(root, options.output);
    const run = fs.mkdtempSync(path.join(output, 'run-'));
    const raw = path.join(run, 'v8');
    fs.mkdirSync(raw);
    const startedAt = new Date().toISOString();
    const manifest = {startedAt, node: process.version, cwd: root, command: options.command,
      sources: sources.map(source => ({file: source.relative, sha256: source.sha256}))};
    fs.writeFileSync(path.join(run, 'manifest.json'), JSON.stringify(manifest, null, 2) + '\n');
    console.error(`CRAP run: ${run}`);
    const child = spawnSync(options.command[0], options.command.slice(1), {
      cwd: root, env: testEnvironment(process.env, raw), stdio: 'inherit', shell: false
    });
    if (child.error || child.signal || child.status !== 0) {
      fs.writeFileSync(path.join(run, 'failure.json'), JSON.stringify({stage: 'tests', exit: child.status,
        signal: child.signal, error: child.error?.message}, null, 2) + '\n');
      console.error('CRAP unavailable: test command failed. Raw evidence retained; no passing report produced.');
      return 3;
    }
    for (const source of sources) {
      if (hash(fs.readFileSync(source.file)) !== source.sha256) throw new Error(`Source changed during measurement: ${source.relative}`);
    }
    const report = {...buildReport(sources, readCoverage(raw, sources), options.maximum),
      ...manifest, completedAt: new Date().toISOString(), testExitCode: child.status};
    fs.writeFileSync(path.join(run, 'report.json'), JSON.stringify(report, null, 2) + '\n');
    console.log('\nFunction / location                                             CC    Cov%       CRAP');
    for (const entry of report.entries) {
      const label = `${entry.file}:${entry.line}:${entry.column} ${entry.name}`.replace(/\s/g, ' ');
      console.log(`${label.padEnd(62)} ${String(entry.complexity).padStart(3)} ${(entry.coverage.fraction * 100).toFixed(1).padStart(7)} ${entry.crap.toFixed(3).padStart(10)}${entry.overLimit ? ' FAIL' : ''}`);
    }
    console.log(`Report: ${path.join(run, 'report.json')}`);
    return report.entries.some(entry => entry.overLimit) ? 1 : 0;
  } catch (error) {
    console.error(`CRAP error: ${error.message}`);
    return 2;
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) process.exitCode = main();
