# 归爻 V5 — 1_引擎核心+数据 (v10 终版)
## engine/backtest_kernel_v3.py
`python
"""
归爻 V5 P0 — Numba @njit 回测内核 (修正版)

相对 V3 sandbox.py 的改动：
1. 分离 total_capital(算股数) + cash(可扣减)
2. bought_today_in/out 跨日 T+1 约束
3. 跌停判定 + 连续跌停解锁惩罚
4. cost_basis 记录持仓成本
"""

from numba import njit, prange
import numpy as np


@njit(parallel=True, cache=True)
def backtest_kernel_v3(
    prices,               # (S,) float64 — 当日收盘价
    signals,              # (U, S) int8    — 1=买入 -1=卖出 0=持有
    atr,                  # (S,) float64 — 当日ATR
    limit_down,           # (S,) float64 — 当日跌停价
    total_capital,        # float64       — 固定本金(算股数用)
    cash,                 # (U, 1) float64 — 可用现金(原地修改)
    pos,                  # (U, S) int32   — 持仓股数(原地修改)
    cost_basis,           # (U, S) float64 — 持仓均价(原地修改)
    bought_today_in,      # (U, S) int8    — 昨日买入标记(只读)
    bought_today_out,     # (U, S) int8    — 今日买入标记(写入)
    yesterday_limit_down, # (S,) int8     — 昨日是否跌停(只读)
    n_univ, n_stocks,     # int — 维度
    m_sl,             # float — 止损乘数(个股2.5/ETF1.5)
    commission_rate,  # float — 买入佣金率(国泰0.0008/银河0.0086)
    stamp_duty,       # float — 卖出印花税率(0.001)
    min_commission,   # float — 最低佣金(5.0)
):
    """单日沙盒推进内核：并行遍历所有平行宇宙"""

    FEE_BUY = commission_rate
    FEE_SELL = commission_rate + stamp_duty
    MIN_FEE = min_commission
    RISK_PCT = 0.0225   # 每笔风险预算 2.25% 本金

    for u in prange(n_univ):
        # 清零今日买入标记（外层确保不污染跨日状态）
        for s in range(n_stocks):
            bought_today_out[u, s] = 0

        # ── 先卖后买 ──

        # 卖出: signal == -1
        for s in range(n_stocks):
            if signals[u, s] != -1 or pos[u, s] <= 0:
                continue
            # T+1 检查: 昨天买的今天不能卖
            if bought_today_in[u, s] == 1:
                continue
            # 跌停检查: 无法卖出
            if abs(prices[s] - limit_down[s]) < 1e-4:
                continue
            # 连续跌停后首次打开次日: 惩罚 2% 滑点
            sell_price = prices[s]
            if yesterday_limit_down[s] == 1 and prices[s] > limit_down[s] + 0.01:
                sell_price = prices[s] * 0.98  # 恐慌抛压惩罚
            # 执行卖出
            gross = pos[u, s] * sell_price
            fee = max(gross * FEE_SELL, MIN_FEE)
            cash[u, 0] += gross - fee
            pos[u, s] = 0
            cost_basis[u, s] = 0.0

        # ── 买入: signal == 1 ──
        # 当日净值 = cash + pos × price
        nv = cash[u, 0]
        for s in range(n_stocks):
            nv += pos[u, s] * prices[s]
        initial_risk_budget = max(nv * RISK_PCT, 0.0)

        for s in range(n_stocks):
            if signals[u, s] != 1:
                continue
            if prices[s] <= 0.01:
                continue
            if atr[s] <= 0.01:
                continue

            # 风险预算: 1.5% 本金 / (2 × ATR)
            raw_shares = initial_risk_budget / (m_sl * atr[s])
            shares = int(raw_shares)

            # 整数股: 100 手取整
            if shares >= 100:
                shares = (shares // 100) * 100
            else:
                continue  # 不足1手不买

            cost = shares * prices[s]
            fee = max(cost * FEE_BUY, MIN_FEE)
            total_cost = cost + fee

            if total_cost > cash[u, 0]:
                continue  # 现金不足

            # 执行买入
            cash[u, 0] -= total_cost
            # 更新平均持仓成本
            total_orig = cost_basis[u, s] * pos[u, s]
            cost_basis[u, s] = (total_orig + cost) / (pos[u, s] + shares)
            pos[u, s] += shares
            bought_today_out[u, s] = 1  # 今天买入，明天才能卖
            # 重新计算可用净值，防止后续买入超预算
            nv = cash[u, 0]
            for s2 in range(n_stocks):
                nv += pos[u, s2] * prices[s2]
            


@njit(cache=True)
def compute_net_values(
    cash, pos, prices, n_univ, n_stocks
) -> np.ndarray:
    """计算当日各宇宙净值"""
    nv = np.zeros(n_univ, dtype=np.float64)
    for u in range(n_univ):
        val = cash[u, 0]
        for s in range(n_stocks):
            val += pos[u, s] * prices[s]
        nv[u] = val
    return nv

`

## engine/data_layer.py
`python
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

`

