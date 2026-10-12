/* Mandatory complete-corpus sharding; keeps four single-worker CI/local parts. */
import { readFileSync, writeFileSync, mkdirSync } from 'node:fs';
import path from 'node:path';
import { createHash } from 'node:crypto';

const inventoryBytes = readFileSync(new URL('./dashboard_shard_inventory.json', import.meta.url));
const inventory = JSON.parse(inventoryBytes);
const key = test => JSON.stringify([path.basename(test.location?.file || test.file), test.title]);

export function partition(tests, canonical = inventory.tests) {
  const actual = new Map();
  for (const test of tests) {
    const id = key(test);
    if (actual.has(id)) throw Error(`Duplicate dashboard test: ${id}`);
    actual.set(id, test);
  }
  if (actual.size !== canonical.length) throw Error('Dashboard corpus is incomplete or expanded; review canonical inventory');
  const files = new Map(), parts = [[], [], [], []];
  for (const expected of canonical) {
    const id = key(expected), test = actual.get(id);
    if (!test || test.expectedStatus !== expected.expectedStatus)
      throw Error(`Dashboard corpus/status mismatch: ${id}`);
    if (test.annotations?.some(a => ['skip', 'fixme', 'fail', 'only'].includes(a.type)))
      throw Error(`Dashboard case has an altered execution contract: ${id}`);
    if (!files.has(expected.file)) files.set(expected.file, []);
    files.get(expected.file).push(test);
  }
  if (new Set(canonical.map(key)).size !== canonical.length) throw Error('Duplicate canonical dashboard test');
  for (const testsInFile of files.values()) {
    testsInFile.forEach((test, index) => parts[index % 4].push(test));
  }
  const union = parts.flat().map(key);
  if (union.length !== canonical.length || new Set(union).size !== canonical.length)
    throw Error('Dashboard parts are not a complete disjoint corpus');
  return parts;
}

export function terminalVerdict(planned, observed, status, outsideErrors) {
  const ids = new Set(planned.map(key));
  const completed = new Set(observed.map(item => item.id));
  return status === 'passed' && outsideErrors.length === 0
    && observed.length === ids.size
    && completed.size === ids.size && [...completed].every(id => ids.has(id))
    && observed.every(item => item.status === 'passed' && item.retry === 0);
}

export default class DashboardShardReporter {
  printsToStdio() { return false; }
  preprocess({ config, suite, testRun }) {
    this.listOnly = process.argv.includes('--list');
    if (!config.shard && !this.listOnly) throw Error('Dashboard execution requires an explicit part of four');
    const { current, total } = config.shard || { current: 1, total: 4 };
    if (total !== 4 || !Number.isInteger(current) || current < 1 || current > 4 || config.workers !== 1)
      throw Error('Dashboard qualification requires four parts with one worker each');
    this.parts = partition(suite.allTests());
    this.planned = config.shard ? this.parts[current - 1] : this.parts.flat();
    this.shard = config.shard ? `${current}/${total}` : 'ALL';
    this.observed = []; this.outsideErrors = [];
    testRun.skipSharding();
    const selected = new Set(this.planned);
    for (const test of suite.allTests()) if (!selected.has(test)) testRun.exclude(test);
  }
  onBegin(config, suite) {
    if (!this.parts) return;
    this.outputDir = config.projects[0].outputDir;
    const actual = suite.allTests().map(key).sort();
    if (JSON.stringify(actual) !== JSON.stringify(this.planned.map(key).sort()))
      throw Error('Dashboard selected corpus changed after partition');
  }
  onTestEnd(test, result) {
    if (this.parts) this.observed.push({ id: key(test), status: result.status, retry: result.retry });
  }
  onError(error) { if (this.parts) this.outsideErrors.push(String(error.message || error)); }
  onEnd(result) {
    if (!this.parts) return;
    const qualified = !this.listOnly && terminalVerdict(this.planned, this.observed, result.status, this.outsideErrors);
    const receipt = {
      contract: 'ep-dashboard-complete-shard/v1', shard: this.shard,
      source_sha: process.env.EP_RUN_EVIDENCE_SOURCE_SHA || null,
      source_tree: process.env.EP_RUN_EVIDENCE_SOURCE_TREE || null,
      wheel_sha256: process.env.EP_RUN_EVIDENCE_WHEEL_SHA256 || null,
      canonical_sha256: createHash('sha256').update(inventoryBytes).digest('hex'),
      canonical_count: inventory.tests.length, parts: this.parts.map(tests => tests.map(key)),
      selected: this.planned.map(key), observed: this.observed, outside_errors: this.outsideErrors,
      status: result.status, list_only: this.listOnly, qualified,
    };
    mkdirSync(this.outputDir, { recursive: true });
    writeFileSync(path.join(this.outputDir, 'complete-shard.json'), JSON.stringify(receipt, null, 2));
    if (!this.listOnly && !qualified) return { status: 'failed' };
  }
}
