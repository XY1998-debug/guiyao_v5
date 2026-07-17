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
        shifted = np.roll(close, i, axis=0)
        shifted[:i] = 0.0  # mask rolled rows to prevent future leak
        high_5d = np.maximum(high_5d, shifted)
    trailing_stop = close[None, :, :] < high_5d[None, :, :] * 0.92
    sell_signal = hard_stop | trailing_stop

    signals = np.where(buy_signal, 1, np.where(sell_signal, -1, 0)).astype(np.int8)
    # 检查第0号宇宙第30天到40天有无买入信号
    u0_buys = np.count_nonzero(signals[0, 30:41, :] == 1)
    u0_total = np.count_nonzero(signals[0, :, :] == 1)
    print(f"  debug: u0_buys[30-40]={u0_buys} u0_total={u0_total}")
    return signals