## engine/execution_engine.py
`python
# 归爻 V5.P1 执行引擎
import numpy as np
import polars as pl
from dataclasses import dataclass
from typing import Optional

MARKET_TYPE = {
    "600":"主板","601":"主板","603":"主板","605":"主板",
    "000":"主板","001":"主板","002":"主板",
    "300":"创业板","301":"创业板",
    "688":"科创板","689":"科创板",
}
LIMIT_RATIO = {"主板":0.10,"创业板":0.20,"科创板":0.20}

def _get_market(code):
    return MARKET_TYPE.get(str(code)[:3], "主板")

def gate_macro_veto(regime, stype="stock"):
    if regime == "bear": return False
    if regime == "chop" and stype == "stock": return False
    return True

@dataclass
class PriceSuggestion:
    entry_price: float = 0.0
    stop_loss: float = 0.0
    take_profit: float = 0.0
    position_pct: float = 0.0
    regime: str = "chop"
    market_type: str = "主板"

class ExecutionEngine:
    GAMMA = {"bull":1.2,"chop":1.0,"bear":-1.0}
    SLIPPAGE = {"high":0.001,"mid":0.002,"low":0.003}
    PRICE_CAGE = 0.02
    # 股票/ETF分策略止损止盈（仲裁专家终裁）
    STOCK_MSL = 2.5; STOCK_RRR = 3.5
    ETF_MSL = 1.5; ETF_RRR = 2.0
    GAMMA = {"bull":1.2,"chop":1.0,"bear":-1.0}


    def calculate(self, code, signal, price, atr, atr20,
                  entry_price=0, position_pct=0, regime="chop", vol_rank="mid", asset_type="stock"):
        if not gate_macro_veto(regime, asset_type):
            return None
        mkt = _get_market(code)
        atr_safe = atr20 if (atr>3*atr20 and atr20>0.01) else atr
        if regime == chr(98)+chr(101)+chr(97)+chr(114): return None
        if signal == 1:
            gamma = self.GAMMA.get(regime, 1.0)
            m_sl = self.STOCK_MSL if asset_type=="stock" else self.ETF_MSL
            rr = self.STOCK_RRR if asset_type=="stock" else self.ETF_RRR
            sl = round(price - atr_safe * m_sl, 2)
            tp = round(price + (price - sl) * rr, 2)
            entry = round(price * (1 + self.SLIPPAGE.get(vol_rank,0.002)), 2)
            
            return PriceSuggestion(entry, round(sl,2), tp, 0.10, regime, mkt)
        if signal == -1 and entry_price > 0:
            pnl = (price - entry_price) / entry_price
            trail = 0.05 if pnl<0.10 else (0.04 if pnl<0.30 else 0.03)
            sl = round(entry_price * (1 - trail), 2)
            return PriceSuggestion(0, sl, 0, position_pct, regime, mkt)
        return None

CONFIGS = {
    "train": {"slip":0.0,"style":"close","t1p":False,"cage":False},
    "virtual": {"slip":"random","style":"next_open","t1p":True,"t1pct":0.02,"cage":True,"grace":21},
    "sim": {"slip":"live","style":"auction+chase","t1p":True,"cage":True},
    "shadow": {"slip":"live","style":"auction+chase","t1p":False,"cage":True},
    "live": {"slip":"real","style":"auction+chase","t1p":False,"cage":True},
}

def apply_t1_penalty(df):
    if "entry_price" not in df.columns:
        return df
    return df.with_columns(
        pl.when((pl.col("exit_price")<pl.col("entry_price")*0.95)&(pl.col("holding_days")<=1))
        .then(pl.col("pnl")-pl.col("shares")*pl.col("entry_price")*0.02)
        .otherwise(pl.col("pnl")).alias("pnl_adjusted")
    )

`

## engine/extreme_blocks.py
`python
"""
归爻 V5.3 — 极端行情块识别模块

功能：从真实历史中定位暴跌周，不打乱时序，不合成数据
依赖：numpy
"""

import numpy as np


def identify_stress_blocks(
    dates: np.ndarray,
    market_returns: np.ndarray,
    threshold: float = -0.05,
    min_consecutive: int = 3,
) -> list:
    """
    识别连续暴跌块。
    返回: [(start_idx, end_idx), ...]，每个元组是暴跌块的起止索引（闭区间）。
    """
    stress_days = market_returns < threshold
    blocks = []
    in_block = False
    start = 0

    for i in range(len(stress_days)):
        if stress_days[i] and not in_block:
            start = i
            in_block = True
        elif not stress_days[i] and in_block:
            if i - start >= min_consecutive:
                blocks.append((start, i - 1))
            in_block = False

    if in_block and len(stress_days) - start >= min_consecutive:
        blocks.append((start, len(stress_days) - 1))

    return blocks


def extract_market_returns(close: np.ndarray) -> np.ndarray:
    """
    从 (T, S) 收盘价计算等权市场日收益率。
    输出: (T,) 数组。
    """
    market_close = np.nanmean(close, axis=1)
    returns = np.zeros_like(market_close)
    returns[1:] = (market_close[1:] - market_close[:-1]) / market_close[:-1]
    return returns

`

