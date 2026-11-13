from dataclasses import dataclass
from enum import Enum
from typing import Mapping


class MembershipState(str, Enum):
    PRESENT = "present"
    ABSENT = "absent"
    UNKNOWN = "unknown"


class QuiescentState(str, Enum):
    YES = "yes"
    NO = "no"
    UNKNOWN = "unknown"


class ReconcileAction(str, Enum):
    ADD = "add"
    REMOVE = "remove"
    NOOP = "noop"


@dataclass(frozen=True)
class RoleInstanceState:
    phase: str | None
    hook_state: str | None


@dataclass(frozen=True)
class ReconcileSnapshot:
    conditions: Mapping[str, bool]
    should_register: bool
    member: MembershipState
    quiescent: QuiescentState
    action: ReconcileAction
