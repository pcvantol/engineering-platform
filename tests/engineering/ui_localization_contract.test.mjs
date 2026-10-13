import test from "node:test";
import assert from "node:assert/strict";
import {
  DASHBOARD_MESSAGES, LocalizationContractError, assertLocalizationContract,
  createTranslator, localizationContractFindings,
} from "../../src/engineering_platform/assets/dashboard_locales.mjs";
import {
  operatorLocalizationKeys, placeholderConsistencyFindings, rawMachineCodeFindings,
  retiredOperatorCopyFindings, userVisibleLiteralFindings, verifyConsoleLocalization,
} from "../../tools/qualification/ui_localization_contract.mjs";

function cloneCatalogs() { return Object.fromEntries(Object.entries(DASHBOARD_MESSAGES).map(([locale, values]) => [locale, { ...values }])); }

test("fails when NL or DE loses a canonical key", () => {
  for (const locale of ["nl", "de"]) {
    const catalogs = cloneCatalogs(); delete catalogs[locale]["transport.quarantine"];
    assert.throws(() => assertLocalizationContract(catalogs), new RegExp(`MISSING_KEY locale=${locale} key=transport\\.quarantine`));
  }
});

test("fails English-only keys and empty or placeholder translations", () => {
  const catalogs = cloneCatalogs(); catalogs.en["future.label"] = "Future";
  assert.match(localizationContractFindings(catalogs).join("\n"), /MISSING_KEY locale=nl key=future.label/);
  catalogs.nl["future.label"] = "TODO";
  assert.match(localizationContractFindings(catalogs).join("\n"), /PLACEHOLDER_TRANSLATION locale=nl key=future.label/);
});

test("strict Console mode fails instead of using English fallback", () => {
  const key = "transport.quarantine", saved = DASHBOARD_MESSAGES.nl[key];
  delete DASHBOARD_MESSAGES.nl[key];
  try {
    assert.throws(() => createTranslator("nl", { strict: true, surface: "Platform Components" })(key), LocalizationContractError);
  } finally { DASHBOARD_MESSAGES.nl[key] = saved; }
});

test("guards raw machine codes and direct user-visible literals", () => {
  assert.equal(rawMachineCodeFindings('node.textContent = "FILE_INGRESS_STOPPED";').length, 1);
  assert.equal(userVisibleLiteralFindings('node.textContent = "New untranslated label";').length, 1);
  assert.equal(userVisibleLiteralFindings('select.add(new Option("New untranslated option", "value"));').length, 1);
  assert.equal(userVisibleLiteralFindings('node.setAttribute("aria-label", "New untranslated tooltip");').length, 1);
  assert.equal(verifyConsoleLocalization({ source: 'node.textContent = "↗";', label: "fixture" }).length, 0);
});

test("guards active legacy copy while allowing stable internal key identifiers", () => {
  assert.deepEqual(operatorLocalizationKeys('node.textContent = t("ui.watcher");'), ["ui.watcher"]);
  assert.equal(retiredOperatorCopyFindings({
    catalogs: { en: { "ui.watcher": "Watcher" }, nl: { "ui.watcher": "Watcher" }, de: { "ui.watcher": "Watcher" }, fr: { "ui.watcher": "Watcher" }, es: { "ui.watcher": "Watcher" } },
    sources: [{ source: 'node.textContent = t("ui.watcher");', label: "fixture" }],
  }).length, 5);
  assert.equal(retiredOperatorCopyFindings({
    sources: [{ source: 'node.textContent = t("ui.execution_status");', label: "fixture" }],
  }).length, 0);
});

test("rejects placeholder drift between required locales", () => {
  const catalogs = cloneCatalogs();
  catalogs.nl["queue.defer_description"] = "Uitstellen zonder titel";
  assert.match(placeholderConsistencyFindings(catalogs).join("\n"), /PLACEHOLDER_MISMATCH locale=nl key=queue\.defer_description/);
});

test("contains a five-locale explanation for every platform-data relocation error", () => {
  const codes = [
    "LOCATION_REQUIRED", "LOCATION_NOT_WRITABLE", "PLATFORM_DATA_DESTINATION_INVALID",
    "PLATFORM_DATA_DESTINATION_FILESYSTEM_UNAVAILABLE", "PLATFORM_DATA_DESTINATION_FILESYSTEM_UNSUPPORTED",
    "PLATFORM_DATA_COPY_VERIFICATION_FAILED", "PLATFORM_DATA_DESTINATION_NOT_PREPARED",
    "PLATFORM_DATA_DESTINATION_NOT_EMPTY", "PLATFORM_DATA_UNAVAILABLE", "PLATFORM_DATA_DESTINATION_EXISTS",
    "RELOCATION_KIND_RETIRED", "RELOCATION_ALREADY_PENDING", "RELOCATION_REQUEST_INVALID",
    "PLATFORM_DATA_RELOCATION_BLOCKED", "UNKNOWN",
  ];
  for (const locale of ["en", "nl", "de", "fr", "es"])
    for (const code of codes)
      assert.ok(DASHBOARD_MESSAGES[locale][`configuration.relocation_error.${code}`], `${locale}:${code}`);
});

test("all finite owning producer events and the read failure have strict labels in five locales", async () => {
  const { execFileSync } = await import("node:child_process");
  const { fileURLToPath } = await import("node:url");
  const actual = JSON.parse(execFileSync("python3", [fileURLToPath(new URL("./dashboard_owned_log_events.py", import.meta.url))], { encoding: "utf8" }));
  assert.equal(actual.events.length, 30);
  assert.deepEqual(actual.calls.filter(row => row.producer_scope === "console_audit_conditional").map(row => row.event).sort(),
    ["dashboard_action_completed", "dashboard_action_failed"]);
  assert.deepEqual(actual.calls.filter(row => row.producer_scope === "console_audit_cancellation").map(row => row.event),
    ["dashboard_action_completed"]);
  assert.equal(actual.technical_forwarders.length, 1);
  for (const locale of ["en", "nl", "de", "fr", "es"]) {
    const translate = createTranslator(locale, { strict: true, surface: "Operations Console logs" });
    for (const code of [...actual.events, "platform_log_unavailable"]) {
      const label = translate(`log_event.${code}`);
      assert.equal(label, DASHBOARD_MESSAGES[locale][`log_event.${code}`]);
      assert.ok(label.trim() && !label.includes("_"), `Actual producer ${code} needs an operator label in ${locale}`);
    }
    assert.throws(() => translate("log_event.declared_unknown_event", {}, "Unapproved fallback"), LocalizationContractError);
  }
});
