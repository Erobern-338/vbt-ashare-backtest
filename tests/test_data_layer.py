"""数据层与策略层单元测试。

全部离线：读取网络的地方一律被 monkeypatch 替换，测试结果不依赖网络和行情。

注意 fetch_hist 默认会按序回退多个数据源 —— 想只测某一个必须显式传 provider
或替换 _PROVIDER_FN，否则会真的打网络。
"""

from __future__ import annotations

import pandas as pd
import pytest

from ashare.data import cache, source
from ashare.strategy import MAParams, drop_limit_signals, limit_pct, ma_cross_signals


def _fake_raw(n: int = 30, start: str = "2024-01-01") -> pd.DataFrame:
    """模拟东财接口：中文列名 + datetime.date 日期。"""
    idx = pd.date_range(start, periods=n, freq="B")
    return pd.DataFrame(
        {
            "日期": idx.date,
            "开盘": range(100, 100 + n),
            "收盘": range(101, 101 + n),
            "最高": range(102, 102 + n),
            "最低": range(99, 99 + n),
            "成交量": [1000] * n,
            "成交额": [1e5] * n,
            "振幅": [1.0] * n,
            "涨跌幅": [0.5] * n,
            "涨跌额": [0.5] * n,
            "换手率": [1.0] * n,
            "股票代码": ["000001"] * n,
        }
    )


def _fake_sina(n: int = 5) -> pd.DataFrame:
    """模拟新浪接口：小写英文列名，turnover 是小数比例，无涨跌幅。"""
    return pd.DataFrame(
        {
            "date": pd.date_range("2024-01-02", periods=n, freq="B").date,
            "open": [100.0] * n,
            "high": [100.0] * n,
            "low": [100.0] * n,
            "close": [100.0, 110.0, 121.0, 121.0, 121.0],
            "volume": [10.0] * n,
            "amount": [100.0] * n,
            "turnover": [0.005] * n,
            "outstanding_share": [1.0] * n,
        }
    )


def _recording_fetch(calls: list, data_start: str = "2024-01-01"):
    def fake_fetch(symbol, start, end, adjust="hfq", period="daily", empty_ok=False):
        calls.append((start, end))
        return source._finalize(_fake_raw(start=data_start), "fake", turnover_is_ratio=False)

    return fake_fetch


# ---------------------------------------------------------------- 规整


def test_eastmoney_normalizes_to_uniform_schema(monkeypatch):
    monkeypatch.setattr(source.ak, "stock_zh_a_hist", lambda **kw: _fake_raw())
    df = source._via_eastmoney("000001", "20240101", "20241231", "hfq", "daily")

    assert isinstance(df.index, pd.DatetimeIndex)
    assert df.index.dtype == "datetime64[ns]", "必须钉在 ns，否则 parquet 往返会变 ms"
    assert df.index.is_monotonic_increasing
    assert list(df.columns) == source.PRICE_COLUMNS
    assert all(df[c].dtype == "float64" for c in source.PRICE_COLUMNS)
    assert df.attrs["source"] == "eastmoney"


def test_sina_converts_turnover_and_computes_pct_change(monkeypatch):
    monkeypatch.setattr(source.ak, "stock_zh_a_daily", lambda **kw: _fake_sina())
    df = source._via_sina("000001", "20240101", "20240201", "hfq", "daily")

    assert df["Turnover"].iloc[0] == pytest.approx(0.5), "小数比例应换算成百分数"
    assert df["PctChg"].iloc[1] == pytest.approx(10.0), "110/100-1 = 10%"
    assert pd.isna(df["PctChg"].iloc[0]), "首行没有前收盘，涨跌幅应为 NaN"
    assert df.attrs["source"] == "sina"


def test_symbols_get_exchange_prefix():
    assert source._prefixed("000001") == "sz000001"
    assert source._prefixed("600519") == "sh600519"
    assert source._prefixed("300750") == "sz300750"
    assert source._prefixed("830799") == "bj830799"


# ---------------------------------------------------------------- 回退链


def test_fetch_hist_falls_back_to_next_provider(monkeypatch):
    """东财限流时（实测会 RemoteDisconnected）应自动切到下一个数据源。"""

    def boom(*args, **kwargs):
        raise ConnectionError("eastmoney throttled")

    def ok(*args, **kwargs):
        return source._finalize(_fake_sina(), "sina", turnover_is_ratio=True)

    monkeypatch.setitem(source._PROVIDER_FN, "eastmoney", boom)
    monkeypatch.setitem(source._PROVIDER_FN, "sina", ok)

    df = source.fetch_hist("000001", "20240101", "20240201")
    assert df.attrs["source"] == "sina"
    assert not df.empty


def test_fetch_hist_reports_all_provider_failures(monkeypatch):
    def boom(*args, **kwargs):
        raise ConnectionError("down")

    for name in source.PROVIDERS:
        monkeypatch.setitem(source._PROVIDER_FN, name, boom)

    with pytest.raises(ValueError) as exc:
        source.fetch_hist("000001", "20240101", "20240201")
    assert "所有数据源均失败" in str(exc.value)
    assert "eastmoney" in str(exc.value), "报错应列出每个数据源的失败原因"


def test_fetch_hist_empty_ok_returns_empty_frame(monkeypatch):
    monkeypatch.setitem(source._PROVIDER_FN, "eastmoney", lambda *a, **k: pd.DataFrame())

    empty = source.fetch_hist(
        "000001", "20260919", "20260919", provider="eastmoney", empty_ok=True
    )
    assert empty.empty
    assert list(empty.columns) == source.PRICE_COLUMNS

    with pytest.raises(ValueError, match="所有数据源均失败"):
        source.fetch_hist("000001", "20260919", "20260919", provider="eastmoney")


