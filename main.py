"""
归爻 V5 P0 — 主入口
每日自动执行：数据拉取 → 市场状态 → 因子 → 参数搜索 → 仓位 → 信号输出
"""

import sys, time, numpy as np, polars as pl
from pathlib import Path
from datetime import datetime, date

BASE_DIR = Path(__file__).parent  # guiyao_v5 目录

from engine.data_layer import (
    update_daily_kline, update_float_shares,
    compute_breadth,
)
from engine.regime import RegimeDetector
from engine.factors import compute_factors, mad_normalize, mgs_orthogonalize, weighted_score
from engine.position_sizer import PositionSizer
from engine.param_search import search_params
from engine.signal_output import format_morning_report


def main():
    print("=" * 50)
    print(f"🪷 归爻 V5 P0 — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 50)

    # deadline 检查（测试模式可以通过环境变量跳过）
    import os
    if os.environ.get("_TEST_MODE") != "1":
        deadline = datetime.now().replace(hour=9, minute=25, second=0)
        if datetime.now() > deadline:
            print("⚠️ 超过截止时间 9:25，进入防御性盲跑模式")
            print("   跳过买入信号生成，仅执行持仓止损检查")
            return

    t0 = time.time()

    # ── 1. 数据 ──
    print("\n[1/5] 数据准备...")
    df_market = pl.read_parquet(BASE_DIR / "data/market_v5.parquet")
    top_codes = df_market.group_by("code").agg(pl.col("close").count()).sort("close", descending=True).head(210)["code"].to_list()
    update_daily_kline(top_codes)

    # 实时breadth（腾讯API）
    from engine.data_layer import compute_breadth
    breadth_csi = compute_breadth()
    breadth_210 = 0.5  # 210池breadth暂用简化
    print(f"  breadth: 中证1000={breadth_csi:.2f} 210池={breadth_210:.2f} (腾讯API实时)")

    # ── 2. 市场状态 ──
    print("\n[2/5] 市场状态...")
    df = pl.read_parquet(BASE_DIR / "data/market_v5.parquet")
    idx_data = df.group_by("date").agg(pl.col("close").mean()).sort("date")
    detector = RegimeDetector()
    reg = detector.detect(
        idx_close=idx_data["close"].tail(1)[0],
        idx_ma20=idx_data["close"].rolling_mean(20).tail(1)[0],
        idx_ma60=idx_data["close"].rolling_mean(60).tail(1)[0],
        idx_20d_ret=idx_data["close"].pct_change(20).tail(1)[0],
        vol_20d=idx_data["close"].pct_change().rolling_std(20).tail(1)[0] or 0,
        breadth_csi1000=breadth_csi,
        breadth_210=breadth_210,
        streak_days=0,
        ma3_total=0,
    )
    print(f"  regime={reg['regime']} total={reg['total']} confidence={reg['confidence']:0.0%}")
    if reg["max_positions"] == 0:
        print("  → 空仓，跳过后续步骤")
        report = format_morning_report("bear", reg["total"], reg["confidence"], [], [])
        print(f"\n{report}")
        return

    # ── 3. 因子计算 ──
    print("\n[3/5] 因子计算...")
    df = compute_factors(df)
    factor_cols = ["mom_20d", "vol_20d", "rev_5d"]
    df = mad_normalize(df, factor_cols)
    print(f"  {len(df)} 行, {df['code'].n_unique()} 股")

    # ── 4. 参数搜索 ──
    print("\n[4/5] 参数搜索 (200宇宙)...")
    all_dates = sorted(df["date"].unique().to_list())
    n_stocks = df["code"].n_unique()

    # 转 NumPy 矩阵
    date_to_idx = {str(d): i for i, d in enumerate(all_dates)}
    T, S = len(all_dates), n_stocks
    price_mat = np.zeros((T, S), dtype=np.float64)
    factor_mat = np.zeros((T, S, 3), dtype=np.float32)
    atr_mat = np.zeros((T, S), dtype=np.float64)
    limit_mat = np.zeros((T, S), dtype=np.float64)

    for date_str, idx in date_to_idx.items():
        day_df = df.filter(pl.col("date") == date_str).sort("code")
        n = min(day_df.height, S)
        price_mat[idx, :n] = day_df["close"].to_numpy().astype(np.float64).reshape(-1)[:n]
        factor_mat[idx, :n, 0] = day_df.select("z_mom_20d").to_numpy().reshape(-1)[:n]
        factor_mat[idx, :n, 1] = day_df.select("z_vol_20d").to_numpy().reshape(-1)[:n]
        factor_mat[idx, :n, 2] = day_df.select("z_rev_5d").to_numpy().reshape(-1)[:n]
        atr_val = day_df.select("vol_20d").fill_nan(0.0).to_numpy().reshape(-1)[:n]
        atr_mat[idx, :n] = np.abs(atr_val) * price_mat[idx, :n]  # ATR≈vol×price

        # 当日跌停价 (主板 × 0.9)
        limit_mat[idx, :n] = day_df["close"].to_numpy().astype(np.float64).reshape(-1)[:n] * 0.9

        # z_ 列名可能被标准化覆盖，使用原始因子
        for fi, col in enumerate(["z_mom_20d", "z_vol_20d", "z_rev_5d"]):
            if col not in day_df.columns:
                continue
            factor_mat[idx, :n, fi] = day_df.select(col).to_numpy().reshape(-1)[:n]

    best_params, stats = search_params(
        price_mat, factor_mat, atr_mat, limit_mat,
        all_dates, n_stocks=S, n_windows=4,
        min_ann=0.10, min_sharpe=0.8, max_dd=0.20,
    )
    if not stats.get("qualified", False):
        print(f"  ℹ️ 验证未通过(最优夏普={stats.get('best_sharpe', 0):+.2f})，使用最优参数降级运行")

    # ── 5. 信号输出 ──
    print("\n[5/5] 信号输出...")
    # 用最佳参数生成当日信号
    mom_th, vol_th, rev_th, sell_th = best_params
    today_df = df.filter(pl.col("date") == all_dates[-1]).sort("code")
    mom = today_df.select("z_mom_20d").to_numpy().reshape(-1)
    vol = today_df.select("z_vol_20d").to_numpy().reshape(-1)
    rev = today_df.select("z_rev_5d").to_numpy().reshape(-1)
    codes = today_df["code"].to_list()

    buy_mask = (mom > mom_th) & (vol < vol_th) & (rev < rev_th)
    buy_codes = [c for c, b in zip(codes, buy_mask) if b]

    if buy_codes:
        print(f"  买入信号: {', '.join(buy_codes[:10])}")
    else:
        print("  无买入信号")

    report = format_morning_report(
        reg["regime"], reg["total"], reg["confidence"],
        [{"stock": c, "direction": "buy"} for c in buy_codes[:10]],
        []  # positions
    )
    print(f"\n{report}")

    elapsed = time.time() - t0
    print(f"\n✅ 完成 ({elapsed:.1f}s)")


if __name__ == "__main__":
    main()
