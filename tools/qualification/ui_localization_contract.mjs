#!/usr/bin/env node
/** Deterministic five-locale Console qualification gate. */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import {
  DASHBOARD_MESSAGES, SUPPORTED_LOCALES, assertLocalizationContract,
} from "../../src/engineering_platform/assets/dashboard_locales.mjs";

export const UI_LITERAL_EXCEPTIONS = new Set([
  "", "⧉", "↑", "i", "×", "↺", "↻", "⌧", "▤", "✓", "✦", "◉", "⋯", "—", "⌄", "↗",
]);
const RAW_MACHINE_CODE = /(?:FILE|HTTP|CLI)_INGRESS_(?:HEALTHY|DOWN|AVAILABLE|DEGRADED|RUNNING|STOPPED)|\b(?:HEALTHY|AVAILABLE|RUNNING|STOPPED|DEGRADED)\b/;

export function userVisibleLiteralFindings(source, label = "dashboard.js") {
  const findings = [];
  for (const match of source.matchAll(/(?:\.textContent|\.innerText|\.title|\.placeholder)\s*=\s*(["'])(.*?)\1/g)) {
    if (!UI_LITERAL_EXCEPTIONS.has(match[2])) findings.push(`UNCLASSIFIED_USER_VISIBLE_LITERAL ${label}:${match[2]}`);
  }
  for (const match of source.matchAll(/(?:label|title|placeholder)\s*:\s*(["'])(.*?)\1/g)) {
    if (!UI_LITERAL_EXCEPTIONS.has(match[2])) findings.push(`UNCLASSIFIED_USER_VISIBLE_LITERAL ${label}:${match[2]}`);
  }
  return findings;
}

export function rawMachineCodeFindings(source, label = "dashboard.js") {
  const findings = [];
  // Console renderers may map a code to `t("transport.status." + code)`, but
  // must never use it as a human fallback.
  for (const line of source.split("\n")) {
    if (RAW_MACHINE_CODE.test(line) && /(?:textContent|innerText|\.title|fallback|String\(component\.(?:state|status_code)\))/.test(line) && !line.includes('t("transport.')) {
      findings.push(`RAW_MACHINE_STATUS_CODE ${label}:${line.trim()}`);
    }
  }
  return findings;
}

export function verifyConsoleLocalization({ catalogs = DASHBOARD_MESSAGES, source, label } = {}) {
  const findings = [];
  try { assertLocalizationContract(catalogs); } catch (error) { findings.push(...String(error.message).split("\n").slice(1)); }
  findings.push(...userVisibleLiteralFindings(source, label));
  findings.push(...rawMachineCodeFindings(source, label));
  return findings;
}

function main() {
  const root = resolve(process.argv[2] || ".");
  const source = readFileSync(resolve(root, "src/engineering_platform/assets/dashboard.js"), "utf8");
  const findings = verifyConsoleLocalization({ source, label: "src/engineering_platform/assets/dashboard.js" });
  if (findings.length) throw new Error(`LOCALIZATION_GATE_FAILED\n${findings.join("\n")}`);
  console.log(`UI-GOLDEN-LOCALIZATION=PASS locales=${SUPPORTED_LOCALES.join(",")} key_parity=PASS strict_no_fallback=PASS raw_machine_code_guard=PASS user_visible_literal_guard=PASS`);
}

if (import.meta.url === `file://${process.argv[1]}`) main();
