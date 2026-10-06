"""Independent public-input verification of the selected FME validation profile."""
from __future__ import annotations

import hashlib
import json
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from jsonschema.exceptions import ValidationError

from engineering_platform.storage import sqlite_connection
from tests.engineering import test_effect_execution as fixtures
from tests.engineering import test_effect_integration as integration


def public_digest(value):
    return "sha256:" + hashlib.sha256(json.dumps(
        value, sort_keys=True, ensure_ascii=True, separators=(",", ":")
    ).encode("ascii")).hexdigest()


class EffectProfileTests(unittest.TestCase):
    setUp = fixtures.EffectLifecycleTests.setUp
    _stop_http = fixtures.EffectLifecycleTests._stop_http
    http = fixtures.EffectLifecycleTests.http
    submit = fixtures.EffectLifecycleTests.submit
    state = integration.EffectIntegrationTests.state
    dispatcher = integration.EffectIntegrationTests.dispatcher

    def test_bounded_change_exposes_digest_inputs_and_rejects_rebound_checkpoint(self):
        dispatcher, remote = self.dispatcher()
        before = integration.files(self.root)
        submission = self.submit(fixtures.effect("BOUNDED_REPOSITORY_CHANGE", "GIT",
            source_revision=self.revision, write_paths=["src/boundary.py"]))
        receipt = dispatcher.dispatch(submission)
        self.assertEqual(self.state(receipt.run_id).phase, "WAIT_FOR_OPERATOR_MERGE")
        remote.protected_merge()
        self.assertEqual(dispatcher.dispatch(submission).state, "COMPLETE")
        path = f"/v1/projects/djconnect/submissions/{submission}/effect-result"
        report = self.http(path)
        profile = report["validation_profile"]
        self.assertEqual(report["contract_version"], "1.1")
        self.assertTrue(report["effect_qualified"])
        self.assertEqual(profile["version"], "effect-validation@1.0")
        self.assertEqual(profile["subject"], report["subject"])
        self.assertEqual(profile["controls"], [
            [item["validation_id"], item["authority"]] for item in report["validation_controls"]])
        self.assertTrue(profile["validation_bindings"])
        self.assertEqual(profile["validation_bindings"],
                         self.state(receipt.run_id).effect_execution["validation_bindings"])
        expected = public_digest(profile)
        for item in (*report["validation_controls"], *report["assurance_reviews"]):
            self.assertEqual(item["profile_digest"], expected)
        incomplete = dict(profile, validation_bindings=[])
        self.assertNotEqual(public_digest(incomplete), expected)
        self.assertNotEqual("sha256:" + "a" * 64, expected)
        fixtures.validate_public_schema("effect-result-v1.1", report)
        fixtures.validate_public_schema("effect-validation-profile-v1", profile)
        for invalid in ({key: value for key, value in profile.items() if key != "validation_bindings"},
                        dict(profile, version="effect-validation@unknown"),
                        dict(profile, validation_bindings=[dict(profile["validation_bindings"][0], command=[])])):
            with self.assertRaises(ValidationError):
                fixtures.validate_public_schema("effect-validation-profile-v1", invalid)
        with self.assertRaises(ValidationError):
            fixtures.validate_public_schema("effect-result-v1.1",
                {key: value for key, value in report.items() if key != "validation_profile"})
        status = self.http(f"/v1/projects/djconnect/submissions/{submission}")
        terminal = self.http("/v1/projects/djconnect/artifacts/" + status["evidence"]["terminal_artifact"]["id"])
        fixtures.validate_public_schema("terminal-evidence-v1.6", terminal)
        self.assertEqual(terminal["effect_result"]["validation_profile"], profile)
        legacy_terminal = json.loads(json.dumps(terminal))
        legacy_terminal["contract_version"] = "1.5"
        legacy_terminal["effect_result"]["contract_version"] = "1.0"
        del legacy_terminal["effect_result"]["validation_profile"]
        fixtures.validate_public_schema("terminal-evidence-v1.5", legacy_terminal)
        self.assertEqual(integration.files(self.root), before)
        calls = (self.prefix / "calls.jsonl").read_bytes()
        with sqlite_connection(self.database) as connection:
            original = connection.execute("SELECT payload FROM engineering_transactions WHERE run_id=?",
                                          (receipt.run_id,)).fetchone()[0]
        for replace_receipts in (False, True):
            with self.subTest(replace_receipts=replace_receipts):
                state = json.loads(original)
                checkpoint = state["effect_execution"]
                checkpoint["validation_bindings"][0]["command"].append("rebound")
                if replace_receipts:
                    changed = dict(profile, validation_bindings=checkpoint["validation_bindings"])
                    attempt = checkpoint["attempts"][-1]
                    for item in (*attempt["controls"], *attempt["reviews"]):
                        item["profile_digest"] = public_digest(changed)
                with sqlite_connection(self.database) as connection:
                    connection.execute("UPDATE engineering_transactions SET payload=? WHERE run_id=?",
                                       (json.dumps(state), receipt.run_id))
                    connection.commit()
                with self.assertRaises(HTTPError) as failure:
                    self.http(path)
                self.assertEqual(failure.exception.code, 409)
                failure.exception.close()
        self.assertEqual((self.prefix / "calls.jsonl").read_bytes(), calls)
        self.assertEqual(integration.files(self.root), before)

    def test_evidence_only_profile_has_explicit_empty_bindings_and_legacy_schema_is_preserved(self):
        endpoint = f"http://127.0.0.1:{self.httpd.server_port}/v1/producer-compatibility"
        with urlopen(Request(endpoint, headers={  # nosec B310
            "Authorization": "Bearer " + self.fixture.credential,
            "EP-Project-ID": "djconnect", "EP-Repository-ID": "djconnect",
        })) as response:
            compatibility = json.load(response)
        self.assertEqual(compatibility["contracts"]["effect_result"], ["1.1"])
        self.assertEqual(compatibility["contracts"]["effect_validation_profile"], ["1.0"])
        self.assertIn("1.5", compatibility["contracts"]["terminal_evidence"])
        self.assertIn("1.6", compatibility["contracts"]["terminal_evidence"])
        submission = self.submit(fixtures.effect(source_revision=self.revision))
        path = f"/v1/projects/djconnect/submissions/{submission}/effect-result"
        pending = self.http(path)
        self.assertEqual(pending, {"contract_version": "1.1", "outcome": "NOT_STARTED", "effect_qualified": False})
        fixtures.validate_public_schema("effect-result-v1.1", pending)
        dispatcher, remote = self.dispatcher()
        self.assertEqual(dispatcher.dispatch(submission).state, "COMPLETE")
        report = self.http(path)
        profile = report["validation_profile"]
        self.assertEqual(profile["validation_bindings"], [])
        self.assertEqual(profile["subject"], report["subject"])
        self.assertEqual({item["profile_digest"] for item in report["validation_controls"]}, {public_digest(profile)})
        fixtures.validate_public_schema("effect-result-v1.1", report)
        # The previous schema remains strict and independently usable for pinned
        # 1.0 captures. It must not silently accept the new contract's field.
        legacy = {key: value for key, value in report.items() if key != "validation_profile"}
        legacy["contract_version"] = "1.0"
        fixtures.validate_public_schema("effect-result-v1", legacy)
        with self.assertRaises(ValidationError):
            fixtures.validate_public_schema("effect-result-v1", report)
        self.assertEqual(remote.creates, 0)
