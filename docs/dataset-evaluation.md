# v0.6 数据集评估报告

> 目标：为 Guardian Angel 的**实验性**活动识别与 PPG 信号质量模型选择数据。
> 本报告只使用公开数据集；不连接真实硬件；不把模拟标签当作医学标签。

## 结论与选择

| 数据集 | 主要用途 | 与 MAX30102/MPU6050 兼容性 | 许可 | 本轮决定 |
|---|---|---|---|---|
| **PAMAP2** | 人体活动识别 | 高（腕部 IMU 100 Hz；需单位/频率适配） | CC BY 4.0 | **下载并训练活动模型** |
| **PPG-DaLiA** | 运动下 PPG 质量 / 心率估计 | 高（E4 PPG 64 Hz、ACC 32 Hz；需重采样） | UCI 标注 CC BY 4.0；原作者页面写科研非商业，商业用途需确认 | **下载并训练质量/HR 基线** |
| Pulse Transit Time PPG | PPG/ECG 时序、PTT、HR | 很高（MAX30101，500 Hz；MAX30102 同族） | ODC-ODbL 1.0 | 评估，暂不下载（体积大） |
| BIDMC | PPG/ECG/呼吸、静息质量 | 中（125 Hz、ICU、无运动） | ODC-BY 1.0 | 评估，作为外部补充 |
| WESAD | 压力/情绪、腕部 BVP | 高（E4 + RespiBAN） | UCI 页面未给明确 SPDX 许可；科研使用需确认 | 评估，不作本轮训练 |
| BIDSleep | 睡眠分期 | 低（Apple Watch HR ≈0.2 Hz，无原始 PPG） | ODC-BY 1.0 | 评估，不作本轮训练 |

**为什么只下载两个**：本轮目标是先把「数据 → 模型 → 回放 → 规则事件」链路跑通，所以只训练两个最贴合现有硬件的数据集；本机实际可用磁盘约 138 GB，空间不是限制。PAMAP2 外层包约 688 MB（内含约 656 MB 的 `PAMAP2_Dataset.zip`），PPG-DaLiA 的 `data.zip` 约 2.7 GB。其余四个先做许可与适配评估。

---

## 1. PPG-DaLiA

- **发布方/地址**：UCI ML Repository #495，`https://archive.ics.uci.edu/dataset/495/ppg+dalia`；原始工作来自 University of Siegen（Reiss et al., *Sensors* 2019, DOI [10.3390/s19143079](https://doi.org/10.3390/s19143079)）。UCI DOI [10.24432/C53890](https://doi.org/10.24432/C53890)。
- **格式**：`data.zip`（约 2.7 GB）内含每名受试者的 `S#.pkl` 和原始 Empatica E4 / RespiBAN 文件；附 `readme.pdf`。
- **规模**：15 名受试者（8 女 / 7 男，20–40 岁），8 种日常活动，约 36 小时；受试者 S6 的最后 3 种活动不可用。
- **采样率/传感器**：
  - 腕部 Empatica E4：BVP/PPG 64 Hz，3 轴 ACC 32 Hz，EDA 4 Hz，温度 4 Hz，HR 1 Hz。
  - 胸部 RespiBAN：ECG 700 Hz，呼吸 700 Hz，3 轴 ACC 700 Hz。
- **标签**：8 种活动标签；参考心率由 ECG 在 8 秒窗口、2 秒步长上计算。**没有官方的逐窗 PPG 质量标签**，需要从“PPG 估计心率 vs ECG 参考心率”派生代理标签。
- **与 MAX30102/MPU6050 兼容性**：高。PPG 64 Hz、ACC 32 Hz 与常见可穿戴采样接近；MAX30102 常用 50/100 Hz，需要重采样；E4 与 MPU6050 的坐标系/量程需适配。
- **许可**：UCI 页面写 **CC BY 4.0**；原作者 Siegen 页面被多方资料描述为“科研使用、需署名、商业用途不明确”。**本项目按非商业科研使用处理；商业使用前必须联系作者确认。**
- **适合性**：本轮 PPG 质量与运动干扰模块的首选；有运动、活动标签和 ECG 参考心率；受试者数量适中，适合按参与者划分。

## 2. Pulse Transit Time PPG Dataset

