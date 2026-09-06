"""Fail-closed responsibility partition canaries for P-PROV."""
import importlib.util
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

AUDIT = Path(__file__).resolve().parents[2] / "tools/extraction/verify_phase3_equivalence.py"
SPEC = importlib.util.spec_from_file_location("pprov", AUDIT)
MODULE = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(MODULE)

class ResponsibilityLineageTests(unittest.TestCase):
    expected={"r1","r2","r3","r4"}
    def check(self, destinations, retirements=[]):
        errors=[]; MODULE.validate_responsibilities(self.expected,destinations,retirements,errors,"A"); return errors
    def test_valid_split(self):
        self.assertEqual([],self.check([{"path":"B","responsibilities":["r1","r2"]},{"path":"C","responsibilities":["r3","r4"]}]))
    def test_missing_and_duplicate_split_fail(self):
        self.assertTrue(self.check([{"path":"B","responsibilities":["r1","r2"]},{"path":"C","responsibilities":["r3"]}]))
        self.assertTrue(self.check([{"path":"B","responsibilities":["r1","r2"]},{"path":"C","responsibilities":["r2","r3","r4"]}]))
    def test_explicit_shared_split_passes(self):
        self.assertEqual([],self.check([{"path":"B","responsibilities":["r1","r2"],"shared_responsibilities":["r2"]},{"path":"C","responsibilities":["r2","r3","r4"],"shared_responsibilities":["r2"]}]))
    def test_merge_and_retirement(self):
        self.assertEqual([],self.check([{"path":"C","responsibilities":["r1","r2","r3","r4"]}]))
        self.assertEqual([],self.check([{"path":"C","responsibilities":["r1","r2","r3"]}],[{"responsibility":"r4","reason":"governed retirement"}]))
    def test_unknown_destination_and_cycle_rejected(self):
        self.assertTrue(self.check([{"path":"../outside","responsibilities":["r1","r2","r3","r4"]}]))
        self.assertTrue(MODULE.cycle_free([("A","B"),("B","C")]))
        self.assertFalse(MODULE.cycle_free([("A","B"),("B","A")]))
    def test_chained_split_merge_graph_and_failures(self):
        nodes=[{"node_id":"A","node_type":"BASELINE","responsibilities":["r1","r2","r3","r4"]},{"node_id":"D","node_type":"BASELINE","responsibilities":["r5","r6"]},{"node_id":"B","node_type":"CURRENT_TARGET"},{"node_id":"C","node_type":"INTERMEDIATE_TARGET"},{"node_id":"E","node_type":"CURRENT_TARGET"}]
        edges=[{"from":"A","to":"B","kind":"SPLIT","responsibilities":["r1","r2"]},{"from":"A","to":"C","kind":"SPLIT","responsibilities":["r3","r4"]},{"from":"C","to":"E","kind":"MERGE","responsibilities":["r3","r4"]},{"from":"D","to":"E","kind":"MERGE","responsibilities":["r5","r6"]}]
        self.assertEqual([],MODULE.validate_graph({"nodes":nodes,"edges":edges}))
        self.assertTrue(MODULE.validate_graph({"nodes":nodes,"edges":edges[:-1]}))
        edges.append({"from":"E","to":"A","kind":"MOVE","responsibilities":["r3"]})
        self.assertTrue(MODULE.validate_graph({"nodes":nodes,"edges":edges}))

