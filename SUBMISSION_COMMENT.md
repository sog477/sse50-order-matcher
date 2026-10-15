## 第三节实践作业：上证50撮合器

**公开仓库：[sog477/sse50-order-matcher](https://github.com/sog477/sse50-order-matcher)**  
**开发模型：GPT-6.1-Sol，推理强度 high（按使用者提供的开发配置记录）。**

基于 **Python + PyArrow/Parquet + 标准库HTTP服务 + HTML/CSS/JavaScript**，实现本地历史订单簿回放与虚拟撮合实验台。

### 程序架构与功能

数据流：本地Order/Trade → 频道内BizIndex排序 → OrderOriNo关联的历史FIFO簿 → 独立虚拟委托/容量/费用 → JSON接口 → 浏览器界面。Snapshot用于展示及离线对照，不用未来快照覆盖历史簿。

| 模块 | 已实现功能 |
|---|---|
| `orderbook.py` | 价格时间优先、同价FIFO、跨档撮合、部分成交、撤单、十档快照、集合竞价统一定价 |
| `simulator.py` | 历史会话；后续行情推进被动委托；历史簿与虚拟容量分别维护；保护价及费用 |
| `webapp.py` | Python标准库HTTP与JSON接口、独立浏览器会话 |
| `web/index.html` | 股票选择、十档盘口、手动限价/四类市价、成交记录、单步/指定时间/自动倍速回放、集合定价演示 |

四类市价为最优五档剩余撤销、最优五档剩余转限价、本方最优及对手方最优。未成交限价余量排队；历史撤单减少队前量，本身不产生成交。虚拟撤单保留既有成交与容量消耗，避免重复吃同一份历史挂量。

### 启动与使用

需要Python 3.10+，合法取得的课程数据自行放到 `sse50_20260923/`，包含 `ord_20260923.parquet`、`exe_20260923.parquet`、`snp_20260923.parquet`。仓库不分发原始行情。

```powershell
git clone https://github.com/sog477/sse50-order-matcher.git
cd sse50-order-matcher
python -m pip install -r requirements.txt
python webapp.py --data sse50_20260923 --port 8765
```

访问 **http://127.0.0.1:8765/**。选择股票并重置；推进到指定时间；填写方向、价格/保护价、数量并提交；继续播放查看排队、部分成交、全部成交或撤单状态。展开集合竞价面板可自行输入订单并生成统一价成交。没有原始数据时可先运行 `python orderbook.py demo` 查看题设跨档例子。

### 核心模块实机截图

以下均为本项目导出版在2026-10-16真实运行截取，点击可看原图。

| 历史盘口与回放 | 被动委托随后成交 | 集合竞价定价与成交 |
|---|---|---|
| [![历史盘口](https://raw.githubusercontent.com/sog477/sse50-order-matcher/main/docs/screenshots/01-history.jpg)](https://raw.githubusercontent.com/sog477/sse50-order-matcher/main/docs/screenshots/01-history.jpg) | [![模拟成交](https://raw.githubusercontent.com/sog477/sse50-order-matcher/main/docs/screenshots/02-passive-fill.jpg)](https://raw.githubusercontent.com/sog477/sse50-order-matcher/main/docs/screenshots/02-passive-fill.jpg) | [![集合竞价](https://raw.githubusercontent.com/sog477/sse50-order-matcher/main/docs/screenshots/03-auction.jpg)](https://raw.githubusercontent.com/sog477/sse50-order-matcher/main/docs/screenshots/03-auction.jpg) |

截图操作：601688在10:00以18.41元挂100股买单，初始排队，后续行情推进后于10:00:08.720成交，计费5.02元；独立集合演示统一价10.02元、成交1,200股。

范围：固定历史路径、零冲击的教学模拟。未建立完整资金/持仓、T+1、涨跌幅/价格笼子和停复牌体系，不连接实盘。费用为可修改的模型假设；界面每会话同时保留一笔未完成虚拟委托。详细安装、事件语义与限制见仓库README。
