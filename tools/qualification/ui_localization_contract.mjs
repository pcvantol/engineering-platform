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
const TECHNICAL_LITERAL_EXCEPTIONS = new Set([
  "application/x-ndjson;charset=utf-8", "button", "dialog", "none", "true", "false",
]);
const RAW_MACHINE_CODE = /(?:FILE|HTTP|CLI)_INGRESS_(?:HEALTHY|DOWN|AVAILABLE|DEGRADED|RUNNING|STOPPED)|\b(?:HEALTHY|AVAILABLE|RUNNING|STOPPED|DEGRADED)\b/;
const RETIRED_OPERATOR_TERMS = /\b(?:watcher|dashboard|finder)\b/i;

/**
 * Extract the static catalog keys used by the supported Server/Console
 * presentation boundary.  Keys are identifiers, not copy, so legacy key
 * names may remain for compatibility; their resolved operator copy may not.
 */
export function operatorLocalizationKeys(source) {
  const keys = new Set();
  for (const pattern of [
    /\bt\(\s*["']([^"']+)["']/g,
    /\btranslate\(\s*["']([^"']+)["']/g,
    /data-i18n(?:-[a-z-]+)?=["']([^"']+)["']/g,
  ]) for (const match of source.matchAll(pattern)) keys.add(match[1]);
  return [...keys].sort();
}

function placeholders(value) {
  return [...String(value).matchAll(/\{([a-z_]+)\}/g)].map((match) => match[1]).sort().join(",");
}

export function placeholderConsistencyFindings(catalogs = DASHBOARD_MESSAGES) {
  const findings = [];
  for (const key of Object.keys(catalogs.en || {})) {
    const expected = placeholders(catalogs.en[key]);
    for (const locale of SUPPORTED_LOCALES) {
      if (placeholders(catalogs[locale]?.[key]) !== expected)
        findings.push(`PLACEHOLDER_MISMATCH locale=${locale} key=${key}`);
    }
  }
  return findings;
}

export function retiredOperatorCopyFindings({ catalogs = DASHBOARD_MESSAGES, sources = [] } = {}) {
  const findings = [];
  for (const { source, label } of sources) {
    for (const key of operatorLocalizationKeys(source)) {
      for (const locale of SUPPORTED_LOCALES) {
        const copy = catalogs[locale]?.[key];
        if (typeof copy === "string" && RETIRED_OPERATOR_TERMS.test(copy))
          findings.push(`STALE_OPERATOR_COPY locale=${locale} key=${key} source=${label}`);
      }
    }
  }
  return findings;
}

export function userVisibleLiteralFindings(source, label = "dashboard.js") {
  const findings = [];
  const patterns = [
    /(?:\.textContent|\.innerText|\.title|\.placeholder)\s*=\s*(["'])(.*?)\1/g,
    /(?:label|title|placeholder|ariaLabel)\s*:\s*(["'])(.*?)\1/g,
    /\.setAttribute\(\s*["'](?:aria-label|title|placeholder)["']\s*,\s*(["'])(.*?)\1\s*\)/g,
    /new Option\(\s*(["'])(.*?)\1/g,
  ];
  for (const pattern of patterns)
    for (const match of source.matchAll(pattern)) {
      const literal = match[2];
      if (!UI_LITERAL_EXCEPTIONS.has(literal) && !TECHNICAL_LITERAL_EXCEPTIONS.has(literal))
        findings.push(`UNCLASSIFIED_USER_VISIBLE_LITERAL ${label}:${literal}`);
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

export function verifyConsoleLocalization({ catalogs = DASHBOARD_MESSAGES, source, label, sources = [] } = {}) {
  const findings = [];
  try { assertLocalizationContract(catalogs); } catch (error) { findings.push(...String(error.message).split("\n").slice(1)); }
  findings.push(...userVisibleLiteralFindings(source, label));
  findings.push(...rawMachineCodeFindings(source, label));
  findings.push(...placeholderConsistencyFindings(catalogs));
  findings.push(...retiredOperatorCopyFindings({ catalogs, sources: [{ source, label }, ...sources] }));
  return findings;
}

function main() {
  const root = resolve(process.argv[2] || ".");
  const source = readFileSync(resolve(root, "src/engineering_platform/assets/dashboard.js"), "utf8");
  const sources = [
    ["src/engineering_platform/server_console_services.py", "Server Console template"],
    ["src/engineering_platform/server.py", "Server enhancement presentation"],
  ].map(([path, label]) => ({ source: readFileSync(resolve(root, path), "utf8"), label: path + ` (${label})` }));
  const findings = verifyConsoleLocalization({ source, label: "src/engineering_platform/assets/dashboard.js", sources });
  if (findings.length) throw new Error(`LOCALIZATION_GATE_FAILED\n${findings.join("\n")}`);
  console.log(`UI-GOLDEN-LOCALIZATION=PASS locales=${SUPPORTED_LOCALES.join(",")} key_parity=PASS placeholder_consistency=PASS strict_no_fallback=PASS raw_machine_code_guard=PASS user_visible_literal_guard=PASS stale_operator_copy_guard=PASS`);
}

if (import.meta.url === `file://${process.argv[1]}`) main();
