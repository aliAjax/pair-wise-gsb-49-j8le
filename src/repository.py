"""SQLite 表结构与事务访问。"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from .domain import Conflict, NotFound, ReinstatementExhausted


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
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);

                -- 恢复台账：按事件登记可用次数，并累计已消耗次数与恢复保费
                CREATE TABLE IF NOT EXISTS reinstatement_ledger (
                    event_id TEXT PRIMARY KEY,
                    total_count INTEGER NOT NULL,
                    used_count INTEGER NOT NULL DEFAULT 0,
                    accumulated_recovery REAL NOT NULL DEFAULT 0,
                    accumulated_premium REAL NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                -- 恢复次数消耗明细：每次结算登记一行，服务重开后逐笔可核对
                CREATE TABLE IF NOT EXISTS reinstatement_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL REFERENCES reinstatement_ledger(event_id),
                    record_id INTEGER NOT NULL REFERENCES records(id),
                    seq INTEGER NOT NULL,
                    recovery_amount REAL NOT NULL,
                    reinstatement_premium REAL NOT NULL,
                    payment_reference TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(event_id, record_id)
                );
                CREATE INDEX IF NOT EXISTS idx_reinst_entries_event ON reinstatement_entries(event_id, seq);
                """
            )

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    def create(self, reference: str, state: str, payload: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        """建案并按事件登记恢复台账（同一事务）。

        事件首次出现时按合约约定登记可用次数；事件已存在时次数必须与原登记一致，
        避免同一巨灾事件在不同案件中登记出互相矛盾的额度。
        """
        now = _now()
        event_id = str(payload["event_id"])
        total_count = int(payload["reinstatement_count"])
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                ledger = connection.execute("SELECT * FROM reinstatement_ledger WHERE event_id=?", (event_id,)).fetchone()
                if ledger is None:
                    connection.execute(
                        "INSERT INTO reinstatement_ledger(event_id,total_count,used_count,accumulated_recovery,accumulated_premium,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                        (event_id, total_count, 0, 0.0, 0.0, now, now),
                    )
                elif int(ledger["total_count"]) != total_count:
                    connection.rollback()
                    raise Conflict(
                        "事件%s已登记恢复次数%s次，与本次申报%s次不一致" % (event_id, ledger["total_count"], total_count),
                        details={"event_id": event_id, "registered_count": int(ledger["total_count"]), "submitted_count": total_count},
                    )
                cursor = connection.execute(
                    "INSERT INTO records(reference,state,version,payload,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (reference, state, 1, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, actor_id, now, now),
                )
                record_id = int(cursor.lastrowid)
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (record_id, "created", actor_id, 1, json.dumps({"state": state, "event_id": event_id, "reinstatement_count": total_count}, ensure_ascii=False, sort_keys=True), now),
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

    def mutate(self, record_id: int, expected_version: int, state: str, payload: Dict[str, Any], actor_id: str, action: str, details: Dict[str, Any]) -> Dict[str, Any]:
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

    def settle_with_reinstatement(self, record_id: int, expected_version: int, payload: Dict[str, Any], actor_id: str, payment_reference: str, event_id: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """结算并在同一事务内消耗一次恢复次数、按摊回金额累计恢复保费。

        次数闸门在写事务内做最后校验，避免并发结算超额消耗；未结算/已拒赔案件
        因未走到本方法，天然不占用次数。返回更新后的记录与本次消耗明细。
        """
        now = _now()
        recovery = round(float(payload["recoverable_amount"]), 2)
        premium = round(float(payload["reinstatement_premium"]), 2)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            ledger = connection.execute("SELECT * FROM reinstatement_ledger WHERE event_id=?", (event_id,)).fetchone()
            if ledger is None:
                connection.rollback()
                raise NotFound("事件%s恢复台账不存在" % event_id)
            if int(ledger["used_count"]) >= int(ledger["total_count"]):
                connection.rollback()
                raise ReinstatementExhausted(
                    event_id=event_id,
                    total_count=int(ledger["total_count"]),
                    used_count=int(ledger["used_count"]),
                    accumulated_premium=float(ledger["accumulated_premium"]),
                    accumulated_recovery=float(ledger["accumulated_recovery"]),
                )
            version = int(expected_version) + 1
            seq = int(ledger["used_count"]) + 1
            connection.execute(
                "UPDATE reinstatement_ledger SET used_count=?,accumulated_recovery=?,accumulated_premium=?,updated_at=? WHERE event_id=?",
                (seq, round(float(ledger["accumulated_recovery"]) + recovery, 2), round(float(ledger["accumulated_premium"]) + premium, 2), now, event_id),
            )
            connection.execute(
                "INSERT INTO reinstatement_entries(event_id,record_id,seq,recovery_amount,reinstatement_premium,payment_reference,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (event_id, record_id, seq, recovery, premium, payment_reference, actor_id, now),
            )
            connection.execute(
                "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                ("settled", version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now, record_id),
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, "settle", actor_id, version, json.dumps({
                    "summary": "摊回赔款已结算",
                    "from": "calculated",
                    "to": "settled",
                    "reinstatement": {"event_id": event_id, "seq": seq, "recovery_amount": recovery, "reinstatement_premium": premium, "payment_reference": payment_reference},
                }, ensure_ascii=False, sort_keys=True), now),
            )
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            entry = connection.execute("SELECT * FROM reinstatement_entries WHERE event_id=? AND record_id=?", (event_id, record_id)).fetchone()
            connection.commit()
        return self._row(result), dict(entry)

    def get_reinstatement(self, event_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM reinstatement_ledger WHERE event_id=?", (event_id,)).fetchone()
        return dict(row) if row is not None else None

    def list_reinstatements(self) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM reinstatement_ledger ORDER BY event_id").fetchall()
        return [dict(row) for row in rows]

    def reinstatement_map(self, event_ids: List[str]) -> Dict[str, Dict[str, Any]]:
        if not event_ids:
            return {}
        with self._connect() as connection:
            placeholders = ",".join("?" for _ in event_ids)
            rows = connection.execute(
                "SELECT * FROM reinstatement_ledger WHERE event_id IN (%s)" % placeholders, tuple(event_ids)
            ).fetchall()
        return {str(row["event_id"]): dict(row) for row in rows}

    def reinstatement_entries(self, event_id: str) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM reinstatement_entries WHERE event_id=? ORDER BY seq", (event_id,)).fetchall()
        return [dict(row) for row in rows]
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
