"""Owning finite Genesis tests; isolated synthetic owner grants, never user repos."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from engineering_platform import genesis_authority as authority, genesis_contract as contract, server
from engineering_platform import genesis_service as service

FIXTURE = Path(__file__).parent / "fixtures/genesis_local/authority-bundle.json"


class GenesisOwnerFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="ep-genesis-source-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.data = self.root / "server"
        self.instance = server.initialize(self.data).instance_id
        self.workspace = self.root / "workspace"
        self.workspace.mkdir(mode=0o700)
        self.bundle = json.loads(FIXTURE.read_text())
        self.validator = self.root / "trusted-validator.py"
        self.validator.write_text(self.bundle["files"][contract.BASE_PATH+"validate.py"]["utf8"])
        self.original_request = {"contract_version": "1.0", "operation_id": "synthetic-operation", "target_ref": "owner-target",
            "effect_plan_binding": {"plan_id": "synthetic-plan", "plan_revision": 1, "plan_digest": "1"*64,
                "decision_id": "synthetic-effect-decision", "decision_digest": "2"*64,
                "proposal_id": "original-r50", "proposal_digest": "3"*64,
                "source_instance_id": "synthetic-forge", "source_actor_id": "synthetic-actor", "context_id": "synthetic-context"},
            "bundle": self.bundle}
        self.config = {"grant_id": "synthetic-grant", "issuance_id": "synthetic-issue", "consumer_id": "forge",
            "principal": "synthetic-service", "target_name": "new-repository", "workspace_root": str(self.workspace),
            "validator_path": str(self.validator), "request": self.original_request, "ttl_seconds": 900}
        self.grant = authority.issue(self.data, expected_instance=self.instance, config=self.config)
        self.headers = dict(zip(authority.HEADERS, ("1.0", self.instance, authority.host_id(), "forge", "synthetic-service", "genesis-bootstrap")))

    def test_real_finite_owner_scope_uses_original_R50_identity_and_bytes(self):
        original, rendered = contract.request(self.original_request)
        self.assertEqual(rendered.bundle_digest, "0c2c0fb41a68793be0960bb4289d7590060c8092ebb976852e1c4674b0f66b1d")
        self.assertEqual(len(rendered.files), 37)
        with authority.store(self.data) as db:
            grant_id, scope = authority.authenticate(db, self.grant["credential"], self.headers)
        self.assertEqual(grant_id, "synthetic-grant")
        self.assertEqual(scope["project_id"], "sample-product")
        self.assertFalse((self.workspace / "new-repository").exists())

    def test_changed_stored_scope_cannot_reauthorize_another_target(self):
        with authority.store(self.data, write=True) as db:
            old = db.execute("SELECT scope FROM ep_genesis_grants WHERE grant_id='synthetic-grant'").fetchone()[0]
            changed = json.loads(old)
            changed["target_name"] = "foreign-target"
            db.execute("UPDATE ep_genesis_grants SET scope=? WHERE grant_id='synthetic-grant'", (contract.canonical(changed).decode(),))
        with self.assertRaisesRegex(contract.BootstrapError, "BOOTSTRAP_GRANT_CORRUPT"):
            with authority.store(self.data) as db:
                authority.authenticate(db, self.grant["credential"], self.headers)
        self.assertFalse((self.workspace / "foreign-target").exists())

    def test_corrupt_operation_cannot_claim_complete_without_commit_or_B8R(self):
        service.initialize(self.data, expected_instance=self.instance)
        service.admit(self.data, token=self.grant["credential"], headers=self.headers, value=self.original_request)
        with authority.store(self.data, write=True) as db:
            db.execute("UPDATE ep_genesis_operations SET state='COMPLETE' WHERE operation_id='synthetic-operation'")
        with self.assertRaisesRegex(contract.BootstrapError, "BOOTSTRAP_OPERATION_CORRUPT"):
            service.read(self.data, operation_id="synthetic-operation", token=self.grant["credential"], headers=self.headers)
        with authority.store(self.data) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM ep_repository_registrations").fetchone()[0], 0)
        self.assertFalse((self.workspace / "new-repository").exists())


if __name__ == "__main__":
    unittest.main()
