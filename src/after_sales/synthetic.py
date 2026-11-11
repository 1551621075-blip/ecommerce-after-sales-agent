"""Original synthetic fixtures expressed only relative to the supplied Unix time."""

_DAY = 86_400


def seed_data(now: int) -> dict[str, list[dict]]:
    """Build independent Demo records without reading a clock or external source.

    At least 40 days of nonnegative Unix history is needed to represent the expired
    case faithfully. These are invented merchant policies, not statutory rights.
    """
    if type(now) is not int or now < 40 * _DAY:
        raise ValueError("now必须是整数Unix秒，且须提供至少40天历史以生成合成场景")

    policy_specs = [
        ("standard", "change_mind", 7, 0, "七天内退货退款", "标准商品签收起7天内可申请改变主意退货退款；定制商品不支持退货。"),
        ("standard", "damaged", 15, 0, "十五天内损坏退货", "标准商品签收起15天内可申请损坏退货退款；损坏事实须人工核实。"),
        ("standard", "exchange", 7, 0, "七天内换货", "标准商品签收起7天内可申请换货，待人工核实商品状态及库存；非资金转出。"),
        ("standard", "not_received", 7, 3, "配送延迟与签收争议", "发货后超过预计送达时间至少3天，或签收起7天内的未收到争议，可申请人工核查。"),
        ("standard", "cancel", 0, 0, "未发货取消订单", "仅已付款且来源物流未发货的订单可申请取消及退款审核。"),
        ("custom", "not_received", 7, 3, "定制商品配送核查", "定制商品也可在延迟至少3天或签收起7天内的未收到争议中申请人工核查，不据客户陈述改写物流。"),
        ("custom", "cancel", 0, 0, "定制商品未发货取消", "定制商品仅已付款未发货时可申请取消审核；本Demo不支持定制商品退货或换货。"),
    ]
    policies = [
        {
            "id": f"demo-{category}-{reason}", "version": 1,
            "title": f"合成商家Demo：{title}",
            "body": body + " 所有申请需人工终审，未退款；金额仅商品实付，不编造运费或未知费用。这是合成商家政策，不是法律规定。",
            "category": category, "reason": reason, "window_days": window,
            "delay_days": delay, "active": True,
        }
        for category, reason, window, delay, title, body in policy_specs
    ]

    # customer, scenario, state, category, days since delivery, product, quantity, cents
    scenarios = [
        ("alice", "delay", "shipped", "standard", None, "合成山色桌面收纳盒", 2, 4900),
        ("alice", "dispute", "delivered", "standard", 2, "合成晨露帆布袋", 1, 6800),
        ("alice", "return", "delivered", "standard", 6, "合成月纹软木杯垫", 3, 1299),
        ("alice", "damaged", "delivered", "standard", 10, "合成云纹陶制笔筒", 2, 8800),
        ("alice", "expired", "delivered", "standard", 20, "合成折页阅读支架", 1, 7600),
        ("alice", "cancel", "paid", "standard", None, "合成青禾旅行收纳袋", 2, 3500),
        ("alice", "cancelled", "cancelled", "standard", None, "合成格纹餐垫", 1, 2200),
        ("alice", "custom", "delivered", "custom", 2, "合成专属刻字木牌", 1, 9900),
        ("alice", "exchange", "delivered", "standard", 4, "合成星点布面书套", 2, 4500),
        ("bob", "return", "delivered", "standard", 3, "合成竹影便携托盘", 1, 5900),
        ("bob", "cancel", "paid", "custom", None, "合成定制色块徽章", 2, 1800),
    ]
    orders = []
    shipments = []
    for customer, scenario, state, category, age, name, quantity, price in scenarios:
        order_id = f"ord-{customer}-{scenario}"
        delivered = now - age * _DAY if age is not None else None
        if delivered is not None:
            placed = delivered - 5 * _DAY
            expected = delivered - _DAY
            updated = delivered
            status = "delivered"
        elif state == "shipped":
            placed, expected, updated = now - 12 * _DAY, now - 5 * _DAY, now - _DAY
            status = "in_transit"
        else:
            placed, expected, updated = now - _DAY, now + 3 * _DAY, now - _DAY
            status = "pending"
        orders.append({
            "id": order_id, "customer_id": customer, "state": state, "placed_at": placed,
            "items": [{"item_id": f"item-{customer}-{scenario}", "product_name": name,
                       "category": category, "quantity": quantity, "unit_paid_cents": price}],
            "currency": "CNY",
        })
        events = [{"at": placed, "status": "pending", "note": "原创合成订单，待仓库处理。"}]
        if state in {"shipped", "delivered"}:
            events.append({"at": placed + _DAY, "status": "in_transit", "note": "合成包裹已由承运方接收。"})
            events.append({
                "at": updated, "status": status,
                "note": "来源合成记录标记已签收。" if delivered is not None else "来源合成记录仍在运输，尚未送达。",
            })
        shipments.append({
            "id": "ship-" + order_id, "order_id": order_id, "status": status,
            "updated_at": updated, "expected_at": expected, "delivered_at": delivered, "events": events,
        })
    return {"orders": orders, "shipments": shipments, "policies": policies}
