# 协作协议（Claude Code + Codex）

本仓库由两个 agent 共同维护：**Claude Code**（启动时读 `CLAUDE.md`）和 **Codex**（读本文件）。
目标：同一时间只有一个 agent 改同一块内容，任何人接手都能从 `STATE.md` 继续。

## 0. 每次开始前

按顺序读：

1. `STATE.md` —— 当前目标、正在跑什么、阻塞
2. `TASKS.md` —— 任务队列、负责人、分支
3. 本文件

然后 `git pull --rebase`。

## 1. 任务认领

- 在 `TASKS.md` 把任务改成 `doing`，负责人写 `claude` 或 `codex`，并填分支名。
- 一个任务只归一个 agent；需要协作就拆子任务。
- 没认领的任务不要直接改；紧急问题先在 `STATE.md` 记一行。

## 2. 分支与提交

- `main` 必须保持可运行、测试通过。
- 从最新 `main` 开分支：`claude/<task-id>-<slug>` 或 `codex/<task-id>-<slug>`。
- 提交信息：`<agent>: <task-id>: <做了什么>`
- 小步提交；合并前先 `git rebase origin/main`。
- 不直接 push `main`；合并前跑测试。

## 3. 每次结束前

更新 `STATE.md`，至少写：

- 最后更新时间和 agent
- 这次做了什么
- 正在运行的训练/进程（run id、命令、日志、GPU）
- 结果/指标
- 下一步、阻塞

同步更新 `TASKS.md`，然后提交、push。

## 4. 训练运行

启动前：

- 确认另一个 agent 没有在跑同一数据集/同一 GPU。
- 生成 run id：`YYYYMMDD-<dataset>-<model>-<short>`。
- 在 `runs/<run-id>.json` 写元数据（模板见 `runs/README.md`）。
- 记下命令、日志路径、checkpoint 路径、开始时间。

结束后：

- 更新 `runs/<run-id>.json` 的 `status`、`metrics`、`finished_at`、`artifacts`。
- 在 `experiments/INDEX.md` 加一行。
- 更新 `STATE.md`。

不要 kill 另一个 agent 启动的进程；要停先在 `STATE.md` 说明。

## 5. 大文件与数据

**不入库**：

- `data/`（数据集，当前约 6.1 GB）
- `artifacts/`（模型二进制，当前约 23 MB）
- `models/`、`*.db`、`.env`、`.venv/`、日志

在 `runs/*.json` 记录路径和 SHA-256。需要跨机器共享模型时用 GitHub Release 或外部存储，不要塞进 git。

## 6. 环境与命令

```bash
source .venv/bin/activate

# 训练
python scripts/train_activity.py
python scripts/train_ppg.py

# 回放
python scripts/replay_dataset.py --dataset pamap2 --subject 105 --activity 1 --seconds 60

# 测试
python -m pytest -q

# 服务
uvicorn guardian.main:app --host 127.0.0.1 --port 8000
curl -s http://127.0.0.1:8000/v1/ml/status
```

- 数据集：`data/raw/`、`data/processed/`
- 模型：`artifacts/<dataset>/`
- 本机 Python：`.venv/bin/python`

## 7. 安全与边界

- 不提交 `.env`、API key、token、数据库。
- 不改另一个 agent 正在改的文件；按目录/文件认领。
- 不改系统代理、全局配置、现有训练环境；需要时先写进 `STATE.md`。
- 训练/服务进程只读观察，不要随意 kill。
- 本项目是研究/黑客松原型，不是医疗器械；不要写出“临床可用”之类结论。

## 8. 冲突处理

- 同时改同一文件：以先认领者为准，后到者 rebase 后重做。
- 合并冲突：保留双方意图，先跑测试再提交。
- 不确定的决策写进 `STATE.md` 的“阻塞/风险”，等用户决定。
