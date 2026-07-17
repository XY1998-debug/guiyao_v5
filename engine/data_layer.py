"""
归爻 V5 P0 — 数据层
职责：mootdx日线 + akshare快照 + 210池管理 + 流通股本缓存 + breadth计算
"""

import polars as pl
import numpy as np
import os, time
from pathlib import Path
from datetime import datetime, date
from typing import Optional, Tuple, List

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
CACHE_DIR = DATA_DIR / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

FLOAT_SHARES_PATH = CACHE_DIR / "float_shares.parquet"
SNAPSHOT_CACHE_PATH = CACHE_DIR / "csi1000_snapshot.parquet"
POOL_PATH = CACHE_DIR / "pool_210.parquet"


# ═══════════════════════════════════════
# 日线数据
# ═══════════════════════════════════════

def update_daily_kline(codes: list[str]) -> pl.DataFrame:
    """增量更新日线 Parquet。返回完整 DataFrame"""
    out = DATA_DIR / "market_v5.parquet"
    existing = pl.read_parquet(out) if out.exists() else pl.DataFrame()
    latest_date = existing["date"].max() if len(existing) > 0 else None

    new_rows = []
    from mootdx.quotes import Quotes
    client = Quotes.factory(market="std")

    for code in codes:
        df = client.bars(symbol=code, frequency=9, start=0, count=800)
        if df is None or len(df) == 0:
            continue
        dates = df["datetime"].str.slice(0, 10)
        if latest_date and all(d <= latest_date for d in dates):
            continue  # 无新数据
        keep = df if latest_date is None else df[dates > latest_date]
        n = len(keep)
        new_rows.append(pl.DataFrame({
            "code": [code] * n,
            "date": list(keep["datetime"].str.slice(0, 10)),
            "open": keep["open"].values.astype(np.float64),
            "high": keep["high"].values.astype(np.float64),
            "low": keep["low"].values.astype(np.float64),
            "close": keep["close"].values.astype(np.float64),
            "volume": keep["volume"].values.astype(np.int64),
            "amount": keep["amount"].values.astype(np.float64),
        }))

    if new_rows:
        new_df = pl.concat(new_rows)
        # 确保新数据schema与已有的匹配
        if len(existing) > 0:
            for col in existing.columns:
                if col in new_df.columns and new_df[col].dtype != existing[col].dtype:
                    new_df = new_df.with_columns(pl.col(col).cast(existing[col].dtype))
        merged = pl.concat([existing, new_df]).sort(["code", "date"]).unique()
        merged.write_parquet(out, compression="zstd")

    return pl.read_parquet(out)


# ═══════════════════════════════════════
# 流通股本 (周末低频缓存)
# ═══════════════════════════════════════

def update_float_shares():
    """每周六调用，拉取全市场流通股本"""
    try:
        import akshare as ak
        df = ak.stock_zh_a_spot_em()
        result = pl.from_pandas(df).select([
            pl.col("代码").alias("code"),
            (pl.col("换手率") * pl.col("成交量")).alias("volume_turnover"),
            # 直接取流通市值 / 股价 反推流通股本
        ])
        # akshare 实时快包含流通市值和换手率，可直接反算
        result = pl.from_pandas(df).select([
            pl.col("代码").str.slice(2).alias("code"),
            pl.col("流通市值").cast(float).alias("float_mv"),
            pl.col("换手率").cast(float).alias("turnover"),
            pl.col("成交量").cast(float).alias("volume"),
        ]).with_columns(
            (pl.col("float_mv") * 1e4 / (pl.col("volume") / pl.col("turnover") * 100)).alias("float_shares")
        ).select(["code", "float_shares"])
        result.write_parquet(FLOAT_SHARES_PATH)
        return result
    except Exception:
        if FLOAT_SHARES_PATH.exists():
            return pl.read_parquet(FLOAT_SHARES_PATH)
        return pl.DataFrame({"code": [], "float_shares": []})


