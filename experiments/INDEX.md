# 实验总表

| run id | 日期 | 数据集 | 模型 | 关键指标 | 产物 |
| --- | --- | --- | --- | --- | --- |
| 20261030-pamap2-activity | 2026-10-30 | PAMAP2 | RandomForest | acc 75.03% / macro F1 73.94% / baseline 11.16% | `artifacts/activity/` |
| [20261031-ppg-dalia-rf-local](../runs/20261031-ppg-dalia-rf-local.json) | 2026-10-31 | PPG-DaLiA | RF quality + HR | test quality acc 75.33% / macro F1 65.95%（baseline acc 75.75% / F1 43.10%）；HR MAE 7.76 / RMSE 12.12 bpm（baseline MAE 20.79）；val HR MAE 17.18 bpm | `artifacts/ppg/` |

新增实验时：

1. 在 `runs/` 写一份 `<run-id>.json`
2. 在本表加一行
3. 把产物路径和 SHA-256 写进 run 文件（产物本身不入库）

PPG 本次为本地 CPU、seed=42、每人前 400 窗 / stride=3（约前 40 分钟）；train S1-S10 / val S11-S12 / test S13-S15，4000 / 800 / 1200 窗。质量是 HR 一致性代理标签；指标仅代表此抽样和受试者划分。

| [20261031-sensemind-v07-phase1](../runs/20261031-sensemind-v07-phase1.json) | 2026-10-31 | 合成安全夹具 + PPG-DaLiA 独立受试者 | 真实 DeepSeek / 离线质量 / 600 秒稳定性 | HR 输出 41.03%；600 遥测 / 593 推理 / 0 HTTP 错误；127 tests | `docs/v07-report.md` |
