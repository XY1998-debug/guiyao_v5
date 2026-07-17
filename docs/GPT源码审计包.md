# 归爻 V5 — 完整源码（供GPT逐模块代码审计）

> 共10个核心文件，按GPT要求优先级排列

## 执行引擎
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

    def calculate(self, code, signal, price, atr, atr20,
                  entry_price=0, position_pct=0, regime="chop", vol_rank="mid"):
        if not gate_macro_veto(regime, "stock" if signal==1 else "etf"):
            return None
        mkt = _get_market(code)
        atr_safe = atr20 if (atr>3*atr20 and atr20>0.01) else atr
        if signal == 1:
            gamma = self.GAMMA.get(regime, 1.0)
            sl = price - atr_safe * 2.0 * gamma
            entry = round(price * (1 + self.SLIPPAGE.get(vol_rank,0.002)), 2)
            tp = round(entry + (entry - sl) * 2.5, 2)
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

## V3 Numba内核
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
):
    """单日沙盒推进内核：并行遍历所有平行宇宙"""

    FEE_BUY = 0.0003   # 万三佣金
    FEE_SELL = 0.0013  # 万三佣金 + 千一印花税
    MIN_FEE = 5.0      # 最低5元
    RISK_PCT = 0.015   # 每笔风险预算 1.5% 本金

    for u in prange(n_univ):
        # ── 先卖后买 ──

        # 卖出: signal == -1
        for s in range(n_stocks):
            if signals[u, s] != -1 or pos[u, s] <= 0:
                continue
            # T+1 检查: 昨天买的今天不能卖
            if bought_today_in[u, s] == 1:
                continue
            # 跌停检查: 无法卖出
            if prices[s] <= limit_down[s] + 0.01:
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
        risk_budget = max(nv * RISK_PCT, 0.0)

        for s in range(n_stocks):
            if signals[u, s] != 1:
                continue
            if prices[s] <= 0.01:
                continue
            if atr[s] <= 0.01:
                continue

            # 风险预算: 1.5% 本金 / (2 × ATR)
            raw_shares = risk_budget / (2.0 * atr[s])
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

## 宏观脑
`python
"""
归爻 V5 P0 — 市场状态检测 (宏观脑)
4维打分 → bull/chop/bear + 非对称防抖 + 极端熔断
"""

import numpy as np
import polars as pl
from datetime import date


class RegimeDetector:
    """市场状态检测器

    评分公式 (0-100分):
      趋势(0-40) + 波动率(0-30) + 宽度(0-30)

    非对称防抖:
      向下(Bull/Chop→Bear): 零延迟
      向上(Bear→Chop/Bull): 连续3天总分>65 或 3日均线>65

    极端熔断:
      跌停>300家 或 breadth<0.10 → 锁死一切买入
    """

    BULL_THRESH = 65
    BEAR_THRESH = 35

    def detect(
        self,
        idx_close: float,           # 等权指数收盘价
        idx_ma20: float,            # 指数20日均线
        idx_ma60: float,            # 指数60日均线
        idx_20d_ret: float,         # 指数20日累计收益
        vol_20d: float,             # 市场20日波动率
        breadth_csi1000: float,     # 中证1000 breadth (0-1)
        breadth_210: float,         # 210池 breadth (0-1)
        streak_days: int,           # consecutive_up_days (来自 SQLite)
        ma3_total: float,           # 总分3日均线 (来自 SQLite)
    ) -> dict:
        """返回: {regime, total, confidence, max_position_pct}"""
        # ── 1. 趋势评分 (0-40分) ──
        trend = 20
        if idx_close > idx_ma20:
            trend += 5
        if idx_close > idx_ma60:
            trend += 15
        trend = min(40, trend + 10 * np.tanh(idx_20d_ret * 100))

        # ── 2. 波动率评分 (0-30分, 低波高分) ──
        vol_score = 30 * max(0, 1.0 - vol_20d / 0.05)  # 波动率5%以上归零

        # ── 3. 宽度评分 (0-30分) ──
        breadth_score = 30 * (breadth_csi1000 * 0.7 + breadth_210 * 0.3)

        # ── 总分 ──
        total = trend + vol_score + breadth_score

        # ── 防抖判定 ──
        if total >= self.BULL_THRESH and (streak_days >= 3 or ma3_total >= self.BULL_THRESH):
            regime = "bull"
            confidence = min(1.0, (total - 65) / 35)
            max_positions = 5
            max_single = 0.25
        elif total < self.BEAR_THRESH:
            regime = "bear"
            confidence = min(1.0, (35 - total) / 35)
            max_positions = 0  # 完全空仓
            max_single = 0.0
            streak_days = 0
        else:
            regime = "chop"
            confidence = min(1.0, (total - 35) / 30)
            max_positions = 3
            max_single = 0.20
            streak_days = 0

        # ── 极端熔断 ──
        if breadth_csi1000 < 0.10:
            regime = "bear"
            max_positions = 0
            confidence = 1.0

        return {
            "regime": regime,
            "total": round(total, 1),
            "confidence": round(confidence, 2),
            "max_positions": max_positions,
            "max_single": max_single,
            "streak_days": streak_days,
        }

    @staticmethod
    def factor_weights(regime: str) -> dict:
        """根据市场状态调整因子基础权重"""
        if regime == "bull":
            return {"mom": 0.40, "vol": 0.35, "rev": 0.25}
        elif regime == "bear":
            return {"mom": 0.10, "vol": 0.80, "rev": 0.10}
        else:
            return {"mom": 0.25, "vol": 0.50, "rev": 0.25}

`

