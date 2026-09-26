"""业务用例编排、权限检查与审计。"""
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, NotFound, PermissionDenied, text
from .repository import Repository
from .rules import DomainRules


class Service:
    def __init__(self, repository: Repository, rules: DomainRules, audit: AuditRecorder = None) -> None:
        self.repository = repository
        self.rules = rules
        self.audit = audit or AuditRecorder(repository)

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _ensure_known_role(self, actor: Actor) -> None:
        if not self.rules.known_role(actor.role):
            raise PermissionDenied("角色无权访问该服务")

    def create(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create(actor.role):
            raise PermissionDenied("角色无权创建记录")
        reference = text({"reference": reference}, "reference")
        prepared = self.rules.prepare_create(payload or {})
        self.rules.check_create_conflicts(prepared, self.repository.list_records(limit=500))
        record = self.repository.create(reference, self.rules.INITIAL_STATE, prepared, actor.user_id)
        return self._attach_reinstatement(record)

    def _attach_reinstatement(self, record: Dict[str, Any]) -> Dict[str, Any]:
        event_id = record.get("payload", {}).get("event_id")
        if event_id:
            ledger = self.repository.get_reinstatement(event_id)
            if ledger is not None:
                record["reinstatement"] = self.rules.reinstatement_snapshot(ledger)
        return record

    def list_records(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_records(state=state, limit=limit)

    def get_record(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self._attach_reinstatement(self.repository.get(record_id))

    def act(self, actor: Actor, record_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        record = self.repository.get(record_id)
        self.rules.require_transition(record, action)
        event_id = record["payload"].get("event_id")
        ledger = self.repository.get_reinstatement(event_id) if event_id else None
        if action == "calculate":
            self.rules.ensure_reinstatement_available(ledger)
        new_state, new_payload, summary = self.rules.apply_action(record, action, data or {}, ledger=ledger)
        details = {"summary": summary, "input": data or {}, "from": record["state"], "to": new_state}
        reinstatement = None
        if action == "settle" and event_id:
            reinstatement = {
                "event_id": event_id,
                "recovery_amount": float(new_payload["recoverable_amount"]),
                "premium": float(new_payload["reinstatement_premium"]),
            }
            details["reinstatement"] = dict(reinstatement)
        updated = self.repository.mutate(
            record_id=record_id,
            expected_version=int(expected_version),
            state=new_state,
            payload=new_payload,
            actor_id=actor.user_id,
            action=action,
            details=details,
            reinstatement=reinstatement,
        )
        return self._attach_reinstatement(updated)

    def list_reinstatements(self, actor: Actor) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return [self.rules.reinstatement_snapshot(ledger) for ledger in self.repository.list_reinstatements()]

    def reinstatement_detail(self, actor: Actor, event_id: str) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        event_id = text({"event_id": event_id}, "event_id")
        ledger = self.repository.get_reinstatement(event_id)
        if ledger is None:
            raise NotFound("事件未登记恢复台账")
        return {
            "ledger": self.rules.reinstatement_snapshot(ledger),
            "entries": self.repository.reinstatement_entries(event_id),
        }

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()
