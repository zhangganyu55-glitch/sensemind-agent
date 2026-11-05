# Claude Code

协作协议以 `AGENTS.md` 为准，**必须遵守**。

## 每次开始

1. 读 `AGENTS.md`、`STATE.md`、`TASKS.md`
2. `git pull --rebase`
3. 在 `TASKS.md` 认领任务

## 每次结束

1. 更新 `STATE.md` 和 `TASKS.md`
2. 训练运行写 `runs/<run-id>.json`
3. 提交并 push

## 注意

- 不提交 `.env`、`data/`、`artifacts/`、`models/`、`*.db`
- 跑训练前确认 Codex 没有在跑同一任务
- 不 kill 另一个 agent 的进程
- 本工程是研究/黑客松原型，不是医疗器械
