from __future__ import annotations

from .policy import evaluate_resolution
from .schemas import (
    Command,
    DomainError,
    MessageCommand,
    NoteCommand,
    ProposalCommand,
    ReviewCommand,
    VersionedCommand,
    digest,
    identifier,
)
from .storage import Store

APPLICATION_PENDING = "submitted_pending_review"
OCCUPYING = {APPLICATION_PENDING, "approved_demo"}
LIVE_RUNS = {"queued", "running"}


def check_version(obj, expected):
    if type(expected) is not int or obj["version"] != expected:
        raise DomainError("stale_version", "对象版本已变化，请刷新后重试", 409)


class Actions:
    def __init__(self, store: Store):
        self.store = store

    def _command(self, principal, op, target, command, model, execute):
        command = model.model_validate(command.model_dump())
        request = {"op": op, "target": target, **command.model_dump()}
        with self.store.transaction() as db:
            old = self.store.receipt(db, principal, command.action_id, request)
            if old is not None:
                return old
            result = execute(db, command)
            self.store.record_receipt(db, principal, command.action_id, request, result)
            self.store.audit(db, principal, op, target or result.get("id", "new"))
            return result

    def customer_session(self, db, principal, sid, *, bot=False):
        self.store.require(db, principal, "customer")
        sess = self.store.get(db, "session", sid, owner=principal.user_id)
        if bot and sess["payload"]["control"] != "bot":
            raise DomainError("human_control", "会话已交给人工，Agent不能继续操作", 409)
        return sess

    def create_session(self, principal, command):
        def execute(db, cmd):
            self.store.require(db, principal, "customer")
            if len(self.store.list(db, "session", owner=principal.user_id)) >= 30:
                raise DomainError("session_limit", "本地演示最多保留30个会话", 409)
            return self.store.create(db, "session", identifier("sess"), principal.user_id, {
                "control": "bot", "control_epoch": 1, "active_run": None,
                "messages": [], "tool_count": 0, "blocked_unknown": False,
            })
        return self._command(principal, "create_session", "", command, Command, execute)

    def _related(self, db, kind, sid, owner):
        return [obj for obj in self.store.list(db, kind, owner=owner)
                if obj["payload"].get("session_id") == sid]

    def _supersede(self, db, sid, owner):
        for obj in self._related(db, "proposal", sid, owner):
            if obj["payload"]["state"] == "pending_confirmation":
                self.store.save(db, obj, {**obj["payload"], "state": "superseded"})

    def message(self, principal, sid, command):
        def execute(db, cmd):
            sess = self.customer_session(db, principal, sid)
            check_version(sess, cmd.expected_version)
            payload = sess["payload"]
            if payload["control"] == "closed":
                raise DomainError("closed", "会话已关闭", 409)
            outstanding = db.execute(
                "SELECT c.state FROM calls c JOIN objects r ON r.kind='run' AND r.id=c.run_id "
                "WHERE c.owner=? AND json_extract(r.payload,'$.session_id')=? "
                "AND c.state IN ('started','unknown')", (principal.user_id, sid)).fetchall()
            if payload["blocked_unknown"] or outstanding:
                raise DomainError("blocked_unknown", "有在途或未知模型调用，需等待返回或先对账", 409)
            if payload["active_run"]:
                raise DomainError("run_active", "当前轮次尚未结束，请等待或取消", 409)
            if len(payload["messages"]) >= 100:
                raise DomainError("message_limit", "本会话消息已达到上限，请新建会话", 409)
            self._supersede(db, sid, principal.user_id)
            messages = [*payload["messages"], {"role": "customer", "text": cmd.text}]
            run = None
            if payload["control"] == "bot":
                run = self.store.create(db, "run", identifier("run"), principal.user_id, {
                    "session_id": sid, "state": "queued", "control_epoch": payload["control_epoch"],
                    "calls": 0, "tools": 0, "guard_rejections": 0, "trace": [],
                    "pending_action": None, "started_at": self.store.now(db),
                })
            sess = self.store.save(db, sess, {**payload, "messages": messages,
                                             "active_run": run["id"] if run else None})
            return {"session": sess, "run": run}
        return self._command(principal, "message", sid, command, MessageCommand, execute)

    def _freeze(self, db, sess, control):
        payload = sess["payload"]
        if payload["active_run"]:
            run = self.store.get(db, "run", payload["active_run"])
            if run["payload"]["state"] in LIVE_RUNS:
                self.store.save(db, run, {**run["payload"], "state": "cancelled", "pending_action": None})
        return self.store.save(db, sess, {**payload, "control": control,
                                         "control_epoch": payload["control_epoch"] + 1,
                                         "active_run": None})

    def cancel(self, principal, sid, command):
        def execute(db, cmd):
            sess = self.customer_session(db, principal, sid)
            check_version(sess, cmd.expected_version)
            self._supersede(db, sid, principal.user_id)
            return self._freeze(db, sess, sess["payload"]["control"])
        return self._command(principal, "cancel", sid, command, VersionedCommand, execute)

    def used_quantity(self, db, owner, order_id, item_id):
        return sum(obj["payload"]["option"]["quantity"]
                   for obj in self.store.list(db, "application", owner=owner)
                   if obj["payload"]["state"] in OCCUPYING
                   and obj["payload"]["order_id"] == order_id
                   and obj["payload"]["option"]["item_id"] == item_id)

    def _evaluate(self, db, principal, args):
        order = self.store.get(db, "order", args["order_id"], owner=principal.user_id)
        shipment = self.store.get(db, "shipment", "ship-" + order["id"], owner=principal.user_id)
        policies = self.store.list(db, "policy", owner="system")
        used = self.used_quantity(db, principal.user_id, order["id"], args["item_id"])
        try:
            result = evaluate_resolution(order["payload"], shipment["payload"],
                                         [p["payload"] for p in policies],
                                         item_id=args["item_id"], quantity=args["quantity"],
                                         reason=args["reason"], statement=args["statement"],
                                         used_quantity=used, now=self.store.now(db))
        except ValueError:
            raise DomainError("invalid_evidence", "订单、物流或政策证据不满足评估条件") from None
        bindings = {"order": order["content_hash"], "shipment": shipment["content_hash"],
                    "policies": digest([p["content_hash"] for p in policies]), "used_quantity": used}
        return result, bindings

    def evaluate(self, db, principal, sid, args):
        from .schemas import EvaluateArgs

        self.customer_session(db, principal, sid, bot=True)
        args = EvaluateArgs.model_validate(args).model_dump()
        result, bindings = self._evaluate(db, principal, args)
        return self.store.create(db, "evaluation", identifier("eval"), principal.user_id, {
            "session_id": sid, "inputs": args, "result": result, "bindings": bindings,
            "expires_at": self.store.now(db) + 900,
        })

    def _fresh_evaluation(self, db, principal, evaluation):
        payload = evaluation["payload"]
        if self.store.now(db) >= payload["expires_at"]:
            raise DomainError("expired", "方案依据已过期，请重新评估和确认", 409)
        result, bindings = self._evaluate(db, principal, payload["inputs"])
        if bindings != payload["bindings"] or result != payload["result"]:
            raise DomainError("evidence_changed", "相关证据或申请占用已变化，需重算并重新确认", 409)
        return result

    def propose(self, db, principal, sid, evaluation_id, option_id):
        self.customer_session(db, principal, sid, bot=True)
        evaluation = self.store.get(db, "evaluation", evaluation_id, owner=principal.user_id)
        if evaluation["payload"]["session_id"] != sid:
            raise DomainError("wrong_session", "评估结果不属于当前会话", 409)
        result = self._fresh_evaluation(db, principal, evaluation)
        option = next((o for o in result["options"] if o["option_id"] == option_id), None)
        if option is None:
            raise DomainError("invalid_option", "必须选择程序评估允许的方案")
        self._supersede(db, sid, principal.user_id)
        return self.store.create(db, "proposal", identifier("prop"), principal.user_id, {
            "session_id": sid, "evaluation_id": evaluation_id, "option": option,
            "order_id": evaluation["payload"]["inputs"]["order_id"],
            "state": "pending_confirmation", "expires_at": evaluation["payload"]["expires_at"],
            "notice": "确认后仅提交本地申请，所有申请需人工终审，不执行支付或履约。",
        })

    def _ticket(self, db, principal, sess, reason, application_id=None):
        active = [t for t in self._related(db, "ticket", sess["id"], principal.user_id)
                  if t["payload"]["state"] in {"queued", "claimed"}]
        if active:
            raise DomainError("ticket_active", "当前会话已有人工工单", 409)
        ticket = self.store.create(db, "ticket", identifier("ticket"), principal.user_id, {
            "session_id": sess["id"], "application_id": application_id, "reason": reason,
            "state": "queued", "staff_id": None, "notes": [],
        })
        self._freeze(db, sess, "human_queue")
        return ticket

    def handoff(self, db, principal, sid, reason):
        sess = self.customer_session(db, principal, sid, bot=True)
        self._supersede(db, sid, principal.user_id)
        return self._ticket(db, principal, sess, reason)

    def decide_proposal(self, principal, proposal_id, command):
        def execute(db, cmd):
            self.store.require(db, principal, "customer")
            proposal = self.store.get(db, "proposal", proposal_id, owner=principal.user_id)
            check_version(proposal, cmd.expected_version)
            if proposal["content_hash"] != cmd.content_hash:
                raise DomainError("proposal_hash", "方案内容不匹配，请刷新", 409)
            payload = proposal["payload"]
            if payload["state"] != "pending_confirmation":
                raise DomainError("proposal_state", "方案已处理或失效", 409)
            sess = self.customer_session(db, principal, payload["session_id"], bot=True)
            if cmd.decision == "decline":
                updated = self.store.save(db, proposal, {**payload, "state": "declined"})
                return {"proposal": updated, "message": "已拒绝此方案，未创建申请。"}
            evaluation = self.store.get(db, "evaluation", payload["evaluation_id"], owner=principal.user_id)
            result = self._fresh_evaluation(db, principal, evaluation)
            if payload["option"] not in result["options"]:
                raise DomainError("option_changed", "允许方案已变化", 409)
            application = self.store.create(db, "application", identifier("app"), principal.user_id, {
                "session_id": sess["id"], "proposal_id": proposal_id, "order_id": payload["order_id"],
                "option": payload["option"], "state": APPLICATION_PENDING,
                "submitted_at": self.store.now(db), "decision_note": None,
            })
            ticket = self._ticket(db, principal, sess, "售后申请人工终审", application["id"])
            updated = self.store.save(db, proposal, {**payload, "state": "confirmed"})
            return {"proposal": updated, "application": application, "ticket": ticket,
                    "message": "申请已提交，等待人工审核。未执行支付、退货入库或换货出库。"}
        return self._command(principal, "decide_proposal", proposal_id, command, ProposalCommand, execute)

    def withdraw(self, principal, application_id, command):
        def execute(db, cmd):
            self.store.require(db, principal, "customer")
            application = self.store.get(db, "application", application_id, owner=principal.user_id)
            check_version(application, cmd.expected_version)
            if application["payload"]["state"] != APPLICATION_PENDING:
                raise DomainError("application_state", "只可撤回未决申请", 409)
            result = self.store.save(db, application, {**application["payload"], "state": "withdrawn"})
            self.store.audit(db, principal, "withdraw_pending_application", application_id)
            return result
        return self._command(principal, "withdraw", application_id, command, VersionedCommand, execute)

    def queue(self, principal):
        with self.store.transaction() as db:
            self.store.require(db, principal, "staff")
            return [{"id": obj["id"], "version": obj["version"],
                     "state": obj["payload"]["state"],
                     "category": "申请审核" if obj["payload"]["application_id"] else "客户转人工"}
                    for obj in self.store.list(db, "ticket")
                    if obj["payload"]["state"] == "queued"
                    or (obj["payload"]["state"] == "claimed"
                        and obj["payload"]["staff_id"] == principal.user_id)]

    def claim(self, principal, ticket_id, command):
        def execute(db, cmd):
            self.store.require(db, principal, "staff")
            ticket = self.store.get(db, "ticket", ticket_id)
            check_version(ticket, cmd.expected_version)
            if ticket["payload"]["state"] != "queued":
                raise DomainError("ticket_state", "工单已被领取或已结束", 409)
            sess = self.store.get(db, "session", ticket["payload"]["session_id"], owner=ticket["owner"])
            self._freeze(db, sess, "human_owned")
            return self.store.save(db, ticket, {**ticket["payload"], "state": "claimed",
                                                "staff_id": principal.user_id})
        return self._command(principal, "claim", ticket_id, command, VersionedCommand, execute)

    def _claimed(self, db, principal, ticket_id):
        self.store.require(db, principal, "staff")
        ticket = self.store.get(db, "ticket", ticket_id)
        if ticket["payload"]["state"] != "claimed" or ticket["payload"]["staff_id"] != principal.user_id:
            raise DomainError("unclaimed", "只有工单当前领取人可访问或处理", 403)
        return ticket

    def ticket_detail(self, principal, ticket_id):
        with self.store.transaction() as db:
            ticket = self._claimed(db, principal, ticket_id)
            payload = ticket["payload"]
            session = self.store.get(db, "session", payload["session_id"], owner=ticket["owner"])
            application = (self.store.get(db, "application", payload["application_id"], owner=ticket["owner"])
                           if payload["application_id"] else None)
            result = {"ticket": ticket, "messages": session["payload"]["messages"],
                      "application": application}
            if application:
                proposal = self.store.get(db, "proposal", application["payload"]["proposal_id"],
                                          owner=ticket["owner"])
                result["evaluation"] = self.store.get(db, "evaluation", proposal["payload"]["evaluation_id"],
                                                       owner=ticket["owner"])
                oid = application["payload"]["order_id"]
                result["order"] = self.store.get(db, "order", oid, owner=ticket["owner"])
                result["shipment"] = self.store.get(db, "shipment", "ship-" + oid, owner=ticket["owner"])
            return result

    def review(self, principal, ticket_id, command):
        def execute(db, cmd):
            ticket = self._claimed(db, principal, ticket_id)
            check_version(ticket, cmd.expected_version)
            if not ticket["payload"]["application_id"]:
                raise DomainError("no_application", "此工单没有待审核申请", 409)
            application = self.store.get(db, "application", ticket["payload"]["application_id"],
                                         owner=ticket["owner"])
            if application["payload"]["state"] != APPLICATION_PENDING:
                raise DomainError("application_state", "申请已处理或已撤回", 409)
            application = self.store.save(db, application, {**application["payload"], "state": cmd.decision,
                                                             "decision_note": cmd.note})
            ticket = self.store.save(db, ticket, {**ticket["payload"],
                                                  "notes": [*ticket["payload"]["notes"], cmd.note]})
            return {"ticket": ticket, "application": application,
                    "message": "人工审核结果已记录；本地演示不执行支付或履约。"}
        return self._command(principal, "review", ticket_id, command, ReviewCommand, execute)

    def end_handoff(self, principal, ticket_id, command):
        def execute(db, cmd):
            ticket = self._claimed(db, principal, ticket_id)
            check_version(ticket, cmd.expected_version)
            if ticket["payload"]["application_id"]:
                application = self.store.get(db, "application", ticket["payload"]["application_id"],
                                             owner=ticket["owner"])
                if application["payload"]["state"] == APPLICATION_PENDING:
                    raise DomainError("review_pending", "请先处理或由客户撤回待审申请", 409)
            sess = self.store.get(db, "session", ticket["payload"]["session_id"], owner=ticket["owner"])
            self._freeze(db, sess, "bot")
            return self.store.save(db, ticket, {**ticket["payload"], "state": "resolved",
                                                "notes": [*ticket["payload"]["notes"], cmd.note]})
        return self._command(principal, "end_handoff", ticket_id, command, NoteCommand, execute)

    def view(self, principal, sid):
        with self.store.transaction() as db:
            sess = self.customer_session(db, principal, sid)
            result = {"session": sess, "now": self.store.now(db)}
            for kind in ("proposal", "application", "ticket", "evidence", "run"):
                result[kind + "s"] = self._related(db, kind, sid, principal.user_id)
            for prop in result["proposals"]:
                prop["display_expired"] = prop["payload"]["expires_at"] <= result["now"]
            return result
