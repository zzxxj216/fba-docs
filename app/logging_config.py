"""轻量日志：get_logger(name) 返回输出到 stdout 的 logger（飞书长连接进程靠它出可见日志）。"""

import logging
import sys

_configured = False


def _ensure():
    global _configured
    if _configured:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s", "%H:%M:%S"))
    root = logging.getLogger("app")
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)
    root.propagate = False
    _configured = True


def get_logger(name="app"):
    _ensure()
    if not name or name == "app" or name.startswith("app."):
        return logging.getLogger(name or "app")
    return logging.getLogger(f"app.{name}")
