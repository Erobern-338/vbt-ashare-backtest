"""回测执行层单元测试。

这里刻意用合成的极小价格序列而不是真实行情：成交时点是否偏移一根 K 线，用固定
的 6 根 bar 就能看得很清楚，比在 2000 行真实数据里翻交易记录可靠得多。

注意本文件会 import vectorbt（经 runner），比 test_data_layer 慢几秒。
"""

from __future__ import annotations

import pandas as pd
import pytest

from ashare.backtest import run_signals


def _bars():
    """6 根 bar，收盘价 10..15。信号落在第 2 根，平仓信号落在第 4 根。"""
    idx = pd.date_range("2024-01-01", periods=6, freq="B")
    close = pd.Series([10.0, 11.0, 12.0, 13.0, 14.0, 15.0], index=idx)
    entries = pd.Series(False, index=idx)
    entries.iloc[1] = True
    exits = pd.Series(False, index=idx)
    exits.iloc[3] = True
    return idx, close, entries, exits


def test_shift_defaults_to_next_bar():
    """回归测试。

    默认值曾经是 0，于是 vectorbt 直接拿信号当根的收盘价成交 —— 而当日收盘价要等
    收盘才知道，等于用未来信息下单。默认值必须站在安全的一侧。
    """
    idx, close, entries, exits = _bars()

    t = run_signals(close, entries, exits).trades.records_readable.iloc[0]

    assert t["Entry Timestamp"] == idx[2], "信号在第 2 根，应在第 3 根成交"
    assert t["Avg Entry Price"] == pytest.approx(close.iloc[2])
    assert t["Exit Timestamp"] == idx[4], "平仓同样顺延一根"
    assert t["Avg Exit Price"] == pytest.approx(close.iloc[4])


def test_shift_0_fills_on_the_signal_bar_itself():
    """显式传 shift=0 时才同根成交，且成交价就是该根收盘价。

    把这个行为钉住，是为了让"同根成交"始终是一个需要主动选择的动作；哪天有人把
    默认值改回 0，上面那条测试会拦下来。
    """
    idx, close, entries, exits = _bars()

    t = run_signals(close, entries, exits, shift=0).trades.records_readable.iloc[0]

    assert t["Entry Timestamp"] == idx[1]
    assert t["Avg Entry Price"] == pytest.approx(close.iloc[1])


def test_shift_does_not_invent_signals_at_the_tail():
    """顺延后落在序列末尾之外的信号应被丢弃，而不是回卷到开头。

    shift 会引入 NaN，若不 fillna(False) 就会变成 object dtype 被 vectorbt 拒绝；
    若被错误地 fill 成 True，则会在最后几根凭空多出信号。
    """
    idx = pd.date_range("2024-01-01", periods=4, freq="B")
    close = pd.Series([10.0, 11.0, 12.0, 13.0], index=idx)
    entries = pd.Series(False, index=idx)
    entries.iloc[-1] = True  # 最后一根才出信号，顺延后已越界
    exits = pd.Series(False, index=idx)

    pf = run_signals(close, entries, exits)

    assert pf.trades.count() == 0, "越界的信号不应产生交易"
