"""
归爻 V5 — 仓位管理与回测引擎

仓位管理原则：
- 等权分配：总资金 ÷ 最大持仓数
- 整数股：向下取整到 100 股
- 凯利启发：胜率×盈亏比决定实际仓位比例
- 单股上限：不超过总资金 25%

回测引擎：
- 周频调仓（每周五收盘后计算，下周一开盘执行）
- T+1 可用（当天买次日才能卖）
- 含交易成本（佣金 0.025% + 印花税 0.1%）
"""

import polars as pl
from typing import List, Dict, Tuple, Optional
from dataclasses import dataclass, field
from datetime import date


@dataclass
class Position:
    """单个持仓"""
    code: str
    shares: int
    cost: float           # 买入均价
    buy_date: date
    current_value: float  # 当前市值


@dataclass
class Trade:
    """一笔交易记录"""
    date: date
    code: str
    direction: str        # "buy" / "sell"
    price: float
    shares: int
    cost_fee: float       # 手续费
    reason: str


@dataclass
class Account:
    """账户状态"""
    cash: float = 100_000.0
    positions: Dict[str, Position] = field(default_factory=dict)
    trades: List[Trade] = field(default_factory=list)
    equity_curve: List[Tuple[date, float]] = field(default_factory=list)

    @property
    def total_value(self) -> float:
        pos_val = sum(p.current_value for p in self.positions.values())
        return self.cash + pos_val

    @property
    def position_count(self) -> int:
        return len(self.positions)


class PositionSizer:
    """仓位计算器"""

    # 交易成本
    BUY_FEE = 0.00086  # 国泰海通个股万8.6       # 佣金 0.025%
    SELL_FEE = 0.00186  # 佣金万8.6 + 印花税千1      # 佣金 + 印花税 = 0.125%

    def __init__(
        self,
        max_positions: int = 3,
        max_single_pct: float = 0.25,
        lot_size: int = 100,
    ):
        self.max_positions = max_positions
        self.max_single_pct = max_single_pct
        self.lot_size = lot_size

    def allocate(
        self,
        account: Account,
        buy_codes: List[str],
        prices: Dict[str, float],
        current_date: date,
    ) -> List[Trade]:
        """根据买入信号和当前持仓，生成调仓交易

        Args:
            account: 当前账户状态
            buy_codes: 本轮应该持有的股票列表（按优先级排序）
            prices: 当前价格 {code: price}
            current_date: 当前日期

        Returns:
            需要执行的交易列表
        """
        trades = []
        current_held = set(account.positions.keys())
        target_set = set(buy_codes[:self.max_positions])

        # 1. 卖出不在目标列表中的持仓
        to_sell = current_held - target_set
        for code in to_sell:
            pos = account.positions[code]
            sell_price = prices.get(code, pos.current_value / pos.shares)
            sell_value = sell_price * pos.shares
            fee = sell_value * self.SELL_FEE
            trades.append(Trade(
                date=current_date, code=code, direction="sell",
                price=sell_price, shares=pos.shares,
                cost_fee=fee, reason="调仓卖出"
            ))

        # 2. 计算每槽位资金
        available_cash = account.cash
        # 加上卖出的回款（简化：T+1 再可用，但这里先算理论值）
        slots = min(self.max_positions, len(target_set - current_held))
        if slots == 0:
            return trades

        per_slot = available_cash / slots

        # 3. 买入新股票
        to_buy = target_set - current_held
        for code in to_buy:
            if code not in prices:
                continue
            price = prices[code]
            # 整数股
            max_shares = int((per_slot / self.lot_size / price)) * self.lot_size
            if max_shares <= 0:
                continue
            # 单股上限
            max_by_cap = int((account.total_value * self.max_single_pct) / price / self.lot_size) * self.lot_size
            shares = min(max_shares, max_by_cap, 10000)  # 最多 10000 股硬上限
            cost = shares * price
            fee = cost * self.BUY_FEE

            if cost + fee > available_cash:
                continue

            trades.append(Trade(
                date=current_date, code=code, direction="buy",
                price=price, shares=shares,
                cost_fee=fee, reason="信号买入"
            ))
            available_cash -= (cost + fee)

        return trades


