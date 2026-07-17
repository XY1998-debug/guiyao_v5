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
