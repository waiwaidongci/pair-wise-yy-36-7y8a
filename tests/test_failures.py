import tempfile, unittest
from pathlib import Path
from src.domain import ConflictError, PermissionDenied
from src.repository import Repository
from src.service import Service
from src.rules import STATES, TRANSITION_ROLES
class FailureTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.repo=Repository(str(Path(self.tmp.name)/"test.db")); self.service=Service(self.repo)
        self.item=self.service.create_item({"title":"failure item","description":"failure scenarios","severity":'exceedance',"quantity":5,"threshold":10,"external_ref":"FAIL-1"},"creator",'operator')
    def tearDown(self): self.repo.close(); self.tmp.cleanup()
    def test_permission_version_duplicate_and_invariant(self):
        with self.assertRaises(PermissionDenied): self.service.transition(self.item["id"],STATES[1],1,"attacker","viewer")
        with self.assertRaises(ConflictError): self.service.transition(self.item["id"],STATES[1],99,"reviewer",TRANSITION_ROLES[STATES[1]][0])
        payload={"kind":"action","detail":"same reference","status":"open","external_ref":"DUP-1"}
        self.service.add_record(self.item["id"],payload,"recorder",'operator')
        with self.assertRaises(ConflictError): self.service.add_record(self.item["id"],payload,"recorder",'operator')
        current=self.service.get_item(self.item["id"],"viewer")
        current=self.service.transition(current["id"],STATES[1],current["version"],"reviewer",TRANSITION_ROLES[STATES[1]][0])
        rect=self.service.register_rectification(self.item["id"],{"assignee":"zhang","reviewer":"wang","due_at":"2999-01-01T00:00:00+00:00"},"officer",'compliance_officer')
        current=self.service.transition(current["id"],STATES[2],current["version"],"reviewer",TRANSITION_ROLES[STATES[2]][0])
        self.service.submit_retest(self.item["id"],rect["id"],{"reading":1},"li",'operator')
        self.service.close_rectification(self.item["id"],rect["id"],"wang",'compliance_officer')
        current=self.service.transition(current["id"],STATES[3],current["version"],"reviewer",TRANSITION_ROLES[STATES[3]][0])
        with self.assertRaises(ConflictError): self.service.transition(current["id"],STATES[-1],current["version"],"reviewer",TRANSITION_ROLES[STATES[-1]][0])
if __name__=="__main__": unittest.main()