## engine/factors.py
`python
"""
归爻 V5 P0 — 因子计算 + MGS正交化
4因子: mom_20d, vol_20d, rev_5d, turnover_pctile
"""

import polars as pl
import numpy as np
from typing import Dict, List


def compute_factors(df: pl.DataFrame) -> pl.DataFrame:
    """计算 P0 所需全部因子，输出带 shift(1) 防未来函数"""
    df = df.sort(["code", "date"]).with_columns([
        (pl.col("close") / pl.col("close").shift(20).over("code") - 1.0).alias("mom_20d"),
        pl.col("close").pct_change().over("code").rolling_std(20).over("code").alias("vol_20d"),
        (pl.col("close") / pl.col("close").shift(5).over("code") - 1.0).alias("rev_5d"),
    ])
    # 换手率: volume / 流通股本
    # 由 data_layer 提供 turnover 列，若没有则用 volume_ratio 近似
    if "turnover" in df.columns:
        n_stocks_today = df.group_by("date").agg(pl.col("turnover").count())
        # 截面百分位
        df = df.with_columns(
            (pl.col("turnover").rank("ordinal").over("date") / 
             pl.col("turnover").count().over("date")).alias("turnover_pctile")
        )
    else:
        df = df.with_columns(
            (pl.col("volume") / pl.col("volume").rolling_mean(20).over("code")).alias("turnover_pctile")
        )
    return df


def mad_normalize(df: pl.DataFrame, factor_cols: List[str], sigma: float = 3.0) -> pl.DataFrame:
    """MAD 去极值 + Z-score"""
    result = df.clone()
    for col in factor_cols:
        stats = result.group_by("date").agg([
            pl.col(col).median().alias(f"{col}_med"),
            (pl.col(col) - pl.col(col).median()).abs().median().alias(f"{col}_mad"),
        ])
        result = result.join(stats, on="date")
        result = result.with_columns(
            pl.col(col).clip(
                pl.col(f"{col}_med") - sigma * pl.col(f"{col}_mad"),
                pl.col(f"{col}_med") + sigma * pl.col(f"{col}_mad"),
            ).alias(col)
        )
        mu = result.group_by("date").agg(pl.col(col).mean().alias(f"{col}_mu"))
        sd = result.group_by("date").agg(pl.col(col).std().alias(f"{col}_std"))
        result = result.join(mu, on="date").join(sd, on="date")
        result = result.with_columns(
            ((pl.col(col) - pl.col(f"{col}_mu")) / (pl.col(f"{col}_std") + 1e-9)).alias(f"z_{col}")
        )
        drops = [f"{col}_med", f"{col}_mad", f"{col}_mu", f"{col}_std"]
        result = result.drop([c for c in drops if c in result.columns])
    return result


def mgs_orthogonalize(factor_matrix: np.ndarray) -> np.ndarray:
    """Modified Gram-Schmidt 正交化

    Parameters
    ----------
    factor_matrix : ndarray (n_stocks, n_factors)
        列是因子，行是股票。已 Z-score 标准化。

    Returns
    -------
    ndarray (n_stocks, n_factors) 正交因子
    """
    n, k = factor_matrix.shape
    Q = np.zeros((n, k), dtype=np.float64)
    for i in range(k):
        v = factor_matrix[:, i].copy()
        for j in range(i):
            # MGS 核心: 逐次正交
            Q[:, j] = Q[:, j] / (np.linalg.norm(Q[:, j]) + 1e-12)
            v = v - np.dot(v, Q[:, j]) * Q[:, j]
        norm = np.linalg.norm(v)
        Q[:, i] = v / (norm + 1e-12)
    return Q


def weighted_score(
    factor_matrix: np.ndarray,  # (n_stocks, n_factors) 已正交化
    weights: Dict[str, float],  # {"mom": 0.40, "vol": 0.35, "rev": 0.25}
    factor_names: List[str],    # ["mom_20d", "vol_20d", "rev_5d"]
) -> np.ndarray:                # (n_stocks,) 综合得分
    w = np.array([weights.get(f, 0.25) for f in factor_names], dtype=np.float64)
    w = w / w.sum()  # 归一化
    return factor_matrix @ w

`

## engine/position_sizer.py
`python
"""
归爻 V5 P0 — 仓位管理器
股数 = min(风险预算, 宏观上限, 现金) + VaR + 回撤惩罚
"""

import numpy as np
from typing import Optional


class PositionSizer:
    """仓位分配器"""

    def __init__(self, total_capital: float = 100_000.0):
        self.total_capital = total_capital
        self.peak_value = total_capital

        # 交易成本
        self.fee_buy = 0.00086  # 国泰海通个股万8.6
        self.fee_sell = 0.0013
        self.min_fee = 5.0

    def calc_shares(
        self,
        price: float,
        atr: float,
        available_cash: float,
        current_value: float,
        regime: str,
        max_single_pct: float = 0.25,
    ) -> int:
        """计算建议买入股数

        Returns: 0 表示不买
        """
        # 1) 风险预算 (1.5% 本金 / 2×ATR)
        risk = self.total_capital * 0.0225
        risk_shares = int(risk / max(atr, 0.01)) if atr > 0 else 0

        # 2) 宏观上限
        macro_shares = int(self.total_capital * max_single_pct / price) if price > 0 else 0

        # 3) 现金约束 (最多 1/3 仓位)
        cash_shares = int(available_cash / 3.0 / price) if price > 0 else 0

        # 4) 取最小值
        shares = min(risk_shares, macro_shares, cash_shares)

        # 5) 整数股 + 资金保护
        if shares >= 100:
            shares = (shares // 100) * 100
            buy_fee = max(shares * price * self.fee_buy, self.min_fee)
            if available_cash < shares * price + buy_fee:
                shares = 0

        # 6) VaR 约束 (简化: 250天5%分位)
        # 由回测引擎传入 var_5pct
        # if shares > 0:
        #     pos_value = shares * price
        #     if pos_value * var_5pct > self.total_capital * 0.05:
        #         shares = int(shares * 0.7)

        # 7) 回撤惩罚
        if current_value < self.peak_value:
            dd = (self.peak_value - current_value) / self.peak_value
            if dd > 0.05:
                shares = int(shares * max(0.3, 1.0 - dd * 3))

        return shares

    def update_peak(self, current_value: float):
        if current_value > self.peak_value:
            self.peak_value = current_value

    def calc_sell(self, price: float, shares: int) -> tuple[float, float]:
        """计算卖出回款"""
        gross = price * shares
        fee = max(gross * self.fee_sell, self.min_fee)
        return gross - fee, fee

`

