"""
归爻 V5 中观脑 — Walk-Forward 验证
22 只 ETF + 周频 RS 排名 + 安全垫 + 缓冲带
"""

import polars as pl
import numpy as np
from pathlib import Path
from datetime import datetime, timedelta
from engine.regime_meso import ETF_CODES, MesoBrain

DATA_FILE = Path(r"H:\guiyao_v5\data\etf_v5.parquet")


def walk_forward_meso(df: pl.DataFrame) -> dict:
    """多段 WF 验证中观脑策略"""
    all_dates = sorted(df["date"].unique().to_list())
    seg_len = len(all_dates) // 4  # 4 段，每段 ~200 天
    results = []

    # 模拟宏观状态：简单用 60日均线 判断
    idx_close = df.filter(pl.col("code") == "510300").sort("date")["close"].to_numpy()

    for i in range(3):  # 3 个测试窗口
        test_end = len(all_dates) - (2 - i) * seg_len
        train_end = test_end - seg_len

        if train_end < 120:
            continue

        train_dates = all_dates[:train_end]
        test_dates = all_dates[:test_end]

        train_df = df.filter(pl.col("date").is_in(train_dates))
        test_df = df.filter(pl.col("date").is_in(test_dates))

        # 训练期结果
        brain = MesoBrain(init_capital=50000.0)
        nv_train = [50000.0]
        for dt in [d for d in train_dates if d >= train_dates[120]][::5]:  # 每周
            # 宏观状态：简单 MA60 判断
            idx_val = idx_close[min(len(idx_close)-1, train_dates.index(dt))]
            idx_60 = np.mean(idx_close[max(0, train_dates.index(dt)-60):train_dates.index(dt)])
            regime = "bear" if idx_val < idx_60 * 0.95 else ("bull" if idx_val > idx_60 * 1.03 else "chop")

            rankings = brain.compute_rankings(train_df, dt)
            if rankings:
                dec = brain.decide_trades(rankings, regime)
                brain.execute_trades(dec, dt)

            pos_val = sum(v.get("shares", 0) for v in brain.positions.values())
            nv_train.append(50000.0 + pos_val)

        nv_train = np.array(nv_train)
        rets_train = np.diff(nv_train) / nv_train[:-1]
        train_sharpe = (np.mean(rets_train) / (np.std(rets_train) + 1e-9)) * np.sqrt(52)

        # 测试期结果
        brain2 = MesoBrain(init_capital=50000.0)
        nv_test = [50000.0]
        for dt in [d for d in test_dates if d >= test_dates[120]][::5]:
            idx_val = idx_close[min(len(idx_close)-1, test_dates.index(dt))]
            idx_60 = np.mean(idx_close[max(0, test_dates.index(dt)-60):test_dates.index(dt)])
            regime = "bear" if idx_val < idx_60 * 0.95 else ("bull" if idx_val > idx_60 * 1.03 else "chop")

            rankings = brain2.compute_rankings(test_df, dt)
            if rankings:
                dec = brain2.decide_trades(rankings, regime)
                brain2.execute_trades(dec, dt)

            pos_val = sum(v.get("shares", 0) for v in brain2.positions.values())
            nv_test.append(50000.0 + pos_val)

        nv_test = np.array(nv_test)
        rets_test = np.diff(nv_test) / nv_test[:-1]
        test_sharpe = (np.mean(rets_test) / (np.std(rets_test) + 1e-9)) * np.sqrt(52)
        test_ann = (nv_test[-1] / nv_test[0]) ** (52 / len(rets_test)) - 1 if len(rets_test) > 0 else 0
        test_dd = 0
        peak = nv_test[0]
        for v in nv_test:
            if v > peak: peak = v
            dd = (peak - v) / peak
            if dd > test_dd: test_dd = dd

        results.append({
            "window": i,
            "train_dates": f"{train_dates[0][:10]}..{train_dates[-1][:10]}",
            "test_dates": f"{test_dates[train_end][:10]}..{test_dates[-1][:10]}",
            "train_sharpe": round(train_sharpe, 2),
            "test_sharpe": round(test_sharpe, 2),
            "test_ann": round(test_ann, 3),
            "test_dd": round(test_dd, 3),
        })

    # 汇总
    sh_mean = np.mean([r["test_sharpe"] for r in results])
    ann_mean = np.mean([r["test_ann"] for r in results])
    dd_max = max([r["test_dd"] for r in results])

    print(f"\n📊 WF 汇总 (22只ETF + 中观脑):")
    print(f"   测试夏普均值: {sh_mean:.2f}  (门槛 > 0.3)  {'✅' if sh_mean > 0.3 else '❌'}")
    print(f"   测试年化均值: {ann_mean:.1%}  (门槛 > 10%)  {'✅' if ann_mean > 0.10 else '❌'}")
    print(f"   最大回撤:     {dd_max:.1%}  (门槛 < 25%)  {'✅' if dd_max < 0.25 else '❌'}")
    print(f"   窗口数:       {len(results)}")

    for r in results:
        print(f"   窗口{r['window']}: {r['test_dates']}  "
              f"夏普{r['test_sharpe']:.2f} 年化{r['test_ann']:.1%} 回撤{r['test_dd']:.1%}")

    passed = sh_mean > 0.3 and ann_mean > 0.10 and dd_max < 0.25
    return {"passed": passed, "sharpe": sh_mean, "ann": ann_mean, "dd": dd_max,
            "details": results}


def main():
    print("=" * 55)
    print("  归爻 V5 中观脑 WF 验证")
    print("=" * 55)

    if not DATA_FILE.exists():
        print("❌ 数据文件不存在，请先运行 etf_rotation.py")
        return

    print(f"\n[1] 加载数据...")
    df = pl.read_parquet(str(DATA_FILE))
    if df["date"].dtype == pl.Date:
        df = df.with_columns(pl.col("date").cast(pl.Utf8))

    available = set(df["code"].unique().to_list())
    missing = [c for c in ETF_CODES if c not in available]
    if missing:
        print(f"   缺少 {len(missing)} 只: {missing[:5]}... 先下载")
        return

    print(f"   {len(df)} 行, {len(available)}/{len(ETF_CODES)} 只 ETF 在池")

    print(f"\n[2] WF 验证...")
    result = walk_forward_meso(df)

    if result["passed"]:
        print(f"\n🎉 中观脑策略通过WF验证！可以准备银河账户上线。")
    else:
        print(f"\n⚠️ WF 未通过，需调整参数")

    print("=" * 55)


if __name__ == "__main__":
    main()
