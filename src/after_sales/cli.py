"""Explicit local-only entry points. Tokens are written once, never printed."""

from __future__ import annotations

import ipaddress
import json
import os
import time
from pathlib import Path

import typer
import uvicorn

from .agent import require_local_tracing_environment
from .api import create_app
from .provider import LocalHTTPProvider, ProviderError
from .schemas import DomainError
from .storage import Store

app = typer.Typer(no_args_is_help=True, pretty_exceptions_enable=False)


@app.command("init")
def initialize(
    data_dir: Path = typer.Option(..., "--data-dir", help="全新数据目录；不覆盖现有内容"),
    credentials: Path = typer.Option(..., "--credentials", help="显式全新凭据文件；仅写入一次"),
    now: int | None = typer.Option(None, "--now", help="合成时钟Unix秒，缺省使用当前时间"),
):
    """创建原创合成数据，将Demo令牌仅写入指定私有文件。"""
    credentials = credentials.absolute()
    descriptor = None
    created = False
    try:
        if not credentials.parent.is_dir():
            raise DomainError("credentials_parent", "凭据文件父目录必须已存在")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(credentials, flags, 0o600)
        created = True
        os.fchmod(descriptor, 0o600)
        _, tokens = Store.initialize(data_dir, now=int(time.time()) if now is None else now)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            descriptor = None
            json.dump(tokens, output, ensure_ascii=False, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
    except (OSError, ValueError, DomainError):
        if descriptor is not None:
            os.close(descriptor)
        if created:
            credentials.unlink(missing_ok=True)
        typer.echo("初始化失败：目录或凭据路径必须全新且可写，时间须合法；未打印任何令牌。", err=True)
        raise typer.Exit(1) from None
    typer.echo(f"合成数据目录：{data_dir.absolute()}")
    typer.echo(f"私有凭据文件：{credentials}")
    typer.echo("请授权用户自行读取凭据并填入网页；令牌不写入URL或浏览器持久存储。")


@app.command()
def serve(
    data_dir: Path = typer.Option(..., "--data-dir"),
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8000, "--port", min=1, max=65535),
    test_mode: bool = typer.Option(False, "--test-mode", help="显式TEST脚本演示，不是LLM"),
    model_url: str | None = typer.Option(None, "--model-url", help="显式数字回环HTTP模型地址"),
    model: str | None = typer.Option(None, "--model", help="显式本地模型名称"),
):
    """启动数字回环服务；不发现、启动或扫描模型，也不读取环境凭据。"""
    if (test_mode and (model_url is not None or model is not None)) or (
        not test_mode and (not model_url or not model)
    ):
        raise typer.BadParameter("必须互斥选择 --test-mode 或同时指定 --model-url 与 --model")
    try:
        require_local_tracing_environment()
    except DomainError as exc:
        typer.echo(exc.message, err=True)
        raise typer.Exit(1) from None
    try:
        address = ipaddress.ip_address(host)
        if not address.is_loopback or host != address.compressed:
            raise ValueError
    except ValueError:
        raise typer.BadParameter("host必须是规范的数字回环地址") from None
    try:
        if test_mode:
            from .demo_provider import DemoProvider

            provider = DemoProvider()
        else:
            provider = LocalHTTPProvider(model_url, model)
        store = Store(data_dir)
        authority = f"[{host}]:{port}" if address.version == 6 else f"{host}:{port}"
        origin = f"http://{authority}"
        application = create_app(store, provider, origin=origin)
    except (DomainError, ProviderError, ValueError, OSError):
        typer.echo("启动配置无效：请检查本实例数据目录和显式模型配置。", err=True)
        raise typer.Exit(1) from None
    typer.echo("TEST：脚本演示，不是LLM；不执行真实支付或履约。" if test_mode else "本地模型模式；不执行真实支付或履约。")
    typer.echo(f"服务地址：{origin}")
    uvicorn.run(application, host=host, port=port, access_log=False, proxy_headers=False,
                forwarded_allow_ips="", server_header=False, log_level="warning")


def main():
    app()


if __name__ == "__main__":
    main()
