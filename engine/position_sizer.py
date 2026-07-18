"""
归爻 V5 P0 — 仓位管理器
股数 = min(风险预算, 宏观上限, 现金) + VaR + 回撤惩罚
"""

import numpy as np
from typing import Optional


class PositionSizer:
    """仓位分配器"""

    def __init__(self, total_capital: float = 100_000.0):
        self.total_capital = total_capital
        self.peak_value = total_capital

        # 交易成本
        self.fee_buy = 0.00008  # 国泰个股万0.8  # 国泰海通个股万8.6
        self.fee_sell = 0.00108  # 万0.8+印花税千1
        self.min_fee = 5.0

    def calc_shares(
        self,
        price: float,
        atr: float,
        available_cash: float,
        current_value: float,
        regime: str,
        max_single_pct: float = 0.25,
    ) -> int:
        """计算建议买入股数

        Returns: 0 表示不买
        """
        # 1) 风险预算 (1.5% 本金 / 2×ATR)
        risk = self.total_capital * 0.0225
        risk_shares = int(risk / max(atr, 0.01)) if atr > 0 else 0

        # 2) 宏观上限
        macro_shares = int(self.total_capital * max_single_pct / price) if price > 0 else 0

        # 3) 现金约束 (最多 1/3 仓位)
        cash_shares = int(available_cash / 3.0 / price) if price > 0 else 0

        # 4) 取最小值
        shares = min(risk_shares, macro_shares, cash_shares)

        # 5) 整数股 + 资金保护
        if shares >= 100:
            shares = (shares // 100) * 100
            buy_fee = max(shares * price * self.fee_buy, self.min_fee)
            if available_cash < shares * price + buy_fee:
                shares = 0

        # 6) VaR 约束 (简化: 250天5%分位)
        # 由回测引擎传入 var_5pct
        # if shares > 0:
        #     pos_value = shares * price
        #     if pos_value * var_5pct > self.total_capital * 0.05:
        #         shares = int(shares * 0.7)

        # 7) 回撤惩罚
        if current_value < self.peak_value:
            dd = (self.peak_value - current_value) / self.peak_value
            if dd > 0.05:
                shares = int(shares * max(0.3, 1.0 - dd * 3))

        return shares

    def update_peak(self, current_value: float):
        if current_value > self.peak_value:
            self.peak_value = current_value

    def calc_sell(self, price: float, shares: int) -> tuple[float, float]:
        """计算卖出回款"""
        gross = price * shares
        fee = max(gross * self.fee_sell, self.min_fee)
        return gross - fee, fee
