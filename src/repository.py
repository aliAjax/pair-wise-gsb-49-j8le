"""SQLite 表结构与事务访问。"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .domain import Conflict, NotFound


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Repository:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reference TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS reinstatement_ledger (
                    event_id TEXT PRIMARY KEY,
                    total_count INTEGER NOT NULL,
                    used_count INTEGER NOT NULL DEFAULT 0,
                    accumulated_premium REAL NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS reinstatement_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    recovery_amount REAL NOT NULL,
                    premium REAL NOT NULL,
                    actor_id TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_reinstatement_entries_event ON reinstatement_entries(event_id, id);
                """
            )

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    def create(self, reference: str, state: str, payload: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        event_id = payload.get("event_id")
        total_count = int(payload.get("reinstatement_count", 0))
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                if event_id:
                    ledger = connection.execute("SELECT total_count FROM reinstatement_ledger WHERE event_id=?", (event_id,)).fetchone()
                    if ledger is None:
                        connection.execute(
                            "INSERT INTO reinstatement_ledger(event_id,total_count,used_count,accumulated_premium,created_at,updated_at) VALUES(?,?,0,0,?,?)",
                            (event_id, total_count, now, now),
                        )
                    elif int(ledger["total_count"]) != total_count:
                        connection.rollback()
                        raise Conflict("事件%s已登记恢复次数%s次，与本次登记的%s次不一致" % (event_id, int(ledger["total_count"]), total_count))
                cursor = connection.execute(
                    "INSERT INTO records(reference,state,version,payload,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (reference, state, 1, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, actor_id, now, now),
                )
                record_id = int(cursor.lastrowid)
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (record_id, "created", actor_id, 1, json.dumps({"state": state}, ensure_ascii=False, sort_keys=True), now),
                )
                row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
                connection.commit()
        except sqlite3.IntegrityError as exc:
            raise Conflict("reference已存在") from exc
        return self._row(row)

    def get(self, record_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise NotFound("记录不存在")
        return self._row(row)

    def list_records(self, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            if state:
                rows = connection.execute("SELECT * FROM records WHERE state=? ORDER BY id DESC LIMIT ?", (state, limit)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM records ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(row) for row in rows]

    def mutate(self, record_id: int, expected_version: int, state: str, payload: Dict[str, Any], actor_id: str, action: str, details: Dict[str, Any], reinstatement: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            version = int(expected_version) + 1
            if reinstatement is not None:
                event_id = reinstatement["event_id"]
                ledger = connection.execute("SELECT used_count,total_count FROM reinstatement_ledger WHERE event_id=?", (event_id,)).fetchone()
                if ledger is None:
                    connection.rollback()
                    raise Conflict("事件%s未登记恢复台账" % event_id)
                cursor = connection.execute(
                    "UPDATE reinstatement_ledger SET used_count=used_count+1, accumulated_premium=ROUND(accumulated_premium+?,2), updated_at=? WHERE event_id=? AND used_count<total_count",
                    (float(reinstatement["premium"]), now, event_id),
                )
                if cursor.rowcount != 1:
                    connection.rollback()
                    raise Conflict("事件%s恢复次数已耗尽：已用%s/%s次，无法结算" % (event_id, int(ledger["used_count"]), int(ledger["total_count"])))
                connection.execute(
                    "INSERT INTO reinstatement_entries(event_id,record_id,recovery_amount,premium,actor_id,created_at) VALUES(?,?,?,?,?,?)",
                    (event_id, record_id, float(reinstatement["recovery_amount"]), float(reinstatement["premium"]), actor_id, now),
                )
            connection.execute(
                "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (state, version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now, record_id),
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, version, json.dumps(details, ensure_ascii=False, sort_keys=True), now),
            )
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        return self._row(result)

    def get_reinstatement(self, event_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM reinstatement_ledger WHERE event_id=?", (event_id,)).fetchone()
        return dict(row) if row is not None else None

    def list_reinstatements(self) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM reinstatement_ledger ORDER BY event_id").fetchall()
        return [dict(row) for row in rows]

    def reinstatement_entries(self, event_id: str) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM reinstatement_entries WHERE event_id=? ORDER BY id", (event_id,)).fetchall()
        return [dict(row) for row in rows]

    def add_audit(self, record_id: int, actor_id: str, action: str, details: Dict[str, Any]) -> None:
        with self._connect() as connection:
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                raise NotFound("记录不存在")
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, int(row["version"]), json.dumps(details, ensure_ascii=False, sort_keys=True), _now()),
            )

    def audit_timeline(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM audit_events WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result

    def stats(self) -> Dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute("SELECT state, COUNT(*) AS total FROM records GROUP BY state").fetchall()
        return {str(row["state"]): int(row["total"]) for row in rows}

    def health(self) -> bool:
        try:
            with self._connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False
