"""TEST ONLY: hard-keyword demo stand-in, NOT a business policy or a real model.

This deliberately limited, stateless script demonstrates the observation/tool loop.
It never reads evals, an oracle, files, configuration, or external services. Never use
it as fallback for LOCAL_LLM. Backend authorization, evaluation, confirmation and
human review remain mandatory; no payment or approval action exists here.
"""
from __future__ import annotations

import re

from .provider import ProviderError, ProviderReply
from .schemas import DomainError, validate_action

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")
_ORDER = re.compile(r"(?<![A-Za-z0-9_-])ord-[A-Za-z0-9_-]+")
_ITEM = re.compile(r"(?<![A-Za-z0-9_-])item-[A-Za-z0-9_-]+")
_REASONS = (
    ("not_received", ("未收到", "没收到", "没有收到")),
    ("damaged", ("破损", "损坏", "碎了")),
    ("exchange", ("换货",)),
    ("cancel", ("取消",)),
    ("change_mind", ("退货", "退款", "不想要")),
)
_STALE = {"expired", "evidence_changed", "stale_evidence", "option_changed"}
_ASSENT = {"好", "好的", "同意", "确认", "可以", "行", "嗯"}


def _has(text: str, words: tuple[str, ...]) -> bool:
    return any(word in text for word in words)


def _reason(text: str) -> str | None:
    return next((reason for reason, words in _REASONS if _has(text, words)), None)


def _customer_text(messages: list) -> str:
    customers = [m.get("text", "") for m in messages
                 if isinstance(m, dict) and m.get("role") in {"customer", "user"}
                 and type(m.get("text")) is str]
    if not customers:
        return ""
    latest = customers[-1]
    if latest.strip(" 。！!，,？?\n") in _ASSENT:
        return latest
    # Only explicit follow-ups to an actual question inherit previous customer
    # input. Assistant text and tool-result strings are never parsed as instructions.
    assistants = [m for m in messages if isinstance(m, dict) and m.get("role") == "assistant"]
    if assistants and assistants[-1].get("kind") == "question" and len(customers) > 1:
        if not (_ORDER.search(latest) and _reason(latest)):
            return "\n".join(customers[-3:])[-2000:]
    return latest[-2000:]


def _reply(tool: str, **arguments) -> ProviderReply:
    try:
        tool, arguments = validate_action({"tool": tool, "arguments": arguments})
    except DomainError:
        raise ProviderError("model_context", sent=False) from None
    return ProviderReply({"tool": tool, "arguments": arguments}, 128, "TEST")


def _handoff() -> ProviderReply:
    return _reply("handoff_to_human", reason="演示替身无法安全继续或后端没有可用方案，请人工核查。")


def _ask(context: dict, index: int) -> ProviderReply:
    questions = context.get("allowed_questions", [])
    if type(questions) is not list or len(questions) <= index or type(questions[index]) is not str:
        return _handoff()
    return _reply("ask_customer", question=questions[index])


def _latest(context: dict, observations: list, tool: str, order_id: str | None = None):
    """Latest matching issued evidence only; freshness/auth checks also run in Tools."""
    for index in range(len(observations) - 1, -1, -1):
        trace = observations[index]
        if not isinstance(trace, dict) or trace.get("tool") != tool:
            continue
        obj = trace.get("observation", {})
        if not isinstance(obj, dict):
            return None
        payload = obj.get("payload", {})
        if not isinstance(payload, dict):
            return None
        data = payload.get("data", {})
        if order_id is not None:
            args = trace.get("action", {}).get("arguments", {})
            observed_id = args.get("order_id")
            if observed_id is None and isinstance(data, dict):
                observed_id = data.get("order_id", data.get("id"))
            if observed_id != order_id:
                continue
        eid = obj.get("id")
        captured = payload.get("captured_at")
        now = context.get("now")
        if (obj.get("kind") != "evidence" or type(eid) is not str or not _IDENTIFIER.fullmatch(eid)
                or payload.get("session_id") != context.get("session_id")
                or payload.get("tool") != tool or type(captured) is not int or type(now) is not int
                or not 0 <= now - captured < 900):
            return None
        return index, obj
    return None


def _answer(kind: str, *records) -> ProviderReply:
    return _reply("respond", kind=kind, text="请查看后端当前引用卡片。",
                  evidence_ids=[record[1]["id"] for record in records])


def _quantity(text: str, item: dict) -> int | None:
    # Deliberately small TEST vocabulary; ambiguous numerals are a question,
    # never a reason to silently extract the suffix of 1.5 or 十一 as 5 or 1.
    values = re.findall(r"(?<![A-Za-z0-9_.一二两三四五六七八九十百千万+-])"
                        r"([0-9]{1,3}|[一二两三四五六七八九十])\s*(?:件|个|份)", text)
    numbers = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
               "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
    parsed = {int(value) if value.isascii() and value.isdigit() else numbers[value] for value in values}
    if "全部" in text:
        value = item.get("quantity")
        if type(value) is int and 1 <= value <= 100 and (not parsed or parsed == {value}):
            return value
        return None
    if len(parsed) != 1:
        return None
    value = parsed.pop()
    return value if 1 <= value <= 100 else None


