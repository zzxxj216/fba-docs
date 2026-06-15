# -*- coding: utf-8 -*-
"""验收2 常驻 worker：本机 sqlalchemy 首次导入异常缓慢(约20分钟，疑似安全软件
扫描原生扩展)。该进程只导入一次，然后轮询命令文件执行任务脚本，并在每次执行前
热重载 app 模块（拾取代码修改）。

命令文件:  %TEMP%/claude/acc2_cmd.txt   内容: "<tag>|<script_path>"
完成标记:  %TEMP%/claude/acc2_done.txt  内容: "<tag>|OK|FAIL"
"""
import importlib
import os
import runpy
import sys
import time
import traceback

sys.path.insert(0, r"D:\amazon")
print("importing app (may take very long on this machine)...", flush=True)
t0 = time.time()
import app.database          # noqa: E402  (一次性付出慢导入成本)
import app.rule_engine       # noqa: E402
import app.field_registry    # noqa: E402
import app.excel_engine      # noqa: E402
from app.services import generate_service, import_service, validate_service  # noqa: E402
print("ready, import took %.1fs" % (time.time() - t0), flush=True)

CMD = r"C:\Users\zane\AppData\Local\Temp\claude\acc2_cmd.txt"
DONE = r"C:\Users\zane\AppData\Local\Temp\claude\acc2_done.txt"


def reload_all():
    # 不 reload app.models（SQLAlchemy 表重复定义会炸）；模型已稳定。
    for m in (app.rule_engine, app.field_registry,
              app.excel_engine, validate_service, import_service,
              generate_service):
        try:
            importlib.reload(m)
        except Exception:
            traceback.print_exc()


last = ""
while True:
    try:
        if os.path.exists(CMD):
            s = open(CMD, encoding="utf-8").read().strip()
            if s and s != last:
                last = s
                tag, path = s.split("|", 1)
                print(f">>> exec {tag}: {path}", flush=True)
                reload_all()
                try:
                    runpy.run_path(path, run_name="__main__")
                    status = "OK"
                except SystemExit:
                    status = "OK"
                except Exception:
                    traceback.print_exc()
                    status = "FAIL"
                open(DONE, "w", encoding="utf-8").write(f"{tag}|{status}")
                print(f"<<< {tag} {status}", flush=True)
    except Exception:
        traceback.print_exc()
    time.sleep(2)
