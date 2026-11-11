from __future__ import annotations

import hashlib
import json
import uuid
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

Identifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")]
Text = Annotated[str, Field(min_length=1, max_length=2000)]
Version = Annotated[int, Field(strict=True, ge=1)]
Quantity = Annotated[int, Field(strict=True, ge=1, le=100)]
Reason = Literal["not_received", "damaged", "change_mind", "cancel", "exchange"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class DomainError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def identifier(prefix: str) -> str:
    return prefix + "-" + uuid.uuid4().hex


class Principal(StrictModel):
    user_id: Identifier
    role: Literal["customer", "staff", "operator"]


class Command(StrictModel):
    action_id: Identifier


class VersionedCommand(Command):
    expected_version: Version


class MessageCommand(VersionedCommand):
    text: Text


class ProposalCommand(VersionedCommand):
    content_hash: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    decision: Literal["confirm", "decline"]


class NoteCommand(VersionedCommand):
    note: Text


class ReviewCommand(NoteCommand):
    decision: Literal["approved_demo", "rejected"]


class ReconcileCommand(Command):
    call_id: Identifier
    tokens: Annotated[int, Field(strict=True, ge=0, le=10000000)] | None = None
    note: Text
    outcome: Literal["no_usable_result"]


class EmptyArgs(StrictModel):
    pass


class OrderArgs(StrictModel):
    order_id: Identifier


class PolicyArgs(StrictModel):
    query: Text
    category: Literal["standard", "custom"] | None = None


class EvaluateArgs(OrderArgs):
    item_id: Identifier
    quantity: Quantity
    reason: Reason
    statement: Text


class AskArgs(StrictModel):
    question: Text


class ProposeArgs(StrictModel):
    evaluation_id: Identifier
    option_id: Identifier


class HandoffArgs(StrictModel):
    reason: Text


class RespondArgs(StrictModel):
    kind: Literal["facts", "policy", "empathy"]
    text: Annotated[str, Field(max_length=2000)]
    evidence_ids: list[Identifier] = Field(max_length=10)


TOOL_ARGUMENTS = {
    "list_my_orders": EmptyArgs, "get_order": OrderArgs, "get_shipment": OrderArgs,
    "search_policies": PolicyArgs, "get_case": EmptyArgs, "evaluate_resolution": EvaluateArgs,
    "ask_customer": AskArgs, "propose_resolution": ProposeArgs,
    "handoff_to_human": HandoffArgs, "respond": RespondArgs,
}


def validate_action(value: object) -> tuple[str, dict]:
    if type(value) is not dict or set(value) != {"tool", "arguments"}:
        raise DomainError("invalid_action", "动作必须只包含 tool 和 arguments")
    name = value["tool"]
    if type(name) is not str or name not in TOOL_ARGUMENTS:
        raise DomainError("invalid_tool", "工具不在允许范围内")
    try:
        args = TOOL_ARGUMENTS[name].model_validate(value["arguments"]).model_dump()
    except ValueError:
        raise DomainError("invalid_arguments", "工具参数不符合严格结构") from None
    return name, args


def action_schema() -> dict:
    variants = []
    for name, args in TOOL_ARGUMENTS.items():
        variants.append({
            "type": "object", "additionalProperties": False,
            "properties": {"tool": {"type": "string", "const": name},
                           "arguments": args.model_json_schema()},
            "required": ["tool", "arguments"],
        })
    return {"anyOf": variants}
