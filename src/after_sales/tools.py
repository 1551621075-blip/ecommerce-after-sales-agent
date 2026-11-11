from __future__ import annotations

from .actions import Actions
from .policy import search_policies
from .schemas import DomainError, digest, identifier, validate_action

QUESTIONS = (
    "请告诉我需要处理的订单号。",
    "这笔订单遇到了什么问题：未收到、破损、退货、换货还是取消？",
    "请确认需要处理的商品项和数量。",
    "请描述商品问题及目前可提供的凭据；这里只登记陈述，不代表问题已核实。",
    "物流显示已签收，请确认是否本人、家人或代收点签收？",
    "是否需要转交人工客服核查？",
)


class Tools:
    def __init__(self, actions: Actions):
        self.actions, self.store = actions, actions.store

    def _collection(self, db, principal, sid, tool):
        if tool == "search_policies":
            sources = self.store.list(db, "policy", owner="system")
        elif tool == "list_my_orders":
            sources = self.store.list(db, "order", owner=principal.user_id)
        elif tool == "get_case":
            sources = [obj for kind in ("proposal", "application", "ticket")
                       for obj in self.actions._related(db, kind, sid, principal.user_id)]
        else:
            return None
        return digest([obj["content_hash"] for obj in sources])

    def issue(self, db, principal, sid, tool, data, sources):
        return self.store.create(db, "evidence", identifier("ev"), principal.user_id, {
            "session_id": sid, "tool": tool, "data": data,
            "collection_hash": self._collection(db, principal, sid, tool),
            "sources": [{"kind": obj["kind"], "id": obj["id"], "hash": obj["content_hash"]}
                        for obj in sources],
            "captured_at": self.store.now(db),
        })

    def current_evidence(self, db, principal, sid, evidence_id):
        evidence = self.store.get(db, "evidence", evidence_id, owner=principal.user_id)
        if evidence["payload"]["session_id"] != sid:
            raise DomainError("wrong_session", "证据不属于此会话")
        if evidence["payload"]["collection_hash"] != self._collection(
                db, principal, sid, evidence["payload"]["tool"]):
            raise DomainError("stale_evidence", "引用集合已变化，请重新查询")
        for source in evidence["payload"]["sources"]:
            current = self.store.get(db, source["kind"], source["id"])
            if current["content_hash"] != source["hash"]:
                raise DomainError("stale_evidence", "引用证据已变化，请重新查询")
        if self.store.now(db) - evidence["payload"]["captured_at"] >= 900:
            raise DomainError("stale_evidence", "引用已超过15分钟，请重新查询")
        return evidence

    def execute(self, db, principal, sid, action):
        name, args = validate_action(action)
        sess = self.actions.customer_session(db, principal, sid, bot=True)
        sources = []
        if name == "list_my_orders":
            sources = self.store.list(db, "order", owner=principal.user_id)
            data = [{"order_id": o["id"], "state": o["payload"]["state"],
                     "items": o["payload"]["items"], "currency": "CNY"} for o in sources]
        elif name in {"get_order", "get_shipment"}:
            order = self.store.get(db, "order", args["order_id"], owner=principal.user_id)
            sources = [order]
            if name == "get_shipment":
                sources.append(self.store.get(db, "shipment", "ship-" + order["id"], owner=principal.user_id))
            data = sources[-1]["payload"]
        elif name == "search_policies":
            policies = self.store.list(db, "policy", owner="system")
            try:
                data = search_policies(args["query"], [p["payload"] for p in policies], args["category"])
            except ValueError:
                raise DomainError("policy_query", "政策检索参数无效") from None
            ids = {p["id"] for p in data}
            sources = [p for p in policies if p["id"] in ids]
        elif name == "get_case":
            data = {}
            for kind in ("proposal", "application", "ticket"):
                objects = self.actions._related(db, kind, sid, principal.user_id)
                data[kind + "s"] = objects
                sources.extend(objects)
            data["notice"] = "状态仅为本地演示记录；approved_demo不代表支付或履约执行。"
        elif name == "evaluate_resolution":
            evaluation = self.actions.evaluate(db, principal, sid, args)
            return {"result": evaluation, "terminal": None}
        elif name == "propose_resolution":
            proposal = self.actions.propose(db, principal, sid, args["evaluation_id"], args["option_id"])
            self._say(db, principal, sid, {"role": "assistant", "kind": "proposal",
                                        "text": "请核对右侧方案卡；只有点击确认才会提交申请。",
                                        "proposal_id": proposal["id"]})
            return {"result": proposal, "terminal": "waiting_confirmation"}
        elif name == "ask_customer":
            if args["question"] not in QUESTIONS:
                raise DomainError("question_template", "请选择上下文中allowed_questions的一条原文问题")
            self._say(db, principal, sid, {"role": "assistant", "kind": "question", "text": args["question"]})
            return {"result": {"question": args["question"]}, "terminal": "waiting_customer"}
        elif name == "handoff_to_human":
            ticket = self.actions.handoff(db, principal, sid, args["reason"])
            self._say(db, principal, sid, {"role": "assistant", "kind": "handoff",
                                        "text": "已进入本地人工队列，等待客服领取。"})
            return {"result": ticket, "terminal": "handed_off"}
        elif name == "respond":
            evidence = [self.current_evidence(db, principal, sid, eid) for eid in args["evidence_ids"]]
            if args["kind"] != "empathy" and not evidence:
                raise DomainError("missing_evidence", "业务回答必须先获取当前证据")
            if args["kind"] == "policy" and not all(e["payload"]["tool"] == "search_policies" for e in evidence):
                raise DomainError("policy_evidence", "政策回答只能引用政策检索结果")
            text = {"facts": "已查询到记录，请查看下方引用卡片中的当前事实。",
                    "policy": "适用政策原文及版本见引用卡片，申请资格仍需结合具体订单评估。",
                    "empathy": "理解你的困扰，我可以查询订单、核对政策，或协助转交人工。"}[args["kind"]]
            # Operational claims never come from free-form model text, including disguised empathy.
            self._say(db, principal, sid, {"role": "assistant", "kind": args["kind"], "text": text,
                                        "evidence_ids": [e["id"] for e in evidence]})
            return {"result": {"text": text, "evidence_ids": args["evidence_ids"],
                               "model_text_published": False}, "terminal": "completed"}
        else:
            raise DomainError("invalid_tool", "工具不存在")
        evidence = self.issue(db, principal, sess["id"], name, data, sources)
        return {"result": evidence, "terminal": None}

    def _say(self, db, principal, sid, message):
        sess = self.actions.customer_session(db, principal, sid)
        self.store.save(db, sess, {**sess["payload"], "messages": [*sess["payload"]["messages"], message]})