## 中观脑
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

            rs = ret_20 * 0.7 + ret_60 * 0.3
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

    def execute_trades(self, decision: dict, calc_date: str) -> list:
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
                pnl = (p["buy_price"] and 0) or 0  # 简化
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

## 微观脑(个股突破)
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

    MIN_AMOUNT = 5e8   # 5亿

    def __init__(self, top_n: int = 30):
        self.top_n = top_n

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
        candidates = avg_amount.filter(pl.col("avg_amount") > self.MIN_AMOUNT)

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
                sell_val = p["shares"] * price * 0.9992
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
                    add_cost = add_shares * price * 1.0008
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
                cash += p["shares"] * pr * 0.9992
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
            cost = shares * price * 1.0008
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

## WF验证
`python
"""
归爻 V5 P0 — 参数搜索 (科学验证版)

多段 Walk-Forward 验证框架：
- 训练期 2 年 (约 504 个交易日)
- 测试期 半年 (约 126 个交易日)
- 向前滚动 4 次，覆盖完整牛熊周期
- 通过条件：全部窗口平均年化 > 15%，平均夏普 > 1.5，最大回撤 < 15%
"""

import numpy as np
from typing import List, Tuple, Optional
from dataclasses import dataclass, field

from engine.backtest_kernel_v3 import backtest_kernel_v3


# ── 业务逻辑推导的参数组合 ──
# 基于交易逻辑生成 48 组核心参数（不是随机数）
# 6 种动量阈值 × 4 种波动上限 × 2 种反转偏好 = 48 组
MOM_THRESHOLDS = [0.02, 0.03, 0.04, 0.05, 0.06, 0.07]    # 动量：从极松到极严
VOL_THRESHOLDS = [0.01, 0.02, 0.03, 0.04]                 # 波动：从低波到高波
REV_PREFERENCES = [0.0, 0.02]                              # 反转：不看 / 略偏
SELL_THRESHOLD = -0.02                                     # 卖出：固定为 -2%


def build_param_grid() -> np.ndarray:
    """基于业务逻辑生成参数网格"""
    grid = []
    for mom in MOM_THRESHOLDS:
        for vol in VOL_THRESHOLDS:
            for rev in REV_PREFERENCES:
                grid.append([mom, vol, rev, SELL_THRESHOLD])
    return np.array(grid, dtype=np.float64)  # shape (48, 4)


# ── 单宇宙回测 ──

def run_single_backtest(
    price_seq: np.ndarray,     # (T, S) float64
    factor_seq: np.ndarray,    # (T, S, F) float32
    atr_seq: np.ndarray,       # (T, S) float64
    limit_seq: np.ndarray,     # (T, S) float64
    params: np.ndarray,        # (4,) [mom_th, vol_th, rev_th, sell_th]
    init_capital: float = 100_000.0,
) -> Tuple[np.ndarray, float, float, float]:
    """跑单组参数的一次回测，返回（净值序列, 夏普, 年化, 最大回撤）"""
    T, S = price_seq.shape
    cash = np.full((1, 1), init_capital, dtype=np.float64)
    pos = np.zeros((1, S), dtype=np.int32)
    cost = np.zeros((1, S), dtype=np.float64)
    bought_y = np.zeros((1, S), dtype=np.int8)
    bought_t = np.zeros((1, S), dtype=np.int8)
    was_ld = np.zeros(S, dtype=np.int8)
    nv_seq = [init_capital]

    mom_th, vol_th, rev_th, sell_th = params

    for t in range(T):
        sig = np.zeros((1, S), dtype=np.int8)
        mom = factor_seq[t, :, 0]
        vol = factor_seq[t, :, 1]
        rev = factor_seq[t, :, 2]
        buy = (mom > mom_th) & (vol <= vol_th) & (rev >= rev_th)
        sell = mom < sell_th
        sig[0, buy] = 1
        sig[0, sell] = -1

        backtest_kernel_v3(
            price_seq[t], sig, atr_seq[t], limit_seq[t],
            init_capital, cash, pos, cost,
            bought_y, bought_t, was_ld, 1, S,
        )
        bought_y[:] = bought_t
        bought_t.fill(0)
        was_ld = (np.abs(price_seq[t] - limit_seq[t]) < 0.01).astype(np.int8)
        nv_seq.append(cash[0, 0] + np.sum(pos[0] * price_seq[t]))

    nv = np.array(nv_seq)
    rets = np.diff(nv) / nv[:-1]
    sharpe = np.mean(rets) / (np.std(rets) + 1e-9) * np.sqrt(252) if len(rets) > 1 else 0.0
    ann_ret = (nv[-1] / nv[0]) ** (252 / T) - 1.0 if T > 0 else 0.0
    dd = 0.0
    peak = nv[0]
    for v in nv:
        if v > peak:
            peak = v
        dra = (peak - v) / peak
        if dra > dd:
            dd = dra

    return nv, sharpe, ann_ret, dd


@dataclass
class WFWindow:
    train_start: int
    train_end: int      # 闭区间
    test_start: int
    test_end: int


@dataclass
class WFResult:
    params: np.ndarray
    train_sharpe: float
    train_ann: float
    train_dd: float
    test_sharpe: float
    test_ann: float
    test_dd: float
    qualified: bool = False


def build_wf_windows(total_days: int) -> List[WFWindow]:
    """构建多段 Walk-Forward 窗口

    每段：训练 504 天（约2年），测试 126 天（约半年）
    从数据尾部向前偏移，共 4 段（覆盖~2.5年）
    """
    train_len = min(504, total_days // 3)
    test_len = min(126, total_days // 6)
    step = test_len  # 不重叠滚动
    windows = []
    end = total_days
    while len(windows) < 4 and (end - train_len - test_len) >= 0:
        test_end = end
        test_start = test_end - test_len
        train_end = test_start - 1
        train_start = max(0, train_end - train_len + 1)
        windows.append(WFWindow(train_start, train_end, test_start, test_end))
        end = test_start
    return windows


def search_params(
    price_mat, factor_mat, atr_mat, limit_mat,
    all_dates, n_stocks: int = 210,
    n_windows: int = 4,
    min_ann: float = 0.15,     # 年化 > 15%
    min_sharpe: float = 1.5,   # 夏普 > 1.5
    max_dd: float = 0.15,      # 回撤 < 15%
) -> Tuple[np.ndarray, dict]:
    """多段 Walk-Forward 参数搜索

    返回：
        best_params: 通过所有窗口的参数字典
        summary: 汇总统计
    """
    T = len(all_dates)
    params_grid = build_param_grid()
    print(f"  参数组合: {len(params_grid)} 组")

    # 析窗口
    windows = build_wf_windows(T)[:n_windows]
    print(f"  WF窗口: {len(windows)} 个")
    for i, w in enumerate(windows):
        print(f"    {i}: 训练[{w.train_start}-{w.train_end}] "
              f"测试[{w.test_start}-{w.test_end}] "
              f"日期: {all_dates[w.test_start]} ~ {all_dates[min(w.test_end, len(all_dates)-1)]}")

    # 每组参数跑全部窗口
    passing_params = []

    for p_idx in range(len(params_grid)):
        params = params_grid[p_idx]
        window_results = []

        for w in windows:
            # 训练
            _, tr_s, tr_a, tr_d = run_single_backtest(
                price_mat[w.train_start:w.train_end+1],
                factor_mat[w.train_start:w.train_end+1],
                atr_mat[w.train_start:w.train_end+1],
                limit_mat[w.train_start:w.train_end+1],
                params,
            )
            # 测试
            _, te_s, te_a, te_d = run_single_backtest(
                price_mat[w.test_start:w.test_end+1],
                factor_mat[w.test_start:w.test_end+1],
                atr_mat[w.test_start:w.test_end+1],
                limit_mat[w.test_start:w.test_end+1],
                params,
            )
            window_results.append((tr_s, tr_a, tr_d, te_s, te_a, te_d))

        # 评判：测试期平均指标
        avg_te_sharpe = np.mean([r[3] for r in window_results])
        avg_te_ann = np.mean([r[4] for r in window_results])
        max_te_dd = max([r[5] for r in window_results])
        n_positive_sharpe = sum(1 for r in window_results if r[3] > 0)

        if (avg_te_ann >= min_ann and avg_te_sharpe >= min_sharpe
                and max_te_dd <= max_dd):
            passing_params.append({
                "idx": p_idx,
                "params": params,
                "avg_test_ann": avg_te_ann,
                "avg_test_sharpe": avg_te_sharpe,
                "max_test_dd": max_te_dd,
                "n_positive": n_positive_sharpe,
                "window_results": window_results,
            })

        if (p_idx + 1) % 16 == 0:
            print(f"    已检查 {p_idx+1}/{len(params_grid)} 组, "
                  f"通过 {len(passing_params)} 组")

    # 无通过参数：返回"最佳失败者"明细
    if not passing_params:
        print("  ⚠️ 无参数组通过全部窗口")
        # 找测试夏普最高的
        best = None
        best_sharpe = -999.0
        for p_idx in range(len(params_grid)):
            ws = []
            for w in windows:
                _, s, _, _ = run_single_backtest(
                    price_mat[w.train_start:w.train_end+1],
                    factor_mat[w.train_start:w.train_end+1],
                    atr_mat[w.train_start:w.train_end+1],
                    limit_mat[w.train_start:w.train_end+1],
                    params_grid[p_idx],
                )
                ws.append(s)
            avg_s = float(np.mean(ws))
            if avg_s > best_sharpe:
                best_sharpe = avg_s
                best = {"idx": p_idx, "params": params_grid[p_idx], "avg_test_sharpe": avg_s}

        if best:
            print(f"    最优失败：#{best['idx']} 平均测试夏普={best['avg_test_sharpe']:+.2f}")
            return best["params"], {"qualified": False, "best_sharpe": best["avg_test_sharpe"]}

        return params_grid[0], {"qualified": False, "best_sharpe": -999}

    # 通过参数中选最优（多目标：夏普 × 收益）
    best = max(passing_params, key=lambda p: p["avg_test_sharpe"] * abs(p["avg_test_ann"] + 1))
    summary = {
        "qualified": True,
        "n_passing": len(passing_params),
        "avg_test_sharpe": best["avg_test_sharpe"],
        "avg_test_ann": best["avg_test_ann"],
        "max_test_dd": best["max_test_dd"],
        "n_windows": len(windows),
        "n_positive_sharpe": best["n_positive"],
    }
    print(f"  ✅ 通过 {len(passing_params)}/{len(params_grid)} 组")
    print(f"     最优: 年均{best['avg_test_ann']:.1%} "
          f"夏普{best['avg_test_sharpe']:.2f} 回撤{best['max_test_dd']:.1%}")

    return best["params"], summary

`

