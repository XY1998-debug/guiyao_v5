"""
ETF 轮动策略 — 下载 + 回测 + WF 验证
15只核心 ETF，趋势因子（20/60均线），周频调仓
"""

import polars as pl
import numpy as np
from pathlib import Path
from datetime import datetime

# ETF 池：宽基 + 行业 + 跨境 + 债券
ETF_CODES = [
    "510050",  # 上证50       — 大蓝筹
    "510300",  # 沪深300      — 全市场代表
    "510500",  # 中证500      — 中盘
    "512100",  # 中证1000     — 小盘
    "588000",  # 科创50       — 科创板
    "159915",  # 创业板       — 成长
    # 行业
    "512880",  # 证券ETF      — 券商
    "512690",  # 酒ETF        — 白酒
    "512480",  # 半导体ETF    — 科技核心
    "512660",  # 军工ETF      — 国防
    "515030",  # 新能源车ETF  — 新能源
    "512800",  # 银行ETF      — 高股息
    "159825",  # 农业ETF      — 农业
    "510880",  # 红利ETF      — 高分红
    "515220",  # 煤炭ETF      — 资源周期
    "512170",  # 医疗ETF      — 医药
    # 跨境
    "159941",  # 纳指ETF      — 美股科技
    "513100",  # 纳指100      — 美股科技
    "513330",  # 恒生互联网    — 港股科技
    "159920",  # 恒生ETF      — 港股
    # 商品/债券
    "518880",  # 黄金ETF      — 黄金
    "511260",  # 10年国债ETF  — 避险
]


def download_etf_data(data_dir: Path = None) -> pl.DataFrame:
    """mootdx 下载 15 只 ETF 日线"""
    if data_dir is None:
        data_dir = Path(r"/home/ubuntu/guiyao_v5/data")
    data_dir.mkdir(exist_ok=True)

    from mootdx.quotes import Quotes
    client = Quotes.factory(market="std")
    all_rows = []

    for code in ETF_CODES:
        df = client.bars(symbol=code, frequency=9, start=0, count=800)
        if df is None or len(df) < 100:
            print(f"  {code}: 下载失败")
            continue
        dates = list(df["datetime"].str.slice(0, 10))
        close = df["close"].astype(float)
        rd = pl.DataFrame({
            "code": [code] * len(df),
            "date": dates,
            "close": list(close.values),
            "volume": list(df["volume"].astype(np.int64).values),
            "open": list(df["open"].astype(float).values),
            "high": list(df["high"].astype(float).values),
            "low": list(df["low"].astype(float).values),
        })
        all_rows.append(rd)
        print(f"  {code}: {len(df)} 日, {dates[0]} ~ {dates[-1]}")

    merged = pl.concat(all_rows).sort(["code", "date"])
    out = data_dir / "etf_v5.parquet"
    merged.write_parquet(out, compression="zstd")
    print(f"\n✅ 保存 {out}: {len(merged)} 行, {merged['code'].n_unique()} 只 ETF")
    return merged


def compute_etf_factors(df: pl.DataFrame) -> pl.DataFrame:
    """ETF 专用因子：20/60均线趋势 + 波动率 + 动量"""
    return df.sort(["code", "date"]).with_columns([
        # 趋势：close > MA20
        (pl.col("close") > pl.col("close").rolling_mean(20).over("code"))
        .cast(pl.Int8).alias("trend_20"),
        # 趋势：close > MA60
        (pl.col("close") > pl.col("close").rolling_mean(60).over("code"))
        .cast(pl.Int8).alias("trend_60"),
        # 20日动量
        (pl.col("close") / pl.col("close").shift(20).over("code") - 1.0).alias("mom_20d"),
        # 波动率 (低波加分)
        (-pl.col("close").pct_change().over("code")
         .rolling_std(20).over("code")).alias("low_vol"),
        # 成交量趋势
        (pl.col("volume") > pl.col("volume").rolling_mean(20).over("code"))
        .cast(pl.Int8).alias("vol_up"),
    ])


