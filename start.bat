@echo off
cd /d %~dp0
echo FBA doc system -> http://127.0.0.1:8000
start "" http://127.0.0.1:8000
rem 本机安全软件扫描使 Python 模块重载极慢，--reload 会卡死服务，勿加
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
pause
