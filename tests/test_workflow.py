import tempfile, unittest
from pathlib import Path
from src.repository import Repository
from src.service import Service
from src.rules import STATES, TRANSITION_ROLES
class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.repo=Repository(str(Path(self.tmp.name)/"test.db")); self.service=Service(self.repo)
    def tearDown(self): self.repo.close(); self.tmp.cleanup()
    def test_complete_workflow_and_audit(self):
        item=self.service.create_item({"title":"workflow item","description":"complete business flow","severity":'exceedance',"quantity":12,"threshold":6,"external_ref":"WF-1"},"creator",'operator')
        self.assertEqual(item["status"],STATES[0])
        self.service.add_record(item["id"],{"kind":"evidence","detail":"evidence registered","status":"closed","external_ref":"EV-1"},"recorder",'operator')
        current=self.service.transition(item["id"],STATES[1],item["version"],"reviewer",TRANSITION_ROLES[STATES[1]][0])
        rect=self.service.register_rectification(item["id"],{"assignee":"zhang","reviewer":"wang","due_at":"2999-01-01T00:00:00+00:00"},"officer",'compliance_officer')
        current=self.service.transition(item["id"],STATES[2],current["version"],"reviewer",TRANSITION_ROLES[STATES[2]][0])
        self.service.submit_retest(item["id"],rect["id"],{"reading":3},"li",'operator')
        self.service.close_rectification(item["id"],rect["id"],"wang",'compliance_officer')
        for target in STATES[3:]:
            current=self.service.transition(current["id"],target,current["version"],"reviewer",TRANSITION_ROLES[target][0])
        self.assertEqual(current["status"],STATES[-1])
        self.assertEqual(len(self.service.list_records(current["id"],"viewer")),1)
        events=self.service.audit("viewer",current["id"]); self.assertGreaterEqual(len(events),len(STATES)+1); self.assertTrue(self.repo.verify_audit_chain())
if __name__=="__main__": unittest.main()
