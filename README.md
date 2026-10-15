# 上证50撮合器

上证50 Level-2 历史订单簿回放与教学撮合终端。开发配置按使用者提供记录为 **GPT-6.1-Sol，推理强度 high**。这里介绍程序模块，不推测开发模型的内部网络架构。

## 架构与数据流

```text
本地 ord / exe / snp Parquet
        ↓ PyArrow读取、股票过滤、频道BizIndex排序
orderbook.py：OrderOriNo关联 → 历史FIFO簿 → 十档快照
        ↓
simulator.py：历史会话 + 独立虚拟容量 + 排队/成交/费用
        ↓
webapp.py：Python标准库 ThreadingHTTPServer / JSON API
        ↓
web/index.html：HTML / CSS / JavaScript 交互界面
```

| 模块 | 已实现功能 |
|---|---|
| 历史回放 | 逐股票回放；十档盘口；单步、跳至指定时间、自动播放及倍速；显示市场成交 |
| 连续撮合 | 价格优先、同价FIFO；跨档限价；部分成交与撤单 |
| 市价指令 | 最优五档剩余撤销/转限价、本方最优、对手方最优；保护限价 |
| 被动成交 | 追踪队前历史订单，后续真实成交及跨价反向剩余ADD驱动虚拟成交；撤单仅推进排队 |
| 容量与费用 | 历史簿不被虚拟成交改写；虚拟消耗独立记账；佣金、过户费和卖出印花税 |
| 集合竞价 | 独立输入的统一清算价及配对；历史会话收盘未完成限价单的情景分支 |
| 数据诊断 | 原始订单关联/数量严格检查；历史Order/Trade核查摘要；既有验证脚本 |

## 安装与启动

建议 Python 3.10+，以下命令从仓库根目录执行：

```powershell
git clone https://github.com/sog477/sse50-order-matcher.git
cd sse50-order-matcher
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe webapp.py --data sse50_20260923 --port 8765
```

打开 <http://127.0.0.1:8765/>。macOS/Linux 用 `.venv/bin/python` 替换 Windows 的解释器路径，也可直接使用装好 PyArrow 的 `python`。Windows 已有可用解释器时可选 `./start.ps1`，后台启动；`./start.ps1 -Stop` 停止该脚本记录的服务。

### 数据由使用者本地准备

本仓库不分发课程原始行情，也没有编造公开下载地址。将合法取得的文件放在：

```text
sse50_20260923/
  ord_20260923.parquet
  exe_20260923.parquet
  snp_20260923.parquet
```

也可通过 `--data "其他绝对路径"` 指向已有目录。`ord` 为新增剩余挂量/撤单/状态，`exe` 为成交，`snp` 为行情快照。此程序适配本课程文件字段，不宣称兼容所有供应商格式。

### 使用顺序

1. 选择证券代码，重置到09:30。
2. 用“+1秒/+10秒/+1分钟”、指定时间或自动播放观察十档变化。
3. 填方向、类型、价格/保护价与数量。主动跨价订单立即按可用容量撮合；本方最优限价通常先排队。
4. 继续推进，检查委托状态、队前量、成交记录及费用；也可撤销剩余量。
5. 展开“集合竞价定价演示”，编辑JSON限价单列表并生成统一价成交。

每个会话同时保留一笔未完成虚拟委托。重置会清空当前虚拟状态。界面中的卖出没有完整持仓约束，不可当作账户回测。

## 无原始行情也能运行的例子

```powershell
python orderbook.py demo
```

题设卖单500股@10.01、1000股@10.02，买800股@10.02：成交500股@10.01、300股@10.02，第二档剩700股。

## 事件语义与边界

历史簿按频道内 `BizIndex` 消费消息，用 `(Channel, OrderOriNo)` 关联委托，`OrderNumber` 不作原单ID。连续竞价可能先披露成交，再披露剩余ADD；不能把该剩余量当原始申报重新撮合。完全主动成交的原始限价/订单类型不能从剩余簿唯一恢复。

虚拟成交采用固定历史路径、零市场冲击及队列情景；参与者不会对虚拟单作反应。默认费用是假设：佣金万二、每笔委托最低5元，部分成交累计计提；双向过户费十万分之一；卖出印花税万五。可修改 `simulator.Fees`，并非替使用者确认实际券商费率。

未实现完整资金/持仓、冻结、T+1交收、涨跌幅/价格笼子、停复牌与真实交易所连接。此处的市价模拟和集合定价不等于完整交易所订单受理系统。

## 截图与已有记录

提交时的真实运行截图见 [docs/screenshots](docs/screenshots)。[截图说明](docs/SCREENSHOTS.md)记录操作与时间。

- [2026-10-15既有功能检查记录](reports/WORKBENCH_VALIDATION.md)：保留当时的19项检查与集合竞价对照说明；本次打包没有将旧记录冒充重新执行的测试。
- `audit.py`、`verify_auction.py`：需原始数据才能复现；快照搜索对齐是离线诊断，不是实时严格同步。
- `test_orderbook.py`、`test_simulator.py`：原有回归用例。
- `SOURCE_MANIFEST.json`：导出脚本哈希及原版本哈希；导出版仅分离研究入口，不改撮合内核。

提交文稿见 [SUBMISSION_COMMENT.md](SUBMISSION_COMMENT.md)。原始行情、私人对话、定时任务、运行日志和参考同学截图均不发布。