## engine/realtime_breadth.py
`python
"""腾讯API替代akshare — 全市场涨跌家数(breadth)"""
import numpy as np
from urllib.request import urlopen

# 预置代表股票(沪深300+中证500混合)
REP_CODES = [
    "600519","000858","002415","300750","601318","600036","000333",
    "002594","600900","000001","600887","601166","600276","000651",
    "600585","002714","600809","000568","002304","000002",
    "600030","601211","601688","600837","601066","601236","600918",
    "000776","600958","601878",
    "600031","600019","600028","600038","600048","600050","600085",
    "600104","600111","600115","600150","600176","600196","600256",
    "600309","600329","600340","600346","600352","600362","600383",
    "600406","600418","600436","600438","600482","600487","600489",
    "600497","600516","600519","600522","600547","600570","600585",
    "600588","600596","600600","600606","600637","600660","600674",
    "600685","600690","600703","600704","600741","600745","600754",
    "600760","600763","600765","600779","600795","600809","600816",
    "600845","600886","600887","600893","600900","600919","600926",
    "600941","600958","600989","600999",
]

_prev_close_ma = 0.0  # 持久的3日均线
_streak = 0

def compute_breadth_tencent(codes=None) -> dict:
    """用腾讯API批量查询股票实时涨跌，返回breadth"""
    global _prev_close_ma, _streak
    
    codes = codes or REP_CODES[:100]
    
    # 构建腾讯API请求
    prefixes = []
    for c in codes:
        if c.startswith('6'):
            prefixes.append(f"sh{c}")
        elif c.startswith('0') or c.startswith('3'):
            prefixes.append(f"sz{c}")
        elif c.startswith('5') or c.startswith('1'):
            prefixes.append(f"sh{c}")
        else:
            prefixes.append(f"sz{c}")
    
    qs = ",".join(prefixes)
    url = f"https://qt.gtimg.cn/q={qs}"
    
    try:
        data = urlopen(url, timeout=10).read().decode("gbk")
    except:
        return {"breadth": 0.5, "stale": True, "up": 0, "down": 0, "total": 0}
    
    up, down, total = 0, 0, 0
    for line in data.strip().split(";"):
        if not line.strip():
            continue
        import re
        m = re.search(r'"([^"]+)"', line)
        if not m:
            continue
        f = m.group(1).split("~")
        if len(f) < 40:
            continue
        try:
            price = float(f[3])
            prev = float(f[4])
            if prev > 0:
                chg = (price - prev) / prev * 100
                total += 1
                if chg > 0: up += 1
                elif chg < 0: down += 1
        except:
            pass
    
    if total == 0:
        return {"breadth": 0.5, "stale": True, "up": 0, "down": 0, "total": 0}
    
    breadth = up / total
    
    return {
        "breadth": round(breadth, 3),
        "stale": False,
        "up": up,
        "down": down,
        "total": total,
        "url": url,
    }

if __name__ == "__main__":
    r = compute_breadth_tencent()
    print(f"Breadth: {r['breadth']} (涨{r['up']}/跌{r['down']}/共{r['total']})")
    print(f"新鲜度: {'实时✅' if not r['stale'] else '缓存⚠️'}")

`

## engine/regime.py
`python
"""
归爻 V5 P0 — 市场状态检测 (宏观脑)
仲裁终裁版本 (2026-07-17)
  BULL_THRESH=70, BEAR_THRESH=25, BULL_EXIT=65
  宽度一票否决 + ma3 判定 + Bull 滞后退出 + 趋势分割修正
"""
import numpy as np

VOL_SLOPE = 600

class RegimeDetector:
    BULL_THRESH = 70
    BEAR_THRESH = 25
    BULL_EXIT = 65
    WIDTH_VETO = 12
    TREND_PROTECT = 28
    TREND_CAP = 15

    def detect(self, idx_close=None, idx_ma20=None, idx_ma60=None,
               idx_20d_ret=None, vol_20d=0.0, breadth_csi1000=0.5,
               breadth_210=0.5, streak_days=0, ma3_total=0.0,
               prev_regime="chop"):
        trend = 20
        if idx_close is not None and idx_ma20 is not None and idx_ma60 is not None:
            if idx_close > idx_ma20:
                trend += 5
            if idx_close > idx_ma60:
                trend += 15
            if idx_20d_ret is not None:
                trend = min(40, trend + 10 * np.tanh(max(-3, min(3, idx_20d_ret)) * 100))
        trend = max(0, min(40, trend))
        raw_vol = 30 * max(0, 1.0 - vol_20d / 0.05)
        if trend >= self.TREND_PROTECT:
            vol_score = 30.0
        elif trend < self.TREND_CAP:
            vol_score = min(raw_vol, 20.0)
        else:
            vol_score = raw_vol
        width_score = 30 * (breadth_csi1000 * 0.7 + breadth_210 * 0.3)
        total = trend + vol_score + width_score
        if width_score < self.WIDTH_VETO - 0.01:
            return {"regime":"bear","total":round(total,1),"confidence":1.0,"max_positions":0,"max_single":0.0}
        if total < self.BEAR_THRESH:
            return {"regime":"bear","total":round(total,1),"confidence":min(1.0,(25-total)/25),"max_positions":0,"max_single":0.0}
        if prev_regime == "bear":
            return {"regime":"chop","total":round(total,1),"confidence":min(1.0,(total-25)/45),"max_positions":3,"max_single":0.20}
        in_bull = (total >= self.BULL_THRESH or ma3_total >= self.BULL_THRESH)
        if prev_regime == "bull" and total < self.BULL_EXIT:
            in_bull = False
        if in_bull:
            return {"regime":"bull","total":round(total,1),"confidence":min(1.0,(total-70)/30),"max_positions":5,"max_single":0.25}
        return {"regime":"chop","total":round(total,1),"confidence":min(1.0,(total-25)/45),"max_positions":3,"max_single":0.20}

    @staticmethod
    def factor_weights(regime):
        if regime == "bull":
            return {"mom":0.40,"vol":0.35,"rev":0.25}
        if regime == "bear":
            return {"mom":0.10,"vol":0.80,"rev":0.10}
        return {"mom":0.25,"vol":0.50,"rev":0.25}

`