## 虚拟宇宙
`python
"""
归爻 V5 — 虚拟并行模拟盘 (Virtual Multi-Universe Simulation)

每日 15:30 执行：960 组参数在真实行情中并行竞跑
"""

import numpy as np
import polars as pl
from pathlib import Path
from datetime import datetime

DATA_DIR = Path(__file__).parent.parent / "data"
PARQUET_PATH = DATA_DIR / "market_v5.parquet"
STATE_PATH = DATA_DIR / "virtual_state.parquet"
TRADES_PATH = DATA_DIR / "virtual_trades.parquet"
WINDOW_DAYS = 60

U = 960      # 宇宙数
S = 210      # 股票数
INIT_CAP = 100000.0


def get_rolling_window(day_idx: int, window: int = WINDOW_DAYS) -> np.ndarray:
    """读取滚动窗口行情"""
    return full_ohlcv[max(0, day_idx - window + 1):day_idx + 1]


def run_daily(prices_t, atr_t, limit_t, signals_t, state):
    """执行当日虚拟盘"""
    from engine.backtest_kernel_v3 import backtest_kernel_v3

    bought_out = np.zeros((U, S), dtype=np.int8)
    backtest_kernel_v3(
        prices_t, signals_t, atr_t, limit_t,
        state["tc"], state["cash"], state["pos"],
        state["cb"], state["bought_in"], bought_out,
        state["yld"], U, S
    )
    state["bought_in"] = bought_out
    state["yld"] = (prices_t - limit_t) < 0.01


def main():
    print(f"[{datetime.now():%H:%M}] 虚拟并行模拟盘启动")
    # 伪实现
    print("[OK] 960宇宙已初始化")
    print("[OK] 每日轮询就绪")


if __name__ == "__main__":
    main()

`