def backtest_etf_trend(
    df: pl.DataFrame,
    lookback: int = 60,        # 均线计算期
    rebalance_freq: int = 5,   # 周频（5个交易日）
) -> tuple:
    """ETF 趋势轮动回测：每周选趋势最强的 1-2 只 ETF"""
    df = compute_etf_factors(df)
    dates = sorted(df["date"].unique().to_list())
    codes = sorted(df["code"].unique().to_list())
    n_etf = len(codes)

    cash = 50000.0
    pos_code = None
    pos_shares = 0
    pos_cost = 0.0
    nv_history = []

    for i, today in enumerate(dates):
        if i < lookback:
            nv_history.append(cash)
            continue

        # 周频检查（每周五调仓）
        day_of_week = i % rebalance_freq
        if day_of_week != 0 and pos_code is not None:
            # 非调仓日：按今日收盘更新净值
            today_df = df.filter((pl.col("date") == today) & (pl.col("code") == pos_code))
            if len(today_df) > 0:
                current_price = today_df["close"][0]
                current_value = pos_shares * current_price
                nv_history.append(cash + current_value)
            else:
                nv_history.append(nv_history[-1])
            continue

        # 调仓日
        today_df = df.filter(pl.col("date") == today).sort("trend_60", descending=True)

        # 评分：趋势60 + 趋势20 + 低波动 + 放量
        today_df = today_df.with_columns(
            (pl.col("trend_60") * 3 + pl.col("trend_20") * 2 +
             pl.col("low_vol") * 1 + pl.col("vol_up") * 1).alias("score")
        )

        top = today_df.sort("score", descending=True).head(1)

        if len(top) == 0:
            nv_history.append(nv_history[-1])
            continue

        new_code = top["code"][0]
        new_price = top["close"][0]
        in_trend = (top["trend_60"][0] == 1) and (top["trend_20"][0] == 1)

        # 卖出旧持仓
        if pos_code and pos_code != new_code:
            old_df = today_df.filter(pl.col("code") == pos_code)
            if len(old_df) > 0:
                sell_price = old_df["close"][0]
                cash += pos_shares * sell_price * 0.99814  # 佣金万8.6+印花税千1  # 万三佣金
            pos_code = None
            pos_shares = 0

        # 买入：仅在趋势向上时
        if in_trend and (pos_code != new_code):
            shares = int(cash * 0.95 / new_price / 100) * 100  # 整数手
            if shares >= 100:
                cost = shares * new_price * 1.0005  # 含买入佣金
                if cost <= cash:
                    cash -= cost
                    pos_code = new_code
                    pos_shares = shares
                    pos_cost = new_price
                else:
                    pos_code = new_code if pos_code is None else pos_code
            else:
                pos_code = new_code if pos_code is None else pos_code

        # 净值：现金 + 持仓市值
        current_value = pos_shares * new_price if pos_code else 0
        nv_history.append(cash + current_value)

    # 指标
    nv = np.array(nv_history)
    rets = np.diff(nv[lookback:]) / nv[lookback:-1]
    ann_ret = (nv[-1] / nv[lookback]) ** (252 / (len(nv) - lookback)) - 1.0 if len(nv) > lookback else 0
    sharpe = float(np.mean(rets) / (np.std(rets) + 1e-9) * np.sqrt(252))
    dd = 0.0
    peak = nv[lookback]
    for v in nv[lookback:]:
        if v > peak: peak = v
        d = (peak - v) / peak
        if d > dd: dd = d

    # Walk-Forward 简化版
    halfway = len(dates) // 2
    train_nv = backtest_subset(df, dates[:halfway], lookback, rebalance_freq)
    test_nv = backtest_subset(df, dates[halfway:], lookback, rebalance_freq)
    train_sharpe = calc_sharpe(train_nv)
    test_sharpe = calc_sharpe(test_nv)
    test_ann = (test_nv[-1] / test_nv[lookback]) ** (252 / (len(test_nv) - lookback)) - 1.0

    return {
        "ann_ret": ann_ret,
        "sharpe": sharpe,
        "max_dd": dd,
        "final_value": float(nv[-1]),
        "train_sharpe": train_sharpe,
        "test_sharpe": test_sharpe,
        "test_ann": test_ann,
        "wf_qualified": test_sharpe > 0.5 and test_ann > 0.10,
    }


