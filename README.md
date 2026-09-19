# vbt-ashare-backtest

A 股日线本地回测环境，基于 [vectorbt](https://github.com/polakowo/vectorbt)。跑通
「取数 → 规整落盘 → 算指标 → 生成信号 → 回测 → 统计」整条链路，数据取自
[akshare](https://github.com/akfamily/akshare)。

> **这不是投资建议，也不是一个可用策略。**
> 仓库里的双均线交叉只是用来验证链路是否打通的载体。回测结果受多项已知偏差影响
> （见下文「已知偏差」），量级足以让结论失真，请勿据此做任何决策。

## 它解决什么

把 akshare 的原始返回值直接喂给 vectorbt 会连踩几个坑，而这些坑是 A 股特有的，
每个策略都要重踩一遍：

- **复权口径**：前复权的历史价格会在每次分红后被追溯重写，同一段历史今天跑和下个月
  跑结果不同，回测不可复现。所以默认用后复权。
- **日期精度**：akshare 返回 `datetime.date`，pandas 3 推成 `datetime64[s]`，而
  parquet 往返又会把它提升成 `[ms]` —— 新取的数据和缓存读的数据索引 dtype 就不一致。
- **涨跌停**：信号落在封板价上时实际挂不出单，不回剔会让结果系统性偏乐观。
- **上游限流**：东财对密集请求会直接断连，需要多源回退。

这些都在数据层和策略层一次性消化掉，上层不用重复处理。

## 分层

| 层 | 模块 | 职责 |
|---|---|---|
| 配置 | `ashare.config` | 路径、默认复权 / 费率 / 资金 |
| 数据 | `ashare.data` | 取数、规整、parquet 缓存 |
| 策略 | `ashare.strategy` | 只产出 `entries` / `exits` 布尔序列 |
| 回测 | `ashare.backtest` | `run_signals`，全项目唯一 import vectorbt 的地方 |

**为什么这样分**：策略层不依赖 vectorbt，同一份信号逻辑因此既能进 notebook、也能
单元测试、也能喂给 `vbt.MA.run_combs` 做参数网格；回测层是唯一接触 vectorbt 的
模块，将来换引擎版本或换库只动这一个文件。

```
src/ashare/
├── config.py
├── data/
│   ├── source.py      # akshare 三源回退 + 统一列名与口径
│   └── cache.py       # parquet 缓存，单 / 多标的统一入口
├── strategy/
│   ├── ma_cross.py    # 双均线信号（纯 pandas）
│   └── filters.py     # 剔除涨跌停信号
└── backtest/
    └── runner.py      # vectorbt 封装
```

## 快速开始

```bash
git clone https://github.com/Erobern-338/vbt-ashare-backtest.git
cd vbt-ashare-backtest
python -m venv .venv
source .venv/Scripts/activate            # PowerShell 用 .venv\Scripts\Activate.ps1
pip install -r requirements.lock.txt     # 含 editable 自安装
pip install pytest jupyterlab ipykernel
python -m ipykernel install --user --name vbt --display-name "Python (vbt)"
```

`ashare` 是 editable 安装，改 `src/` 下的代码直接生效，不用重装。

跑一遍验证：

```bash
python -m pytest tests/ -q
python -m jupyter lab notebooks/01_dual_ma_e2e.ipynb   # 内核选 "Python (vbt)"
```

## 用法

### 单只标的

```python
from ashare.data import get_prices
from ashare.strategy import MAParams, ma_cross_signals, drop_limit_signals, limit_pct
from ashare.backtest import run_signals

df = get_prices("000001", "20180101", "20260919")   # 首次联网，之后读缓存
close = df["Close"]

entries, exits = ma_cross_signals(close, MAParams(fast=10, slow=50))
entries = drop_limit_signals(entries, df["PctChg"], limit_pct("000001"))

pf = run_signals(close, entries, exits)
pf.stats()
```

取数返回的列固定为 `Open High Low Close Volume Amount PctChg Turnover`，索引是
`DatetimeIndex`（钉在 ns）。`drop_limit_signals` 剔除信号当天已封板的单。

### 换成自己的策略

策略层只需要满足一个契约：**返回与 `close` 同形的两个布尔序列**。其余全部不用改。

```python
def breakout_signals(close, window=20):
    high = close.rolling(window).max()
    entries = (close > high.shift(1)) & (close.shift(1) <= high.shift(2))
    exits   = close < close.rolling(window).min().shift(1)
    return entries, exits
```

`close` 传 DataFrame 时 rolling 按列独立计算，所以多标的可以共用同一个函数。

### 多标的

```python
from ashare.data import load_prices, trade_calendar

wide = load_prices(["000001", "600519"], "20180101", "20260919",
                   calendar=trade_calendar())
```

返回宽表，每列一个标的 —— 正是 vectorbt 的入参格式。传 `calendar` 会对齐交易日历，
停牌日为 NaN，上层据此把停牌日的信号视为不可成交。

两个尚未处理的点（属里程碑 3）：

- `load_prices` 一次只取一个字段，要 `PctChg` 得再调一次 `field="PctChg"`；而
  `limit_pct` 只接受单个代码，多标的的涨跌停阈值得按列分别处理。
- `Portfolio.from_signals` 拿到宽表时默认**每列各拿一份 `init_cash` 独立回测**，
  不是共享资金池。真正的组合需要显式开 `cash_sharing` + `group_by`，尚未验证。

### 缓存

缓存在 `data/cache/daily/<adjust>/<symbol>.parquet` + 同名 `.json`。

- 默认**缓存优先**：请求区间被覆盖就完全离线返回，不打网络。周末、节假日、断网
  都能正常工作。
- `refresh=True`：缓存够用也联网核对一次，用于取最新行情。
- `force=True`：忽略缓存整段重取。**前复权必须用它**，因为除权后历史会被整段重写。

元信息记录的是**请求区间**而非实际数据区间 —— 请求区间可能起止于非交易日，拿实际
数据区间去比会把「起点正好是节假日」误判成缓存缺数据，导致每次调用都整段重取。
`source` 字段记录该区间实际来自哪家数据源。

清缓存：`rm -rf data/cache`，下次调用自动重取。

### 其他数据入口

```python
from ashare.data import list_symbols, trade_calendar, fetch_index

list_symbols()            # 全 A 股代码 + 名称
trade_calendar()          # 交易日历
fetch_index("000300")     # 大盘基准，仅走东财、无回退
```

## 已知偏差

**完整清单以 [notebooks/01_dual_ma_e2e.ipynb](notebooks/01_dual_ma_e2e.ipynb) 第 6 节为准**，
那里随代码一起维护。摘要：

- 开源版 vectorbt **没有限价单**，只能按信号价成交。已剔除信号当天封板的单，但次日
  跳空封板仍然无法成交 —— 结果仍偏乐观。
- 费率是对称的（`0.0008` 近似覆盖双边），高估了买入腿；开源版无法只对卖出腿征印花税。
- 无滑点，成交价直接取收盘价，未建模冲击成本和买卖价差。
- 停牌日在缓存中不存在，vectorbt 不会在停牌 bar 上成交，但持仓市值会跨停牌期连续计算。

举个量级上的参考：同一段数据、同一条信号，**仅把成交时点从「信号当根收盘」改成
「顺延一根 K 线」**，总收益就从 25.28% 变成 34.84%。偏差不是小数点后面的噪声，所以
`run_signals` 的 `shift` 默认值取 1 —— 默认值必须站在安全的一侧。

## 测试

```bash
python -m pytest tests/ -q
```

- `tests/test_data_layer.py` —— 数据层与策略层，**全离线**（网络调用一律 monkeypatch），约 1 秒。
- `tests/test_backtest.py` —— 回测层成交时点，用合成小序列，会 import vectorbt，慢几秒。

惯例：每个修掉的 bug 都带一条回归测试，并在 docstring 里注明修之前错在哪。

## 数据来源

行情经 akshare 取自东方财富 / 新浪 / 腾讯三家的公开接口，按序回退。

**缓存目录 `data/` 已被 `.gitignore` 排除，仓库不包含行情数据文件。**

需注意的是 notebook 带输出提交，其中图表内嵌了该标的的收盘价序列（约 2 千个点）。
若在意衍生数据的再分发，可清空 notebook 输出后再提交。

## 路线图

- [x] **里程碑 1**：单标的端到端跑通（双均线交叉）
- [ ] **里程碑 2**：参数网格寻优 `vbt.MA.run_combs` + 热力图。必须配合
      `vbt.rolling_split()` 做样本内外分割 —— 换参数直到好看就是过拟合，开源版没有
      现成的交叉验证工具，得自己拼。
- [ ] **里程碑 3**：多标的组合（上市时间不齐、资金分配方式）
