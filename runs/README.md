# 训练运行记录

每次训练在 `runs/<run-id>.json` 写一份元数据。run id 格式：

```text
YYYYMMDD-<dataset>-<model>-<short>
例：20261030-pamap2-activity
```

| 字段 | 说明 |
| --- | --- |
| run_id | 唯一 id |
| agent | `claude` 或 `codex` |
| task_id | 对应 `TASKS.md` |
| git_commit | 启动时的 commit；没有就 `null` |
| started_at / finished_at | ISO 8601 UTC |
| status | `running` / `done` / `failed` |
| command | 完整命令 |
| dataset / dataset_path | 数据集名和本地路径 |
| config | 关键超参、特征版本、seed、窗口 |
| split | 受试者划分和窗口数 |
| artifacts | 路径 + SHA-256 |
| metrics | 测试/验证指标 |
| notes | 已知问题、限制 |

示例见 `20261030-pamap2-activity.json`。

注意：`artifacts/` 和 `data/` 不入库，这里只记录路径和哈希，不要把大文件放进 `runs/`。
