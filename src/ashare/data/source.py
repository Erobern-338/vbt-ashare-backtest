"""akshare 取数，并规整为统一结构。

统一输出：DatetimeIndex(ns) + float64 列
    Open High Low Close Volume Amount PctChg Turnover

三个上游各有各的脏活，全部在这一层消化掉：

* **列名**：东财返回中文列名，新浪/腾讯返回小写英文列名。
* **日期**：都是 datetime.date，pandas 3 会推成 datetime64[s]，而 parquet 往返
  会把秒精度提升成毫秒 —— 同一天的数据"新取的"和"缓存读的"索引 dtype 就不一致。
  统一钉在 ns 上（见 _finalize）。
* **涨跌幅**：只有东财直接给；新浪和腾讯都没有，统一用收盘价自己算。
* **换手率**：东财给百分数，新浪/腾讯给小数比例，统一换算成百分数。

**为什么要按序回退**：东财（push2his.eastmoney.com）会对密集请求限流，实测连续
几次调用后就被 RemoteDisconnected 拒绝。新浪和腾讯走不同上游，可用性互补。
"""

from __future__ import annotations

import akshare as ak
import pandas as pd

PRICE_COLUMNS = ["Open", "High", "Low", "Close", "Volume", "Amount", "PctChg", "Turnover"]

PROVIDERS = ("eastmoney", "sina", "tencent")

_EASTMONEY_RENAME = {
    "日期": "Date",
    "开盘": "Open",
    "最高": "High",
    "最低": "Low",
    "收盘": "Close",
    "成交量": "Volume",
    "成交额": "Amount",
    "换手率": "Turnover",
    "股票代码": "Symbol",
}

# 新浪与腾讯用同一套小写列名，且 turnover 是小数比例而非百分数
_LOWER_RENAME = {
    "date": "Date",
    "open": "Open",
    "high": "High",
    "low": "Low",
    "close": "Close",
    "volume": "Volume",
    "amount": "Amount",
    "turnover": "Turnover",
}


def _prefixed(symbol: str) -> str:
    """6 位代码 → 带交易所前缀，新浪和腾讯的接口需要。"""
    if symbol.startswith("6"):
        return f"sh{symbol}"
    if symbol.startswith(("4", "8")):
        return f"bj{symbol}"
    return f"sz{symbol}"


def _finalize(raw: pd.DataFrame, source_name: str, turnover_is_ratio: bool) -> pd.DataFrame:
    """把各家列名统一成 PRICE_COLUMNS，并统一口径。"""
    df = raw.rename(columns={**_EASTMONEY_RENAME, **_LOWER_RENAME})
    missing = [c for c in ("Date", "Open", "High", "Low", "Close", "Volume") if c not in df.columns]
    if missing:
        raise ValueError(f"返回列与预期不符，缺少 {missing}；实际列 {list(raw.columns)}")

    df["Date"] = pd.to_datetime(df["Date"]).dt.as_unit("ns")
    df = df.set_index("Date").sort_index()
    df = df[~df.index.duplicated(keep="last")]

    if "Amount" not in df.columns:
        df["Amount"] = float("nan")
    if "Turnover" not in df.columns:
        df["Turnover"] = float("nan")

    df = df[["Open", "High", "Low", "Close", "Volume", "Amount", "Turnover"]].astype("float64")

    # 涨跌幅统一自己算：只有东财提供，且复权价的日收益率才是真实收益率
    df["PctChg"] = df["Close"].pct_change() * 100
    if turnover_is_ratio:
        df["Turnover"] = df["Turnover"] * 100

    out = df[PRICE_COLUMNS]
    out.attrs["source"] = source_name
    return out


def _via_eastmoney(symbol: str, start: str, end: str, adjust: str, period: str) -> pd.DataFrame:
    raw = ak.stock_zh_a_hist(
        symbol=symbol, period=period, start_date=start, end_date=end, adjust=adjust
    )
    if raw is None or raw.empty:
        return pd.DataFrame()
    return _finalize(raw, "eastmoney", turnover_is_ratio=False)


def _via_sina(symbol: str, start: str, end: str, adjust: str, period: str) -> pd.DataFrame:
    raw = ak.stock_zh_a_daily(
        symbol=_prefixed(symbol), start_date=start, end_date=end, adjust=adjust
    )
    if raw is None or raw.empty:
        return pd.DataFrame()
    return _finalize(raw, "sina", turnover_is_ratio=True)


def _via_tencent(symbol: str, start: str, end: str, adjust: str, period: str) -> pd.DataFrame:
    raw = ak.stock_zh_a_hist_tx(
        symbol=_prefixed(symbol), start_date=start, end_date=end, adjust=adjust
    )
    if raw is None or raw.empty:
        return pd.DataFrame()
    return _finalize(raw, "tencent", turnover_is_ratio=True)


_PROVIDER_FN = {
    "eastmoney": _via_eastmoney,
    "sina": _via_sina,
    "tencent": _via_tencent,
}


def fetch_hist(
    symbol: str,
    start: str,
    end: str,
    adjust: str = "hfq",
    period: str = "daily",
    empty_ok: bool = False,
    provider: str | None = None,
) -> pd.DataFrame:
    """单只 A 股行情，按 PROVIDERS 顺序回退，返回第一个成功且有数据的结果。

    symbol: 6 位代码，如 "000001" / "600519"
    adjust: "qfq" 前复权 | "hfq" 后复权 | "" 不复权
    empty_ok: 全部数据源都拿不到数据时返回空表而非报错（周末/节假日的空窗口用）
    provider: 指定单一数据源，跳过回退。默认 None 表示按序尝试。
    """
    names = (provider,) if provider else PROVIDERS
    failures: list[str] = []

    for name in names:
        try:
            df = _PROVIDER_FN[name](symbol, start, end, adjust, period)
        except Exception as exc:  # 任一数据源失败都继续试下一个，全部失败时统一报出
            failures.append(f"{name}({type(exc).__name__}: {str(exc)[:70]})")
            continue
        if not df.empty:
            return df
        failures.append(f"{name}(空数据)")

    if empty_ok:
        return pd.DataFrame(columns=PRICE_COLUMNS)

    raise ValueError(f"{symbol} {start}~{end} 所有数据源均失败: " + "; ".join(failures))


def fetch_index(symbol: str = "000300", start: str = "19900101", end: str = "20500101") -> pd.DataFrame:
    """指数日线，用作回测基准。symbol 如 "000300"(沪深300) / "000001"(上证指数)。

    仅走东财接口，没有回退 —— 里程碑 1 用不到基准对比，暂不做多源适配。
    """
    raw = ak.index_zh_a_hist(symbol=symbol, period="daily", start_date=start, end_date=end)
    if raw is None or raw.empty:
        raise ValueError(f"指数 {symbol} 在 {start}~{end} 无数据")
    return _finalize(raw, "eastmoney", turnover_is_ratio=False)


def list_symbols() -> pd.DataFrame:
    """全部 A 股代码与名称，列为 Symbol / Name。"""
    return ak.stock_info_a_code_name().rename(columns={"code": "Symbol", "name": "Name"})


def trade_calendar() -> pd.DatetimeIndex:
    """A 股交易日历（含历史与已公布的未来交易日）。"""
    df = ak.tool_trade_date_hist_sina()
    return pd.DatetimeIndex(pd.to_datetime(df["trade_date"]))
