import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import Reporter, { partition, terminalVerdict } from './dashboard_shard_reporter.mjs';

const corpus = JSON.parse(readFileSync(new URL('./dashboard_shard_inventory.json', import.meta.url))).tests;
const tests = () => corpus.map(row => ({ ...row, location: { file: row.file }, annotations: row.annotations.map(a => ({ ...a })) }));
const id = row => JSON.stringify([row.file, row.title]);

test('all 534 contracts retain their exact ownership with the added provenance regression and three stored-case moves', () => {
  const parts = partition(tests());
  assert.equal(parts.flat().length, 534);
  assert.equal(new Set(parts.flat().map(id)).size, 534);
  assert.deepEqual(parts.flat().map(id).sort(), corpus.map(id).sort());
  assert.deepEqual(parts.map(rows => rows.length), [133, 135, 135, 131]);
  const moved = new Map([
    [JSON.stringify(['dashboard_run_evidence.integration.spec.mjs',
      'reads actual normal en/dark/390 dispatcher evidence without execution effects']), { originalPart: 0, targetPart: 2 }],
    [JSON.stringify(['dashboard_run_evidence.integration.spec.mjs',
      'reads actual normal nl/dark/390 dispatcher evidence without execution effects']), { originalPart: 0, targetPart: 1 }],
    [JSON.stringify(['dashboard_run_evidence.integration.spec.mjs',
      'reads actual normal nl/dark/1280 dispatcher evidence without execution effects']), { originalPart: 3, targetPart: 2 }],
  ]);
  const original = new Map(), fileOrdinals = new Map();
  for (const row of corpus) {
    const ordinal = fileOrdinals.get(row.file) || 0;
    original.set(id(row), ordinal % 4);
    fileOrdinals.set(row.file, ordinal + 1);
  }
  const changes = [], restored = [[], [], [], []];
  parts.forEach((rows, actualPart) => rows.forEach(row => {
    const identity = id(row), originalPart = original.get(identity);
    assert.equal(actualPart, moved.get(identity)?.targetPart ?? originalPart, identity);
    if (actualPart !== originalPart) {
      assert.equal(originalPart, moved.get(identity).originalPart);
      changes.push(identity);
    }
    if (row.expectedStatus === 'skipped') assert.equal(actualPart, originalPart);
    restored[originalPart].push(row);
  }));
  assert.deepEqual(changes.sort(), [...moved.keys()].sort());
  assert.deepEqual(restored.map(rows => rows.length), [135, 134, 133, 132]);
  for (const file of new Set(corpus.map(row => row.file))) {
    const counts = restored.map(rows => rows.filter(row => row.file === file).length);
    assert.ok(Math.max(...counts) - Math.min(...counts) <= 1);
  }
  const groupedCanonical = [...new Set(corpus.map(row => row.file))]
    .flatMap(file => corpus.filter(row => row.file === file));
  parts.forEach((rows, actualPart) => assert.deepEqual(rows.map(id),
    groupedCanonical.filter(row => (moved.get(id(row))?.targetPart ?? original.get(id(row))) === actualPart).map(id)));
  const absent = corpus.filter(row => !moved.has(id(row)));
  assert.throws(() => partition(tests().filter(row => !moved.has(id(row))), absent), /placement is missing/);
  for (const identity of moved.keys()) {
    const changedOrigin = [...corpus];
    const first = changedOrigin.findIndex(row => id(row) === identity);
    const next = changedOrigin.findIndex((row, index) => index > first
      && row.file === changedOrigin[first].file && !moved.has(id(row)));
    assert.ok(next > first);
    [changedOrigin[first], changedOrigin[next]] = [changedOrigin[next], changedOrigin[first]];
    assert.throws(() => partition(tests(), changedOrigin), error =>
      error.message === `Stored dashboard case has an altered original placement: ${identity}`);
  }
});

test('narrowed, renamed, repeated and expanded corpora remain red', () => {
  assert.throws(() => partition(tests().slice(1)), /incomplete/);
  assert.throws(() => partition(tests().slice(0, 528)), /incomplete/);
  assert.throws(() => partition(tests().slice(0, 532)), /incomplete/);
  const renamed = tests(); renamed[0].title += ' changed';
  assert.throws(() => partition(renamed), /mismatch/);
  assert.throws(() => partition([...tests(), tests()[0]]), /Duplicate/);
  const repeated = tests(); repeated[1] = repeated[0];
  assert.throws(() => partition(repeated), /Duplicate/);
});

