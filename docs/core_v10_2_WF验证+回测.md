# 归爻 V5 — 2_WF验证+回测 (v10 终版)
## engine/backtest.py
`python
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

`

## engine/numpy_metrics.py
`python
import numpy as np

def calc_dsr(sharpe, n, n_trials):
    if abs(sharpe) < 1e-10:
        return 0.0
    import math
    var = 1 + 0.5*sharpe**2 - sharpe*math.sqrt(0.5)*math.erf(sharpe/2**0.5) + sharpe*(2*math.pi)**-0.5*np.exp(-0.5*sharpe**2)
    emsr = math.sqrt(var / n)
    z = sharpe - math.sqrt(var) * ((1-1e-6)**(1/(n_trials-1)) - 1) / ((n-1)**0.5)
    z = z / emsr if emsr > 0 else 0
    return 0.5 * (1 + math.erf(z / 2**0.5)) - 0.5

def calc_ks(x, y):
    combined = np.sort(np.concatenate([x, y]))
    cdf_x = np.searchsorted(x, combined, side='right') / len(x)
    cdf_y = np.searchsorted(y, combined, side='right') / len(y)
    d = np.max(np.abs(cdf_x - cdf_y))
    n_eff = len(x)*len(y) / (len(x) + len(y))
    p = 2 * np.exp(-2 * d**2 * n_eff)
    return d, min(p, 1.0)

def calc_calmar(ret):
    cum = np.cumprod(1 + ret)
    peak = np.maximum.accumulate(cum)
    dd = (cum - peak) / peak
    mdd = abs(np.min(dd))
    ann_ret = (cum[-1] / cum[0])**(252/len(ret)) - 1 if len(ret)>0 else 0
    return ann_ret / mdd if mdd > 0 else 0

`

## engine/param_search.py
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
    was_ld = np.zeros(S, dtype=np.int8)  # 首日无昨日跌停
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
            2.5, 0.00086, 0.001, 5.0,
        )
        # 跨日 T+1 状态迁移: 今日买入标记 → 昨日买入标记
        np.copyto(bought_y, bought_t)
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

## engine/state_evaluator.py
`python
import numpy as np
import polars as pl
LEVELS = {"bull":{"sharpe":0.5,"dd":0.15,"profit":2.5},"chop":{"sharpe":0.2,"dd":0.20,"profit":2.0},"bear":{"sharpe":-99,"dd":0.25,"profit":1.5}}
def classify(score,breadth,lc):
    if breadth<0.1 and lc>300: return "extreme"
    if score>=70: return "bull"
    if score<=30: return "bear"
    return "chop"
def evaluate(params,dates,ret,rd,w):
    tr = ret[w["test"][0]:w["test"][1]]
    if len(tr)<5: return {"pass":False}
    ds = rd.filter(pl.col("date").is_between(rd["date"][w["test"][0]],rd["date"][w["test"][1]-1]))
    dom = ds["regime"].mode()[0] if len(ds) > 0 else "chop"
    lv = LEVELS.get(dom,LEVELS["chop"])
    sh = np.mean(tr)/np.std(tr)*252**0.5 if np.std(tr)>0 else 0
    cum = np.cumprod(1 + tr)
    peak = np.maximum.accumulate(cum)
    dd = abs(np.min((cum - peak) / peak))
    return {"pass":sh>lv["sharpe"] and dd<lv["dd"],"state":dom,"sharpe":sh,"dd":dd}

`

## engine/stress_chamber.py
`python
import numpy as np
def stress_chamber_veto(params,engine,stress_data):
    for evt,data in stress_data.items():
        nav = np.cumprod(1+engine(params,data["ret"]))
        peak = np.maximum.accumulate(nav)
        dd = abs(np.min((nav-peak)/peak))
        if dd>0.35 or nav[-1]<0.65:
            return True,evt
    return False,""

`

## engine/wf_main.py
`python
import numpy as np
HOLDOUT = [('2024-01-02','2024-02-05'),('2024-09-24','2024-10-08')]
HOLDOUT_MAX_DD = 0.25
HOLDOUT_MAX_LOSS = 4
from engine.wf_scheduler import generate_windows,verify_no_leakage
from engine.numpy_metrics import calc_dsr
from engine.state_evaluator import evaluate
from engine.stress_chamber import stress_chamber_veto
def run_wf_validation(params_grid,dates,ret,rd,eng,stress=None,mode="full"):
    w = generate_windows(len(dates))
    verify_no_leakage(w)
    passed,sl = [],[]
    for p in params_grid:
        evals = [evaluate(p,dates,ret,rd,wi) for wi in w]
        wp = [e["pass"] for e in evals]
        if sum(wp)/len(wp)>=0.85:
            passed.append(p)
            sl.append([e.get("sharpe",0) for e in evals])
    if mode=="fast":
        return passed
    final = []
    for p in passed:
        n = len(passed)
        v3 = eng
        dsr = avg_sh = np.mean([s for sub in sl for s in sub]) if sl else 0
        n_samples = len([s for sub in sl for s in sub]) if sl else 1
        dsr = calc_dsr(avg_sh, n_samples, n)
        if dsr<=0: continue
        if stress:
            vet,evt = stress_chamber_veto(p,v3,stress)
            if vet: continue
        final.append(p)
    return final

`

## engine/wf_scheduler.py
`python
WARMUP = 60; TRAIN = 400; PURGE = 35; TEST = 80; STEP = 50
TOTAL_WINDOW = WARMUP + TRAIN + PURGE + TEST

def generate_windows(n_days):
    windows = []
    start = 0
    while start + TOTAL_WINDOW <= n_days:
        w1 = start + WARMUP
        w2 = w1 + TRAIN
        w3 = w2 + PURGE
        w4 = w3 + TEST
        windows.append({"idx":len(windows),"warmup":(start,w1),"train":(w1,w2),"test":(w3,w4)})
        start += STEP
    return windows

def verify_no_leakage(windows):
    for w in windows:
        assert w["train"][1] + PURGE == w["test"][0], f"leak in window {w['idx']}"

`
