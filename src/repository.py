from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .domain import ConflictError, NotFoundError
from .rules import ID_PREFIX, STATES, retest_verdict


class Repository:
    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._create_schema()

    def _create_schema(self) -> None:
        statuses = ",".join("'" + s.replace("'", "''") + "'" for s in STATES)
        with self.conn:
            self.conn.executescript(f"""
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    quantity REAL NOT NULL DEFAULT 0,
                    threshold REAL NOT NULL DEFAULT 1,
                    status TEXT NOT NULL CHECK(status IN ({statuses})),
                    version INTEGER NOT NULL DEFAULT 1,
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_items_external_ref
                    ON items(external_ref) WHERE external_ref IS NOT NULL;
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK(status IN ('open','closed')),
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, external_ref)
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER NOT NULL,
                    actor TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    entry_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS remediations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    assignee TEXT NOT NULL,
                    reviewer TEXT NOT NULL,
                    due_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK(status IN ('open','closed')),
                    closed_by TEXT,
                    closed_at TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_remediations_one_open
                    ON remediations(item_id) WHERE status='open';
                CREATE TABLE IF NOT EXISTS remediation_assessments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    remediation_id INTEGER NOT NULL REFERENCES remediations(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL CHECK(kind IN ('retest','rejudgment')),
                    value REAL NOT NULL,
                    verdict TEXT NOT NULL CHECK(verdict IN ('pass','fail')),
                    basis_quantity REAL NOT NULL,
                    basis_threshold REAL NOT NULL,
                    basis_due_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'valid'
                        CHECK(status IN ('valid','invalidated','superseded')),
                    actor TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
            """)

    @staticmethod
    def _item(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    def create_item(self, title: str, description: str, severity: str,
                    quantity: float, threshold: float, external_ref: Optional[str],
                    actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO items(title, description, severity, quantity, threshold,
                       status, version, external_ref, created_by, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (title, description, severity, quantity, threshold, STATES[0], 1,
                     external_ref, actor, now, now),
                )
                item_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("external_ref已存在") from exc
        return self.get_item(item_id)

    def get_item(self, item_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise NotFoundError("项目不存在")
        return self._item(row)

    def list_items(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM items"
        params: tuple = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY id DESC"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [self._item(row) for row in rows]

    def transition_item(self, item_id: int, target: str, expected_version: int,
                        actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE items SET status=?, version=version+1, updated_at=?
                   WHERE id=? AND version=?""",
                (target, now, item_id, expected_version),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("项目不存在")
                raise ConflictError("版本冲突，请刷新后重试")
        return self.get_item(item_id)

    def add_record(self, item_id: int, kind: str, detail: str, status: str,
                   external_ref: Optional[str], actor: str) -> Dict[str, Any]:
        now = utc_now()
        self.get_item(item_id)
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO records(item_id, kind, detail, status, external_ref,
                       created_by, created_at) VALUES(?,?,?,?,?,?,?)""",
                    (item_id, kind, detail, status, external_ref, actor, now),
                )
                record_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("记录唯一标识已存在") from exc
        with self._lock:
            row = self.conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        return dict(row)

    def list_records(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM records WHERE item_id=? ORDER BY id", (item_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def open_record_count(self, item_id: int) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM records WHERE item_id=? AND status='open'",
                (item_id,),
            ).fetchone()
        return int(row["n"])

    def register_remediation_with_transition(self, item_id: int, target: str,
                                             expected_version: int, assignee: str,
                                             reviewer: str, due_at: str,
                                             actor: str) -> tuple:
        now = utc_now()
        with self._lock, self.conn:
            exists = self.conn.execute(
                "SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone()
            if exists is None:
                raise NotFoundError("项目不存在")
            try:
                cur = self.conn.execute(
                    """INSERT INTO remediations(item_id, assignee, reviewer, due_at,
                       status, created_by, created_at) VALUES(?,?,?,?,'open',?,?)""",
                    (item_id, assignee, reviewer, due_at, actor, now),
                )
                remediation_id = int(cur.lastrowid)
            except sqlite3.IntegrityError as exc:
                raise ConflictError("同一事件只留一项未结束整改") from exc
            cur = self.conn.execute(
                """UPDATE items SET status=?, version=version+1, updated_at=?
                   WHERE id=? AND version=?""",
                (target, now, item_id, expected_version),
            )
            if cur.rowcount == 0:
                raise ConflictError("版本冲突，请刷新后重试")
        return self.get_item(item_id), self.get_remediation(remediation_id)

    def get_remediation(self, remediation_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM remediations WHERE id=?", (remediation_id,)).fetchone()
        if row is None:
            raise NotFoundError("整改记录不存在")
        return dict(row)

    def get_open_remediation(self, item_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM remediations WHERE item_id=? AND status='open'",
                (item_id,)).fetchone()
        return dict(row) if row else None

    def list_remediations(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM remediations WHERE item_id=? ORDER BY id", (item_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def get_assessment(self, assessment_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM remediation_assessments WHERE id=?",
                (assessment_id,)).fetchone()
        if row is None:
            raise NotFoundError("复测记录不存在")
        return dict(row)

    def current_assessment(self, remediation_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                """SELECT * FROM remediation_assessments
                   WHERE remediation_id=? AND status='valid' ORDER BY id DESC LIMIT 1""",
                (remediation_id,)).fetchone()
        return dict(row) if row else None

    def list_assessments(self, remediation_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                """SELECT * FROM remediation_assessments
                   WHERE remediation_id=? ORDER BY id""", (remediation_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def submit_retest(self, remediation_id: int, value: float, verdict: str,
                      basis_quantity: float, basis_threshold: float,
                      basis_due_at: str, actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT status FROM remediations WHERE id=?", (remediation_id,)).fetchone()
            if row is None:
                raise NotFoundError("整改记录不存在")
            if row["status"] != "open":
                raise ConflictError("整改已结束")
            self.conn.execute(
                """UPDATE remediation_assessments SET status='superseded'
                   WHERE remediation_id=? AND status='valid'""", (remediation_id,))
            cur = self.conn.execute(
                """INSERT INTO remediation_assessments(remediation_id, kind, value,
                   verdict, basis_quantity, basis_threshold, basis_due_at, status,
                   actor, created_at) VALUES(?,?,?,?,?,?,?,'valid',?,?)""",
                (remediation_id, "retest", value, verdict, basis_quantity,
                 basis_threshold, basis_due_at, actor, now),
            )
            assessment_id = int(cur.lastrowid)
        return self.get_assessment(assessment_id)

    def _rejudge_locked(self, remediation: Dict[str, Any], quantity: float,
                        threshold: float, due_at: str, actor: str,
                        now: str) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            """SELECT * FROM remediation_assessments
               WHERE remediation_id=? AND status='valid'""",
            (remediation["id"],)).fetchall()
        rejudged = []
        for row in rows:
            self.conn.execute(
                "UPDATE remediation_assessments SET status='invalidated' WHERE id=?",
                (row["id"],))
            verdict = retest_verdict(row["value"], threshold)
            cur = self.conn.execute(
                """INSERT INTO remediation_assessments(remediation_id, kind, value,
                   verdict, basis_quantity, basis_threshold, basis_due_at, status,
                   actor, created_at) VALUES(?,'rejudgment',?,?,?,?,?,'valid',?,?)""",
                (remediation["id"], row["value"], verdict, quantity, threshold,
                 due_at, actor, now),
            )
            rejudged.append({"previous_assessment_id": row["id"],
                             "assessment_id": int(cur.lastrowid),
                             "value": row["value"], "verdict": verdict})
        return rejudged

    def amend_remediation_due(self, remediation_id: int, due_at: str,
                              actor: str) -> tuple:
        now = utc_now()
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT * FROM remediations WHERE id=?", (remediation_id,)).fetchone()
            if row is None:
                raise NotFoundError("整改记录不存在")
            remediation = dict(row)
            if remediation["status"] != "open":
                raise ConflictError("整改已结束")
            rejudged = []
            if remediation["due_at"] != due_at:
                item = self.conn.execute(
                    "SELECT quantity, threshold FROM items WHERE id=?",
                    (remediation["item_id"],)).fetchone()
                self.conn.execute(
                    "UPDATE remediations SET due_at=? WHERE id=?",
                    (due_at, remediation_id))
                rejudged = self._rejudge_locked(
                    remediation, item["quantity"], item["threshold"], due_at, actor, now)
        return self.get_remediation(remediation_id), rejudged

    def close_remediation(self, remediation_id: int, actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE remediations SET status='closed', closed_by=?, closed_at=?
                   WHERE id=? AND status='open'""",
                (actor, now, remediation_id),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute(
                    "SELECT 1 FROM remediations WHERE id=?",
                    (remediation_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("整改记录不存在")
                raise ConflictError("整改已结束")
        return self.get_remediation(remediation_id)

    def update_item_parameters(self, item_id: int, quantity: float, threshold: float,
                               expected_version: int, actor: str) -> tuple:
        now = utc_now()
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT quantity, threshold FROM items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                raise NotFoundError("项目不存在")
            previous = {"quantity": row["quantity"], "threshold": row["threshold"]}
            cur = self.conn.execute(
                """UPDATE items SET quantity=?, threshold=?, version=version+1,
                   updated_at=? WHERE id=? AND version=?""",
                (quantity, threshold, now, item_id, expected_version),
            )
            if cur.rowcount == 0:
                raise ConflictError("版本冲突，请刷新后重试")
            rejudged = []
            if previous["quantity"] != quantity or previous["threshold"] != threshold:
                open_rows = self.conn.execute(
                    "SELECT * FROM remediations WHERE item_id=? AND status='open'",
                    (item_id,)).fetchall()
                for remediation in open_rows:
                    rejudged.extend(self._rejudge_locked(
                        dict(remediation), quantity, threshold,
                        remediation["due_at"], actor, now))
            previous["rejudgments"] = rejudged
        return self.get_item(item_id), previous

    def append_audit(self, action: str, entity_type: str, entity_id: int,
                     actor: str, detail: dict) -> Dict[str, Any]:
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT entry_hash FROM audit_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
            previous = row["entry_hash"] if row else "GENESIS"
            event = make_entry(action, entity_type, entity_id, actor, detail, previous)
            cur = self.conn.execute(
                """INSERT INTO audit_events(action, entity_type, entity_id, actor, detail,
                   previous_hash, entry_hash, created_at) VALUES(?,?,?,?,?,?,?,?)""",
                (event["action"], event["entity_type"], event["entity_id"], event["actor"],
                 json.dumps(event["detail"], ensure_ascii=False, sort_keys=True),
                 event["previous_hash"], event["entry_hash"], event["created_at"]),
            )
            event_id = int(cur.lastrowid)
        event["id"] = event_id
        return event

    def list_audit(self, entity_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM audit_events"
        params: tuple = ()
        if entity_id is not None:
            sql += " WHERE entity_id=?"
            params = (entity_id,)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item["detail"])
            result.append(item)
        return result

    def verify_audit_chain(self) -> bool:
        from .audit import calculate_hash
        with self._lock:
            rows = self.conn.execute("SELECT * FROM audit_events ORDER BY id").fetchall()
        previous = "GENESIS"
        for row in rows:
            if row["previous_hash"] != previous:
                return False
            payload = {
                "action": row["action"], "entity_type": row["entity_type"],
                "entity_id": row["entity_id"], "actor": row["actor"],
                "detail": json.loads(row["detail"]), "created_at": row["created_at"],
            }
            if calculate_hash(previous, payload) != row["entry_hash"]:
                return False
            previous = row["entry_hash"]
        return True

    def close(self) -> None:
        with self._lock:
            self.conn.close()
