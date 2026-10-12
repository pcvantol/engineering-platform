import { test } from "node:test";
import assert from "node:assert/strict";
import inspector from "node:inspector";
import vm from "node:vm";
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { createHash } from "node:crypto";
import { qualify } from "../../tools/qualification/dashboard_js_coverage.mjs";

const source = "function first() {\n return 1;\n}\nfunction second() {\n return 2;\n}\nif (selected === 'first') first(); else second();\n";
const sha256 = (text) => createHash("sha256").update(text).digest("hex");
async function nativeCoverage(selected) {
  const session = new inspector.Session(); session.connect();
  const command = (method, args = {}) => new Promise((resolve, reject) => session.post(method, args, (error, result) => error ? reject(error) : resolve(result)));
  try {
    await command("Profiler.enable"); await command("Profiler.startPreciseCoverage", { callCount: true, detailed: true });
    vm.runInNewContext(source, { selected }, { filename: "https://fixture.invalid/assets/dashboard.js" });
    const result = await command("Profiler.takePreciseCoverage");
    return result.result.filter((entry) => entry.url === "https://fixture.invalid/assets/dashboard.js")
      .map((entry) => ({ ...entry, source, sha256: sha256(source) }));
  } finally { session.disconnect(); }
}
function fixture(entries) {
  const root = mkdtempSync(path.join(tmpdir(), "ep-js-line-coverage-"));
  mkdirSync(path.join(root, "src/engineering_platform/assets"), { recursive: true });
  mkdirSync(path.join(root, "evidence"));
  writeFileSync(path.join(root, "src/engineering_platform/assets/dashboard.js"), source);
  writeFileSync(path.join(root, "evidence/dashboard-v8-coverage.json"), JSON.stringify({ contract: "ep-dashboard-v8-coverage/v1", entries }));
  return root;
}
test("native module evaluation never proves an uncalled function body", async () => {
  const entries = await nativeCoverage("first"), root = fixture(entries);
  try {
    const result = await qualify(root, path.join(root, "evidence"), ["dashboard.js"]);
    assert(result.files[0].uncovered_lines.includes(5));
    assert(!result.files[0].uncovered_lines.includes(2));
    const partial = entries.map((entry) => ({ ...entry, functions: entry.functions.filter((item) =>
      item.ranges[0].startOffset === 0 && item.ranges[0].endOffset === source.length) }));
    writeFileSync(path.join(root, "evidence/dashboard-v8-coverage.json"), JSON.stringify({ contract: "ep-dashboard-v8-coverage/v1", entries: partial }));
    const incomplete = await qualify(root, path.join(root, "evidence"), ["dashboard.js"]);
    assert(incomplete.files[0].uncovered_lines.includes(2));
    assert(incomplete.files[0].uncovered_lines.includes(5));
    assert.equal(incomplete.qualified, false);
  } finally { rmSync(root, { recursive: true, force: true }); }
});
test("exact-byte native observations merge while stale bytes and missing evidence fail", async () => {
  const entries = [...await nativeCoverage("first"), ...await nativeCoverage("second")], root = fixture(entries);
  try {
    const result = await qualify(root, path.join(root, "evidence"), ["dashboard.js"]);
    assert.equal(result.qualified, true);
    assert.equal(result.files[0].percent, 100);
    writeFileSync(path.join(root, "src/engineering_platform/assets/dashboard.js"), source + "// changed\n");
    await assert.rejects(qualify(root, path.join(root, "evidence"), ["dashboard.js"]), /Stale or foreign/);
    mkdirSync(path.join(root, "empty"));
    await assert.rejects(qualify(root, path.join(root, "empty"), ["dashboard.js"]), /Missing browser V8/);
  } finally { rmSync(root, { recursive: true, force: true }); }
});
