"""进程入口：装配 app.bootstrap 并启动 Uvicorn（单 Worker、仅回环监听）。"""

import argparse

import uvicorn

from app.bootstrap import create_app

_LOOPBACK_CHOICES = ("127.0.0.1", "localhost", "::1")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="fit-agent",
        description="Fit-Agent 本地服务（单 Uvicorn Worker，仅回环监听）",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        choices=_LOOPBACK_CHOICES,
        help="监听地址（仅回环，10.1）",
    )
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    uvicorn.run(create_app(), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
