"""
app/__main__.py — 一条命令启动: python -m app

默认只监听 127.0.0.1。端口可用 NI43_PORT 覆盖。
"""
import os

import uvicorn

from app.server import app


def main() -> None:
    host = os.environ.get("NI43_HOST", "127.0.0.1")
    port = int(os.environ.get("NI43_PORT", "8000"))
    print(f"NI 43-101 平台: http://{host}:{port}")
    print("提示: 单机自用, 没有鉴权; 不要把端口暴露到公网。")
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
