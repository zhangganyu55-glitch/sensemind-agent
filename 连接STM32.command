#!/bin/zsh
set -eu
cd -- "$(dirname -- "$0")"
if [[ ! -x .venv/bin/python ]]; then
  echo "请先运行“启动守护天使.command”准备运行环境。"
  exit 1
fi
echo "正在连接 STM32。保持 ST-Link、传感器和开发板连接。"
echo "设备编号：stm32_f103_001；按 Control+C 停止连接。"
exec .venv/bin/python -m guardian.stm32_bridge
