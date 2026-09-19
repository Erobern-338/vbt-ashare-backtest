"""回测执行层 —— 全项目唯一 import vectorbt 的模块。

隔离在这里的原因是：vectorbt 的版本迭代或引擎替换（开源版 / PRO / 换库）都
只影响这一个文件，策略层和数据层不受牵连。
"""

from __future__ import annotations

import vectorbt as vbt

from ..config import DEFAULT_FEES, DEFAULT_FREQ, DEFAULT_INIT_CASH


def run_signals(
    close,
    entries,
    exits,
    init_cash: float = DEFAULT_INIT_CASH,
    fees: float = DEFAULT_FEES,
    freq: str = DEFAULT_FREQ,
    shift: int = 1,
):
    """按信号回测，返回 vectorbt Portfolio。

    shift 默认 1，即信号次一根 K 线成交。这不是风格偏好，是默认值该站在安全的一侧：
    shift=0 会让 vectorbt 拿信号当根的收盘价成交，而当日收盘价要等收盘才知道，
    等于用未来信息下单。想要同根成交必须显式传 shift=0 并自担该偏差。

    A 股 T+1 在"收盘价算信号 + 次日成交"的组合下天然满足。

    fees 是对称的：开源版无法只对卖出腿征收印花税，0.0008 近似覆盖双边成本，
    会略微高估买入腿的实际费用。
    """
    if shift:
        # shift 会引入 NaN 并把 dtype 变成 object，vectorbt 只接受布尔
        entries = entries.shift(shift).fillna(False).astype(bool)
        exits = exits.shift(shift).fillna(False).astype(bool)

    return vbt.Portfolio.from_signals(
        close,
        entries,
        exits,
        init_cash=init_cash,
        fees=fees,
        freq=freq,
    )
