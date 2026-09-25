import tempfile, unittest
from pathlib import Path
from src.repository import Repository
from src.service import Service
from src.rules import STATES, TRANSITION_ROLES
FUTURE="2099-01-01T00:00:00Z"
class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.repo=Repository(str(Path(self.tmp.name)/"test.db")); self.service=Service(self.repo)
    def tearDown(self): self.repo.close(); self.tmp.cleanup()
    def test_complete_workflow_and_audit(self):
        item=self.service.create_item({"title":"workflow item","description":"complete business flow","severity":'exceedance',"quantity":12,"threshold":6,"external_ref":"WF-1"},"creator",'operator')
        self.assertEqual(item["status"],STATES[0])
        self.service.add_record(item["id"],{"kind":"evidence","detail":"evidence registered","status":"closed","external_ref":"EV-1"},"recorder",'operator')
        current=self.service.transition(item["id"],STATES[1],item["version"],"reviewer",TRANSITION_ROLES[STATES[1]][0])
        current=self.service.transition(current["id"],STATES[2],current["version"],"reviewer",TRANSITION_ROLES[STATES[2]][0],{"assignee":"worker","reviewer":"checker","due_at":FUTURE})
        remediation=self.service.list_remediations(current["id"],"viewer")[0]
        self.assertEqual(remediation["assignee"],"worker"); self.assertEqual(remediation["reviewer"],"checker"); self.assertEqual(remediation["status"],"open")
        self.service.submit_retest(current["id"],remediation["id"],{"value":3},"retester",'operator')
        self.service.close_remediation(current["id"],remediation["id"],"checker",'compliance_officer')
        current=self.service.transition(current["id"],STATES[3],current["version"],"reviewer",TRANSITION_ROLES[STATES[3]][0])
        current=self.service.transition(current["id"],STATES[4],current["version"],"reviewer",TRANSITION_ROLES[STATES[4]][0])
        self.assertEqual(current["status"],STATES[-1])
        self.assertEqual(len(self.service.list_records(current["id"],"viewer")),1)
        events=self.service.audit("viewer",current["id"]); self.assertGreaterEqual(len(events),len(STATES)+1); self.assertTrue(self.repo.verify_audit_chain())
if __name__=="__main__": unittest.main()