# ---------------------------------------------------------------- 缓存


def test_get_prices_offline_on_repeat(monkeypatch, tmp_path):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    calls: list[tuple] = []
    monkeypatch.setattr(source, "fetch_hist", _recording_fetch(calls))

    cache.get_prices("000001", "20240101", "20240201")
    assert len(calls) == 1

    cache.get_prices("000001", "20240101", "20240201")
    assert len(calls) == 1, "同一请求重复调用应完全离线"


def test_get_prices_refetches_only_when_window_grows(monkeypatch, tmp_path):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    calls: list[tuple] = []
    monkeypatch.setattr(source, "fetch_hist", _recording_fetch(calls))

    cache.get_prices("000001", "20240101", "20240201")
    cache.get_prices("000001", "20240101", "20241231")
    assert len(calls) == 2, "请求区间扩大时应补取"
    assert calls[1] == ("20240101", "20241231"), "应取一个连续窗口"

    cache.get_prices("000001", "20240101", "20241231")
    assert len(calls) == 2, "补齐后再次调用应离线"


def test_get_prices_refresh_forces_network(monkeypatch, tmp_path):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    calls: list[tuple] = []
    monkeypatch.setattr(source, "fetch_hist", _recording_fetch(calls))

    cache.get_prices("000001", "20240101", "20240201")
    cache.get_prices("000001", "20240101", "20240201", refresh=True)
    assert len(calls) == 2, "refresh=True 即使缓存够用也要联网核对一次"


def test_request_starting_on_non_trading_day_uses_cache(monkeypatch, tmp_path):
    """回归测试。

    实测请求 2018-01-01（元旦）时首个交易日是 01-02。此前拿"实际数据区间"去比
    "请求区间"，会把"起点正好是节假日"误判成"缓存缺数据"，于是每次调用都整段重取。
    元信息改记请求区间后不再有这个问题。
    """
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    calls: list[tuple] = []
    monkeypatch.setattr(source, "fetch_hist", _recording_fetch(calls, data_start="2024-01-02"))

    first = cache.get_prices("000001", "20240101", "20240201")
    assert first.index.min() == pd.Timestamp("2024-01-02")

    cache.get_prices("000001", "20240101", "20240201")
    assert len(calls) == 1, "起点落在非交易日不应导致每次重取"


def test_get_prices_degrades_to_cache_on_network_failure(monkeypatch, tmp_path):
    """回归测试。

    修复前只要"缓存最后日期 < 请求结束日期"就联网，于是周末/断网时连读自己的缓存
    都会抛 ConnectionError。现在网络故障应降级返回缓存并给出警告。
    """
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(source, "fetch_hist", _recording_fetch([]))
    cache.get_prices("000001", "20240101", "20240201")

    def boom(*args, **kwargs):
        raise ConnectionError("network down")

    monkeypatch.setattr(source, "fetch_hist", boom)

    with pytest.warns(UserWarning, match="刷新失败"):
        out = cache.get_prices("000001", "20240101", "20241231")
    assert len(out) > 0, "应降级返回缓存数据而不是抛错"


def test_get_prices_raises_when_no_cache_and_network_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)

    def boom(*args, **kwargs):
        raise ConnectionError("network down")

    monkeypatch.setattr(source, "fetch_hist", boom)

    with pytest.raises(ConnectionError):
        cache.get_prices("000001", "20240101", "20240201")


def test_cache_meta_records_source_and_window(monkeypatch, tmp_path):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(source, "fetch_hist", _recording_fetch([]))

    cache.get_prices("000001", "20240101", "20240201")
    parquet_path, meta_path = cache._paths("000001", "hfq")
    assert parquet_path.exists() and meta_path.exists()

    import json

    meta = json.loads(meta_path.read_text("utf-8"))
    assert meta["fetched_start"] == "20240101"
    assert meta["fetched_end"] == "20240201"
    assert meta["source"] == "fake"


# ---------------------------------------------------------------- 策略


def test_ma_cross_detects_crossover():
    close = pd.Series([10, 9, 8, 7, 6, 7, 8, 9, 10, 11], dtype=float)
    entries, exits = ma_cross_signals(close, MAParams(fast=2, slow=3))

    assert entries.sum() == 1
    assert exits.sum() == 0
    assert not (entries & exits).any(), "同一根 K 线不能既开又平"


def test_ma_cross_no_signal_during_warmup():
    close = pd.Series(range(10, 40), dtype=float)  # 单调上涨
    entries, exits = ma_cross_signals(close, MAParams(fast=5, slow=20))

    assert not entries.iloc[:20].any(), "均线预热期内不应出信号"
    assert not exits.any(), "单调上涨不应有死叉"


def test_ma_params_validates():
    with pytest.raises(ValueError, match="必须小于"):
        MAParams(fast=50, slow=10)


def test_limit_pct_by_board():
    assert limit_pct("000001") == pytest.approx(9.8)
    assert limit_pct("600519") == pytest.approx(9.8)
    assert limit_pct("300750") == pytest.approx(19.8)  # 创业板
    assert limit_pct("688981") == pytest.approx(19.8)  # 科创板


def test_drop_limit_signals():
    signals = pd.Series([True, True, True, True])
    pct_chg = pd.Series([1.0, 10.0, -9.9, -1.0])
    out = drop_limit_signals(signals, pct_chg, limit_pct("000001"))

    assert out.tolist() == [True, False, False, True]
