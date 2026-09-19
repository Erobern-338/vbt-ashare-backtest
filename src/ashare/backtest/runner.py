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
    shift: int = 0,
):
    """按信号回测，返回 vectorbt Portfolio。

    shift=1 表示信号次日执行，用于消除"用当日收盘价算信号、又用当日收盘价成交"
    的未来函数。A 股 T+1 在"收盘价信号 + 次日执行"的组合下天然满足。

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
