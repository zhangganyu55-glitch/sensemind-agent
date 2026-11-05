# 数据目录

`data/` 不入库（当前约 6.1 GB）。这里只记录数据位置、版本和下载状态。

## 结构

```text
data/
├── raw/          # 原始下载包 / 解压数据
└── processed/    # 预处理产物
```

## 当前状态

- **PAMAP2**：已完成，解压在 `data/raw/PAMAP2_Dataset/`
- **PPG-DaLiA**：未完成；`data/raw/` 里有三个不完整 `.part`，**不得**用于训练
- 目标文件：`data/raw/ppg-dalia.zip`（约 2,864,337,783 bytes）

## 下载与解压

从 UCI 官方源获取 `ppg+dalia.zip`，保存为 `data/raw/ppg-dalia.zip`。完成后：

```bash
unzip -l data/raw/ppg-dalia.zip | head
unzip -q data/raw/ppg-dalia.zip -d data/raw/
# 若外层嵌套 data.zip，继续解压
```

任何不完整 zip 都不要解压训练。下载完成后把文件大小和 SHA-256 记到 `STATE.md` 或对应的 `runs/*.json`。
