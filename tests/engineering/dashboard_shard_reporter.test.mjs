import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import Reporter, { partition, terminalVerdict } from './dashboard_shard_reporter.mjs';

const corpus = JSON.parse(readFileSync(new URL('./dashboard_shard_inventory.json', import.meta.url))).tests;
const tests = () => corpus.map(row => ({ ...row, location: { file: row.file }, annotations: row.annotations.map(a => ({ ...a })) }));
const id = row => JSON.stringify([row.file, row.title]);

test('all 532 cases remain disjoint and each owning file is balanced across four parts', () => {
  const parts = partition(tests());
  assert.equal(parts.flat().length, 532);
  assert.equal(new Set(parts.flat().map(id)).size, 532);
  assert.deepEqual(parts.flat().map(id).sort(), corpus.map(id).sort());
  for (const file of new Set(corpus.map(row => row.file))) {
    const counts = parts.map(rows => rows.filter(row => row.file === file).length);
    assert.ok(Math.max(...counts) - Math.min(...counts) <= 1);
  }
});

test('narrowed, renamed, repeated and expanded corpora remain red', () => {
  assert.throws(() => partition(tests().slice(1)), /incomplete/);
  assert.throws(() => partition(tests().slice(0, 528)), /incomplete/);
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
  assert.equal(all.filter(row => row.expectedStatus === 'passed').length, 528);
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
    assert.equal(selected.length + excluded.length, 532);
    reporter.onBegin({ projects: [{ outputDir: root }] }, { allTests: () => selected });
    selected.forEach(row => reporter.onTestEnd(row, { status: row.expectedStatus, retry: 0 }));
    assert.equal(reporter.onEnd({ status: 'passed' }), undefined);
    const receipt = JSON.parse(readFileSync(path.join(root, 'complete-shard.json')));
    assert.equal(receipt.qualified, true);
    assert.equal(receipt.parts.flat().length, 532);
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
