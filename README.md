# 电商售后客服 Agent

用于生产部署前本地验证的 **Demo／测试版**。中文客服工作台 + 独立人工队列，使用原创合成订单、物流与商家政策，不接真实支付或业务系统。

不是单纯聊天框：模型每次从十个工具中选择一个行动，程序执行后把真实结果返回模型，再由模型决定查证、补问、提出方案或转人工。金额、资格、权限、确认和人工审批由程序控制。

## 当前交付状态

本地TEST演示版已通过程序、浏览器和安装验证，可用于本地体验；真实模型完整验收仍待完成。全量自动化454项测试通过（另有427个参数子检查），两次独立TEST浏览器完整闭环及源码外锁定依赖安装通过。验收器已补负例并修复已发现的漏检，修后30场景与8核心TEST机器检查通过；针对原八类漏检的限定独立复核也已通过，包含47项测试及42次定向场景运行，不代表全系统无缺陷。真实模型核心8例尚未运行，人工解释审查也未完成，因此还不能称完整交付。不要把 `TEST` 当成真实模型成绩，详情见 [验收记录](docs/validation.md)。

## 业务流程

1. 客户咨询政策、查询本人订单和物流，说明问题。
2. 模型自主选择查询与补问；程序根据版本化政策评估申请选项。
3. 模型提出具体方案，客户核对订单项、数量、金额和条件后点击确认。
4. 系统原子创建本地申请与人工工单；工作人员领取、审核、结束接管。
5. 客户查询结果，或在审核前撤回申请。

**所有退换货／退款类申请都需人工终审。** `approved_demo` 只表示本地演示审核通过，绝不表示资金到账、退货入库或换货出库。自然语言“好的”不是方案卡确认。

## 本地运行

要求 Python 3.12、uv。运行目录与一次性凭据文件放在源码外，不要提交到仓库。

```bash
uv sync --locked
uv run after-sales --help
uv run after-sales init --data-dir /tmp/after-sales-demo-data --credentials /tmp/after-sales-demo-credentials.json
uv run after-sales serve --data-dir /tmp/after-sales-demo-data --port 8000 --test-mode
```

初始化目录和凭据文件必须全新。根据 CLI 输出的路径，在本机查看凭据文件，复制相应角色的令牌到页面。不要把令牌贴进对话、URL 或日志。打开 `http://127.0.0.1:8000`。

`--test-mode` 是固定演示策略，不是 LLM；页面会明确标识。真实模型必须改为显式参数，两种模式互斥：

```bash
uv run after-sales serve --data-dir /tmp/after-sales-demo-data --port 8000 \
  --model-url http://127.0.0.1:YOUR_PORT --model YOUR_MODEL_ID
```

上述模型地址仅为占位示例，不能直接运行。项目不包含、不下载、不启动模型运行时。接口需要兼容 `POST /v1/chat/completions` 和严格 JSON Schema 输出，具体服务兼容性须实际验证。

## 示例

使用 Alice 身份新建会话，输入：

> 我要退货 ord-alice-return 1件，不想要了。

查看模型动作与证据卡，确认方案后切换 Staff 身份，在人工队列领取、审核并结束接管。再以 Alice 身份询问：

> 我的售后申请现在什么状态？

更多角色与恢复操作见 [使用说明](docs/usage.md)。

## 验证

```bash
uv run pytest
uv run ruff check .
node --check src/after_sales/static/app.js

# 新建源码外结果目录；保留每例成功、失败、未知和未运行
uv run python scripts/run_acceptance.py --test-mode --core --output-dir /tmp/after-sales-core-check
uv run python scripts/run_acceptance.py --test-mode --all --output-dir /tmp/after-sales-all-check

# macOS，本机Chrome与Node 24；脚本创建独立profile和合成数据
node scripts/browser_smoke.mjs --port 18761 --cdp-port 18762
uv run python scripts/check_install.py
```

验收输出目录必须全新，重复运行请换新目录而不是覆盖历史记录。安装脚本只使用离线公开依赖缓存，缺缓存会失败，不自动联网。

获得明确的本机模型地址与模型ID后，才执行真实模型核心验收（占位值不能直接运行）：

```bash
uv run python scripts/run_acceptance.py --model-url http://127.0.0.1:YOUR_PORT \
  --model YOUR_MODEL_ID --core --output-dir /tmp/after-sales-real-core-check
```

脚本保留失败、未知与未运行，不自动重跑挑选成功。解释质量人工审查是另一道验收，机器通过不会自动改写为人审通过。

浏览器、源码外安装、冻结场景和真实模型的结果分别记录，不能互相替代。

- [架构与模型决策边界](docs/architecture.md)
- [逐步场景讲解](docs/walkthrough.md)
- [依赖说明](docs/dependencies.md)

不采集真实客户资料，不作生产性能或商业成效声明。项目未配置 GitHub 发布、部署或整体开源许可证。