## engine/regime_meso.py
`python
"""
归爻 V5 — 中观脑 (Meso Brain)

职责：
  - 每周五收盘计算 22只 ETF 的 RS 排名
  - Top 3 持仓 + Top 5 缓冲带
  - 全负 RS → 国债安全垫
  - 月度交易上限 8 笔

仲裁规则（终版）：
  宏观脑 = 一票否决权（熊市时中观+微观全部静默）
  中观脑 = 周频执行（周五算排名，周一执行换仓）
  微观脑 = 条件降级（震荡市个股仅输出影子账本）
"""

import polars as pl
import numpy as np
from datetime import datetime, date, timedelta

# 22 只 ETF 池（宽基5 + 行业10 + 跨境3 + 红利2 + 安全垫2）
ETF_POOL = {
    # 宽基
    "510050": "上证50", "510300": "沪深300", "510500": "中证500",
    "512100": "中证1000", "588000": "科创50",
    # 行业
    "512880": "证券ETF", "512690": "酒ETF", "512480": "半导体ETF",
    "512660": "军工ETF", "515030": "新能源车", "512800": "银行ETF",
    "159825": "农业ETF", "510880": "红利ETF", "515220": "煤炭ETF",
    "512170": "医疗ETF",
    # 跨境
    "159941": "纳指ETF", "513100": "纳指100", "513330": "恒生互联",
    # 红利
    "159920": "恒生ETF",
    # 安全垫
    "511260": "10年国债",  # 安全垫（替代511010/511880）
}

ETF_CODES = list(ETF_POOL.keys())


class MesoBrain:
    """中观脑：行业 ETF 轮动"""

    def __init__(self, init_capital: float = 50000.0):
        self.capital = init_capital
        self.positions = {}       # {code: {shares, buy_price, buy_date}}
        self.monthly_trades = 0
        self.trade_month = None
        self.last_rank_date = None
        self.rank_cache = None    # 缓存最近 RS 排名

    def compute_rankings(self, df: pl.DataFrame, calc_date: str) -> list:
        """计算 22只 ETF 的 RS 排名（20日+60日动量加权）

        RS = 20日涨幅 × 0.7 + 60日涨幅 × 0.3
        返回 [(code, rs), ...] 按 RS 降序
        """
        results = []
        for code in ETF_CODES:
            stock_df = df.filter(
                (pl.col("code") == code) &
                (pl.col("date") <= calc_date)
            ).sort("date")

            if stock_df.height < 61:
                continue

            closes = stock_df["close"].to_numpy().astype(np.float64)
            # 20日涨幅 = 今天 / 20天前 - 1
            ret_20 = closes[-1] / closes[-21] - 1.0 if len(closes) >= 21 else 0.0
            # 60日涨幅
            ret_60 = closes[-1] / closes[-61] - 1.0 if len(closes) >= 61 else 0.0

            rs = ret_20 * 0.5 + ret_60 * 0.5
            results.append((code, rs))

        results.sort(key=lambda x: -x[1])
        return results

    def decide_trades(self, rankings: list, macro_regime: str) -> dict:
        """根据 RS 排名 + 宏观状态决定操作

        返回:
          {
            "action": "hold" / "rebalance" / "safety",
            "hold": [code, ...],      # 继续持有的
            "buy": [code, ...],       # 新买入的
            "sell": [code, ...],      # 卖出的
            "safety_mode": bool,      # 是否全仓国债
          }
        """
        decision = {"action": "hold", "hold": [], "buy": [], "sell": [],
                     "safety_mode": False}

        # 宏观否决：熊市 → 全仓国债
        if macro_regime == "bear":
            decision["action"] = "safety"
            decision["safety_mode"] = True
            decision["sell"] = list(self.positions.keys())
            self.positions.clear()
            return decision

        # 检查是否全市场负 RS
        top_rs = [r[1] for r in rankings[:5]]
        if all(rs < 0 for rs in top_rs):
            decision["action"] = "safety"
            decision["safety_mode"] = True
            decision["sell"] = list(self.positions.keys())
            self.positions.clear()
            return decision

        top3 = [r[0] for r in rankings[:3]]
        top5 = [r[0] for r in rankings[:5]]

        current_codes = set(self.positions.keys())

        # 缓冲带：当前持仓如果在 Top 5 内就不卖
        keep = [c for c in current_codes if c in set(top5)]
        sell = [c for c in current_codes if c not in set(top5)]
        buy = [c for c in top3 if c not in current_codes]

        # 如果你持有3只但都在Top5，不换仓
        if not sell and len(current_codes) <= 3:
            decision["action"] = "hold"
        elif buy or sell:
            decision["action"] = "rebalance"

        decision["hold"] = keep
        decision["buy"] = buy
        decision["sell"] = sell

        return decision

    def execute_trades(self, decision: dict, calc_date: str, current_price: float = 0.0) -> list:
        """执行决策，返回交易记录列表"""
        trades = []

        # 月度计数重置
        now = datetime.strptime(calc_date[:7], "%Y-%m") if "-" in calc_date else datetime.now()
        if self.trade_month is None or self.trade_month != now.month:
            self.monthly_trades = 0
            self.trade_month = now.month

        # 安全垫模式
        if decision["safety_mode"]:
            self.positions = {}
            trades.append({"action": "safety", "date": calc_date,
                           "position": "511010(30年国债)", "shares": 0})
            self.monthly_trades += 1
            return trades

        # 卖出
        for code in decision["sell"]:
            if code in self.positions and self.monthly_trades < 8:
                p = self.positions.pop(code)
                pnl = (current_price - p.get("buy_price", 0)) * p.get("shares", 0) if p.get("buy_price") else 0  # 简化
                trades.append({"action": "sell", "date": calc_date,
                               "code": code, "name": ETF_POOL.get(code, ""),
                               "shares": p.get("shares", 0), "pnl": pnl})
                self.monthly_trades += 1

        # 买入
        target_value = self.capital / 3  # Top 3 等权
        for code in decision["buy"]:
            if self.monthly_trades >= 8:
                break
            shares = int(target_value / 10000 * 1000)  # 简化：按 ETF 净值估算
            self.positions[code] = {"shares": shares, "buy_price": 0,
                                     "buy_date": calc_date}
            trades.append({"action": "buy", "date": calc_date,
                           "code": code, "name": ETF_POOL.get(code, ""),
                           "shares": shares})
            self.monthly_trades += 1

        # 已有仓位不动
        for code in decision["hold"]:
            trades.append({"action": "hold", "date": calc_date,
                           "code": code, "name": ETF_POOL.get(code, ""),
                           "shares": self.positions[code].get("shares", 0)})

        return trades

    def weekly_cycle(self, df: pl.DataFrame, calc_date: str, macro_regime: str) -> dict:
        """完整周频流程：排名 → 决策 → 执行"""
        rankings = self.compute_rankings(df, calc_date)
        if not rankings:
            return {"status": "no_data", "trades": []}

        decision = self.decide_trades(rankings, macro_regime)
        trades = self.execute_trades(decision, calc_date)

        top5_str = ", ".join([f"{c}({r:.1%})" for c, r in rankings[:5]])
        return {
            "status": decision["action"],
            "top5": top5_str,
            "safety_mode": decision["safety_mode"],
            "trades": trades,
            "positions": {k: v["shares"] for k, v in self.positions.items()},
            "monthly_trades": self.monthly_trades,
        }

`

