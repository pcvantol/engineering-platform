"""Fail-closed responsibility partition canaries for P-PROV."""
import importlib.util
from pathlib import Path
import unittest

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

if __name__ == "__main__": unittest.main()
