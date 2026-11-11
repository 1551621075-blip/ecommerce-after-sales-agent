# 架构与边界

## 一轮请求如何运行

```text
浏览器提交消息 + action_id + expected_version
  → API身份/同源/JSON边界
  → 短SQLite事务：收据去重、保存消息、创建queued run
  → 后台线程运行LangGraph
      agent → guard → tools → agent
        │        │        └→ 真实Observation写账，再交给模型
        │        └→ 拒绝非法动作，错误Observation交给模型重新选择
        └→ 一个本地结构化HTTP请求，不自动重试
  → ask/propose/respond/handoff暂停或终止
```

框架为 LangGraph 1.2.5，单 Agent，不是多个角色互聊。暂停、确认、人工接管与恢复保存在 SQLite；不使用 LangGraph interrupt/checkpointer。刷新和轮询不启动模型。

## 模型决定什么

- 当前该读订单、查物流、检索政策还是先补问。
- 根据已返回的事实，是否继续取证、评估方案、询问客户或转人工。
- 引用哪个本会话评估结果、从中选择哪个允许的方案。
- 什么时候结束当前调查。

工具：`list_my_orders`、`get_order`、`get_shipment`、`search_policies`、`get_case`、`evaluate_resolution`、`ask_customer`、`propose_resolution`、`handoff_to_human`、`respond`。

不是由后端固定要求依次跑完十个工具。证明点不是“路径不同”，而是每步选择实际来自 Provider 响应，执行结果进入下一次 Provider 请求。`ScriptedProvider`、`DemoProvider` 只是显式 TEST 替身，不能提供真实模型自主决策的验收证据。

## 程序负责什么

| 边界 | 实施方式 |
|---|---|
| 身份 | 令牌hash认证，数据库重新核验 active/role，模型无法传入角色 |
| 金额与数量 | CNY整数分，严格int，拒bool/float；金额来自订单实付 |
| 资格 | 原创版本化商家政策、物流事实、时效、已占用数量 |
| 确认 | proposal ID/version/hash/action_id，事务内重算；自然语言同意无效 |
| 幂等 | 用户+action_id绑定请求摘要；异内容冲突，不同ID仍受业务占用限制 |
| 人工 | 未领取最少摘要；领取后仅工单上下文，不能改确认金额 |
| 状态输出 | 订单/申请/资金等关键事实用后端卡片，不发布模型自报的操作结果 |
| 成本与失败 | 调用/工具计数上限、token与主动耗时软门槛；未知先阻断后人工对账 |

当前问题展示使用后端许可的澄清模板，共情和状态引导也由后端模板生成。模型 `respond.text` 不直接面向客户发布。这比依赖关键词屏蔽“已到账”更保守；并不阻止模型选择下一业务动作。后续若开放自由政策解释，必须另外做逐条事实忠实度评测。

## 业务状态互相独立

- Session：`bot / human_queue / human_owned / closed`。
- Run：`queued / running`；正常结束为等待客户、等待确认、完成、交接；异常为取消、预算耗尽、中断、未知等。
- Proposal：`pending_confirmation / confirmed / declined / superseded`；过期由后端时钟和有效期核验并在页面提示。
- Application：`submitted_pending_review → approved_demo / rejected / withdrawn`。
- Ticket：`queued → claimed → resolved`。

`approved_demo` 没有支付含义。已批准申请仍占用商品数量；被拒绝或已撤回申请释放占用。一个会话同一时刻只允许一个待处理人工工单。

客户确认时程序重新评估；之后人工审批不自动重跑原评估，也不把原方案15分钟有效期当成申请审核时限。人工查看当前订单/物流和原评估，负责再次核实适用条件，仅记录对已确认方案的演示决定。它不是自动履约的最终资格检查，接入真实执行系统前仍需另设执行前校验。

`max_tokens` 是调用前与返回后的软门槛，不是模型服务的硬计费上限。例如门槛200、两次各128 tokens，账本仍记录实际256，但第二次行动不执行。在途HTTP不能靠取消追回用量；主动耗时也会受单次请求时限影响。

## 迟到结果与未知调用

HTTP在数据库写事务外执行。发送前写call账本并记下run/session版本；返回后先记录usage，再重新检查版本和控制权。取消或接管后的返回不得继续执行旧动作。

请求已发送但结果或计量未知时，call为`unknown`，session为`blocked_unknown`。保留未知，不假装0，不重放旧动作。操作员必须依据实际本地模型记录填写计量和对账依据。对账不会清空调用预算，也不会自动恢复旧run；客户发新消息才产生新轮次。

重启时把未完成调用转未知，其他未完成run结束为中断。单个数据目录只允许一个服务进程持有锁，防止另一个进程把正在执行的调用误判为崩溃。

## 数据与时钟

SQLite WAL、短 `BEGIN IMMEDIATE` 事务、对象版本CAS、请求绑定回执与审计。对象hash用于检测意外改动，不宣称能抵御拥有数据库写权限的攻击者。

初始化使用合成业务时钟；以保存的墙钟锚点推进，避免方案永不过期。合成订单与政策是本 Demo 自己的事实，不是法律条款，也不是真实商家业务数据。

单机 Demo 不承诺分布式一致性、防本机管理员篡改或生产吞吐能力。