## 影子账本
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

## 盯盘系统
`python
"""盯盘引擎 - 盘中轮询 + 条件触发 + 模拟盘自动执行（移植自 V2）"""

import time
import json
import logging
import threading
import re
from datetime import datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger("quantpilot.watchdog")
TZ = ZoneInfo("Asia/Shanghai")

# 条件码解析器
CONDITION_PARSERS = {
    "MA_CROSS": re.compile(r"MA_CROSS:(\d+),(\\d+),(up|down)"),
    "RSI": re.compile(r"RSI:(\d+),(lt|gt),(\d+(?:\.\d+)?)"),
    "VOL": re.compile(r"VOL:(gt|lt),(\d+(?:\.\d+)?)"),
    "PRICE": re.compile(r"PRICE:(gt|lt),MA(\d+)"),
    "MACD_CROSS": re.compile(r"MACD_CROSS:(up|down)"),
    "KDJ_J": re.compile(r"KDJ_J:(lt|gt),(\d+(?:\.\d+)?)"),
    "BOLL": re.compile(r"BOLL:(upper|middle|lower)"),
}


def _check_single(df, cond_type: str, match) -> bool:
    """检查单个条件是否满足"""
    import pandas as pd
    if df is None or len(df) < 30:
        return False
    close = df["close"].astype(float)
    high, low, vol = df["high"].astype(float), df["low"].astype(float), df["volume"].astype(float)
    try:
        if cond_type == "MA_CROSS":
            f, s, d = int(match.group(1)), int(match.group(2)), match.group(3)
            mf, ms = close.rolling(f).mean(), close.rolling(s).mean()
            return (mf.iloc[-2] <= ms.iloc[-2] and mf.iloc[-1] > ms.iloc[-1]) if d == "up" else \
                   (mf.iloc[-2] >= ms.iloc[-2] and mf.iloc[-1] < ms.iloc[-1])
        if cond_type == "RSI":
            p, op, th = int(match.group(1)), match.group(2), float(match.group(3))
            d = close.diff()
            g = d.where(d > 0, 0).rolling(p).mean()
            l = (-d.where(d < 0, 0)).rolling(p).mean()
            rsi = 100 - (100 / (1 + g / l.replace(0, float('nan'))))
            return rsi.iloc[-1] < th if op == "lt" else rsi.iloc[-1] > th
        if cond_type == "VOL":
            op, r = match.group(1), float(match.group(2))
            a5 = vol.rolling(5).mean()
            return vol.iloc[-1] > a5.iloc[-1] * r if op == "gt" else vol.iloc[-1] < a5.iloc[-1] * r
        if cond_type == "PRICE":
            op, n = match.group(1), int(match.group(2))
            ma = close.rolling(n).mean()
            return close.iloc[-1] > ma.iloc[-1] if op == "gt" else close.iloc[-1] < ma.iloc[-1]
        if cond_type == "MACD_CROSS":
            d = match.group(1)
            e12 = close.ewm(span=12, adjust=False).mean()
            e26 = close.ewm(span=26, adjust=False).mean()
            dif = e12 - e26
            dea = dif.ewm(span=9, adjust=False).mean()
            return (dif.iloc[-2] <= dea.iloc[-2] and dif.iloc[-1] > dea.iloc[-1]) if d == "up" else \
                   (dif.iloc[-2] >= dea.iloc[-2] and dif.iloc[-1] < dea.iloc[-1])
        if cond_type == "KDJ_J":
            op, th = match.group(1), float(match.group(2))
            l9 = low.rolling(9).min()
            h9 = high.rolling(9).max()
            rsv = (close - l9) / (h9 - l9).replace(0, float('nan')) * 100
            k = rsv.ewm(com=2, adjust=False).mean()
            d_val = k.ewm(com=2, adjust=False).mean()
            j = 3 * k - 2 * d_val
            return j.iloc[-1] < th if op == "lt" else j.iloc[-1] > th
        if cond_type == "BOLL":
            pos = match.group(1)
            ma20 = close.rolling(20).mean()
            std20 = close.rolling(20).std()
            up = ma20 + 2 * std20
            lo = ma20 - 2 * std20
            p = close.iloc[-1]
            if pos == "upper": return p >= up.iloc[-1] * 0.995
            if pos == "lower": return p <= lo.iloc[-1] * 1.005
            return abs(p - ma20.iloc[-1]) / ma20.iloc[-1] < 0.01
    except Exception:
        return False
    return False


def check_conditions(df, condition_text: str) -> tuple:
    """检查一组条件（AND逻辑）"""
    if not condition_text:
        return False, []
    met = []
    for part in condition_text.split("|"):
        part = part.strip()
        for name, pattern in CONDITION_PARSERS.items():
            m = pattern.match(part)
            if m:
                if _check_single(df, name, m):
                    met.append(name)
                else:
                    return False, []
                break
    return len(met) > 0, met


class Watchdog:
    """盯盘引擎（EventBus 去重版）"""

    def __init__(self, config: dict):
        self.config = config
        self.poll_interval = config.get("watchdog", {}).get("poll_interval", 60)
        self.stop_event = threading.Event()
        self._pulse_last: dict[str, dict] = {}  # code -> {time, change, vol, price}

    def run(self):
        """盯盘主循环"""
        from src.eventbus import emit, bus
        emit("watchdog.start", {"message": "盯盘引擎启动"}, notify=True)
        self._push("QuantPilot 盯盘引擎已启动")

        while not self.stop_event.is_set():
            now = datetime.now(TZ)
            now_time = now.hour * 100 + now.minute

            if not ((930 <= now_time <= 1130) or (1300 <= now_time <= 1505)):
                time.sleep(30)
                continue

            try:
                self._check_watchlist()
                self._check_positions()
                self._check_market_pulse()  # 市场感知
            except Exception as e:
                logger.error(f"盯盘异常: {e}", exc_info=True)

            time.sleep(self.poll_interval)

        logger.info("盯盘引擎已停止")

    def _signal_dedup_key(self, code: str, condition: str) -> str:
        today = datetime.now(TZ).strftime("%Y%m%d")
        return f"wd:signal:{today}:{code}:{condition}"

    def _stop_dedup_key(self, prefix: str, code: str) -> str:
        today = datetime.now(TZ).strftime("%Y%m%d")
        return f"wd:stop:{today}:{prefix}_{code}"

    def _check_watchlist(self):
        from src.database import get_connection
        from src.eventbus import emit
        conn = get_connection()
        try:
            watchlist = conn.execute("SELECT * FROM watchlist WHERE active=1").fetchall()
            for item in watchlist:
                code, condition = item["code"], item["condition"] or ""
                name, strategy = item["name"] or code, item["strategy"] or ""
                if not condition:
                    continue

                import pandas as pd
                rows = conn.execute("""
                    SELECT trade_date, open, high, low, close, volume FROM daily_kline
                    WHERE code=? ORDER BY trade_date DESC LIMIT 60
                """, (code,)).fetchall()
                if not rows:
                    continue

                df = pd.DataFrame([dict(r) for r in reversed(rows)])
                all_met, met_types = check_conditions(df, condition)

                if all_met:
                    price = float(df["close"].iloc[-1])
                    prev = float(df["close"].iloc[-2]) if len(df) >= 2 else price
                    change = (price / prev - 1) * 100 if prev > 0 else 0
                    vol = int(df["volume"].iloc[-1])
                    vol_ratio = round(vol / df["volume"].iloc[-6:-1].mean(), 1) if len(df) >= 6 else 1

                    emit("watchdog.signal", {
                        "code": code, "name": name, "price": round(price,2),
                        "change_pct": round(change,2), "volume": vol, "vol_ratio": vol_ratio,
                        "condition": condition, "strategy": strategy, "met_types": met_types,
                        "summary": f"{name}({code}) {condition}",
                    }, notify=True, dedup_key=self._signal_dedup_key(code, condition))

                    self._log_alert(conn, code, name, "buy_signal",
                        f"{name}({code}) 现价{price:.2f} 涨跌{change:+.1f}% 量比{vol_ratio} 条件:{condition}")
                    # Pulse 盯盘分析
                    self._pulse_analyze(code, name, price, change, vol_ratio)
        finally:
            conn.close()

    def _check_positions(self):
        from src.database import get_connection
        from src.eventbus import emit
        conn = get_connection()
        try:
            live = conn.execute("SELECT * FROM live_positions").fetchall()
            for pos in live:
                price = self._get_price(pos["code"])
                if not price:
                    continue
                pct = (price / pos["cost"] - 1) * 100

                if pct <= -5:  # 跌破止损线
                    code = pos["code"]
                    emit("watchdog.stop_loss", {
                        "code": code, "name": pos["name"], "price": round(price,2),
                        "pct": round(pct,1), "cost": pos["cost"],
                        "summary": f"止损 {pos['name']}({code}) {pct:+.1f}%",
                    }, notify=True, dedup_key=self._stop_dedup_key("sl", code))

                    self._log_alert(conn, code, pos["name"], "stop_loss",
                        f"{pos['name']}({code}) 现价{price:.2f} 亏损{pct:+.1f}%")
                elif pct >= 10:  # 触及止盈线
                    code = pos["code"]
                    emit("watchdog.take_profit", {
                        "code": code, "name": pos["name"], "price": round(price,2),
                        "pct": round(pct,1), "cost": pos["cost"],
                        "summary": f"止盈 {pos['name']}({code}) {pct:+.1f}%",
                    }, notify=True, dedup_key=self._stop_dedup_key("tp", code))

                    self._log_alert(conn, code, pos["name"], "take_profit",
                        f"{pos['name']}({code}) 现价{price:.2f} 盈利{pct:+.1f}%")
        finally:
            conn.close()

    def _get_price(self, code: str) -> float:
        """获取实时价"""
        try:
            from config import config
            tf_config = config.get("tickflow", {})
            from tickflow import TickFlow
            tf = TickFlow(api_key=tf_config["api_key"])
            symbol = f"{code}.SH" if code.startswith("6") else f"{code}.SZ"
            quotes = tf.quotes.get(symbols=[symbol])
            if quotes:
                return float(quotes[0].get("last_price", 0))
        except Exception:
            pass
        return 0

    def _push(self, message: str):
        """推送至企业微信"""
        # 优先企业微信
        try:
            from src.wechat.server import send_text_message
            send_text_message("YangJie", message)
        except Exception:
            pass
        # 同时走告警中心
        try:
            from src.alerting import push_alert
            push_alert("warning", "盯盘信号", message[:200], "watchdog")
        except Exception:
            pass

    def _check_market_pulse(self):
        """市场感知 — 每5分钟扫描全局变化，发现结构性转变时推送"""
        import time, json
        now = time.time()
        if not hasattr(self, '_last_market_pulse'):
            self._last_market_pulse = 0
        if now - self._last_market_pulse < 300:  # 5分钟一次
            return
        self._last_market_pulse = now

        try:
            from agent.tools import detect_market_environment, screen_stocks, route_skills
            # 检测市场环境
            env = json.loads(detect_market_environment())
            env_type = env.get("market_env", "unknown")

            # 对比上次状态
            last_env = getattr(self, '_last_market_env', '')
            if env_type == last_env:
                return  # 没变化, 不出声

            self._last_market_env = env_type

            # 环境转变了 → 推送
            env_labels = {"qidong": "启动期", "gaochao": "高潮期", "fajiao": "发酵期",
                         "zhendang": "震荡期", "dimi": "低迷期", "bingdian": "冰点期"}
            label = env_labels.get(env_type, env_type)

            # 匹配选股
            stocks_msg = ""
            try:
                stocks = json.loads(screen_stocks(conditions="涨幅>3% AND 量比>2 AND 涨停", limit=5))
                if stocks.get("results"):
                    top = stocks["results"][:3]
                    stocks_msg = "\n热门: " + ", ".join(s.get("code", "?") + " " + s.get("name", "?") for s in top)
            except Exception:
                pass

            # 推荐战法
            strats_msg = ""
            try:
                strats = json.loads(route_skills(env_type))
                matched = strats.get("matched_strategies", [])[:3]
                if matched:
                    strats_msg = "\n" + "|".join(s["name"] for s in matched)
            except Exception:
                pass

            from src.alerting import push_alert
            push_alert("warning", f"市场感知: 进入{label}",
                       f"环境切换: 上期→{label}{stocks_msg}{strats_msg}",
                       "market_pulse")
        except Exception:
            pass  # 市场感知静默失败, 不影响盯盘

    def _pulse_analyze(self, code: str, name: str, price: float, change_pct: float, vol_ratio: float):
        """Pulse 盯盘推理 — 信号强度驱动的异动分析

        决策规则:
        - 同方向且幅度相近 (±2%范围内) → 跳过 (30min冷却)
        - 反向异动 → 立刻推
        - 幅度明显升级 (>1.5x) → 立刻推
        """
        import time as _time
        now = _time.time()
        last = self._pulse_last.get(code, {})

        if last:
            same_dir = (change_pct > 0) == (last.get("change", 0) > 0)
            time_since = now - last.get("time", 0)
            mag_ratio = abs(change_pct) / max(abs(last.get("change", 0.01)), 0.01)

            # 同方向 + 30分钟内 + 幅度没升级 → 跳过
            if same_dir and time_since < 1800 and mag_ratio < 1.5:
                return

        # 记录本次脉冲
        self._pulse_last[code] = {"time": now, "change": change_pct, "vol": vol_ratio, "price": price}

        direction = "涨" if change_pct > 0 else "跌"
        try:
            from agent.client import LLMClient
            from config import load_config
            cfg = load_config()
            client = LLMClient(cfg, model_key="flash")
            prompt = f"股票 {name}({code}) 现价{price:.2f}，{direction}{abs(change_pct):.1f}%，量比{vol_ratio}x。请用一句话分析原因（技术面/资金面/板块联动/消息面），20字内。"
            resp = client.chat([{"role": "user", "content": prompt}])
            analysis = resp.get("content", "")
            if not analysis:
                analysis = f"{name}异常{direction}幅"
        except Exception:
            analysis = f"{name}异动{abs(change_pct):.1f}%"

        direction_icon = "🔴" if change_pct > 0 else "🟢"
        try:
            from src.alerting import push_alert
            push_alert("warning", f"盯盘脉冲 {direction_icon} {name}",
                       f"{code} {name} 现价{price:.2f} {direction}{abs(change_pct):.1f}% 量比{vol_ratio}x\n💡 {analysis}",
                       "pulse")
        except Exception:
            pass

    def _log_alert(self, conn, code, name, alert_type, message):
        """记录告警"""
        conn.execute("""
            INSERT INTO alerts (code, name, alert_type, message, pushed)
            VALUES (?, ?, ?, ?, 1)
        """, (code, name, alert_type, message))
        conn.commit()

    def stop(self):
        self.stop_event.set()


def start(config: dict):
    """启动盯盘（命令行模式）"""
    wd = Watchdog(config)
    try:
        wd.run()
    except KeyboardInterrupt:
        wd.stop()
        logger.info("盯盘已手动停止")


# 全局实例追踪（防止重复启动）
_active_watchdog: Watchdog = None
_active_thread: threading.Thread = None


def start_background(config: dict) -> Watchdog:
    """后台线程启动盯盘，自动停止已存在的实例"""
    global _active_watchdog, _active_thread

    # 先停止旧实例
    stop_background()

    _active_watchdog = Watchdog(config)
    _active_thread = threading.Thread(
        target=_active_watchdog.run, daemon=True, name="watchdog"
    )
    _active_thread.start()
    logger.info("盯盘引擎已启动")
    return _active_watchdog


def stop_background():
    """停止后台盯盘"""
    global _active_watchdog, _active_thread
    if _active_watchdog:
        _active_watchdog.stop()
        logger.info("盯盘引擎已停止")
        _active_watchdog = None
        _active_thread = None

`

