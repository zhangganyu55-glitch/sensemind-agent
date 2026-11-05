#!/bin/zsh
set -eu
cd -- "$(dirname -- "$0")"
if [[ ! -x .venv/bin/python ]]; then
  echo "未找到 .venv；先运行“启动守护天使.command”完成环境准备。"
  exit 1
fi
PORT="${GUARDIAN_PORT:-8000}"
echo "知微 SenseMind Mac 上位机"
echo "控制台：http://127.0.0.1:${PORT}/console"
echo "仅绑定 127.0.0.1，不暴露到局域网或公网。Control+C 停止。"
( sleep 2; open "http://127.0.0.1:${PORT}/console" ) &
exec .venv/bin/python -m uvicorn guardian.main:app --host 127.0.0.1 --port "${PORT}"