def get_turnover(volume_series: pl.Series, code: str, date_str: str) -> pl.Series:
    """volume(股) / 流通股本 → 换手率"""
    fs = pl.read_parquet(FLOAT_SHARES_PATH) if FLOAT_SHARES_PATH.exists() else pl.DataFrame()
    row = fs.filter(pl.col("code") == code)
    if len(row) == 0:
        return volume_series / volume_series.rolling_mean(20)  # fallback: volume_ratio
    float_shares = row["float_shares"][0]
    if float_shares <= 0:
        return volume_series / volume_series.rolling_mean(20)
    return volume_series / float_shares * 100


# ═══════════════════════════════════════
# 实时 Breadth（腾讯API替代akshare）
# ═══════════════════════════════════════

def compute_breadth_tencent() -> dict:
    """腾讯API批量查询实时涨跌家数"""
    import re
    from urllib.request import urlopen

    codes = [
        "600519","000858","002415","601318","600036","000333",
        "002594","600900","000001","600887","600276","000651",
        "600585","002714","600809","000568","002304","000002",
        "600030","601211","600031","600028","600050","600085",
        "600104","600309","600406","600436","600438","600570",
        "600585","600588","600690","600703","600741","600745",
        "600760","600763","600809","600845","600886","600887",
        "600900","600919","600926","600941","600989","600999",
        "601012","601066","601088","601111","601117","601138",
        "601166","601186","601211","601225","601236","601288",
        "601318","601319","601328","601336","601360","601377",
        "601390","601398","601555","601601","601607","601628",
        "601633","601668","601669","601688","601689","601698",
        "601727","601728","601766","601788","601800","601816",
        "601818","601857","601878","601881","601888","601899",
        "601901","601919","601939","601985","601988","601989",
        "601995","603259","603288","603986","688981",
    ]
    prefixes = []
    for c in codes:
        prefixes.append(f"sh{c}" if c[0] in "165" else f"sz{c}")
    try:
        data = urlopen(f"https://qt.gtimg.cn/q={','.join(prefixes)}", timeout=10).read().decode("gbk")
    except:
        return 0.5

    up, down, total = 0, 0, 0
    for line in data.strip().split(";"):
        m = re.search(r'"([^"]+)"', line)
        if not m: continue
        f = m.group(1).split("~")
        if len(f) < 40: continue
        try:
            chg = (float(f[3]) - float(f[4])) / float(f[4]) * 100 if float(f[4]) > 0 else 0
            total += 1
            if chg > 0: up += 1
            elif chg < 0: down += 1
        except: pass
    return up / total if total > 0 else 0.5


def compute_breadth(snapshot=None) -> float:
    """腾讯API实时breadth，snapshot参数保留兼容"""
    return compute_breadth_tencent()


# ═══════════════════════════════════════
# 210池 双轨制
# ═══════════════════════════════════════

def screen_pool_210(df: pl.DataFrame) -> pl.DataFrame:
    """月度全量筛选：120天动量最强 + 日均成交额>2000万"""
    codes = df["code"].unique().to_list()
    result = []
    from mootdx.quotes import Quotes
    client = Quotes.factory(market="std")
    for code in codes:
        k = client.bars(symbol=code, frequency=9, start=0, count=120)
        if k is None or len(k) < 60:
            continue
        close = k["close"].astype(float)
        vol = k["volume"].astype(float)
        momentum = close.iloc[-1] / close.iloc[0] - 1
        avg_amount = (vol * close).mean()
        if avg_amount < 2000e4:
            continue
        result.append((code, momentum))
    result.sort(key=lambda x: -x[1])
    pool = pl.DataFrame({"code": [r[0] for r in result[:210]]})
    pool.write_parquet(POOL_PATH)
    return pool


def hard_exclude(code: str, today_close: float, today_vol: float,
                 yesterday_close: float, pre_close: float) -> str | None:
    """硬性剔除检查。返回剔除原因或 None"""
    # ST 检查（通过名称判断，待扩充）
    # 一字跌停: 今日收盘 == 跌停价 且 成交量极小
    limit_down = pre_close * 0.9
    if abs(today_close - limit_down) < 0.01 and today_vol < 1000:
        return "一字跌停"
    # 单日 > 15% 且放量
    if (today_close / pre_close - 1) < -0.15 and today_vol > 10:
        return "暴跌放量"
    return None
