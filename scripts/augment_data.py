"""
归爻 V5.3 — 合成数据生成引擎

基于专家方案实现：历史碎片拼接 + 价格重整 + LHS 扰动 + 持仓注入
"""

import numpy as np
import polars as pl
from pathlib import Path
from datetime import datetime, timedelta
import random

SEED = 20260715
rng = np.random.default_rng(SEED)


def rebase_concatenate(fragments_ohlcv: list) -> np.ndarray:
    """价格重整拼接：多段OHLCV → 连续序列"""
    T, S = fragments_ohlcv[0].shape[0], fragments_ohlcv[0].shape[2]
    result = np.zeros((sum(f.shape[0] for f in fragments_ohlcv), S, 5))
    idx = 0
    for frag_i, frag in enumerate(fragments_ohlcv):
        for s in range(S):
            if frag_i == 0:
                last_close = frag[-1, s, 3]  # close
            else:
                last_close = result[idx - 1, s, 3]
                ratio = last_close / frag[0, s, 3] if frag[0, s, 3] > 0 else 1.0
                frag[:, s, :] *= ratio
        Tf = frag.shape[0]
        result[idx:idx + Tf] = frag
        idx += Tf
    return result


def augment_dataset(original: np.ndarray, n_synthetic: int = 5):
    """从原始数据生成合成行情"""
    T, S, C = original.shape  # C: [open, high, low, close, volume]
    syn = np.zeros((n_synthetic, T, S, C))
    for i in range(n_synthetic):
        noise = 1.0 + rng.normal(0, 0.015, (T, S, C))
        syn[i] = original * noise
    return syn
