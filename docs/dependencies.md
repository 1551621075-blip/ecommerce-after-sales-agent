# 依赖与运行环境

- Python：`>=3.12,<3.13`，每项目独立 `.venv`，不使用旧工程虚拟环境。
- uv：依赖解析与 `uv.lock`；复现时使用 `uv sync --locked`。
- FastAPI / Pydantic / Uvicorn：本地HTTP、严格输入与ASGI运行。
- SQLite：Python标准库，WAL、短事务和版本CAS。
- LangGraph：固定 `1.2.5`，只用单Agent状态图。
- LangSmith：固定 `0.14.17`，只调用本地 `tracing_context(enabled=False)` 显式关闭追踪；不使用其在线服务。
- httpx：显式数字loopback模型请求，禁环境代理、重定向、自动重试。
- Typer：CLI。
- pytest / Ruff / build：开发与验收。
- 浏览器验收：本机已安装 Chrome 与 Node 24；不引入CDN或前端构建工具。

不需要向量数据库、embedding服务、云API、支付SDK或工业算法包。LangGraph的传递依赖由锁文件固定，不代表项目使用这些包的所有服务。

首次离线安装因缺少LangGraph缓存失败，随后从公开软件源完成依赖安装；这不是业务材料外发，也不是模型验收。

源码外安装脚本始终离线：先构建sdist，再从sdist构建wheel；从锁文件导出完整运行时清单，以 `--no-deps` 将清单和wheel装入新venv，随后必须通过 `uv pip check`，并逐项核对实际依赖版本与锁文件一致。最后从源码外运行真实CLI及TEST API/静态资源检查，不借源码 `.venv` 或修改导入路径。

这属于 `offline_fully_locked_install`，不是重新解析依赖的验证。本机首次普通离线解析曾因 `xxhash==4.1.0` 的macOS arm64 wheel元数据缓存缺失而失败；可离线安装对应文件不等于解析所需元数据齐全。脚本不自动联网补缓存，也不将历史诊断当成每次运行的当前结果。

实际版本以 `uv.lock` 为准，不对第三方依赖重新授予许可证，也不擅自为整个项目添加开源许可证。
