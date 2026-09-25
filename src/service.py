from __future__ import annotations

from typing import Any, Dict, List, Optional

from .audit import utc_now_dt
from .domain import (ConflictError, NotFoundError, PermissionDenied, ValidationError,
                     ensure_role, format_timestamp, normalize_severity,
                     parse_timestamp, require_number, require_text)
from .repository import Repository
from .rules import (AUDIT_ROLES, CREATE_ROLES, DEADLINE_ROLES, ENTITY,
                    PARAMETER_ROLES, RECORD_ROLES, RECTIFICATION_ENTITY,
                    REGISTER_ROLES, REGISTER_STATES, RETEST_ROLES, TITLE,
                    VIEW_ROLES, closure_blockers, completion_blockers,
                    escalation_required, inspection_blockers, judge_retest,
                    priority_score, response_deadline_hours, role_for_transition,
                    validate_transition)


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
                   actor: str, role: str) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        validate_transition(item["status"], target)
        ensure_role(role, role_for_transition(target))
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        if target == "remediation" and not self.repository.get_open_rectification(item_id):
            raise ConflictError("进入整改前须登记责任人、复核人与到期时间")
        if target == "inspection":
            rectification = self.repository.get_open_rectification(item_id)
            retests = (self.repository.current_retests(rectification["id"])
                       if rectification else [])
            blockers = inspection_blockers(rectification, retests, item["threshold"],
                                           utc_now_dt())
            if blockers:
                raise ConflictError("；".join(blockers))
        blockers = completion_blockers(
            target, self.repository.open_record_count(item_id),
            1 if self.repository.get_open_rectification(item_id) else 0)
        if blockers:
            raise ConflictError("；".join(blockers))
        updated = self.repository.transition_item(item_id, target, expected_version, actor)
        self.repository.append_audit("transition", ENTITY, item_id, actor, {
            "from": item["status"], "to": target,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
        })
        return self.enrich(updated)

    def get_item(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.enrich(self.repository.get_item(item_id))

    def list_items(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        return [self.enrich(item) for item in self.repository.list_items(status)]

    def list_records(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_records(item_id)

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

    def register_rectification(self, item_id: int, payload: Dict[str, Any],
                               actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, REGISTER_ROLES)
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        if item["status"] not in REGISTER_STATES:
            raise ConflictError("当前状态不能登记整改")
        assignee = require_text(payload.get("assignee"), "assignee", 100)
        reviewer = require_text(payload.get("reviewer"), "reviewer", 100)
        if assignee == reviewer:
            raise ValidationError("复核人不能与责任人相同")
        due_at = format_timestamp(parse_timestamp(payload.get("due_at"), "due_at"))
        rectification = self.repository.create_rectification(
            item_id, assignee, reviewer, due_at, actor)
        self.repository.append_audit("register_rectification", RECTIFICATION_ENTITY,
                                     item_id, actor, {
                                         "rectification_id": rectification["id"],
                                         "assignee": assignee, "reviewer": reviewer,
                                         "due_at": due_at,
                                     })
        return rectification

    def submit_retest(self, item_id: int, rectification_id: int, payload: Dict[str, Any],
                      actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, RETEST_ROLES)
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        rectification = self._open_rectification(item_id, rectification_id)
        if item["status"] != "remediation":
            raise ConflictError("仅整改阶段可提交复测")
        reading = require_number(payload.get("reading"), "reading")
        now = utc_now_dt()
        conclusion = judge_retest(reading, item["threshold"], now,
                                  parse_timestamp(rectification["due_at"], "due_at"))
        retest = self.repository.add_retest(
            rectification_id, reading, conclusion, item["threshold"], item["quantity"],
            rectification["due_at"], actor, format_timestamp(now))
        self.repository.append_audit("retest", RECTIFICATION_ENTITY, item_id, actor, {
            "rectification_id": rectification_id, "retest_id": retest["id"],
            "reading": reading, "conclusion": conclusion,
        })
        return retest

    def close_rectification(self, item_id: int, rectification_id: int,
                            actor: str, role: str) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        rectification = self._open_rectification(item_id, rectification_id)
        if item["status"] != "remediation":
            raise ConflictError("仅整改阶段可关闭整改")
        if actor != rectification["reviewer"]:
            raise PermissionDenied("须由登记的复核人关闭整改")
        blockers = closure_blockers(rectification,
                                    self.repository.current_retests(rectification_id))
        if blockers:
            raise ConflictError("；".join(blockers))
        closed = self.repository.close_rectification(rectification_id, actor)
        self.repository.append_audit("close_rectification", RECTIFICATION_ENTITY,
                                     item_id, actor,
                                     {"rectification_id": rectification_id})
        return closed

    def update_deadline(self, item_id: int, rectification_id: int, payload: Dict[str, Any],
                        actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, DEADLINE_ROLES)
        actor = require_text(actor, "actor", 100)
        rectification = self._open_rectification(item_id, rectification_id)
        due_at = format_timestamp(parse_timestamp(payload.get("due_at"), "due_at"))
        if due_at == rectification["due_at"]:
            return rectification
        updated = self.repository.update_rectification_deadline(rectification_id, due_at)
        rejudged = self._rejudge(item_id, "deadline")
        self.repository.append_audit("update_deadline", RECTIFICATION_ENTITY, item_id,
                                     actor, {
                                         "rectification_id": rectification_id,
                                         "previous": rectification["due_at"],
                                         "due_at": due_at, "rejudged": len(rejudged),
                                     })
        return updated

    def update_parameters(self, item_id: int, payload: Dict[str, Any],
                          actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, PARAMETER_ROLES)
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        if item["status"] == "closed":
            raise ConflictError("事件已关闭，不能修改限值或浓度")
        quantity = require_number(payload.get("quantity", item["quantity"]), "quantity")
        threshold = require_number(payload.get("threshold", item["threshold"]),
                                   "threshold", 0.000001)
        expected = payload.get("expected_version")
        if not isinstance(expected, int) or expected < 1:
            raise ValueError("expected_version必须是正整数")
        if quantity == item["quantity"] and threshold == item["threshold"]:
            return self.enrich(item)
        updated = self.repository.update_item_parameters(
            item_id, quantity, threshold, expected, actor)
        rejudged = self._rejudge(item_id, "parameters")
        self.repository.append_audit("update_parameters", ENTITY, item_id, actor, {
            "quantity": {"from": item["quantity"], "to": quantity},
            "threshold": {"from": item["threshold"], "to": threshold},
            "rejudged": len(rejudged),
        })
        return self.enrich(updated)

    def list_rectifications(self, role: str, item_id: Optional[int] = None,
                            status: Optional[str] = None) -> list:
        self._view(role)
        if status is not None and status not in ("open", "closed"):
            raise ValidationError("status必须是open或closed")
        rows = self.repository.list_rectifications(item_id, status)
        return [self._enrich_rectification(row) for row in rows]

    def list_retests(self, item_id: int, rectification_id: int, role: str) -> list:
        self._view(role)
        rectification = self.repository.get_rectification(rectification_id)
        if rectification["item_id"] != item_id:
            raise NotFoundError("整改记录不存在")
        return self.repository.list_retests(rectification_id)

    def _open_rectification(self, item_id: int, rectification_id: int) -> Dict[str, Any]:
        rectification = self.repository.get_rectification(rectification_id)
        if rectification["item_id"] != item_id:
            raise NotFoundError("整改记录不存在")
        if rectification["status"] != "open":
            raise ConflictError("整改已关闭")
        return rectification

    def _rejudge(self, item_id: int, trigger: str) -> List[Dict[str, Any]]:
        rectification = self.repository.get_open_rectification(item_id)
        if not rectification:
            return []
        current = self.repository.current_retests(rectification["id"])
        if not current:
            return []
        item = self.repository.get_item(item_id)
        due_at = parse_timestamp(rectification["due_at"], "due_at")
        judgments = [{
            "old_id": retest["id"],
            "conclusion": judge_retest(
                retest["reading"], item["threshold"],
                parse_timestamp(retest["submitted_at"], "submitted_at"), due_at),
            "basis_threshold": item["threshold"],
            "basis_quantity": item["quantity"],
            "basis_due_at": rectification["due_at"],
        } for retest in current]
        results = self.repository.rejudge_retests(rectification["id"], judgments)
        self.repository.append_audit("rejudge", RECTIFICATION_ENTITY, item_id, "system", {
            "rectification_id": rectification["id"], "trigger": trigger,
            "invalidated": [judgment["old_id"] for judgment in judgments],
            "conclusions": [{"retest_id": row["id"], "conclusion": row["conclusion"]}
                            for row in results],
        })
        return results

    def _enrich_rectification(self, rectification: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(rectification)
        item = self.repository.get_item(rectification["item_id"])
        result["item_title"] = item["title"]
        result["item_status"] = item["status"]
        current = self.repository.current_retests(rectification["id"])
        result["latest_retest"] = current[-1] if current else None
        return result

    def enrich(self, item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"])
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        rectification = self.repository.get_open_rectification(item["id"])
        result["rectification"] = rectification
        if rectification:
            current = self.repository.current_retests(rectification["id"])
            result["latest_retest"] = current[-1] if current else None
        else:
            result["latest_retest"] = None
        return result