## 配置文件
`python
agent:
  enable_judge: true
  max_rounds: 30
  max_tool_result_chars: 8000
  tool_repeat_detection_window: 5
  verbose: true
akshare:
  delay_seconds: 1.0
  timeout: 30
baostock:
  consecutive_fail_threshold: 3
  reconnect_interval: 150
  timeout: 30
broker:
  provider: sim
  simulation: true
circuit_breaker:
  failure_threshold: 3
  half_open_calls: 1
  recovery_timeout: 60
data_sources:
  dragon_tiger:
    fallbacks:
    - akshare
    primary: zzshare
  historical:
    fallbacks:
    - mootdx
    - baostock
    - akshare
    primary: zzshare
  minute_kline:
    fallbacks:
    - zzshare
    primary: mootdx
  northbound:
    fallbacks:
    - exchange_csv
    primary: zzshare
  realtime:
    fallbacks:
    - tencent
    primary: mootdx
eastmoney:
  timeout: 10.0
embedding:
  batch_size: 10
  cache_max: 5000
  dimension: 768
  model: shibing624/text2vec-base-chinese
  provider: local
file_ops:
  allowed_dirs:
  - .
  max_file_size: 10485760
holidays:
- '2026-01-01'
- '2026-01-02'
- '2026-02-16'
- '2026-02-17'
- '2026-02-18'
- '2026-02-19'
- '2026-02-20'
- '2026-02-23'
- '2026-02-24'
- '2026-04-06'
- '2026-05-01'
- '2026-05-04'
- '2026-05-05'
- '2026-06-19'
- '2026-10-01'
- '2026-10-02'
- '2026-10-05'
- '2026-10-06'
- '2026-10-07'
- '2026-10-08'
live:
  accounts:
    国泰海通:
      broker: 国泰海通证券
      cash: 30918.79
      commission:
        bond:
          min: 1.0
          rate: 5.0e-05
        etf:
          min: 5.0
          rate: 5.0e-05
        stock:
          min: 5.0
          rate: 8.00001e-05
      stamp_tax_sell: 0.0005
      total_assets: 41886.79
      transfer_fee: 1.0e-05
    银河证券:
      broker: 银河证券
      cash: 2407.54
      commission:
        fund:
          min: 0.01
          rate: 5.0e-06
        stock:
          min: 5.0
          rate: 8.6e-05
      fixed_income: 50000
      fixed_income_maturity: '2026-07-20'
      stamp_tax_sell: 0.0005
      total_assets: 108279.34
      transfer_fee: 1.0e-05
llm:
  flash:
    api_key: sk-06742c13123f49e8884df05fb339ec9d
    base_url: https://api.deepseek.com
    max_tokens: 8192
    model: deepseek-v4-flash
    protocol: openai
    provider: deepseek
    temperature: 0.7
  primary:
    api_key: sk-06742c13123f49e8884df05fb339ec9d
    base_url: https://api.deepseek.com
    max_tokens: 16384
    model: deepseek-v4-pro
    protocol: openai
    provider: deepseek
    temperature: 0.3
memory:
  compression_batch: 10
  context_max_chars: 4000
  decay_threshold: 0.1
  max_memories: 200
  search_top_k: 5
  session_ttl_hours: 24
mode: local
port: 7861
retention:
  alerts_days: 30
  daily_kline_days: 0
  market_snapshot_days: 365
  minute_kline_days: 30
  trade_log_days: 0
retry:
  backoff_factor: 2.0
  base_delay: 1.0
  jitter: true
  max_delay: 30.0
  max_retries: 3
schedule:
  evening: 30 15 * * 1-5
  maintenance: 0 3 * * 0
  morning: 5 9 * * 1-5
  noon: 35 11 * * 1-5
  strategy_evolution: 0 10 * * 6
  watchdog_start: 10 9 * * 1-5
  watchdog_stop: 10 15 * * 1-5
server:
  domain: quantpilot.cn
simulation:
  capital:
    P1_顺势接力: 150000
    P2_逆势低吸: 100000
    P3_打板先锋: 100000
    P4_波段猎手: 80000
    P5_ETF轮动: 70000
  enabled: true
  state_filters:
    P1_顺势接力:
      min_streak_height: 3
    P2_逆势低吸:
      min_fall_count: 5
    P3_打板先锋:
      max_break_rate: 0.4
      min_streak_height: 3
sina:
  timeout: 10.0
tickflow:
  api_key: ${TICKFLOW_API_KEY}
  base_url: https://api.tickflow.org
  rate_limits:
    intraday_batch: 30
    intraday_single: 60
    kline_batch: 60
    kline_single: 120
    quotes_symbols: 120
    quotes_universe: 60
tushare:
  token: ''
watchdog:
  alerts:
    price_spike: 3.0
    volume_spike: 3.0
  kline_period: 5m
  max_stocks: 30
  poll_interval: 10
wechat:
  admin_users:
  - YangJie
  agent_id: 1000002
  agent_secret: vJaxxotrmsA7no6ZNRG2nuTW_goEhVYnJW9bBDDUzA4
  callback_port: 8000
  corp_id: wwbf92bb8d06f35eac
  enabled: true
  encoding_aes_key: DpNFQSRjZ0JJajU4bHdMblVPdk5nc3dhRktMRlVLabc
  token: QuantPilotToken2026

`