class BacktestEngine:
    """回测引擎 — 周频调仓"""

    def __init__(
        self,
        sizer: PositionSizer,
        lookback_weeks: int = 40,      # 因子计算需要的暖场期
    ):
        self.sizer = sizer
        self.lookback_weeks = lookback_weeks

    def run(
        self,
        df: pl.DataFrame,
        regime_signals: Dict[str, dict],   # {date: {label, position_pct}}
        weekly_picks: Dict[str, List[str]], # {date: [codes]}  每周选股结果
    ) -> Account:
        """执行回测

        Args:
            df: 含因子的行情 DataFrame，必须按 date 排序
            regime_signals: 每日市场状态
            weekly_picks: 每周选股结果（周五的日期为 key）

        Returns:
            回测后的 Account 状态
        """
        account = Account()
        all_dates = sorted(df["date"].unique().to_list())

        # 获取所有周五的日期
        friday_dates = [d for d in all_dates if d.weekday() == 4]  # Monday=0, Friday=4
        if len(friday_dates) < self.lookback_weeks:
            return account

        # 从暖场期结束后开始
        for i, friday in enumerate(friday_dates[self.lookback_weeks:], self.lookback_weeks):
            # 获取下周一（执行日）
            next_monday = self._next_trading_day(friday, all_dates)
            if next_monday is None:
                continue

            # 1. 市场状态检查
            regime = regime_signals.get(str(friday), {})
            position_pct = regime.get("position_pct", 0.0)
            if position_pct < 0.3:
                # 清仓
                for code in list(account.positions.keys()):
                    price = self._get_price(df, code, next_monday)
                    if price:
                        self._sell(account, code, price, next_monday, "市场状态空仓")
                self._record_equity(account, df, next_monday)
                continue

            # 2. 获取本周选股
            picks = weekly_picks.get(str(friday), [])
            if not picks:
                self._record_equity(account, df, next_monday)
                continue

            # 3. 获取价格
            prices = {}
            for code in picks:
                p = self._get_price(df, code, next_monday)
                if p:
                    prices[code] = p
            # 加上已持仓股票的价格
            for code in account.positions:
                if code not in prices:
                    p = self._get_price(df, code, next_monday)
                    if p:
                        prices[code] = p

            # 4. 生成交易
            trades = self.sizer.allocate(account, picks, prices, next_monday)
            self._execute_trades(account, trades, next_monday)

            # 5. 记录权益
            self._record_equity(account, df, next_monday)

        return account

    def _get_price(self, df: pl.DataFrame, code: str, d: date) -> Optional[float]:
        row = df.filter(pl.col("code") == code, pl.col("date") == d)
        if len(row) == 0:
            return None
        return row["open"][0]  # 用开盘价执行

    def _sell(self, account: Account, code: str, price: float, d: date, reason: str):
        pos = account.positions.get(code)
        if not pos:
            return
        value = price * pos.shares
        fee = value * self.sizer.SELL_FEE
        account.cash += value - fee
        account.trades.append(Trade(date=d, code=code, direction="sell",
                                     price=price, shares=pos.shares, cost_fee=fee, reason=reason))
        del account.positions[code]

    def _execute_trades(self, account: Account, trades: List[Trade], d: date):
        for t in trades:
            if t.direction == "sell":
                pos = account.positions.pop(t.code, None)
                if pos:
                    account.cash += t.price * t.shares - t.cost_fee
                    account.trades.append(t)
            else:
                cost = t.price * t.shares + t.cost_fee
                if account.cash >= cost:
                    account.cash -= cost
                    account.positions[t.code] = Position(
                        code=t.code, shares=t.shares, cost=t.price,
                        buy_date=d, current_value=t.price * t.shares
                    )
                    account.trades.append(t)

    def _record_equity(self, account: Account, df: pl.DataFrame, d: date):
        # 更新持仓市值
        for code, pos in account.positions.items():
            row = df.filter(pl.col("code") == code, pl.col("date") == d)
            if len(row) > 0:
                pos.current_value = row["close"][0] * pos.shares
        account.equity_curve.append((d, account.total_value))

    def _next_trading_day(self, current: date, all_dates: List[date]) -> Optional[date]:
        """获取下一个交易日"""
        for d in all_dates:
            if d > current:
                return d
        return None