test('changed expected outcomes and skip/fixme/fail annotations remain red', () => {
  const changed = tests(); changed[0].expectedStatus = 'failed';
  assert.throws(() => partition(changed), /status mismatch/);
  for (const type of ['skip', 'fixme', 'fail', 'only']) {
    const marked = tests(); marked[0].annotations = [{ type }];
    assert.throws(() => partition(marked), /altered execution/);
  }
});

test('only the four unchanged baseline skip contracts remain separately NOT_RUN', () => {
  const all = tests(), baseline = all.filter(row => row.expectedStatus === 'skipped');
  assert.equal(baseline.length, 4);
  assert.equal(all.filter(row => row.expectedStatus === 'passed').length, 530);
  const stripped = tests(); stripped.find(row => row.expectedStatus === 'skipped').annotations = [];
  assert.throws(() => partition(stripped), /altered execution/);
  const changed = tests(); changed.find(row => row.expectedStatus === 'skipped').expectedStatus = 'passed';
  assert.throws(() => partition(changed), /status mismatch/);
  const observed = baseline.map(row => ({ id: id(row), status: 'skipped', retry: 0 }));
  assert.equal(terminalVerdict(baseline, observed, 'passed', []), true);
  assert.equal(terminalVerdict(baseline, [{ ...observed[0], status: 'passed' }, ...observed.slice(1)], 'passed', []), false);
});

test('actual missing, failed, skipped, interrupted, flaky and outside-error outcomes remain red', () => {
  const planned = partition(tests())[0];
  const observed = planned.map(row => ({ id: id(row), status: row.expectedStatus, retry: 0 }));
  assert.equal(terminalVerdict(planned, observed, 'passed', []), true);
  assert.equal(terminalVerdict(planned, observed.slice(1), 'passed', []), false);
  for (const status of ['failed', 'skipped', 'interrupted', 'timedOut']) {
    assert.equal(terminalVerdict(planned, [{ ...observed[0], status }, ...observed.slice(1)], 'passed', []), false);
  }
  assert.equal(terminalVerdict(planned, [...observed, { ...observed[0], status: 'failed' }], 'passed', []), false);
  assert.equal(terminalVerdict(planned, [...observed, observed[0]], 'passed', []), false);
  assert.equal(terminalVerdict(planned, [{ ...observed[0], retry: 1 }, ...observed.slice(1)], 'passed', []), false);
  assert.equal(terminalVerdict(planned, observed, 'failed', []), false);
  assert.equal(terminalVerdict(planned, observed, 'passed', ['outside case error']), false);
  assert.equal(terminalVerdict(planned, [...observed, { id: 'foreign', status: 'passed' }], 'passed', []), false);
});

test('supported reporter excludes only out-of-part cases and binds complete terminal receipt', () => {
  const all = tests(), reporter = new Reporter(), excluded = [];
  const root = mkdtempSync(path.join(tmpdir(), 'ep-shard-reporter-'));
  try {
    reporter.preprocess({ config: { shard: { current: 2, total: 4 }, workers: 1 },
      suite: { allTests: () => all }, testRun: { skipSharding() {}, exclude(row) { excluded.push(row); } } });
    const selected = all.filter(row => !excluded.includes(row));
    assert.equal(selected.length + excluded.length, 534);
    reporter.onBegin({ projects: [{ outputDir: root }] }, { allTests: () => selected });
    selected.forEach(row => reporter.onTestEnd(row, { status: row.expectedStatus, retry: 0 }));
    assert.equal(reporter.onEnd({ status: 'passed' }), undefined);
    const receipt = JSON.parse(readFileSync(path.join(root, 'complete-shard.json')));
    assert.equal(receipt.qualified, true);
    assert.equal(receipt.parts.flat().length, 534);
    reporter.onError(Error('outside case failure'));
    assert.deepEqual(reporter.onEnd({ status: 'passed' }), { status: 'failed' });
  } finally { rmSync(root, { recursive: true }); }
});

test('different topology/worker count and changed selected corpus are refused', () => {
  const all = tests();
  assert.throws(() => new Reporter().preprocess({ config: { workers: 1 }, suite: { allTests: () => all } }), /explicit part/);
  for (const [current, total, workers] of [[1, 3, 1], [0, 4, 1], [5, 4, 1], [1, 4, 2]]) {
    assert.throws(() => new Reporter().preprocess({ config: { shard: { current, total }, workers }, suite: { allTests: () => all } }), /four parts/);
  }
  const reporter = new Reporter();
  reporter.preprocess({ config: { shard: { current: 1, total: 4 }, workers: 1 }, suite: { allTests: () => all },
    testRun: { skipSharding() {}, exclude() {} } });
  assert.throws(() => reporter.onBegin({ projects: [{ outputDir: tmpdir() }] }, { allTests: () => all }), /selected corpus changed/);
});
