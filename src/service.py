from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from .domain import (ConflictError, NotFoundError, PermissionDenied,
                     ValidationError, as_datetime, ensure_role,
                     normalize_severity, parse_timestamp, require_number,
                     require_text)
from .repository import Repository
from .rules import (AMEND_ROLES, AUDIT_ROLES, CLOSE_ROLES, CREATE_ROLES, ENTITY,
                    PARAMETER_ROLES, RECORD_ROLES, RETEST_ROLES, TITLE,
                    VIEW_ROLES, completion_blockers, escalation_required,
                    inspection_blockers, priority_score,
                    response_deadline_hours, retest_verdict,
                    role_for_transition, validate_transition)


class Service:
    def __init__(self, repository: Repository):
        self.repository = repository

    def _view(self, role: str) -> None:
        ensure_role(role, VIEW_ROLES)

    def create_item(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        title = require_text(payload.get("title"), "title", 200)
        description = require_text(payload.get("description"), "description")
        severity = normalize_severity(payload.get("severity"))
        quantity = require_number(payload.get("quantity", 0), "quantity")
        threshold = require_number(payload.get("threshold", 1), "threshold", 0.000001)
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        item = self.repository.create_item(title, description, severity, quantity,
                                           threshold, external_ref, actor)
        self.repository.append_audit("create", ENTITY, item["id"], actor, {
            "title": title, "severity": severity, "quantity": quantity,
            "priority": priority_score(severity, quantity, threshold),
        })
        return self.enrich(item)

    def add_record(self, item_id: int, payload: Dict[str, Any], actor: str,
                   role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        kind = require_text(payload.get("kind"), "kind", 100)
        detail = require_text(payload.get("detail"), "detail")
        status = payload.get("status", "open")
        if status not in ("open", "closed"):
            raise ValueError("status必须是open或closed")
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        record = self.repository.add_record(item_id, kind, detail, status,
                                            external_ref, actor)
        self.repository.append_audit("record", ENTITY, item_id, actor, {
            "record_id": record["id"], "kind": kind, "status": status,
        })
        return record

    def transition(self, item_id: int, target: str, expected_version: int,
                   actor: str, role: str,
                   remediation: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        validate_transition(item["status"], target)
        ensure_role(role, role_for_transition(target))
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        blockers = completion_blockers(target, self.repository.open_record_count(item_id))
        if target in ("inspection", "closed"):
            blockers += self._remediation_blockers(item_id, target)
        if blockers:
            raise ConflictError("；".join(blockers))
        if target == "remediation":
            plan = self._validate_remediation_plan(remediation)
            updated, entry = self.repository.register_remediation_with_transition(
                item_id, target, expected_version, plan["assignee"],
                plan["reviewer"], plan["due_at"], actor)
            self.repository.append_audit("transition", ENTITY, item_id, actor, {
                "from": item["status"], "to": target,
                "remediation": {"id": entry["id"], "assignee": entry["assignee"],
                                "reviewer": entry["reviewer"], "due_at": entry["due_at"]},
            })
            return self.enrich(updated)
        updated = self.repository.transition_item(item_id, target, expected_version, actor)
        self.repository.append_audit("transition", ENTITY, item_id, actor, {
            "from": item["status"], "to": target,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
        })
        return self.enrich(updated)

    def _remediation_blockers(self, item_id: int, target: str) -> list:
        open_remediation = self.repository.get_open_remediation(item_id)
        if open_remediation is None:
            return []
        if target == "closed":
            return ["仍有未结束整改"]
        assessment = self.repository.current_assessment(open_remediation["id"])
        return inspection_blockers(open_remediation, assessment,
                                   datetime.now(timezone.utc))

    @staticmethod
    def _validate_remediation_plan(plan: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        if not isinstance(plan, dict):
            raise ValidationError("进入整改前必须登记责任人、复核人与到期时间")
        assignee = require_text(plan.get("assignee"), "assignee", 100)
        reviewer = require_text(plan.get("reviewer"), "reviewer", 100)
        if assignee == reviewer:
            raise ValidationError("责任人与复核人不能为同一人")
        due_at = parse_timestamp(plan.get("due_at"), "due_at")
        return {"assignee": assignee, "reviewer": reviewer, "due_at": due_at}

    def _owned_remediation(self, item_id: int, remediation_id: int) -> Dict[str, Any]:
        remediation = self.repository.get_remediation(remediation_id)
        if remediation["item_id"] != item_id:
            raise NotFoundError("整改记录不存在")
        return remediation

    def submit_retest(self, item_id: int, remediation_id: int,
                      payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, RETEST_ROLES)
        actor = require_text(actor, "actor", 100)
        value = require_number(payload.get("value"), "value")
        remediation = self._owned_remediation(item_id, remediation_id)
        if remediation["status"] != "open":
            raise ConflictError("整改已结束")
        if actor == remediation["assignee"]:
            raise PermissionDenied("复测须换人进行，责任人不能复测本人整改")
        item = self.repository.get_item(item_id)
        verdict = retest_verdict(value, item["threshold"])
        assessment = self.repository.submit_retest(
            remediation_id, value, verdict, item["quantity"], item["threshold"],
            remediation["due_at"], actor)
        self.repository.append_audit("retest", ENTITY, item_id, actor, {
            "remediation_id": remediation_id, "assessment_id": assessment["id"],
            "value": value, "verdict": verdict,
        })
        return assessment

    def amend_remediation(self, item_id: int, remediation_id: int,
                          payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, AMEND_ROLES)
        actor = require_text(actor, "actor", 100)
        due_at = parse_timestamp(payload.get("due_at"), "due_at")
        self._owned_remediation(item_id, remediation_id)
        remediation, rejudged = self.repository.amend_remediation_due(
            remediation_id, due_at, actor)
        self.repository.append_audit("remediation_amend", ENTITY, item_id, actor, {
            "remediation_id": remediation_id, "due_at": due_at,
            "rejudgments": rejudged,
        })
        return self._ledger_entry(remediation)

    def close_remediation(self, item_id: int, remediation_id: int, actor: str,
                          role: str) -> Dict[str, Any]:
        ensure_role(role, CLOSE_ROLES)
        actor = require_text(actor, "actor", 100)
        remediation = self._owned_remediation(item_id, remediation_id)
        if remediation["status"] != "open":
            raise ConflictError("整改已结束")
        if actor != remediation["reviewer"]:
            raise PermissionDenied("仅复核人可关闭整改")
        assessment = self.repository.current_assessment(remediation_id)
        if assessment is None or assessment["verdict"] != "pass":
            raise ConflictError("复测未合格，不能关闭整改")
        closed = self.repository.close_remediation(remediation_id, actor)
        self.repository.append_audit("remediation_close", ENTITY, item_id, actor, {
            "remediation_id": remediation_id, "assessment_id": assessment["id"],
        })
        return self._ledger_entry(closed)

    def update_parameters(self, item_id: int, payload: Dict[str, Any], actor: str,
                          role: str) -> Dict[str, Any]:
        ensure_role(role, PARAMETER_ROLES)
        actor = require_text(actor, "actor", 100)
        quantity = require_number(payload.get("quantity"), "quantity")
        threshold = require_number(payload.get("threshold"), "threshold", 0.000001)
        expected = payload.get("expected_version")
        if not isinstance(expected, int) or expected < 1:
            raise ValueError("expected_version必须是正整数")
        item, previous = self.repository.update_item_parameters(
            item_id, quantity, threshold, expected, actor)
        self.repository.append_audit("parameters", ENTITY, item_id, actor, {
            "quantity": quantity, "threshold": threshold,
            "previous_quantity": previous["quantity"],
            "previous_threshold": previous["threshold"],
            "rejudgments": previous["rejudgments"],
        })
        return self.enrich(item)

    def get_item(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.enrich(self.repository.get_item(item_id))

    def list_items(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        return [self.enrich(item) for item in self.repository.list_items(status)]

    def list_records(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_records(item_id)

    def list_remediations(self, item_id: int, role: str) -> list:
        self._view(role)
        return [self._ledger_entry(remediation)
                for remediation in self.repository.list_remediations(item_id)]

    def get_remediation(self, item_id: int, remediation_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self._ledger_entry(self._owned_remediation(item_id, remediation_id))

    def _ledger_entry(self, remediation: Dict[str, Any]) -> Dict[str, Any]:
        entry = dict(remediation)
        entry["assessments"] = self.repository.list_assessments(remediation["id"])
        current = self.repository.current_assessment(remediation["id"])
        entry["current_verdict"] = current["verdict"] if current else None
        entry["overdue"] = (remediation["status"] == "open"
                            and datetime.now(timezone.utc) > as_datetime(remediation["due_at"]))
        return entry

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

    @staticmethod
    def enrich(item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"])
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        return result
