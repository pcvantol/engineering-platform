from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest
from unittest.mock import patch
from engineering_platform.operational_installation_record import (
 OperationalInstallationRecordError, OperationalInstallationRecordNotFound, load, record,
)

class OperationalInstallationRecordTests(unittest.TestCase):
 def test_record_is_atomic_idempotent_and_identity_bound(self):
  with TemporaryDirectory() as temporary:
   root=Path(temporary)/"data root"; executable=Path(temporary)/"python"; executable.write_text(""); executable.chmod(0o755)
   data=dict(installation_id="instance",version="2.3.1",channel="stable",artifact_digest="sha256:"+"a"*64,source_revision="b"*40,interpreter=executable,roles={"server":"com.engineeringplatform.server"},desired_state="ACTIVE",observed_state="ACTIVE",verification={"result":"PASS"},cleanup={"result":"PENDING"})
   saved=record(root,**data); self.assertEqual(load(root),saved); self.assertEqual(record(root,**data),saved)
   with self.assertRaisesRegex(OperationalInstallationRecordError,"different identity"): record(root,**{**data,"version":"2.3.2"})

 def test_load_rejects_incomplete_or_malformed_identity(self):
  with TemporaryDirectory() as temporary:
   root=Path(temporary); path=root/"operational-installation.json"
   path.write_text(json.dumps({"schema_version":1,"installation_id":"instance"}))
   with self.assertRaisesRegex(OperationalInstallationRecordError,"invalid"): load(root)
   path.write_text(json.dumps({"schema_version":1,"installation_id":"instance","version":"2.3.1","channel":"stable","artifact_digest":"sha256:"+"Z"*64,"source_revision":"b"*40,"interpreter":"relative/python","roles":{"server":"label"},"desired_state":"ACTIVE","observed_state":"ACTIVE","verification":{},"cleanup":{}}))
   with self.assertRaisesRegex(OperationalInstallationRecordError,"invalid"): load(root)

 def test_record_preserves_the_selected_venv_launcher_not_its_base_interpreter(self):
  with TemporaryDirectory() as temporary:
   root=Path(temporary)/"data"; base=Path(temporary)/"base-python"; base.write_text(""); base.chmod(0o755)
   launcher=Path(temporary)/"venv with spaces"/"bin"/"python"; launcher.parent.mkdir(parents=True); launcher.symlink_to(base)
   saved=record(root,installation_id="instance",version="2.3.1",channel="stable",artifact_digest="sha256:"+"a"*64,source_revision="b"*40,interpreter=launcher,roles={"server":"com.engineeringplatform.server"},desired_state="ACTIVE",observed_state="ACTIVE",verification={"result":"PASS"},cleanup={"result":"COMPLETE"})
   self.assertEqual(saved["interpreter"],str(launcher.absolute()))

 def test_only_a_truly_absent_directory_entry_is_reported_missing(self):
  with TemporaryDirectory() as temporary:
   root=Path(temporary)
   with self.assertRaises(OperationalInstallationRecordNotFound): load(root)
   path=root/"operational-installation.json"; path.symlink_to(root/"missing-target")
   with self.assertRaisesRegex(OperationalInstallationRecordError,"unreadable"): load(root)

 def test_record_never_overwrites_a_file_that_wins_the_creation_race(self):
  with TemporaryDirectory() as temporary:
   root=Path(temporary)/"data"; executable=Path(temporary)/"python"; executable.write_text(""); executable.chmod(0o755)
   data=dict(installation_id="instance",version="2.3.1",channel="stable",artifact_digest="sha256:"+"a"*64,source_revision="b"*40,interpreter=executable,roles={"server":"com.engineeringplatform.server"},desired_state="ACTIVE",observed_state="ACTIVE",verification={"result":"PASS"},cleanup={"result":"PENDING"})
   def racing_link(source, destination):
    Path(destination).write_text("{corrupt", encoding="utf-8")
    raise FileExistsError
   with patch("engineering_platform.operational_installation_record.os.link", side_effect=racing_link):
    with self.assertRaisesRegex(OperationalInstallationRecordError,"unreadable"):
     record(root,**data)
   self.assertEqual((root/"operational-installation.json").read_text(encoding="utf-8"),"{corrupt")
