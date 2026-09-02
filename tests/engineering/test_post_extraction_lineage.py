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
    def test_chained_split_merge_graph_and_failures(self):
        nodes=[{"node_id":"A","node_type":"BASELINE","responsibilities":["r1","r2","r3","r4"]},{"node_id":"D","node_type":"BASELINE","responsibilities":["r5","r6"]},{"node_id":"B","node_type":"CURRENT_TARGET"},{"node_id":"C","node_type":"INTERMEDIATE_TARGET"},{"node_id":"E","node_type":"CURRENT_TARGET"}]
        edges=[{"from":"A","to":"B","kind":"SPLIT","responsibilities":["r1","r2"]},{"from":"A","to":"C","kind":"SPLIT","responsibilities":["r3","r4"]},{"from":"C","to":"E","kind":"MERGE","responsibilities":["r3","r4"]},{"from":"D","to":"E","kind":"MERGE","responsibilities":["r5","r6"]}]
        self.assertEqual([],MODULE.validate_graph({"nodes":nodes,"edges":edges}))
        self.assertTrue(MODULE.validate_graph({"nodes":nodes,"edges":edges[:-1]}))
        edges.append({"from":"E","to":"A","kind":"MOVE","responsibilities":["r3"]})
        self.assertTrue(MODULE.validate_graph({"nodes":nodes,"edges":edges}))

if __name__ == "__main__": unittest.main()