class GovernedPhaseBaselineTests(unittest.TestCase):
    anchor = "8237d078e14d59aa3572c5cbaac89b290d08d71c"
    head = "a0694530ea54fac9a47e0898738105dfc719b935"
    first = "f8aa3e61fdb7297d9e687337d3edd5465d48ff7c"
    last = "f8aa3e61fdb7297d9e687337d3edd5465d48ff7c"
    completion = "a0694530ea54fac9a47e0898738105dfc719b935"

    def record(self):
        return {"historical_target_path":"x.py", "baseline_sha256":MODULE.sha(b"old"), "evolution_type":"GOVERNED_MODIFICATION", "first_governed_commit":self.first, "last_governed_commit":self.last, "destinations":[{"path":"x.py", "current_sha256":MODULE.sha(b"new"), "responsibilities":["x.py::module"]}]}

    def seal(self, record):
        return {"phase_id":"P-SEALED", "completion_baseline":self.completion, "completion_status":"COMPLETE", "sealed_evolution_paths":["x.py"], "sealed_evolutions_sha256":MODULE.canonical_digest([record])}

    def stage2(self, record, seals, ancestor_fn):
        with TemporaryDirectory() as directory:
            target=Path(directory); (target / "x.py").write_bytes(b"new")
            ledger={"historical_extraction":{"standalone_lineage_anchor_commit":self.anchor}, "evolutions":[record], "governed_phase_seals":seals}
            errors=[]
            def fake_run(_target, *args, binary=False): return b"" if binary else self.head
            def fake_blob(_target, ref, _path): return b"old" if ref == self.anchor else b"new"
            with patch.object(MODULE,"run",fake_run), patch.object(MODULE,"blob",fake_blob), patch.object(MODULE,"ancestor",ancestor_fn):
                MODULE.stage2(target,[{"target_path":"x.py","target_final_digest":MODULE.sha(b"old")}],ledger,errors)
            return errors

    def test_current_phase_commit_missing_fails(self):
        record=self.record()
        self.assertFalse(MODULE.current_phase_chain_reachable(Path("."),self.anchor,self.head,self.first,self.last))
        errors=self.stage2(record,[],lambda *_args: False)
        self.assertTrue(any("current-phase governed commit chain" in error for error in errors))

    def test_current_phase_reachable_passes(self):
        record=self.record()
        self.assertEqual([],self.stage2(record,[],lambda *_args: True))

    def test_sealed_predecessor_with_reachable_baseline_passes(self):
        record=self.record(); seal=self.seal(record)
        def ancestry(_target, older, newer):
            return (older,newer) in {(self.anchor,self.completion),(self.completion,self.head),(self.first,self.last)}
        self.assertEqual([],self.stage2(record,[seal],ancestry))

    def test_sealed_predecessor_baseline_absent_fails(self):
        record=self.record(); seal=self.seal(record)
        errors=self.stage2(record,[seal],lambda *_args: False)
        self.assertTrue(any("unreachable governed phase completion baseline" in error for error in errors))

    def test_tampered_or_ambiguous_seal_fails(self):
        record=self.record(); seal=self.seal(record); seal["sealed_evolutions_sha256"]="0" * 64
        errors=self.stage2(record,[seal],lambda *_args: True)
        self.assertTrue(any("tampered or missing sealed provenance ledger" in error for error in errors))

    def test_unaccounted_path_fails(self):
        with TemporaryDirectory() as directory:
            target=Path(directory); (target / "x.py").write_bytes(b"new")
            errors=[]
            with patch.object(MODULE,"run",lambda *_args, **_kwargs: self.head), patch.object(MODULE,"blob",lambda *_args, **_kwargs: b"old"):
                inventory=MODULE.stage2(target,[{"target_path":"x.py","target_final_digest":MODULE.sha(b"old")}],{"historical_extraction":{"standalone_lineage_anchor_commit":self.anchor},"evolutions":[]},errors)
            self.assertEqual("UNACCOUNTED",inventory[0]["classification"])
            self.assertTrue(any("unaccounted current target" in error for error in errors))

class SuccessorReceiptModelTests(unittest.TestCase):
    anchor="a" * 40
    completion_a="b" * 40
    completion_b="c" * 40
    completion_c="d" * 40

    def receipt(self, identity, phase):
        return {"receipt_id":identity,"phase_id":phase,"historical_target_path":identity + ".py"}

    def completions(self, receipts):
        b=[receipt for receipt in receipts.values() if receipt["phase_id"]=="B"]
        c=[receipt for receipt in receipts.values() if receipt["phase_id"]=="C"]
        return {"governed_phase_seals":[{"phase_id":"A","completion_baseline":self.completion_a}],"successor_phase_completions":[
            {"phase_id":"B","predecessor_phase_id":"A","predecessor_completion_sha":self.completion_a,"completion_sha":self.completion_b,"receipt_ids":[x["receipt_id"] for x in b],"receipts_sha256":MODULE.canonical_digest(b)},
            {"phase_id":"C","predecessor_phase_id":"B","predecessor_completion_sha":self.completion_b,"completion_sha":self.completion_c,"receipt_ids":[x["receipt_id"] for x in c],"receipts_sha256":MODULE.canonical_digest(c)}]}

    def test_multihop_successor_completion_chain_passes(self):
        receipts={"b.py":self.receipt("b","B"),"c.py":self.receipt("c","C")}; errors=[]
        with patch.object(MODULE,"ancestor",lambda *_args: True):
            phases=MODULE.successor_phase_completions(Path("."),self.anchor,self.completion_c,self.completions(receipts),receipts,errors)
        self.assertEqual({"A":self.completion_a,"B":self.completion_b,"C":self.completion_c},phases)
        self.assertEqual([],errors)

    def test_wrong_predecessor_or_digest_is_rejected(self):
        receipts={"b.py":self.receipt("b","B"),"c.py":self.receipt("c","C")}; ledger=self.completions(receipts)
        ledger["successor_phase_completions"][1]["predecessor_completion_sha"]="0" * 40
        ledger["successor_phase_completions"][0]["receipts_sha256"]="0" * 64
        errors=[]
        with patch.object(MODULE,"ancestor",lambda *_args: True):
            MODULE.successor_phase_completions(Path("."),self.anchor,self.completion_c,ledger,receipts,errors)
        self.assertEqual(2,sum("invalid successor phase completion" in error for error in errors))

    def test_duplicate_successor_claim_is_rejected(self):
        errors=[]
        receipts=[{"historical_target_path":"x.py"},{"historical_target_path":"x.py"}]
        self.assertEqual({"x.py"},set(MODULE.successor_receipts({"successor_evolutions":receipts},errors)))
        self.assertTrue(any("duplicate successor receipt" in error for error in errors))

    def test_malformed_successor_claim_is_rejected(self):
        errors=[]
        self.assertEqual({},MODULE.successor_receipts({"successor_evolutions":[{"historical_target_path":"../x.py"}]},errors))
        self.assertTrue(any("malformed" in error for error in errors))

    def test_non_list_successor_receipts_are_rejected(self):
        errors=[]
        self.assertEqual({},MODULE.successor_receipts({"successor_evolutions":{}},errors))
        self.assertIn("successor evolutions must be a list",errors)

if __name__ == "__main__": unittest.main()
