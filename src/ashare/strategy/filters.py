"""信号后处理：剔除在涨跌停价上挂不出去的单。

开源版 vectorbt 没有限价单，只能按信号价成交。信号恰好落在涨停/跌停收盘价上
时实际无法成交，不回剔会让回测结果系统性偏乐观。
"""

from __future__ import annotations

from ..config import LIMIT_PCT_GEM, LIMIT_PCT_MAIN


def limit_pct(symbol: str) -> float:
    """按代码前缀返回涨跌停幅度阈值(%)。"""
    # 创业板 300xxx、科创板 688xxx 为 20%，其余主板为 10%
    return LIMIT_PCT_GEM if symbol.startswith(("300", "688")) else LIMIT_PCT_MAIN


def drop_limit_signals(signals, pct_chg, threshold: float):
    """把落在涨跌停价上的信号置为 False。

    注意：这只是剔除了"信号当天已封板"的情况。若次日跳空封板，仍然无法成交，
    这个偏差开源版无法建模，需在结论中标注。
    """
    return signals & ~(pct_chg.abs() >= threshold)
