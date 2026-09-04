import test from "node:test";
import assert from "node:assert/strict";
import {
  DASHBOARD_MESSAGES, LocalizationContractError, assertLocalizationContract,
  createTranslator, localizationContractFindings,
} from "../../src/engineering_platform/assets/dashboard_locales.mjs";
import {
  rawMachineCodeFindings, userVisibleLiteralFindings, verifyConsoleLocalization,
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
