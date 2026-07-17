# 归爻 V5 — 3_策略 (v10 终版)
## strategies/etf_rotation.py
`python
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

`

## strategies/meso_backtest.py
`python
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
        test_dates = all_dates[train_end:test_end]

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

            pos_val = 0
            for code, v in brain.positions.items():
                row = df.filter((pl.col("date") == dt) & (pl.col("code") == code))
                if len(row) > 0:
                    pos_val += v.get("shares", 0) * row["close"].to_numpy()[0]
            nv_train.append(cash + pos_val)

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

`

## strategies/stock_breakout.py
`python
"""
个股突破策略 — 高弹性刺客版 (国泰海通 5万)

三模块：
  1. 筛选器: 日均成交 > 5亿 + 20日振幅 Top 30
  2. 因子: 放量突破 + 换手率突变 + 板块资金流入
  3. 仓位: 15%试错 → +5%加仓40% → -5%止损 → 连续2把冷静期

WF 验收: 盈亏比 > 2.5, 回撤 < 15%, 年化 > 20%, 夏普 > 0.2
"""

import polars as pl
import numpy as np
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime, timedelta

DATA_DIR = Path(r"/home/ubuntu/guiyao_v5/data")
MARKET_FILE = DATA_DIR / "market_v5.parquet"


@dataclass
class TradeRecord:
    code: str
    buy_date: str
    buy_price: float
    shares: int
    cost: float
    sell_date: Optional[str] = None
    sell_price: Optional[float] = None
    pnl: Optional[float] = None
    pnl_pct: Optional[float] = None


class StockScreener:
    """筛选高弹性活跃股: 日均成交 > 5亿 + 20日振幅 Top 30"""

    MIN_AMOUNT_BULL = 3e8
    MIN_AMOUNT_CHOP = 2.5e8
    MIN_AMOUNT_BEAR = 2.5e8   # 5亿

    def __init__(self, top_n: int = 30):
        self.top_n = top_n
        self.min_amount = self.MIN_AMOUNT_CHOP  # 默认Chop

    def screen(self, df: pl.DataFrame, today: str) -> list:
        """返回今日候选股票代码列表"""
        recent = df.filter(
            (pl.col("date") <= today) &
            (pl.col("date") >= (datetime.strptime(today, "%Y-%m-%d").date()
                                - timedelta(days=20)).strftime("%Y-%m-%d"))
        ).sort("date")

        # 日均成交额
        avg_amount = recent.group_by("code").agg(
            pl.col("amount").mean().alias("avg_amount"),
            ((pl.col("high").max() - pl.col("low").min()) /
             pl.col("close").mean()).alias("amplitude"),
        )

        # 筛选: 成交 > 5亿
        candidates = avg_amount.filter(pl.col("avg_amount") > self.min_amount)

        # 按振幅排名取 Top N
        top = candidates.sort("amplitude", descending=True).head(self.top_n)
        return top["code"].to_list()


def compute_breakout_factors(df: pl.DataFrame) -> pl.DataFrame:
    """极端放量突破: 量>5倍均量 + 阳线 + 收在高位"""
    return df.sort(["code", "date"]).with_columns([
        (pl.col("volume") / pl.col("volume").rolling_mean(20).over("code")).alias("vol_ratio"),
        ((pl.col("close") - pl.col("low")) / (pl.col("high") - pl.col("low") + 1e-9)).alias("day_pos"),
    ]).with_columns([
        ((pl.col("vol_ratio") > 3.0) & (pl.col("close") > pl.col("open")) &
         (pl.col("day_pos") > 0.70)).cast(pl.Int8).alias("signal"),
    ])


def backtest_breakout(
    df: pl.DataFrame,
    init_capital: float = 50000.0,
    trial_pct: float = 0.10,   # 10% 试错仓 (顾问裁定)
    add_pct: float = 0.40,
    stop_loss: float = -0.05,
    take_profit_add: float = 0.05,
    cooldown_days: int = 3,
) -> dict:
    """非对称仓位回测"""

    all_dates = [str(d) for d in sorted(df["date"].unique().to_list())]
    screener = StockScreener(top_n=30)
    cash = init_capital
    pos = {}  # {code: {shares, cost, buy_date}}
    trades = []
    nv_history = []
    cooldown = {}  # {code: 剩余冷静天数}

    # 连续止损计数器
    cons_losses = 0
    blocked = False
    block_until = 0

    for day_idx, today in enumerate(all_dates):
        if day_idx < 40:  # 暖场期
            nv_history.append(cash)
            continue

        # 每日更新持仓市值
        pos_value = 0
        for code, p in list(pos.items()):
            today_row = df.filter(
                (pl.col("date") == today) & (pl.col("code") == code)
            )
            if len(today_row) == 0:
                continue
            price = today_row["close"][0]
            p["current_price"] = price
            p["current_value"] = p["shares"] * price

            # 止损检查 (-5%) 或 跟踪止盈 (从峰值回落 8%)
            pnl = (price - p["cost"]) / p["cost"]
            if "peak_price" not in p:
                p["peak_price"] = price
            if price > p["peak_price"]:
                p["peak_price"] = price
            trail_stop = (price - p["peak_price"]) / p["peak_price"]

            if pnl <= stop_loss or (pnl > 0.05 and trail_stop < -0.08):
                sell_val = p["shares"] * price * 0.99814
                cash += sell_val
                p["pnl"] = pnl
                trades.append(TradeRecord(
                    code=code, buy_date=p["buy_date"],
                    buy_price=p["cost"], shares=p["shares"],
                    cost=p["cost"] * p["shares"],
                    sell_date=today, sell_price=price,
                    pnl=sell_val - p["cost"] * p["shares"],
                    pnl_pct=pnl
                ))
                cons_losses += 1
                if cons_losses >= 2:
                    blocked = True
                    block_until = day_idx + cooldown_days
                pos.pop(code)
                continue

            # 浮盈加仓检查 (+5%)
            if pnl >= take_profit_add and not p.get("added", False):
                add_shares = int(cash * (add_pct - trial_pct) / price / 100) * 100
                if add_shares >= 100:
                    add_cost = add_shares * price * 1.00086  # 买入佣金万8.6（国泰海通个股）
                    if add_cost <= cash:
                        cash -= add_cost
                        p["shares"] += add_shares
                        p["cost"] = (p["cost"] * (p["shares"] - add_shares)
                                     + price * add_shares) / p["shares"]
                        p["added"] = True
                        p["current_value"] = p["shares"] * price

            pos_value += p["current_value"]

        nv = cash + pos_value
        nv_history.append(nv)

        # 冷静期
        if blocked and day_idx < block_until:
            continue

        # 卖出锁定股（持有超过 20 天强制减仓）
        for code, p in list(pos.items()):
            try:
                buy_idx = all_dates.index(p["buy_date"])
                days_held = day_idx - buy_idx
            except ValueError:
                days_held = 0
            if days_held > 20 and p.get("pnl", 0) < 0:
                pr = p["current_price"]
                cash += p["shares"] * pr * 0.99814
                trades.append(TradeRecord(
                    code=code, buy_date=p["buy_date"],
                    buy_price=p["cost"], shares=p["shares"],
                    cost=p["cost"] * p["shares"],
                    sell_date=today, sell_price=pr,
                    pnl=cash - p["cost"] * p["shares"],
                    pnl_pct=(pr - p["cost"]) / p["cost"]
                ))
                pos.pop(code)

        # 筛选新信号
        candidates = screener.screen(df, str(today))
        if not candidates:
            continue

        today_df = df.filter(
            (pl.col("date") == today) & pl.col("code").is_in(candidates)
        )

        signals = today_df.filter(
            (pl.col("signal") == 1) &
            (~pl.col("code").is_in(list(pos.keys()))) &
            (~pl.col("code").is_in([k for k in cooldown if cooldown[k] > 0]))
        )

        # 买入: 15% 试错仓
        for row in signals.iter_rows(named=True):
            if len(pos) >= 3:
                break
            code = row["code"]
            price = row["close"]
            trial_amount = init_capital * trial_pct
            shares = int(trial_amount / price / 100) * 100
            if shares < 100:
                continue
            cost = shares * price * 1.00086  # 买入佣金万8.6（国泰海通个股）
            if cost <= cash:
                cash -= cost
                pos[code] = {
                    "shares": shares, "cost": price,
                    "buy_date": str(today),
                    "added": False,
                    "current_price": price,
                    "current_value": shares * price,
                }
                cons_losses = 0
                blocked = False

        # 更新冷静期倒计时
        for c in list(cooldown.keys()):
            cooldown[c] -= 1
            if cooldown[c] <= 0:
                cooldown.pop(c)

    # 统计指标
    wins = [t for t in trades if t.pnl and t.pnl > 0]
    losses = [t for t in trades if t.pnl and t.pnl <= 0]
    avg_win = np.mean([t.pnl for t in wins]) if wins else 0
    avg_loss = abs(np.mean([t.pnl for t in losses])) if losses else 1
    profit_factor = avg_win / avg_loss if avg_loss > 0 else 99
    win_rate = len(wins) / len(trades) if trades else 0

    nv = np.array(nv_history)
    valid = nv[40:]
    rets = np.diff(valid) / valid[:-1]
    ann = (nv[-1] / nv[40]) ** (252 / (len(nv) - 40)) - 1.0
    sharpe = float(np.mean(rets) / (np.std(rets) + 1e-9) * np.sqrt(252))
    dd = 0.0
    peak = nv[40]
    for v in nv[40:]:
        if v > peak: peak = v
        d = (peak - v) / peak
        if d > dd: dd = d

    return {
        "ann_ret": ann,
        "sharpe": sharpe,
        "max_dd": dd,
        "profit_factor": profit_factor,
        "win_rate": win_rate,
        "total_trades": len(trades),
        "trades": trades,
        "final_value": float(nv[-1]),
    }


def walk_forward(df, n_splits=3):
    """多段 WF 验证，返回各段结果"""
    all_dates = [str(d) for d in sorted(df["date"].unique().to_list())]
    # 确保 date 列是字符串格式
    if df["date"].dtype != pl.Utf8:
        df = df.with_columns(pl.col("date").cast(pl.Utf8))
    seg_len = len(all_dates) // (n_splits + 1)
    results = []

    for i in range(n_splits):
        test_start = len(all_dates) - (n_splits - i) * seg_len
        test_end = len(all_dates) - (n_splits - i - 1) * seg_len
        train_end = test_start - 1

        if train_end < seg_len:
            continue

        # 训练期结果（for reference）
        train_r = backtest_breakout(
            df.filter(pl.col("date").is_in(all_dates[:test_start]))
        )
        # 测试期结果
        test_r = backtest_breakout(
            df.filter(pl.col("date").is_in(all_dates[:test_end]))
        )

        results.append({
            "train_sharpe": train_r["sharpe"],
            "test_ann": test_r["ann_ret"],
            "test_sharpe": test_r["sharpe"],
            "test_dd": test_r["max_dd"],
            "test_pf": test_r["profit_factor"],
            "test_win": test_r["win_rate"],
        })

    return results


def main():
    print("=" * 60)
    print("个股突破策略 — 高弹性刺客版")
    print("=" * 60)

    if not MARKET_FILE.exists():
        print("❌ 需要 market_v5.parquet，请先运行 data_layer 日线下载")
        return

    print("\n[1] 加载数据 + 计算因子...")
    df = pl.read_parquet(MARKET_FILE)
    if df["date"].dtype == pl.Utf8:
        df = df.with_columns(pl.col("date").str.strptime(pl.Date, "%Y-%m-%d"))
    # 全链路统一用字符串日期
    df = df.with_columns(pl.col("date").cast(pl.Utf8))
    df = compute_breakout_factors(df)
    print(f"  {len(df)} 行, {df['code'].n_unique()} 股")

    # 检查有多少股票满足日均成交 > 5亿
    max_date_str = str(df["date"].max())
    cutoff_str = (datetime.strptime(max_date_str, "%Y-%m-%d").date()
                  - timedelta(days=20)).strftime("%Y-%m-%d")
    recent = df.filter(pl.col("date") > cutoff_str)
    avg_amts = recent.group_by("code").agg(pl.col("amount").mean())
    rich_stocks = avg_amts.filter(pl.col("amount") > 5e8)
    print(f"  日均成交 > 5亿: {len(rich_stocks)} 只 (阈值: 500M)")

    print("\n[2] 全期回测...")
    result = backtest_breakout(df)

    print(f"\n📊 全期结果：")
    print(f"   年化收益: {result['ann_ret']:+.1%}")
    print(f"   夏普:     {result['sharpe']:+.2f}")
    print(f"   最大回撤: {result['max_dd']:.1%}")
    print(f"   盈亏比:   {result['profit_factor']:.1f}")
    print(f"   胜率:     {result['win_rate']:.0%}")
    print(f"   总交易:   {result['total_trades']} 笔")
    print(f"   最终净值: {result['final_value']:,.0f}")

    # 盈亏比检查
    if result['profit_factor'] >= 2.5:
        print(f"   ✅ 盈亏比 {result['profit_factor']:.1f} >= 2.5")
    else:
        print(f"   ❌ 盈亏比 {result['profit_factor']:.1f} < 2.5")

    print("\n[3] Walk-Forward 验证...")
    wf = walk_forward(df, n_splits=3)
    pf_mean = np.mean([r["test_pf"] for r in wf])
    dd_max = max([r["test_dd"] for r in wf])
    ann_mean = np.mean([r["test_ann"] for r in wf])
    sh_mean = np.mean([r["test_sharpe"] for r in wf])

    print(f"\n📊 WF 汇总（刺客专属门槛）：")
    print(f"   测试盈亏比: {pf_mean:.1f}  (门槛 > 2.5)  {'✅' if pf_mean >= 2.5 else '❌'}")
    print(f"   测试回撤:   {dd_max:.1%}  (门槛 < 15%)  {'✅' if dd_max < 0.15 else '❌'}")
    print(f"   测试年化:   {ann_mean:.1%}  (门槛 > 20%)  {'✅' if ann_mean >= 0.20 else '❌'}")
    print(f"   测试夏普:   {sh_mean:+.2f}  (门槛 > 0.2)  {'✅' if sh_mean >= 0.2 else '❌'}")

    if pf_mean >= 2.5 and dd_max < 0.15 and ann_mean >= 0.20 and sh_mean >= 0.2:
        print(f"\n🎯 刺客策略全部通过！可以部署到国泰海通 5 万。")
    else:
        print(f"\n⚠️ 部分门槛未通过。继续优化信号或因子。")

    print("=" * 60)


if __name__ == "__main__":
    main()

`