class DemoProvider:
    """Explicit TEST keyword rules. No mutable state and no hidden expected answers."""

    is_test_provider = True
    fingerprint = "TEST-demo-v1"

    def decide(self, context: dict, schema: dict) -> ProviderReply:
        if not isinstance(context, dict) or context.get("mode") != "TEST":
            raise ProviderError("model_context", sent=False)
        messages = context.get("messages", [])
        observations = context.get("observations", [])
        if type(messages) is not list or type(observations) is not list:
            raise ProviderError("model_context", sent=False)
        text = _customer_text(messages)
        # After backend invalidation, rebuild from new observations, never repair a
        # stale proposal by fabricating IDs or retaining an earlier evaluation.
        for index in range(len(observations) - 1, -1, -1):
            trace = observations[index]
            obj = trace.get("observation", {}) if isinstance(trace, dict) else {}
            if isinstance(obj, dict) and obj.get("error") in _STALE:
                observations = observations[index + 1:]
                break
        if observations:
            last = observations[-1].get("observation", {})
            if isinstance(last, dict) and last.get("error"):
                return _handoff()
        if _has(text, ("转人工", "人工客服", "找人工")):
            return _handoff()
        if (text.strip(" 。！!，,？?\n") in _ASSENT
                or _has(text, ("申请状态", "申请进度", "工单", "审核结果", "处理进度", "售后进度", "退款进度"))
                or (_has(text, ("申请", "售后")) and _has(text, ("状态", "进度")))):
            case = _latest(context, observations, "get_case")
            return _answer("facts", case) if case else _reply("get_case")
        order_ids = set(_ORDER.findall(text))
        reason = _reason(text)
        policy_question = _has(text, ("政策", "期限", "几天", "规则", "可以退", "能退", "支持退", "可以换"))
        if policy_question:
            policy = _latest(context, observations, "search_policies")
            return (_answer("policy", policy) if policy else
                    _reply("search_policies", query=text, category="custom" if "定制" in text else None))
        if len(order_ids) != 1:
            orders = _latest(context, observations, "list_my_orders")
            return _ask(context, 0) if orders else _reply("list_my_orders")
        order_id = next(iter(order_ids))
        if not _IDENTIFIER.fullmatch(order_id):
            return _ask(context, 0)
        order = _latest(context, observations, "get_order", order_id)
        if not order:
            return _reply("get_order", order_id=order_id)
        shipment = _latest(context, observations, "get_shipment", order_id)
        if not shipment:
            return _reply("get_shipment", order_id=order_id)
        if reason is None:
            if _has(text, ("物流", "到哪里", "快递", "查询", "查一下", "订单状态")):
                return _answer("facts", order, shipment)
            return _ask(context, 1)
        policy = _latest(context, observations, "search_policies")
        if not policy:
            return _reply("search_policies", query=text, category=None)
        data = order[1]["payload"]["data"]
        items = data.get("items", []) if isinstance(data, dict) else []
        explicit_items = set(_ITEM.findall(text))
        selected = [item for item in items if isinstance(item, dict)
                    and (item.get("item_id") in explicit_items if explicit_items else len(items) == 1)]
        if len(selected) != 1:
            return _ask(context, 2)
        item = selected[0]
        quantity = _quantity(text, item)
        if quantity is None:
            return _ask(context, 2)
        inputs = {"order_id": order_id, "item_id": item.get("item_id"), "quantity": quantity, "reason": reason}
        latest_read = max(order[0], shipment[0], policy[0])
        for trace in reversed(observations[latest_read + 1:]):
            if trace.get("tool") != "evaluate_resolution":
                continue
            evaluation = trace.get("observation", {})
            payload = evaluation.get("payload", {})
            expires = payload.get("expires_at")
            eid = evaluation.get("id")
            if (evaluation.get("kind") != "evaluation" or type(eid) is not str
                    or not _IDENTIFIER.fullmatch(eid) or payload.get("session_id") != context.get("session_id")
                    or type(expires) is not int or expires <= context.get("now", 0)
                    or any(payload.get("inputs", {}).get(key) != value for key, value in inputs.items())):
                break
            options = payload.get("result", {}).get("options", [])
            if type(options) is not list or not options:
                return _handoff()
            option_id = options[0].get("option_id") if isinstance(options[0], dict) else None
            if type(option_id) is not str or not _IDENTIFIER.fullmatch(option_id):
                return _handoff()
            return _reply("propose_resolution", evaluation_id=eid, option_id=option_id)
        return _reply("evaluate_resolution", **inputs, statement=text)
