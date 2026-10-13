/* Mandatory complete-corpus sharding; keeps four single-worker CI/local parts. */
import { readFileSync, writeFileSync, mkdirSync } from 'node:fs';
import path from 'node:path';
import { createHash } from 'node:crypto';

const inventoryBytes = readFileSync(new URL('./dashboard_shard_inventory.json', import.meta.url));
const inventory = JSON.parse(inventoryBytes);
const key = test => JSON.stringify([path.basename(test.location?.file || test.file), test.title]);
const annotations = test => (test.annotations || []).map(({ location, ...contract }) => contract);
// These independent stored-data cases each own their worker, database and guards.
// Place their measured work in the two spare parts; every case still executes.
const storedCaseParts = new Map([
  [JSON.stringify(['dashboard_run_evidence.integration.spec.mjs',
    'reads actual normal en/dark/390 dispatcher evidence without execution effects']), { originalPart: 0, targetPart: 2 }],
  [JSON.stringify(['dashboard_run_evidence.integration.spec.mjs',
    'reads actual normal nl/dark/390 dispatcher evidence without execution effects']), { originalPart: 0, targetPart: 1 }],
  [JSON.stringify(['dashboard_run_evidence.integration.spec.mjs',
    'reads actual normal nl/dark/1280 dispatcher evidence without execution effects']), { originalPart: 3, targetPart: 2 }],
]);

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
    if (JSON.stringify(annotations(test)) !== JSON.stringify(expected.annotations || []))
      throw Error(`Dashboard case has an altered execution contract: ${id}`);
    if (!files.has(expected.file)) files.set(expected.file, []);
    files.get(expected.file).push(test);
  }
  if (new Set(canonical.map(key)).size !== canonical.length) throw Error('Duplicate canonical dashboard test');
  const placedStoredCases = new Set();
  for (const testsInFile of files.values()) {
    testsInFile.forEach((test, index) => {
      const id = key(test), originalPart = index % 4;
      const placement = storedCaseParts.get(id);
      if (placement) {
        if (originalPart !== placement.originalPart || placedStoredCases.has(id))
          throw Error(`Stored dashboard case has an altered original placement: ${id}`);
        placedStoredCases.add(id);
      }
      parts[placement?.targetPart ?? originalPart].push(test);
    });
  }
  if (placedStoredCases.size !== storedCaseParts.size)
    throw Error('Stored dashboard case placement is missing its exact canonical case');
  const union = parts.flat().map(key);
  if (union.length !== canonical.length || new Set(union).size !== canonical.length)
    throw Error('Dashboard parts are not a complete disjoint corpus');
  return parts;
}

export function terminalVerdict(planned, observed, status, outsideErrors) {
  const ids = new Set(planned.map(key));
  const expected = new Map(planned.map(test => [key(test), test.expectedStatus]));
  const completed = new Set(observed.map(item => item.id));
  return status === 'passed' && outsideErrors.length === 0
    && observed.length === ids.size
    && completed.size === ids.size && [...completed].every(id => ids.has(id))
    && observed.every(item => item.status === expected.get(item.id) && item.retry === 0);
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
      mandatory_count: inventory.mandatory_count,
      baseline_not_run: this.planned.filter(test => test.expectedStatus === 'skipped').map(key),
      selected: this.planned.map(key), observed: this.observed, outside_errors: this.outsideErrors,
      status: result.status, list_only: this.listOnly, qualified,
    };
    mkdirSync(this.outputDir, { recursive: true });
    writeFileSync(path.join(this.outputDir, 'complete-shard.json'), JSON.stringify(receipt, null, 2));
    if (!this.listOnly && !qualified) return { status: 'failed' };
  }
}
