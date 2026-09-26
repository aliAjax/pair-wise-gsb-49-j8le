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

    def _event_id(self, record: Dict[str, Any]) -> str:
        return str(record["payload"]["event_id"])

    def _ledger_view(self, event_id: str) -> Optional[Dict[str, Any]]:
        return self.rules.reinstatement_view(self.repository.get_reinstatement(event_id))

    def _with_ledger(self, record: Dict[str, Any]) -> Dict[str, Any]:
        record["reinstatement"] = self._ledger_view(self._event_id(record))
        return record

    def create(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create(actor.role):
            raise PermissionDenied("角色无权创建记录")
        reference = text({"reference": reference}, "reference")
        prepared = self.rules.prepare_create(payload or {})
        self.rules.check_create_conflicts(prepared, self.repository.list_records(limit=500))
        record = self.repository.create(reference, self.rules.INITIAL_STATE, prepared, actor.user_id)
        return self._with_ledger(record)

    def list_records(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        records = self.repository.list_records(state=state, limit=limit)
        ledgers = self.repository.reinstatement_map([self._event_id(record) for record in records])
        for record in records:
            record["reinstatement"] = self.rules.reinstatement_view(ledgers.get(self._event_id(record)))
        return records

    def get_record(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self._with_ledger(self.repository.get(record_id))

    def act(self, actor: Actor, record_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        record = self.repository.get(record_id)
        self.rules.require_transition(record, action)
        if action in {"submit_claim", "calculate"}:
            # 受理与核定前先确认恢复次数；错误中写明已用/缺少次数与累计保费
            ledger = self.repository.get_reinstatement(self._event_id(record))
            if ledger is None:
                raise NotFound("事件%s恢复台账不存在" % self._event_id(record))
            self.rules.require_reinstatement_available(ledger)
        new_state, new_payload, summary = self.rules.apply_action(record, action, data or {})
        if action == "settle":
            event_id = self._event_id(record)
            updated, entry = self.repository.settle_with_reinstatement(
                record_id=record_id,
                expected_version=int(expected_version),
                payload=new_payload,
                actor_id=actor.user_id,
                payment_reference=str(new_payload["payment_reference"]),
                event_id=event_id,
            )
            return self._with_ledger(updated)
        updated = self.repository.mutate(
            record_id=record_id,
            expected_version=int(expected_version),
            state=new_state,
            payload=new_payload,
            actor_id=actor.user_id,
            action=action,
            details={"summary": summary, "input": data or {}, "from": record["state"], "to": new_state},
        )
        return self._with_ledger(updated)

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()

    def list_reinstatements(self, actor: Actor) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return [view for view in (self.rules.reinstatement_view(row) for row in self.repository.list_reinstatements()) if view]

    def get_reinstatement(self, actor: Actor, event_id: str) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        event_id = text({"event_id": event_id}, "event_id")
        ledger = self.repository.get_reinstatement(event_id)
        if ledger is None:
            raise NotFound("事件%s恢复台账不存在" % event_id)
        view = self.rules.reinstatement_view(ledger)
        view["entries"] = self.repository.reinstatement_entries(event_id)
        return view