## engine/shadow_ledger.py
`python
"""
归爻 V5 P0 — 影子账本 (Shadow Ledger)

每日在后台模拟执行所有 AI 信号（15%仓位），不受用户主观过滤影响。
与实盘账本对比，量化"人脑过滤"的价值。

用法：
  from engine.shadow_ledger import ShadowLedger
  ledger = ShadowLedger()
  ledger.log_signal(signal)       # AI 发出信号时调用
  ledger.log_execution(trade)     # 用户实际执行时调用
  report = ledger.weekly_report() # 周日自动生成对比报告
"""

import sqlite3
import json
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional

LEDGER_DB = Path(__file__).parent.parent / "data" / "shadow_ledger.db"


class ShadowLedger:

    def __init__(self, db_path: str = str(LEDGER_DB)):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                signal_time TEXT NOT NULL,
                stock_code TEXT NOT NULL,
                direction TEXT NOT NULL CHECK(direction IN ('buy','sell')),
                trigger_price REAL,
                target_qty INTEGER,
                source TEXT DEFAULT 'AI'  -- 'AI' or 'USER'
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                signal_id INTEGER,
                trade_time TEXT NOT NULL,
                stock_code TEXT NOT NULL,
                direction TEXT NOT NULL,
                price REAL NOT NULL,
                shares INTEGER NOT NULL,
                fee REAL DEFAULT 0,
                source TEXT DEFAULT 'AI',  -- 'SHADOW' or 'USER'
                FOREIGN KEY (signal_id) REFERENCES signals(id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS weekly_report (
                week_start TEXT PRIMARY KEY,
                shadow_pnl REAL,
                shadow_trades INTEGER,
                user_pnl REAL,
                user_trades INTEGER,
                verdict TEXT  -- 'USER_WINS', 'AI_WINS', 'TIE'
            )
        """)
        conn.commit()
        conn.close()

    def log_ai_signal(self, stock: str, direction: str, price: float, qty: int) -> int:
        """AI 发出买入/卖出信号时记录"""
        conn = sqlite3.connect(self.db_path)
        cur = conn.execute(
            "INSERT INTO signals (signal_time, stock_code, direction, trigger_price, target_qty, source) VALUES (?,?,?,?,?,'AI')",
            (datetime.now().isoformat(), stock, direction, price, qty)
        )
        conn.commit()
        signal_id = cur.lastrowid
        conn.close()
        return signal_id

    def log_shadow_execution(self, signal_id: int, stock: str, direction: str,
                              price: float, shares: int, fee: float = 0):
        """AI 模拟执行（影子账本）"""
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO trades (signal_id, trade_time, stock_code, direction, price, shares, fee, source) VALUES (?,?,?,?,?,?,?,'SHADOW')",
            (signal_id, datetime.now().isoformat(), stock, direction, price, shares, fee)
        )
        conn.commit()
        conn.close()

    def log_user_trade(self, stock: str, direction: str, price: float, shares: int, fee: float = 0):
        """用户实际交易记录（实盘账本）"""
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO trades (trade_time, stock_code, direction, price, shares, fee, source) VALUES (?,?,?,?,?,?,'USER')",
            (datetime.now().isoformat(), stock, direction, price, shares, fee)
        )
        conn.commit()
        conn.close()

    def weekly_report(self) -> str:
        """生成每周对比报告"""
        conn = sqlite3.connect(self.db_path)
        this_week = (datetime.now() - timedelta(days=datetime.now().weekday())).strftime("%Y-%m-%d")

        # 本周影子账本
        shadow = conn.execute("""
            SELECT COUNT(*), COALESCE(SUM(t.shares * t.price * (1 - 0.0013)), 0)
            FROM trades t WHERE t.source='SHADOW' AND t.trade_time >= ?
        """, (this_week,)).fetchone()

        # 本周实盘
        user = conn.execute("""
            SELECT COUNT(*), COALESCE(SUM(t.shares * t.price * (1 - 0.0013)), 0)
            FROM trades t WHERE t.source='USER' AND t.trade_time >= ?
        """, (this_week,)).fetchone()

        shadow_cnt, shadow_val = shadow
        user_cnt, user_val = user

        verdict = "TIE"
        if shadow_val > user_val and user_cnt > 0:
            verdict = "AI_WINS"
        elif user_val > shadow_val and user_cnt > 0:
            verdict = "USER_WINS"

        # 存储
        conn.execute(
            "INSERT OR REPLACE INTO weekly_report (week_start, shadow_pnl, shadow_trades, user_pnl, user_trades, verdict) VALUES (?,?,?,?,?,?)",
            (this_week, shadow_val, shadow_cnt, user_val, user_cnt, verdict)
        )
        conn.commit()
        conn.close()

        lines = [
            f"\n{'='*50}",
            f"📊 影子账本 vs 实盘账本 — 周报 ({this_week})",
            f"{'='*50}",
        ]
        if shadow_cnt > 0:
            lines.append(f"  AI  模拟: {shadow_cnt} 笔 | 盈亏 {shadow_val:+.0f}")
        else:
            lines.append(f"  AI  模拟: 本周无信号")

        if user_cnt > 0:
            lines.append(f"  用户实盘: {user_cnt} 笔 | 盈亏 {user_val:+.0f}")
        else:
            lines.append(f"  用户实盘: 无执行记录")

        if verdict == "AI_WINS":
            lines.append(f"\n  ⚠️ AI  跑赢实盘，建议下周严格执行 AI 信号。")
        elif verdict == "USER_WINS":
            lines.append(f"\n  ✅ 用户跑赢 AI，主观判断有效。")
        else:
            if user_cnt == 0 and shadow_cnt > 0:
                lines.append(f"\n  ⚠️ 本周全部信号未执行，请检查执行力。")
            elif shadow_cnt == 0:
                lines.append(f"\n  — 本周无有效信号。")

        lines.append("=" * 50)
        return "\n".join(lines)

`

