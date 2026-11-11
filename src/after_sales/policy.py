"""Deterministic synthetic Demo policy retrieval and review eligibility.

No clock, persistence, language-model calls, or execution of customer text. Policies
are Demo merchant rules, not legal advice. Every eligible result is only a review
proposal; it never records a refund, shipment change, or inventory reservation.
"""

from copy import deepcopy
from unicodedata import normalize

_DAY = 86_400
_REASONS = {"not_received", "damaged", "change_mind", "cancel", "exchange"}
_CATEGORIES = {"standard", "custom"}
_SHIPMENT_STATES = {"pending", "in_transit", "delivered", "exception"}
_ORDER_STATES = {"paid", "shipped", "delivered", "cancelled"}


def _integer(value: object, name: str, minimum: int = 0, maximum: int | None = None) -> int:
    if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
        raise ValueError(f"{name}必须是范围内的整数，不能是布尔值")
    return value


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name}必须是非空字符串")
    return value


def _choice(value: object, name: str, choices: set[str]) -> str:
    value = _text(value, name)
    if value not in choices:
        raise ValueError(f"{name}不受支持")
    return value


def _latest_policies(policies: list[dict]) -> list[dict]:
    if not isinstance(policies, list):
        raise ValueError("policies必须是列表")
    latest: dict[str, dict] = {}
    seen: set[tuple[str, int]] = set()
    for policy in policies:
        if not isinstance(policy, dict):
            raise ValueError("政策必须是字典")
        identifier = _text(policy.get("id"), "政策id")
        version = _integer(policy.get("version"), "政策版本", 1)
        for field in ("title", "body"):
            _text(policy.get(field), f"政策{field}")
        _choice(policy.get("category"), "政策类别", _CATEGORIES)
        _choice(policy.get("reason"), "政策原因", _REASONS)
        for field in ("window_days", "delay_days"):
            _integer(policy.get(field), f"政策{field}", 0, 365)
        if type(policy.get("active")) is not bool:
            raise ValueError("政策active必须是布尔值")
        identity = (identifier, version)
        if identity in seen:
            raise ValueError("政策id和版本重复，无法确定唯一来源")
        seen.add(identity)
        if identifier not in latest or version > latest[identifier]["version"]:
            latest[identifier] = policy
    # Select the latest version BEFORE filtering active: retirement must not revive history.
    return [latest[key] for key in sorted(latest) if latest[key]["active"]]


def _is_han(character: str) -> bool:
    code = ord(character)
    return (0x3400 <= code <= 0x4DBF or 0x4E00 <= code <= 0x9FFF
            or 0xF900 <= code <= 0xFAFF or 0x20000 <= code <= 0x323AF)


def _terms(text: str) -> set[str]:
    """NFKC/casefold terms; consecutive Han spans become overlapping bigrams."""
    terms: set[str] = set()
    run = ""
    previous = ""
    for character in normalize("NFKC", text).casefold() + " ":
        kind = "han" if _is_han(character) else "word" if character.isalnum() else ""
        if run and kind != previous:
            if previous == "han" and len(run) > 1:
                terms.update(run[index:index + 2] for index in range(len(run) - 1))
            else:
                terms.add(run)
            run = ""
        if kind:
            run += character
        previous = kind
    return terms


def search_policies(query: str, policies: list[dict], category: str | None = None) -> list[dict]:
    """Return up to five independent source copies with a transparent integer score.

    Score = 4 * distinct title-term matches + 1 * body-term matches +
    2 * category/reason-term matches. No matches means no result, with no fallback.
    Ties use policy id. Only the highest active version of each id is searchable;
    if the highest version is inactive, older versions stay retired. Input text is
    data, including instruction-like text. There is no semantic instruction parser.
    """
    _text(query, "query")
    if category is not None:
        _choice(category, "category", _CATEGORIES)
    sources = _latest_policies(policies)
    query_terms = _terms(query)
    results = []
    for policy in sources:
        if category is not None and policy["category"] != category:
            continue
        score = (
            4 * len(query_terms & _terms(policy["title"]))
            + len(query_terms & _terms(policy["body"]))
            + 2 * len(query_terms & _terms(policy["category"] + " " + policy["reason"]))
        )
        if score:
            result = deepcopy(policy)
            result["score"] = score
            results.append(result)
    return sorted(results, key=lambda result: (-result["score"], result["id"]))[:5]


