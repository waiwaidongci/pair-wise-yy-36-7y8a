import tempfile, unittest
from pathlib import Path
from src.domain import ConflictError, PermissionDenied, ValidationError
from src.repository import Repository
from src.service import Service
from src.rules import STATES, TRANSITION_ROLES

FUTURE = "2999-01-01T00:00:00+00:00"
PAST = "2000-01-01T00:00:00+00:00"


class RectificationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)
        self.item = self.service.create_item(
            {"title": "rectification item", "description": "ledger scenarios",
             "severity": "exceedance", "quantity": 12, "threshold": 6,
             "external_ref": "RECT-1"}, "creator", "operator")

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _to_assessing(self):
        item = self.service.get_item(self.item["id"], "viewer")
        return self.service.transition(item["id"], STATES[1], item["version"],
                                       "officer", TRANSITION_ROLES[STATES[1]][0])

    def _register(self, due_at=FUTURE, assignee="zhang", reviewer="wang"):
        return self.service.register_rectification(
            self.item["id"], {"assignee": assignee, "reviewer": reviewer,
                              "due_at": due_at}, "officer", "compliance_officer")

    def _to_remediation(self, due_at=FUTURE):
        current = self._to_assessing()
        rect = self._register(due_at)
        current = self.service.transition(current["id"], STATES[2], current["version"],
                                          "op", TRANSITION_ROLES[STATES[2]][0])
        return current, rect

    def test_register_validation_and_single_open(self):
        self._to_assessing()
        with self.assertRaises(PermissionDenied):
            self.service.register_rectification(
                self.item["id"], {"assignee": "a", "reviewer": "b", "due_at": FUTURE},
                "op", "operator")
        with self.assertRaises(ValidationError):
            self._register(assignee="same", reviewer="same")
        with self.assertRaises(ValidationError):
            self._register(due_at="not-a-date")
        with self.assertRaises(ValidationError):
            self._register(assignee="")
        rect = self._register()
        self.assertEqual(rect["status"], "open")
        self.assertEqual(rect["assignee"], "zhang")
        self.assertEqual(rect["reviewer"], "wang")
        with self.assertRaises(ConflictError):
            self._register()

    def test_remediation_requires_registration(self):
        current = self._to_assessing()
        with self.assertRaises(ConflictError) as ctx:
            self.service.transition(current["id"], STATES[2], current["version"],
                                    "op", TRANSITION_ROLES[STATES[2]][0])
        self.assertIn("登记", str(ctx.exception))
        self._register()
        current = self.service.transition(current["id"], STATES[2], current["version"],
                                          "op", TRANSITION_ROLES[STATES[2]][0])
        self.assertEqual(current["status"], "remediation")
        self.assertEqual(current["rectification"]["assignee"], "zhang")

    def test_overdue_without_retest_blocks_inspection(self):
        current, rect = self._to_remediation(due_at=PAST)
        with self.assertRaises(ConflictError) as ctx:
            self.service.transition(current["id"], STATES[3], current["version"],
                                    "officer", TRANSITION_ROLES[STATES[3]][0])
        self.assertIn("逾期未交复测", str(ctx.exception))

    def test_exceeding_reading_blocks_inspection(self):
        current, rect = self._to_remediation()
        retest = self.service.submit_retest(self.item["id"], rect["id"],
                                            {"reading": 9}, "li", "operator")
        self.assertEqual(retest["conclusion"], "fail")
        with self.assertRaises(ConflictError) as ctx:
            self.service.transition(current["id"], STATES[3], current["version"],
                                    "officer", TRANSITION_ROLES[STATES[3]][0])
        self.assertIn("仍高于许可限值", str(ctx.exception))

    def test_close_requires_reviewer_and_independent_pass(self):
        current, rect = self._to_remediation()
        with self.assertRaises(ConflictError):
            self.service.close_rectification(self.item["id"], rect["id"], "wang",
                                             "compliance_officer")
        self.service.submit_retest(self.item["id"], rect["id"], {"reading": 3},
                                   "zhang", "operator")
        with self.assertRaises(ConflictError) as ctx:
            self.service.close_rectification(self.item["id"], rect["id"], "wang",
                                             "compliance_officer")
        self.assertIn("责任人以外", str(ctx.exception))
        self.service.submit_retest(self.item["id"], rect["id"], {"reading": 3},
                                   "li", "operator")
        with self.assertRaises(PermissionDenied):
            self.service.close_rectification(self.item["id"], rect["id"], "li",
                                             "operator")
        closed = self.service.close_rectification(self.item["id"], rect["id"], "wang",
                                                  "compliance_officer")
        self.assertEqual(closed["status"], "closed")
        self.assertEqual(closed["closed_by"], "wang")
        current = self.service.get_item(self.item["id"], "viewer")
        current = self.service.transition(current["id"], STATES[3], current["version"],
                                          "officer", TRANSITION_ROLES[STATES[3]][0])
        self.assertEqual(current["status"], "inspection")

    def test_late_retest_fails_and_deadline_extension_rejudges(self):
        current, rect = self._to_remediation(due_at=PAST)
        retest = self.service.submit_retest(self.item["id"], rect["id"],
                                            {"reading": 3}, "li", "operator")
        self.assertEqual(retest["conclusion"], "fail")
        with self.assertRaises(PermissionDenied):
            self.service.update_deadline(self.item["id"], rect["id"],
                                         {"due_at": FUTURE}, "op", "operator")
        self.service.update_deadline(self.item["id"], rect["id"], {"due_at": FUTURE},
                                     "officer", "compliance_officer")
        history = self.service.list_retests(self.item["id"], rect["id"], "viewer")
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["status"], "invalidated")
        self.assertEqual(history[0]["conclusion"], "fail")
        self.assertEqual(history[1]["status"], "current")
        self.assertEqual(history[1]["conclusion"], "pass")
        self.assertEqual(history[1]["rejudged_from"], history[0]["id"])
        self.assertEqual(history[1]["reading"], 3)
        self.assertEqual(history[1]["submitted_by"], "li")

    def test_parameter_change_rejudges_pending_conclusions(self):
        current, rect = self._to_remediation()
        retest = self.service.submit_retest(self.item["id"], rect["id"],
                                            {"reading": 5}, "li", "operator")
        self.assertEqual(retest["conclusion"], "pass")
        current = self.service.get_item(self.item["id"], "viewer")
        self.service.update_parameters(
            self.item["id"], {"threshold": 4, "expected_version": current["version"]},
            "officer", "compliance_officer")
        history = self.service.list_retests(self.item["id"], rect["id"], "viewer")
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["status"], "invalidated")
        self.assertEqual(history[1]["conclusion"], "fail")
        self.assertEqual(history[1]["basis_threshold"], 4)
        current = self.service.get_item(self.item["id"], "viewer")
        self.service.update_parameters(
            self.item["id"], {"threshold": 6, "expected_version": current["version"]},
            "officer", "compliance_officer")
        history = self.service.list_retests(self.item["id"], rect["id"], "viewer")
        self.assertEqual(len(history), 3)
        self.assertEqual(history[-1]["conclusion"], "pass")
        with self.assertRaises(ConflictError):
            self.service.update_parameters(
                self.item["id"], {"threshold": 7, "expected_version": 1},
                "officer", "compliance_officer")

    def test_closed_rectification_conclusions_are_final(self):
        current, rect = self._to_remediation()
        self.service.submit_retest(self.item["id"], rect["id"], {"reading": 3},
                                   "li", "operator")
        self.service.close_rectification(self.item["id"], rect["id"], "wang",
                                         "compliance_officer")
        current = self.service.get_item(self.item["id"], "viewer")
        self.service.update_parameters(
            self.item["id"], {"threshold": 1, "expected_version": current["version"]},
            "officer", "compliance_officer")
        history = self.service.list_retests(self.item["id"], rect["id"], "viewer")
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["status"], "current")
        self.assertEqual(history[0]["conclusion"], "pass")
        with self.assertRaises(ConflictError):
            self.service.update_deadline(self.item["id"], rect["id"],
                                         {"due_at": PAST}, "officer",
                                         "compliance_officer")

    def test_ledger_listing_and_item_enrichment(self):
        current, rect = self._to_remediation()
        self.service.submit_retest(self.item["id"], rect["id"], {"reading": 3},
                                   "li", "operator")
        ledger = self.service.list_rectifications("viewer")
        self.assertEqual(len(ledger), 1)
        self.assertEqual(ledger[0]["item_title"], "rectification item")
        self.assertEqual(ledger[0]["latest_retest"]["conclusion"], "pass")
        open_only = self.service.list_rectifications("viewer", status="open")
        self.assertEqual(len(open_only), 1)
        self.assertEqual(self.service.list_rectifications("viewer", status="closed"), [])
        item = self.service.get_item(self.item["id"], "viewer")
        self.assertEqual(item["rectification"]["reviewer"], "wang")
        self.assertEqual(item["latest_retest"]["reading"], 3)


if __name__ == "__main__":
    unittest.main()
