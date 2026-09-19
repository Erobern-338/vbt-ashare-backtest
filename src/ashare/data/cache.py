"""parquet 缓存层：单标的与多标的读同一批文件。

目录 data/cache/daily/<adjust>/，每个标的一对文件：

    <symbol>.parquet   行情数据
    <symbol>.json      覆盖区间元信息（fetched_start / fetched_end / last_sync）

**为什么元信息记的是"请求区间"而不是"实际数据区间"**：请求区间可能起止于非交易
日（元旦、周末），而实际数据必然从下一个交易日才开始。拿实际数据区间去和请求区间
比，会把"起点正好是节假日"误判成"缓存缺数据"，导致每次调用都整段重取，缓存形同
虚设 —— 实测请求 2018-01-01 就踩到了这个坑（当天元旦，首个交易日是 01-02）。
"""

from __future__ import annotations

import json
import warnings
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from ..config import CACHE_DIR, DEFAULT_ADJUST
from . import source


def _paths(symbol: str, adjust: str) -> tuple[Path, Path]:
    d = CACHE_DIR / (adjust or "raw")
    return d / f"{symbol}.parquet", d / f"{symbol}.json"


def _fmt(ts: pd.Timestamp) -> str:
    return ts.strftime("%Y%m%d")


def _read(symbol: str, adjust: str) -> tuple[pd.DataFrame, dict | None]:
    parquet_path, meta_path = _paths(symbol, adjust)
    if not parquet_path.exists():
        return pd.DataFrame(), None
    try:
        meta = json.loads(meta_path.read_text("utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        meta = None
    return pd.read_parquet(parquet_path), meta


def _write(symbol: str, adjust: str, df: pd.DataFrame, start: str, end: str) -> None:
    parquet_path, meta_path = _paths(symbol, adjust)
    parquet_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(parquet_path)
    meta_path.write_text(
        json.dumps(
            {
                "adjust": adjust,
                "fetched_start": start,
                "fetched_end": end,
                "source": df.attrs.get("source"),
                "last_sync": datetime.now(timezone.utc).isoformat(),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def _covers(meta: dict | None, start: str, end: str) -> bool:
    if not meta:
        return False
    return meta.get("fetched_start", "99999999") <= start and meta.get("fetched_end", "00000000") >= end


def get_prices(
    symbol: str,
    start: str = "19900101",
    end: str | None = None,
    adjust: str = DEFAULT_ADJUST,
    force: bool = False,
    refresh: bool = False,
) -> pd.DataFrame:
    """取单只行情。

    默认「缓存优先」：请求区间被缓存覆盖时完全离线返回，不打网络。这保证了周末、
    节假日和断网时都能正常工作。

    force=True   忽略缓存整段重取。前复权数据在除权后历史会被整段重写，必须用它。
    refresh=True 即使缓存看起来够用也向网络核对一次，用于取最新行情。

    联网时会取「请求区间 ∪ 已有缓存区间」这一个连续窗口，而不是分段拼接 ——
    分段增量容易在中间留下空洞，一次取全则天然连续。
    """
    end_ts = pd.Timestamp(end) if end is not None else pd.Timestamp.today().normalize()
    start_ts = pd.Timestamp(start)
    s, e = _fmt(start_ts), _fmt(end_ts)

    cached, meta = _read(symbol, adjust)

    if not force and not refresh and not cached.empty and _covers(meta, s, e):
        return cached.loc[start_ts:end_ts]

    fetch_start, fetch_end = s, e
    if not cached.empty and meta:
        fetch_start = min(s, meta.get("fetched_start", s))
        fetch_end = max(e, meta.get("fetched_end", e))

    try:
        fresh = source.fetch_hist(symbol, fetch_start, fetch_end, adjust=adjust, empty_ok=True)
    except (OSError, ValueError) as exc:
        # requests 的 RequestException 继承自 OSError，网络故障走这里。
        # 既然缓存里有数据，断网时降级返回缓存比直接抛错有用得多。
        if cached.empty:
            raise
        warnings.warn(
            f"{symbol} 刷新失败，返回缓存数据（至 {cached.index.max().date()}）: {exc}",
            stacklevel=2,
        )
        return cached.loc[start_ts:end_ts]

    if fresh.empty:
        if cached.empty:
            raise ValueError(f"{symbol} 在 {fetch_start}~{fetch_end} 区间无数据")
        return cached.loc[start_ts:end_ts]

    _write(symbol, adjust, fresh, fetch_start, fetch_end)
    return fresh.loc[start_ts:end_ts]


def load_prices(
    symbols: list[str],
    start: str = "19900101",
    end: str | None = None,
    field: str = "Close",
    adjust: str = DEFAULT_ADJUST,
    calendar: pd.DatetimeIndex | None = None,
    refresh: bool = False,
) -> pd.DataFrame:
    """多标的宽表：每列一个标的。这正是 vectorbt 的入参格式。

    calendar 传入交易日历时按其对 (reindex)，停牌日会是 NaN —— 上层据此
    把停牌日的信号置为不可成交。
    """
    cols = {
        sym: get_prices(sym, start, end, adjust=adjust, refresh=refresh)[field]
        for sym in symbols
    }
    wide = pd.DataFrame(cols).sort_index()
    if calendar is not None:
        wide = wide.reindex(calendar)
    return wide