def _validate_records(order: dict, shipment: dict, item_id: str, now: int) -> dict:
    if not isinstance(order, dict) or not isinstance(shipment, dict):
        raise ValueError("订单和物流记录必须是字典")
    order_id = _text(order.get("id"), "订单id")
    _text(order.get("customer_id"), "customer_id")
    state = _choice(order.get("state"), "订单状态", _ORDER_STATES)
    if order.get("currency") != "CNY":
        raise ValueError("仅支持CNY商品实付金额")
    placed = _integer(order.get("placed_at"), "下单时间", 0, now)
    items = order.get("items")
    if not isinstance(items, list) or not items:
        raise ValueError("订单必须包含商品项")
    by_id = {}
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("商品项必须是字典")
        identifier = _text(item.get("item_id"), "商品项id")
        if identifier in by_id:
            raise ValueError("订单商品项id必须唯一")
        _text(item.get("product_name"), "商品名")
        _choice(item.get("category"), "商品类别", _CATEGORIES)
        _integer(item.get("quantity"), "原商品数量", 1)
        _integer(item.get("unit_paid_cents"), "商品实付单价")
        by_id[identifier] = item
    if item_id not in by_id:
        raise ValueError("商品项不属于本订单")
    if shipment.get("order_id") != order_id or shipment.get("id") != "ship-" + order_id:
        raise ValueError("物流记录与订单不匹配")
    status = _choice(shipment.get("status"), "物流状态", _SHIPMENT_STATES)
    compatible = {"paid": {"pending"}, "cancelled": {"pending"},
                  "shipped": {"in_transit", "exception"}, "delivered": {"delivered"}}
    if status not in compatible[state]:
        raise ValueError("订单状态和来源物流状态矛盾")
    updated = _integer(shipment.get("updated_at"), "物流更新时间", placed, now)
    # An expectation is a plan, not a fact observed in the future.
    _integer(shipment.get("expected_at"), "预计送达时间", placed)
    if "delivered_at" not in shipment:
        raise ValueError("物流缺少签收时间字段")
    delivered = shipment["delivered_at"]
    if status == "delivered":
        _integer(delivered, "签收时间", placed, updated)
    elif delivered is not None:
        raise ValueError("未签收的来源记录不能带有签收时间")
    events = shipment.get("events")
    if not isinstance(events, list) or not events:
        raise ValueError("物流事件缺失，不能进行政策判定")
    previous_at = placed
    observed_delivery = False
    for event in events:
        if not isinstance(event, dict):
            raise ValueError("物流事件必须是字典")
        at = _integer(event.get("at"), "事件时间", previous_at, updated)
        event_status = _text(event.get("status"), "事件状态")
        if not isinstance(event.get("note"), str):
            raise ValueError("事件说明必须是字符串")
        if event_status == "delivered":
            if delivered is None or at < delivered:
                raise ValueError("签收事件与签收时间矛盾")
            observed_delivery = True
        elif observed_delivery or (delivered is not None and at > delivered):
            raise ValueError("签收后不能出现未签收状态的事件")
        if state in {"paid", "cancelled"} and event_status in {"in_transit", "delivered", "exception"}:
            raise ValueError("未发货订单不能包含已发货事件")
        previous_at = at
    if events[-1]["status"] != status:
        raise ValueError("最新物流事件与物流状态矛盾")
    return by_id[item_id]


def _denied(reason: str) -> dict:
    return {"eligible": False, "options": [], "reasons": [reason], "missing": [], "policy_ids": []}


