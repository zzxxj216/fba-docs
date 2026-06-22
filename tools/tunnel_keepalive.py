"""gradio 隧道保活：用固定子域名常驻暴露本机 8000 到公网，72h 过期后自动重起。

固定 URL = https://<由 SUBDOMAIN 哈希出的固定串>.gradio.live （同一 SUBDOMAIN 恒定不变）。
重起前先杀残留 frpc 并等待，避免"同子域名再注册冲突(new proxy error)"。

用法：python tools/tunnel_keepalive.py    （建议注册成开机自启计划任务，常驻后台）
改子域名：改下面 SUBDOMAIN 即可（URL 会随之变成另一个固定串）。
"""

import subprocess
import sys
import time

SUBDOMAIN = "fbadocs-zane"   # 固定子域名标识（决定固定 URL；想换 URL 改这里）
PORT = "8000"
RELEASE_WAIT = 60            # 杀残留后等服务器释放子域名的秒数


def _kill_stray_frpc():
    subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "Get-CimInstance Win32_Process | Where-Object { $_.Name -match 'frpc' } | "
         "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"],
        capture_output=True)


def main():
    while True:
        _kill_stray_frpc()
        time.sleep(RELEASE_WAIT)         # 等旧注册释放，避免冲突
        print(f"[keepalive] start tunnel :{PORT} --sd {SUBDOMAIN}", flush=True)
        proc = subprocess.Popen(
            [sys.executable, "-u", "-c",
             f"import sys; sys.argv=['gt','--port','{PORT}','--sd','{SUBDOMAIN}']; "
             "from gradio_tunneling.main import main; main()"])
        proc.wait()                       # 阻塞 ~72h 或异常退出
        print("[keepalive] tunnel exited, restarting...", flush=True)
        time.sleep(5)


if __name__ == "__main__":
    main()
