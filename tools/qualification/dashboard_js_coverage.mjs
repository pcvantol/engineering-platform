/* Exact-byte browser V8 coverage over executable tokens, with no ignore hints. */
import { readFileSync, readdirSync, writeFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { createHash } from "node:crypto";
import { parse, tokenizer } from "acorn";

export const TARGETS = ["dashboard.js", "dashboard_locales.mjs", "dashboard_run_evidence.mjs"];
const nonExecutable = new Set(["eof", "{", "}", "(", ")", "[", "]", ";", ",", ".", ":", "=>"]);
const digest = (value) => createHash("sha256").update(value).digest("hex");

function reports(directory) {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const name = path.join(directory, entry.name);
    return entry.isDirectory() ? reports(name) : entry.isFile() && entry.name === "dashboard-v8-coverage.json" ? [name] : [];
  });
}

export async function qualify(sourceRoot, evidenceRoot, targets = TARGETS) {
  const sources = new Map(targets.map((name) => {
    const filename = path.join(sourceRoot, "src/engineering_platform/assets", name);
    const source = readFileSync(filename, "utf8");
    if (/\/\*\s*(?:c8|v8|node:coverage)\s+ignore\b/.test(source)) throw new Error(`Coverage ignore annotation forbidden: ${name}`);
    const executable = new Set();
    const tokens = [];
    for (const token of tokenizer(source, { ecmaVersion: "latest", sourceType: "module", locations: true })) {
      if (!nonExecutable.has(token.type.label)) {
        tokens.push(token);
        for (let line = token.loc.start.line; line <= token.loc.end.line; line++) executable.add(line);
      }
    }
    const scopes = [];
    const visit = (node) => {
      if (!node || typeof node !== "object") return;
      if (["FunctionDeclaration", "FunctionExpression", "ArrowFunctionExpression"].includes(node.type)) {
        scopes.push({ start: node.body.start, end: node.body.end, functionEnd: node.end });
      }
      for (const child of Object.values(node)) if (Array.isArray(child)) child.forEach(visit);
      else if (child && typeof child === "object" && child.type) visit(child);
    };
    visit(parse(source, { ecmaVersion: "latest", sourceType: "module" }));
    for (const token of tokens) token.scope = scopes.filter((item) => token.start >= item.start && token.start < item.end)
      .sort((a, b) => (a.end - a.start) - (b.end - b.start))[0];
    return [name, { filename, source, sha256: digest(source), executable, tokens, scopes, covered: new Set(), observations: 0 }];
  }));
  const inputs = reports(evidenceRoot);
  if (!inputs.length) throw new Error("Missing browser V8 coverage evidence");
  for (const input of inputs) {
    const report = JSON.parse(readFileSync(input, "utf8"));
    if (report.contract !== "ep-dashboard-v8-coverage/v1" || !Array.isArray(report.entries)) throw new Error(`Invalid browser coverage report: ${input}`);
    for (const entry of report.entries) {
      const name = path.basename(new URL(entry.url).pathname), expected = sources.get(name);
      if (!expected) continue;
      if (entry.sha256 !== expected.sha256 || digest(entry.source) !== expected.sha256) throw new Error(`Stale or foreign browser asset: ${name}`);
      const moduleObservation = entry.functions.find((item) => item.functionName === "" && item.ranges[0].startOffset === 0
        && item.ranges[0].endOffset >= expected.source.trimEnd().length);
      const functionsByEnd = new Map();
      for (const item of entry.functions) if (item !== moduleObservation) {
        const end = item.ranges[0].endOffset;
        functionsByEnd.set(end, [...functionsByEnd.get(end) || [], item]);
      }
      for (const token of expected.tokens) {
        const scope = token.scope;
        // A module evaluation is no evidence that a lazy function body ran.
        // Require an explicit native function observation for its AST body.
        const observation = scope ? (functionsByEnd.get(scope.functionEnd) || [])
          .filter((item) => item.ranges[0].startOffset <= scope.start)
          .sort((a, b) => b.ranges[0].startOffset - a.ranges[0].startOffset)[0] : moduleObservation;
        if (!observation) continue;
        const range = observation.ranges.filter((item) => token.start >= item.startOffset && token.start < item.endOffset)
          .sort((a, b) => (a.endOffset - a.startOffset) - (b.endOffset - b.startOffset))[0];
        if (range?.count > 0) for (let line = token.loc.start.line; line <= token.loc.end.line; line++) expected.covered.add(line);
      }
      expected.observations++;
    }
  }
  const files = [...sources].map(([name, item]) => ({ name, sha256: item.sha256, observations: item.observations,
    executable_lines: item.executable.size, covered_lines: item.covered.size,
    percent: item.executable.size ? 100 * item.covered.size / item.executable.size : 0,
    uncovered_lines: [...item.executable].filter((line) => !item.covered.has(line)).sort((a, b) => a - b),
  }));
  return { contract: "ep-dashboard-executable-line-coverage/v1", minimum_exclusive: 80.2,
    qualified: files.every((item) => item.observations > 0 && item.percent > 80.2), input_count: inputs.length, files };
}

if (import.meta.url === pathToFileURL(process.argv[1] || "").href) {
  const [sourceRoot, evidenceRoot, output] = process.argv.slice(2);
  if (!sourceRoot || !evidenceRoot || !output) throw new Error("Usage: dashboard_js_coverage.mjs SOURCE_ROOT EVIDENCE_ROOT OUTPUT_JSON");
  const result = await qualify(path.resolve(sourceRoot), path.resolve(evidenceRoot));
  writeFileSync(output, JSON.stringify(result, null, 2) + "\n");
  for (const file of result.files) console.log(`${file.name}: ${file.covered_lines}/${file.executable_lines} (${file.percent.toFixed(3)}%)`);
  if (!result.qualified) process.exitCode = 1;
}