def evaluate_resolution(
    order: dict,
    shipment: dict,
    policies: list[dict],
    *,
    item_id: str,
    quantity: int,
    reason: str,
    statement: str,
    used_quantity: int,
    now: int,
) -> dict:
    """Validate source data and propose one human-reviewed resolution, never execute it.

    Malformed/contradictory inputs raise ValueError. Valid but unsupported requests
    return an explicit denial. The caller supplies previously reserved quantities;
    this pure function cannot reserve capacity or authenticate a customer.
    Highest-version active policy fields are authoritative; overlapping applicable
    rules use the shortest window and longest delay (days are exact 86,400 seconds).
    """
    _integer(now, "now")
    _text(item_id, "item_id")
    _integer(quantity, "申请数量", 1)
    _integer(used_quantity, "已占用数量")
    _choice(reason, "reason", _REASONS)
    if not isinstance(statement, str):
        raise ValueError("客户陈述必须是字符串")
    item = _validate_records(order, shipment, item_id, now)
    if used_quantity > item["quantity"] or quantity > item["quantity"] - used_quantity:
        raise ValueError("申请数量超过原数量减去已占用数量的可申请余额")
    active = _latest_policies(policies)
    if order["state"] == "cancelled":
        return _denied("来源订单已取消，不可重复申请。")
    if item["category"] == "custom" and reason in {"change_mind", "damaged", "exchange"}:
        return _denied("合成商家Demo政策不支持定制商品退货或换货；这不是法律规定。")
    applicable = [policy for policy in active
                  if policy["category"] == item["category"] and policy["reason"] == reason]
    if not applicable:
        return _denied("没有当前有效且适用于本商品类别及申请原因的Demo政策。")
    window = min(policy["window_days"] for policy in applicable) * _DAY
    delay = max(policy["delay_days"] for policy in applicable) * _DAY
    conditions = ["仅提交人工终审申请，当前未退款、未换货，也未承诺审批通过。"]
    if reason == "cancel":
        if order["state"] != "paid" or shipment["status"] != "pending":
            return _denied("取消仅适用于已付款且来源物流尚未发货的订单。")
        kind = "refund_review"
        explanation = "来源记录显示已付款且未发货，可提交取消及退款人工审核。"
        conditions.append("人工终审前须再次核实订单仍未发货，避免与履约操作冲突。")
    elif reason == "not_received":
        kind = "refund_review"
        if shipment["status"] == "delivered":
            if now - shipment["delivered_at"] > window:
                return _denied("已超过合成商家Demo政策的签收争议申请窗口。")
            explanation = "来源物流为已签收，只能提出待核实的未收到争议申请。"
            conditions.append("来源物流仍为已签收；客户陈述不构成物流造假的证明，须人工核实签收争议。")
        elif order["state"] == "shipped" and shipment["status"] in {"in_transit", "exception"}:
            if now - shipment["expected_at"] < delay:
                return _denied("尚未达到合成商家Demo政策要求的完整延迟天数。")
            explanation = "来源物流已达到Demo政策延迟门槛，可提出未收到人工核查申请。"
            conditions.append("须人工核实承运状态、收货事实及客户陈述，不能据陈述改写来源物流。")
        else:
            return _denied("来源订单未发货，不符合未收到政策的延迟或签收争议条件。")
    else:
        if order["state"] != "delivered" or shipment["status"] != "delivered":
            return _denied("退货、损坏或换货申请须有一致的已送达来源记录。")
        if now - shipment["delivered_at"] > window:
            return _denied("已超过合成商家Demo政策自签收起计算的申请窗口。")
        kind = "exchange_review" if reason == "exchange" else "return_refund_review"
        explanation = "商品类别和自签收起的申请时间符合当前Demo政策，可提交人工审核。"
        conditions.append("退回安排、商品状态及损坏陈述（如适用）须由人工核实，不自动证明客户主张。")
    item_value = item["unit_paid_cents"] * quantity
    if kind == "exchange_review":
        amount = 0
        conditions.append(f"换货申请金额为0；商品实付价值{item_value}分仅供核查引用，非资金转出。")
    else:
        amount = item_value
        conditions.append("申请金额仅为商品实付单价乘申请数量，不包含未知运费或其他未经确认的费用。")
    policy_ids = [policy["id"] for policy in applicable]
    conditions.append("依据合成商家Demo政策版本：" + "、".join(
        f"{policy['id']} v{policy['version']}" for policy in applicable) + "；不代表法律规定。")
    option = {"option_id": kind, "kind": kind, "item_id": item_id, "quantity": quantity,
              "amount_cents": amount, "conditions": conditions, "policy_ids": list(policy_ids)}
    return {"eligible": True, "options": [option], "reasons": [explanation],
            "missing": [], "policy_ids": list(policy_ids)}
