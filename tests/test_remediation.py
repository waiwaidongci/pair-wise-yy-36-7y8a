import tempfile, unittest
from pathlib import Path
from src.domain import ConflictError, PermissionDenied, ValidationError
from src.repository import Repository
from src.service import Service
from src.rules import STATES, TRANSITION_ROLES
FUTURE="2099-01-01T00:00:00Z"; PAST="2000-01-01T00:00:00Z"
class RemediationTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.repo=Repository(str(Path(self.tmp.name)/"test.db")); self.service=Service(self.repo)
        self.item=self.service.create_item({"title":"remediation item","description":"ledger scenarios","severity":'exceedance',"quantity":12,"threshold":6,"external_ref":"REM-1"},"creator",'operator')
    def tearDown(self): self.repo.close(); self.tmp.cleanup()
    def _assessing(self):
        return self.service.transition(self.item["id"],STATES[1],self.item["version"],"officer",TRANSITION_ROLES[STATES[1]][0])
    def _remediation(self,due_at=FUTURE,assignee="worker",reviewer="checker"):
        current=self._assessing()
        current=self.service.transition(current["id"],STATES[2],current["version"],"operator",TRANSITION_ROLES[STATES[2]][0],{"assignee":assignee,"reviewer":reviewer,"due_at":due_at})
        return current,self.service.list_remediations(current["id"],"viewer")[0]
    def test_registration_required_before_remediation(self):
        current=self._assessing()
        with self.assertRaises(ValidationError): self.service.transition(current["id"],STATES[2],current["version"],"operator",TRANSITION_ROLES[STATES[2]][0])
        with self.assertRaises(ValidationError): self.service.transition(current["id"],STATES[2],current["version"],"operator",TRANSITION_ROLES[STATES[2]][0],{"assignee":"same","reviewer":"same","due_at":FUTURE})
        with self.assertRaises(ValidationError): self.service.transition(current["id"],STATES[2],current["version"],"operator",TRANSITION_ROLES[STATES[2]][0],{"assignee":"worker","reviewer":"checker","due_at":"not-a-date"})
    def test_single_open_remediation_per_item(self):
        current,remediation=self._remediation()
        self.assertEqual(remediation["status"],"open")
        with self.assertRaises(ConflictError): self.repo.register_remediation_with_transition(current["id"],STATES[2],current["version"],"a","b",FUTURE,"operator")
    def test_overdue_without_retest_blocks_inspection(self):
        current,remediation=self._remediation(due_at=PAST)
        self.assertTrue(self.service.get_remediation(current["id"],remediation["id"],"viewer")["overdue"])
        with self.assertRaisesRegex(ConflictError,"逾期未交复测"): self.service.transition(current["id"],STATES[3],current["version"],"officer",TRANSITION_ROLES[STATES[3]][0])
    def test_failing_retest_blocks_inspection(self):
        current,remediation=self._remediation()
        assessment=self.service.submit_retest(current["id"],remediation["id"],{"value":9},"retester",'operator')
        self.assertEqual(assessment["verdict"],"fail")
        with self.assertRaisesRegex(ConflictError,"新监测读数仍高于许可限值"): self.service.transition(current["id"],STATES[3],current["version"],"officer",TRANSITION_ROLES[STATES[3]][0])
    def test_retest_must_be_by_another_person(self):
        current,remediation=self._remediation()
        with self.assertRaises(PermissionDenied): self.service.submit_retest(current["id"],remediation["id"],{"value":3},"worker",'operator')
    def test_close_requires_pass_and_reviewer(self):
        current,remediation=self._remediation()
        with self.assertRaises(ConflictError): self.service.close_remediation(current["id"],remediation["id"],"checker",'compliance_officer')
        self.service.submit_retest(current["id"],remediation["id"],{"value":3},"retester",'operator')
        with self.assertRaises(PermissionDenied): self.service.close_remediation(current["id"],remediation["id"],"worker",'operator')
        closed=self.service.close_remediation(current["id"],remediation["id"],"checker",'compliance_officer')
        self.assertEqual(closed["status"],"closed"); self.assertEqual(closed["closed_by"],"checker")
        moved=self.service.transition(current["id"],STATES[3],current["version"],"officer",TRANSITION_ROLES[STATES[3]][0])
        self.assertEqual(moved["status"],STATES[3])
    def test_parameter_change_invalidates_and_rejudges(self):
        current,remediation=self._remediation()
        self.service.submit_retest(current["id"],remediation["id"],{"value":5},"retester",'operator')
        updated=self.service.update_parameters(current["id"],{"quantity":12,"threshold":4,"expected_version":current["version"]},"officer",'compliance_officer')
        self.assertEqual(updated["threshold"],4.0)
        entry=self.service.get_remediation(current["id"],remediation["id"],"viewer")
        self.assertEqual(entry["current_verdict"],"fail")
        statuses=[a["status"] for a in entry["assessments"]]; kinds=[a["kind"] for a in entry["assessments"]]
        self.assertEqual(statuses,["invalidated","valid"]); self.assertEqual(kinds,["retest","rejudgment"])
        self.assertEqual(entry["assessments"][0]["basis_threshold"],6.0); self.assertEqual(entry["assessments"][1]["basis_threshold"],4.0)
        with self.assertRaises(ConflictError): self.service.close_remediation(current["id"],remediation["id"],"checker",'compliance_officer')
        with self.assertRaisesRegex(ConflictError,"新监测读数仍高于许可限值"): self.service.transition(current["id"],STATES[3],updated["version"],"officer",TRANSITION_ROLES[STATES[3]][0])
    def test_amend_due_at_invalidates_and_rejudges(self):
        current,remediation=self._remediation()
        self.service.submit_retest(current["id"],remediation["id"],{"value":5},"retester",'operator')
        amended=self.service.amend_remediation(current["id"],remediation["id"],{"due_at":"2099-06-01T00:00:00Z"},"officer",'compliance_officer')
        self.assertEqual(amended["due_at"],"2099-06-01T00:00:00+00:00")
        kinds=[a["kind"] for a in amended["assessments"]]; self.assertEqual(kinds,["retest","rejudgment"])
        self.assertEqual(amended["assessments"][1]["basis_due_at"],"2099-06-01T00:00:00+00:00")
        with self.assertRaises(PermissionDenied): self.service.amend_remediation(current["id"],remediation["id"],{"due_at":FUTURE},"officer",'operator')
    def test_update_parameters_version_conflict(self):
        current,remediation=self._remediation()
        with self.assertRaises(ConflictError): self.service.update_parameters(current["id"],{"quantity":1,"threshold":2,"expected_version":99},"officer",'compliance_officer')
    def test_ledger_keeps_history_queryable(self):
        current,remediation=self._remediation()
        self.service.submit_retest(current["id"],remediation["id"],{"value":8},"retester",'operator')
        self.service.submit_retest(current["id"],remediation["id"],{"value":5},"retester",'operator')
        entry=self.service.get_remediation(current["id"],remediation["id"],"viewer")
        self.assertEqual([a["status"] for a in entry["assessments"]],["superseded","valid"])
        self.assertEqual(entry["current_verdict"],"pass")
        self.assertEqual(len(self.service.list_remediations(current["id"],"viewer")),1)
if __name__=="__main__": unittest.main()
