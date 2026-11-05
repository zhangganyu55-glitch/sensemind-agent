#!/bin/zsh
set -eu
cd -- "$(dirname -- "$0")"
if [[ ! -x .venv/bin/python ]]; then
  echo "首次启动，正在准备运行环境……"
  python3 -m venv .venv
  .venv/bin/python -m pip install -r requirements.txt
fi
echo "守护天使正在启动。打开 http://127.0.0.1:8000/docs 使用问答接口。"
echo "监控控制台：http://127.0.0.1:8000/console"
echo "手机 APK 接入请另开“手机接入HTTPS.command”；配对码见下方，默认 10 分钟、仅一次有效。"
echo "关闭此窗口或按 Control+C 停止服务。"
exec .venv/bin/python -m uvicorn guardian.main:app --host 127.0.0.1 --port 8000
