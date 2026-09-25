from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .domain import ConflictError, NotFoundError
from .rules import ID_PREFIX, STATES


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
                CREATE TABLE IF NOT EXISTS rectifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    assignee TEXT NOT NULL,
                    reviewer TEXT NOT NULL,
                    due_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK(status IN ('open','closed')),
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    closed_by TEXT,
                    closed_at TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_rectifications_open
                    ON rectifications(item_id) WHERE status='open';
                CREATE TABLE IF NOT EXISTS retests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    rectification_id INTEGER NOT NULL
                        REFERENCES rectifications(id) ON DELETE CASCADE,
                    reading REAL NOT NULL,
                    conclusion TEXT NOT NULL CHECK(conclusion IN ('pass','fail')),
                    basis_threshold REAL NOT NULL,
                    basis_quantity REAL NOT NULL,
                    basis_due_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'current'
                        CHECK(status IN ('current','invalidated')),
                    rejudged_from INTEGER,
                    superseded_by INTEGER,
                    submitted_by TEXT NOT NULL,
                    submitted_at TEXT NOT NULL,
                    invalidated_at TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_retests_rectification
                    ON retests(rectification_id, status);
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

    def update_item_parameters(self, item_id: int, quantity: float, threshold: float,
                               expected_version: int, actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE items SET quantity=?, threshold=?, version=version+1, updated_at=?
                   WHERE id=? AND version=?""",
                (quantity, threshold, now, item_id, expected_version),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("项目不存在")
                raise ConflictError("版本冲突，请刷新后重试")
        return self.get_item(item_id)

    def create_rectification(self, item_id: int, assignee: str, reviewer: str,
                             due_at: str, actor: str) -> Dict[str, Any]:
        now = utc_now()
        self.get_item(item_id)
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO rectifications(item_id, assignee, reviewer, due_at, status,
                       created_by, created_at) VALUES(?,?,?,?,'open',?,?)""",
                    (item_id, assignee, reviewer, due_at, actor, now),
                )
                rectification_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("同一事件只留一项未结束整改") from exc
        return self.get_rectification(rectification_id)

    def get_rectification(self, rectification_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM rectifications WHERE id=?", (rectification_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError("整改记录不存在")
        return dict(row)

    def get_open_rectification(self, item_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM rectifications WHERE item_id=? AND status='open'",
                (item_id,),
            ).fetchone()
        return dict(row) if row else None

    def list_rectifications(self, item_id: Optional[int] = None,
                            status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM rectifications"
        clauses, params = [], []
        if item_id is not None:
            clauses.append("item_id=?")
            params.append(item_id)
        if status:
            clauses.append("status=?")
            params.append(status)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id DESC"
        with self._lock:
            rows = self.conn.execute(sql, tuple(params)).fetchall()
        return [dict(row) for row in rows]

    def close_rectification(self, rectification_id: int, actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE rectifications SET status='closed', closed_by=?, closed_at=?
                   WHERE id=? AND status='open'""",
                (actor, now, rectification_id),
            )
            if cur.rowcount == 0:
                raise ConflictError("整改已关闭或不存在")
        return self.get_rectification(rectification_id)

    def update_rectification_deadline(self, rectification_id: int,
                                      due_at: str) -> Dict[str, Any]:
        with self._lock, self.conn:
            cur = self.conn.execute(
                "UPDATE rectifications SET due_at=? WHERE id=? AND status='open'",
                (due_at, rectification_id),
            )
            if cur.rowcount == 0:
                raise ConflictError("整改已关闭或不存在")
        return self.get_rectification(rectification_id)

    def add_retest(self, rectification_id: int, reading: float, conclusion: str,
                   basis_threshold: float, basis_quantity: float, basis_due_at: str,
                   submitted_by: str, submitted_at: str,
                   rejudged_from: Optional[int] = None) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """INSERT INTO retests(rectification_id, reading, conclusion, basis_threshold,
                   basis_quantity, basis_due_at, status, rejudged_from, submitted_by,
                   submitted_at, created_at) VALUES(?,?,?,?,?,?,'current',?,?,?,?)""",
                (rectification_id, reading, conclusion, basis_threshold, basis_quantity,
                 basis_due_at, rejudged_from, submitted_by, submitted_at, now),
            )
            retest_id = int(cur.lastrowid)
            row = self.conn.execute("SELECT * FROM retests WHERE id=?", (retest_id,)).fetchone()
        return dict(row)

    def list_retests(self, rectification_id: int) -> List[Dict[str, Any]]:
        self.get_rectification(rectification_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM retests WHERE rectification_id=? ORDER BY id",
                (rectification_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def current_retests(self, rectification_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                """SELECT * FROM retests WHERE rectification_id=? AND status='current'
                   ORDER BY id""",
                (rectification_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def rejudge_retests(self, rectification_id: int,
                        judgments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        now = utc_now()
        results = []
        with self._lock, self.conn:
            for judgment in judgments:
                cur = self.conn.execute(
                    """INSERT INTO retests(rectification_id, reading, conclusion,
                       basis_threshold, basis_quantity, basis_due_at, status, rejudged_from,
                       submitted_by, submitted_at, created_at)
                       SELECT rectification_id, reading, ?, ?, ?, ?, 'current', id,
                              submitted_by, submitted_at, ?
                       FROM retests WHERE id=? AND rectification_id=? AND status='current'""",
                    (judgment["conclusion"], judgment["basis_threshold"],
                     judgment["basis_quantity"], judgment["basis_due_at"], now,
                     judgment["old_id"], rectification_id),
                )
                if cur.rowcount == 0:
                    raise ConflictError("复测结论已失效，请刷新后重试")
                new_id = int(cur.lastrowid)
                self.conn.execute(
                    """UPDATE retests SET status='invalidated', superseded_by=?,
                       invalidated_at=? WHERE id=?""",
                    (new_id, now, judgment["old_id"]),
                )
                row = self.conn.execute("SELECT * FROM retests WHERE id=?", (new_id,)).fetchone()
                results.append(dict(row))
        return results

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
