"""领域基础类型与输入校验。"""
from dataclasses import dataclass
from typing import Any, Dict, List


class DomainError(Exception):
    status = 400
    code = "domain_error"

    def __init__(self, message: str = "", details: Dict[str, Any] = None) -> None:
        super().__init__(message)
        self.details = details


class ValidationError(DomainError):
    status = 422
    code = "validation_error"


class NotFound(DomainError):
    status = 404
    code = "not_found"


class Conflict(DomainError):
    status = 409
    code = "conflict"


class PermissionDenied(DomainError):
    status = 403
    code = "permission_denied"


class ReinstatementExhausted(Conflict):
    """恢复次数耗尽，携带已用、缺少次数与累计保费，便于受理时直接告知。"""

    code = "reinstatement_exhausted"

    def __init__(self, event_id: str, total_count: int, used_count: int, accumulated_premium: float, accumulated_recovery: float = 0.0, needed: int = 1) -> None:
        available = max(0, int(total_count) - int(used_count))
        short = max(0, int(needed) - available)
        message = "事件%s恢复次数已耗尽：已用%s/%s次，缺少%s次，累计恢复保费%.2f" % (
            event_id, used_count, total_count, short, float(accumulated_premium),
        )
        super().__init__(message, details={
            "event_id": event_id,
            "total_count": int(total_count),
            "used_count": int(used_count),
            "available_count": available,
            "short_count": short,
            "accumulated_premium": round(float(accumulated_premium), 2),
            "accumulated_recovery": round(float(accumulated_recovery), 2),
        })


@dataclass(frozen=True)
class Actor:
    user_id: str
    role: str
    organization: str = ""


def text(data: Dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("%s不能为空" % key)
    return value.strip()


def optional_text(data: Dict[str, Any], key: str, default: str = "") -> str:
    value = data.get(key, default)
    if value is None:
        return default
    if not isinstance(value, str):
        raise ValidationError("%s必须是文本" % key)
    return value.strip()


def number(data: Dict[str, Any], key: str, minimum: float = None, maximum: float = None) -> float:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError("%s必须是数字" % key)
    value = float(value)
    if minimum is not None and value < minimum:
        raise ValidationError("%s不能小于%s" % (key, minimum))
    if maximum is not None and value > maximum:
        raise ValidationError("%s不能大于%s" % (key, maximum))
    return value


def integer(data: Dict[str, Any], key: str, minimum: int = None, maximum: int = None) -> int:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError("%s必须是整数" % key)
    if minimum is not None and value < minimum:
        raise ValidationError("%s不能小于%s" % (key, minimum))
    if maximum is not None and value > maximum:
        raise ValidationError("%s不能大于%s" % (key, maximum))
    return value


def choice(data: Dict[str, Any], key: str, allowed: List[str]) -> str:
    value = text(data, key)
    if value not in allowed:
        raise ValidationError("%s只能是%s" % (key, "/".join(allowed)))
    return value


def boolean(data: Dict[str, Any], key: str, default: bool = False) -> bool:
    value = data.get(key, default)
    if not isinstance(value, bool):
        raise ValidationError("%s必须是布尔值" % key)
    return value


def text_list(data: Dict[str, Any], key: str, minimum: int = 0) -> List[str]:
    value = data.get(key, [])
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValidationError("%s必须是文本列表" % key)
    if len(value) < minimum:
        raise ValidationError("%s至少需要%s项" % (key, minimum))
    return [item.strip() for item in value]