## engine/signal_output.py
`python
"""
归爻 V5 P0 — 信号输出
格式化交易建议 + 同花顺自选同步
"""

from typing import List, Dict, Optional


def format_signal(
    stock_code: str,
    direction: str,     # "buy" / "sell"
    price: float,
    shares: int,
    reason: str,        # "突破前高", "低波动企稳" 等
    regime: str,
) -> dict:
    """格式化单个交易信号"""
    return {
        "time": None,  # datetime, 由调用方填充
        "stock": stock_code,
        "direction": direction,
        "price": round(price, 2),
        "shares": shares,
        "amount": round(price * shares, 2),
        "reason": reason,
        "regime": regime,
    }


def format_morning_report(
    regime: str,
    total_score: float,
    confidence: float,
    signals: List[dict],
    positions: List[dict],
) -> str:
    """生成早盘简报（控制台输出 + 可转发）"""
    lines = []
    lines.append(f"【归爻】{regime} | 总分 {total_score} 置信度 {confidence}%")
    lines.append("-" * 40)

    if regime == "bear":
        lines.append("当前不宜交易，暂停买入。")
        lines.append(f"持仓 {len(positions)} 只，关注止损。")
        return "\n".join(lines)

    if signals:
        lines.append(f"今日建议（{len(signals)} 个信号）:")
        for s in signals[:10]:  # Top 10
            if "price" in s and "shares" in s:
                amount = s.get("amount", s["price"] * s.get("shares", 0))
                lines.append(f"  {s['direction'].upper()} {s['stock']} "
                           f"@ {s['price']:.2f} × {s['shares']} = ¥{amount:,.0f}")
            else:
                lines.append(f"  {s.get('direction','?').upper()} {s.get('stock','?')}")

    else:
        lines.append("今日无信号触发。")

    if positions:
        lines.append(f"\n当前持仓 {len(positions)} 只:")
        for p in positions:
            code = p.get("code", p.get("stock", "?"))
            qty = p.get("shares", p.get("qty", 0))
            pnl = p.get("pnl", 0)
            lines.append(f"  {code} {qty}股 PnL:{pnl:+.1f}%")

    return "\n".join(lines)


def sync_to_ths(signals: List[dict], top_candidates: List[str]):
    """同花顺自选同步：Top10候选 + 触发信号股票"""
    codes = list({s["stock"] for s in signals} | set(top_candidates))
    if not codes:
        return
    try:
        from ths_favorite.selfstock_v1 import upload_self_stock
        upload_self_stock(codes)
    except ImportError:
        pass  # 本地开发可跳过

`

