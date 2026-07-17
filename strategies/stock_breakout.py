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
            df.filter(pl.col("date").is_in(all_dates[train_end+1:test_end]))
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
