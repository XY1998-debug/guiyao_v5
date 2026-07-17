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
    m_sl,             # float — 止损乘数(个股2.5/ETF1.5)
    commission_rate,  # float — 买入佣金率(国泰0.0008/银河0.0086)
    stamp_duty,       # float — 卖出印花税率(0.001)
    min_commission,   # float — 最低佣金(5.0)
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
            raw_shares = risk_budget / (m_sl * atr[s])
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
