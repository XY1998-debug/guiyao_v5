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