- **发布方/地址**：PhysioNet，`https://physionet.org/content/pulse-transit-time-ppg/1.1.0/`；DOI [10.13026/jpan-6n92](https://doi.org/10.13026/jpan-6n92)。
- **格式**：WFDB（`.hea/.dat/.atr`）与 CSV；页面列出约 **2.9 GB WFDB / 955.9 MB CSV**。
- **规模**：22 名健康受试者（6 女，20–53 岁），坐/走/跑 3 种活动，66 段记录，>40,000 次心搏。
- **采样率/传感器**：双 MAX30101 PPG（红/IR/绿）500 Hz；AD8232 ECG 500 Hz + 手工 R 峰；MPU-9250 九轴 500 Hz；TAL221 压力 80 Hz；温度 10 Hz；活动前后 BP/HR/SpO2。
- **标签**：活动类型、ECG R 峰、少量点测血压/HR/SpO2；没有逐拍连续血压标签。
- **兼容性**：很高。MAX30101 与 MAX30102 同族（MAX30102 为红/IR 双 LED），500 Hz 需降采样；MPU-9250 与 MPU6050 同类，量程/坐标需转换。
- **许可**：**ODC-ODbL 1.0**；允许使用与派生，但派生数据库有 share-alike 义务，商用前需核对条款。
- **适合性**：适合 PPG/ECG 时序、PTT、HR 算法基线；不适合活动识别；本轮因体积和任务范围不下载。

## 3. PAMAP2

- **发布方/地址**：UCI ML Repository #231，`https://archive.ics.uci.edu/dataset/231/pamap2+physical+activity+monitoring`；DOI [10.24432/C5NW2H](https://doi.org/10.24432/C5NW2H)。Reiss & Stricker, 2012。
- **格式**：`PAMAP2_Dataset.zip`（页面显示 **656.3 MB**），每名受试者一个 `.dat`（空格分隔）和 `readme.pdf`。
- **规模**：9 名受试者（1 女/8 男），约 10 小时，约 8 小时有标签；3,850,505 行、54 列。
- **采样率/传感器**：3 个 Colibri IMU（腕/胸/踝）100 Hz，每个含 3 轴 ACC（±16g 与 ±6g）、3 轴陀螺、3 轴磁力计、温度；BM-CS5SR 心率约 9 Hz。
- **标签**：18 种活动（12 种协议活动 + 6 种可选），活动 ID 1–24；大量“无活动”0 标签；缺失值需处理。
- **兼容性**：高。腕部 ACC 100 Hz → 可降采样至 MPU6050 常用 50/100 Hz；PAMAP2 用 m/s²，MPU6050 常用 g，需换算；量程 ±16g 覆盖。
- **许可**：**CC BY 4.0**（UCI 页面与 readme 均允许学术使用，需署名）。
- **适合性**：本轮活动识别模型的首选；受试者少但活动类别丰富，适合传统特征 + 树模型基线。

## 4. BIDSleep

- **发布方/地址**：PhysioNet，`https://physionet.org/content/bidsleep-dataset/1.0.0/`；DOI [10.13026/a0sy-7t69](https://doi.org/10.13026/a0sy-7t69)。
- **格式**：每名受试者/夜三个文件：`motion.csv`（Apple Watch 三轴加速度）、`hr.csv`（Apple Watch 瞬时心率，约 0.2 Hz）、`labels.mat`（Dreem 2 与专家睡眠分期）。
- **规模**：47 名受试者，每人 3–7 夜。
- **采样率/传感器**：Apple Watch 加速度（时间戳不规则，非固定高频），瞬时心率约 5 秒一个点；Dreem 2 EEG 仅提供标签，不提供原始 EEG。
- **标签**：30 秒睡眠分期（Wake/N1/N2/N3/REM/Unknown），Dreem 2 自动标签与专家校正标签；推荐按参与者交叉验证。
- **兼容性**：低。没有原始 PPG 波形，心率采样太稀，无法支持本项目的逐窗 PPG 质量/活动识别。
- **许可**：**ODC-BY 1.0**；页面写明“任何人均可访问并遵守许可”，但使用 Apple Watch/Dreem 数据仍需遵守原始条款。
- **适合性**：低（睡眠分期是另一个任务）；本轮不下载。

## 5. BIDMC

- **发布方/地址**：PhysioNet，`https://physionet.org/content/bidmc/1.0.0/`；DOI [10.13026/C2208R](https://doi.org/10.13026/C2208R)。
- **格式**：WFDB 与 CSV；53 段 ICU 记录，每段约 8 分钟。
- **采样率/传感器**：PPG/ECG/阻抗呼吸 125 Hz；HR/脉搏/RR/SpO2 数值 1 Hz；含年龄、性别、MIMIC II 关联号。
- **标签**：两次人工呼吸标注；**没有活动标签**，也没有逐窗质量标签。
- **兼容性**：中。PPG 125 Hz 需降采样；ICU 静息场景缺少运动干扰，与 MAX30102 的日常佩戴场景差距较大。
- **许可**：**ODC-BY 1.0**；允许商用，需署名。
- **适合性**：可作为静息 PPG/呼吸算法的外部验证，不适合本轮活动/运动干扰模型。

## 6. WESAD

- **发布方/地址**：UCI ML Repository #465，`https://archive.ics.uci.edu/dataset/465/wesad+wearable+stress+and+affect+detection`；DOI [10.24432/C57K5T](https://doi.org/10.24432/C57K5T)。Schmidt & Reiss, UbiComp 2018。
- **格式**：`S#.pkl`（同步数据）与原始 Empatica/RespiBAN 文件。
- **规模**：15 名受试者（17 记录，清洗后 15），每人约 36 分钟；状态：baseline、stress（TSST）、amusement，另有 recovery。
- **采样率/传感器**：E4 BVP 64 Hz、ACC 32 Hz、EDA/温度 4 Hz；RespiBAN 8 通道 700 Hz（ECG/EDA/EMG/呼吸/温度/ACC）。
- **标签**：压力/情绪状态；**没有活动标签**；E4 与 RespiBAN 需要同步。
- **兼容性**：高（与 PPG-DaLiA 同一 E4 硬件族）；但任务不同，且许可不明确。
- **许可**：UCI 页面未给出明确 SPDX 许可，只写“见原始数据集”；科研可用，商业使用需作者确认。
- **适合性**：可作为压力/情绪辅助模型的数据，不适合本轮的两个模型；不下载。

---

## 与本项目硬件的映射

| 公开数据 | Guardian Angel 目标 | 适配动作 |
|---|---|---|
| PAMAP2 腕部 ACC 100 Hz, m/s² | MPU6050 3 轴 ACC，g | 单位换算、重采样到 50/100 Hz、坐标系映射 |
| PAMAP2 心率 9 Hz | MAX30102 心率/SpO2 | 只作辅助通道；不作为 PPG 波形 |
| PPG-DaLiA E4 BVP 64 Hz | MAX30102 PPG（常见 50/100 Hz） | 带通滤波、重采样、窗口对齐 |
| PPG-DaLiA E4 ACC 32 Hz | MPU6050 ACC | 重采样、量程/坐标转换 |
| PPG-DaLiA ECG 700 Hz | 参考标签，不是目标硬件 | 只用于生成 HR/质量代理标签 |
| PTT PPG MAX30101 500 Hz | MAX30102 同族 | 降采样；不直接拼接不同受试者 |
| BIDMC 125 Hz | 静息 PPG 外部验证 | 降采样；不与运动数据拼接 |

## 数据使用原则

1. **按参与者隔离**：训练/验证/测试以 `subject_id` 分组，绝不把同一参与者的窗口分到不同集合。
2. **不拼接**：不把不同参与者的心率、PPG、加速度拼成一个“多模态样本”；模拟回放保留 `dataset/participant/source/sampling_hz`。
3. **标签说明**：PPG-DaLiA 的“可靠/不可靠”是代理标签（PPG 心率与 ECG 参考心率的误差），不是医学标签；PAMAP2 活动标签是实验活动，不是临床状态。
4. **不宣称临床能力**：模型只作为辅助证据，不覆盖规则引擎告警。

## 许可注意

- PAMAP2、BIDMC、BIDSleep：CC BY 4.0 / ODC-BY 1.0，商用需署名。
- PTT PPG：ODC-ODbL 1.0，派生数据库有 share-alike 义务。
- PPG-DaLiA：UCI 标 CC BY 4.0，但原始发布方页面被描述为科研非商业；**商业使用前必须联系作者**。
- WESAD：许可不明确，商用需作者确认。
- 本仓库不再分发原始数据；下载后的数据只放在本地 `data/raw/`（已 gitignore）。

## 参考来源

- [PPG-DaLiA (UCI 495)](https://archive.ics.uci.edu/dataset/495/ppg+dalia)
- [PAMAP2 (UCI 231)](https://archive.ics.uci.edu/dataset/231/pamap2+physical+activity+monitoring)
- [Pulse Transit Time PPG Dataset (PhysioNet)](https://physionet.org/content/pulse-transit-time-ppg/1.1.0/)
- [BIDMC PPG and Respiration Dataset (PhysioNet)](https://physionet.org/content/bidmc/1.0.0/)
- [BIDSleep (PhysioNet)](https://physionet.org/content/bidsleep-dataset/1.0.0/)
- [WESAD (UCI 465)](https://archive.ics.uci.edu/dataset/465/wesad+wearable+stress+and+affect+detection)