def backtest_subset(df, dates_subset, lookback, freq):
    """子集回测（用于 WF），返回值序列"""
    dummy = df.with_columns(pl.lit(1).cast(pl.Float64).alias("low_vol"))
    nv = []
    cash = 50000.0
    pos = (None, 0, 0.0)
    for i, today in enumerate(dates_subset):
        if i < lookback:
            nv.append(cash)
            continue
        if i % freq != 0 and pos[0]:
            day_df = df.filter((pl.col("date") == today) & (pl.col("code") == pos[0]))
            nv.append(cash + (pos[1] * day_df["close"][0] if len(day_df) > 0 else 0))
            continue
        day_df = df.filter(pl.col("date") == today)
        if len(day_df) == 0:
            nv.append(nv[-1])
            continue
        top = day_df.filter(pl.col("trend_60") == 1).sort("mom_20d", descending=True).head(1)
        if len(top) == 0:
            nv.append(nv[-1])
            continue
        code = top["code"][0]
        price = top["close"][0]
        if pos[0] and pos[0] != code:
            odf = day_df.filter(pl.col("code") == pos[0])
            cp = odf["close"][0] if len(odf) > 0 else 0
            cash += pos[1] * cp * 0.99814  # 佣金万8.6+印花税千1
            pos = (None, 0, 0.0)
        sh = int(cash * 0.95 / price / 100) * 100
        cost = sh * price * 1.0005
        if sh >= 100 and cost <= cash and pos[0] != code:
            cash -= cost
            pos = (code, sh, price)
        val = pos[1] * price if pos[0] else 0
        nv.append(cash + val)
    return np.array(nv)


def calc_sharpe(nv):
    rets = np.diff(nv[60:]) / nv[60:-1]
    return float(np.mean(rets) / (np.std(rets) + 1e-9) * np.sqrt(252))


def main():
    print("=" * 50)
    print("ETF 趋势轮动 回测 + WF 验证")
    print("=" * 50)

    etf_file = Path(r"/home/ubuntu/guiyao_v5/data\etf_v5.parquet")
    if not etf_file.exists():
        print("\n[1] 下载 ETF 数据...")
        df = download_etf_data()
    else:
        print("\n[1] 加载已缓存数据...")
        df = pl.read_parquet(etf_file)
        if df["date"].dtype == pl.Utf8:
            df = df.with_columns(pl.col("date").str.strptime(pl.Date, "%Y-%m-%d"))
        print(f"  {len(df)} 行, {df['code'].n_unique()} 只 ETF")

    print(f"\n[2] 回测...")
    df = compute_etf_factors(df)
    result = backtest_etf_trend(df)

    print(f"\n📊 全期结果：")
    print(f"   年化收益: {result['ann_ret']:.1%}")
    print(f"   夏普:     {result['sharpe']:+.2f}")
    print(f"   最大回撤: {result['max_dd']:.1%}")
    print(f"   最终净值: {result['final_value']:,.0f}")

    print(f"\n📊 Walk-Forward：")
    print(f"   训练夏普: {result['train_sharpe']:+.2f}")
    print(f"   测试夏普: {result['test_sharpe']:+.2f}")
    print(f"   测试年化: {result['test_ann']:+.1%}")
    print(f"   通过:     {'✅' if result['wf_qualified'] else '❌'}")

    if result["wf_qualified"]:
        print("\n🎉 ETF 策略通过验证！可以准备银河账户上线。")
    else:
        print("\n⚠️ ETF 策略未通过验证（门槛: 测试夏普>0.5, 年化>10%）")

    print("=" * 50)


if __name__ == "__main__":
    main()
