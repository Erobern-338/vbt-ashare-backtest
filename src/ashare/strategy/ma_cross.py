"""双均线交叉信号。

纯 pandas 实现，刻意不依赖 vectorbt：策略逻辑可以独立单元测试，同一个函数
既能给 notebook 用，也能喂给 vectorbt 的向量化网格（MA.run_combs）或多标的循环。
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class MAParams:
    fast: int = 10
    slow: int = 50

    def __post_init__(self) -> None:
        if self.fast < 1 or self.slow < 1:
            raise ValueError("窗口必须为正整数")
        if self.fast >= self.slow:
            raise ValueError(f"fast({self.fast}) 必须小于 slow({self.slow})")


def ma_cross_signals(close, p: MAParams = MAParams()):
    """金叉入场、死叉离场。

    比较均线差值的前后状态，而不是直接比较两条均线 —— 这样只有状态翻转的那
    一根 K 线才算信号，不会把"持续在上方"重复当作入场。

    返回 (entries, exits)，与 close 同形的布尔结构。均线预热期内两者均为 False。
    """
    diff = close.rolling(p.fast).mean() - close.rolling(p.slow).mean()
    prev = diff.shift(1)
    entries = (diff > 0) & (prev <= 0)
    exits = (diff < 0) & (prev >= 0)
    return entries, exits
