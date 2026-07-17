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