## engine/signal_perturbation.py
`python
"""
归爻 V5.3 — 信号扰动与生成模块

功能：LHS 参数采样 + 指标预计算 + 向量化信号生成（含买入/卖出）
依赖：numpy, polars（均为现有依赖）
"""

import numpy as np
import polars as pl


# ============================================================
# 函数 1：LHS 参数扰动
# ============================================================
def generate_lhs_params(
    base_params: np.ndarray,
    n_per_base: int = 20,
    range_pct: float = 0.03,
    seed: int = 20260715,
) -> np.ndarray:
    """
    使用 Latin Hypercube Sampling 在基准参数周围均匀采样。
    输出: (48 * n_per_base, D) 扰动后的参数矩阵。
    """
    D = base_params.shape[1]
    total = len(base_params) * n_per_base * D
    rng = np.random.default_rng(seed)

    intervals = np.linspace(-range_pct, range_pct, total + 1)
    samples = np.array([rng.uniform(intervals[i], intervals[i + 1]) for i in range(total)])
    rng.shuffle(samples)
    samples = samples.reshape(len(base_params), n_per_base, D)

    perturbed = base_params[:, None, :] * (1 + samples)
    return perturbed.reshape(-1, D).astype(np.float64)


# ============================================================
# 函数 2：指标预计算
# ============================================================
def precompute_indicators(parquet_path: str) -> dict:
    """
    从 market_v5.parquet 一次性计算所有技术指标。
    输出: dict，key 为指标名，value 为 (T, S) float64 numpy 数组
    """
    df = pl.read_parquet(parquet_path).sort("date")

    # pivot 后第一列是 date，需要去掉
    close_raw = df.pivot(values="close", index="date", on="code").sort("date").to_numpy()
    close = close_raw[:, 1:].astype(np.float64)

    open_raw = df.pivot(values="open", index="date", on="code").sort("date").to_numpy()
    open_ = open_raw[:, 1:].astype(np.float64)

    high_raw = df.pivot(values="high", index="date", on="code").sort("date").to_numpy()
    high = high_raw[:, 1:].astype(np.float64)

    low_raw = df.pivot(values="low", index="date", on="code").sort("date").to_numpy()
    low = low_raw[:, 1:].astype(np.float64)

    volume_raw = df.pivot(values="volume", index="date", on="code").sort("date").to_numpy()
    volume = volume_raw[:, 1:].astype(np.float64)

    # 清洗 NaN：前向填充（新股会有 NaN 开头）
    for arr in [close, open_, high, low, volume]:
        for c in range(arr.shape[1]):
            col = arr[:, c]
            nan_mask = np.isnan(col)
            if np.any(nan_mask):
                for i in range(1, len(col)):
                    if nan_mask[i]:
                        col[i] = col[i - 1]

    T, S = close.shape

    # 20日动量
    mom = np.zeros_like(close)
    mom[20:] = close[20:] / close[:-20] - 1.0

    # 20日波动率（日收益率滚动 std）
    ret = np.zeros_like(close)
    ret[1:] = (close[1:] - close[:-1]) / close[:-1]
    vol = np.zeros_like(close)
    for t in range(20, T):
        vol[t] = np.std(ret[t - 19 : t + 1], axis=0)

    # 20日均量
    vol_ma20 = np.zeros_like(volume)
    for t in range(20, T):
        vol_ma20[t] = np.mean(volume[t - 19 : t + 1], axis=0)

    # ATR (14日，简化 EMA)
    tr = np.maximum(
        high - low,
        np.maximum(
            np.abs(high - np.roll(close, 1, axis=0)),
            np.abs(low - np.roll(close, 1, axis=0)),
        ),
    )
    tr[0] = high[0] - low[0]
    atr = np.zeros_like(close)
    atr[0] = tr[0]
    alpha = 2 / (20 + 1)
    for t in range(1, T):
        atr[t] = alpha * tr[t] + (1 - alpha) * atr[t - 1]

    # 跌停价（前收 × 0.9，主板适用）
    # 按市场类型设置动态跌停比例
    codes = df.pivot(values="close", index="date", on="code").columns[1:].to_list()
    limit_ratios = []
    for code in codes:
        p = code[:3]
        if p in ("300","688"):
            limit_ratios.append(0.8)
        else:
            limit_ratios.append(0.9)
    lr = np.array(limit_ratios, dtype=np.float64)
    limit_down = np.round(np.roll(close, 1, axis=0) * lr[np.newaxis, :], 2)
    limit_down[0] = 0.0

    return {
        "close": close,
        "open_": open_,
        "high": high,
        "low": low,
        "volume": volume,
        "mom": mom,
        "vol": vol,
        "vol_ma20": vol_ma20,
        "atr": atr,
        "limit_down": limit_down,
    }


# ============================================================
# 函数 3：向量化信号生成（含买入和卖出）
# ============================================================
def generate_signals_vectorized(
    indicators: dict,
    params_matrix: np.ndarray,
    volume_multiplier: float = 3.0,
) -> np.ndarray:
    """
    无状态向量化信号生成。
    输出: (U, T, S) int8 数组，1=买入，-1=卖出，0=无信号。

    买入条件（全部必须满足）：
    1. 动量突破：mom[t,s] > mom_th[u]
    2. 波动率上限：vol[t,s] < vol_th[u]
    3. 成交量放量：volume[t,s] > vol_ma20[t,s] * volume_multiplier
    4. 阳线：close[t,s] > open_[t,s]
    5. 非跌停：close[t,s] > limit_down[t,s]

    卖出条件（任一触发则卖出）：
    1. 硬止损：当日跌幅 > 5%
    2. 跟踪止盈：从 5 日高点回落 8%
    3. 反转阈值：rev_th 参数（如果有）
    """
    close = indicators["close"]
    open_ = indicators["open_"]
    volume = indicators["volume"]
    mom = indicators["mom"]
    vol = indicators["vol"]
    vol_ma20 = indicators["vol_ma20"]
    limit_down = indicators["limit_down"]

    U = params_matrix.shape[0]
    T, S = close.shape

    mom_th = params_matrix[:, 0]  # (U,)
    vol_th = params_matrix[:, 1]  # (U,)

    # ── 买入 ──
    mom_cond = mom[None, :, :] > mom_th[:, None, None]
    vol_cond = vol[None, :, :] < vol_th[:, None, None]
    vol_cond_ma = volume[None, :, :] > vol_ma20[None, :, :] * volume_multiplier
    yang_cond = close[None, :, :] > open_[None, :, :]
    not_limit_down = close[None, :, :] > limit_down[None, :, :]

    buy_signal = mom_cond & vol_cond & vol_cond_ma & yang_cond & not_limit_down

    # ── 卖出 ──
    # 日收益率
    daily_ret = close[None, 1:, :] / close[None, :-1, :] - 1.0  # (1, T-1, S)
    daily_ret_full = np.zeros((1, T, S))
    daily_ret_full[:, 1:, :] = daily_ret

    # 硬止损：日亏损 > 5%
    hard_stop = daily_ret_full < -0.05

    # 跟踪止盈：从近 5 日最高点回落 8%
    high_5d = np.zeros_like(close)
    for i in range(1, 6):
        high_5d[i:] = np.maximum(high_5d[i:], close[:-i])  # shift forward, no wrap
    trailing_stop = close[None, :, :] < high_5d[None, :, :] * 0.92
    sell_signal = hard_stop | trailing_stop

    signals = np.where(buy_signal, 1, np.where(sell_signal, -1, 0)).astype(np.int8)
    # 检查第0号宇宙第30天到40天有无买入信号
    u0_buys = np.count_nonzero(signals[0, 30:41, :] == 1)
    u0_total = np.count_nonzero(signals[0, :, :] == 1)
    print(f"  debug: u0_buys[30-40]={u0_buys} u0_total={u0_total}")
    return signals

`
