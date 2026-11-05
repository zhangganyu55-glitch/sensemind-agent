#!/bin/zsh
set -eu
cd -- "$(dirname -- "$0")"

LOCAL_HEALTH="http://127.0.0.1:8000/health"
PUBLIC_PORT="${GUARDIAN_PUBLIC_PORT:-8010}"
PUBLIC_HEALTH="http://127.0.0.1:${PUBLIC_PORT}/healthz"

if ! curl -sf --max-time 2 "$LOCAL_HEALTH" >/dev/null 2>&1; then
  echo "请先双击“启动守护天使.command”，确认本地服务已经启动。"
  exit 1
fi
if [[ ! -x .venv/bin/python ]]; then
  echo "请先运行“启动守护天使.command”准备运行环境。"
  exit 1
fi
if ! command -v cloudflared >/dev/null 2>&1; then
  echo "未安装 cloudflared。请先执行：brew install cloudflared"
  exit 1
fi

mkdir -p .runtime
PUBLIC_PID=""
cleanup() {
  if [[ -n "$PUBLIC_PID" ]]; then
    kill "$PUBLIC_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

if ! curl -sf --max-time 2 "$PUBLIC_HEALTH" >/dev/null 2>&1; then
  echo "正在启动公共入口（只暴露已认证的 APK /v1 接口）……"
  .venv/bin/python -m uvicorn guardian.public:app \
    --host 127.0.0.1 --port "$PUBLIC_PORT" \
    >> .runtime/public-server.log 2>&1 &
  PUBLIC_PID=$!
fi

for _ in {1..50}; do
  if curl -sf --max-time 1 "$PUBLIC_HEALTH" >/dev/null 2>&1; then
    break
  fi
  sleep 0.2
done
if ! curl -sf --max-time 2 "$PUBLIC_HEALTH" >/dev/null 2>&1; then
  echo "公共入口启动失败，请查看 .runtime/public-server.log"
  exit 1
fi

echo "公共入口已就绪：只包含 /healthz 与 APK /v1 契约，本地 Agent/遥测/文档接口不会暴露。"
echo "正在建立临时 HTTPS 入口（需要外网）。"
echo "把终端出现的 https://xxxx.trycloudflare.com 填进 App 的“业务入口”。"
echo "演示结束按 Control+C 关闭隧道；不要把地址发到公开群。"
cloudflared tunnel --url "http://127.0.0.1:${PUBLIC_PORT}"
