# 归爻系统 — 核心代码节选（供GPT体检参考）

> 以下代码节选仅供了解关键实现细节。完整代码在 Gitee: https://gitee.com/quantpilot/guiyao_v5

---

## 一、执行引擎（execution_engine.py）
```python
class ExecutionEngine:
    """Gate → Calculator → Injector 三段式"""
    GAMMA = {"bull":1.2,"chop":1.0,"bear":-1.0}
    SLIPPAGE = {"high":0.001,"mid":0.002,"low":0.003}
    PRICE_CAGE = 0.02

    def calculate(self, code, signal, price, atr, atr20,
                  entry_price=0, position_pct=0, regime="chop", vol_rank="mid"):
        # Gate: 宏观一票否决
        if not gate_macro_veto(regime, "stock" if signal==1 else "etf"):
            return None
        # ATR熔断
        atr_safe = atr20 if (atr>3*atr20 and atr20>0.01) else atr
        if signal == 1:  # 买入: 动态止损 + 止盈
            gamma = self.GAMMA.get(regime, 1.0)
            sl = price - atr_safe * 2.0 * gamma  # 止损 = 价 - ATR*2*宏观系数
            entry = round(price * (1 + self.SLIPPAGE.get(vol_rank,0.002)), 2)
            tp = round(entry + (entry - sl) * 2.5, 2)  # 止盈 = 入场 + (入场-止损)*2.5
            return PriceSuggestion(entry, round(sl,2), tp, 0.10, regime, mkt)
        if signal == -1 and entry_price > 0:  # 持仓: 跟踪止盈
            pnl = (price - entry_price) / entry_price
            trail = 0.05 if pnl<0.10 else (0.04 if pnl<0.30 else 0.03)
            sl = round(entry_price * (1 - trail), 2)
            return PriceSuggestion(0, sl, 0, position_pct, regime, mkt)
        return None
```

---

## 二、V3 Numba 引擎（backtest_kernel_v3.py 核心头部）
```python
@njit(parallel=True, cache=True)
def backtest_kernel_v3(prices, signals, atrs, limit_down,
                       total_capital, cash, positions, cost_basis,
                       bought_today_in, bought_today_out,
                       yesterday_limit_down, n_univ, n_stocks):
    for u in prange(n_univ):
        for s in range(n_stocks):
            if signals[u, s] == -1 and positions[u, s] > 0 \
               and bought_today_in[u, s] != 1:  # T+1检查
                if abs(prices[s] - limit_down[s]) >= 0.01:  # 非跌停可卖
                    sell_price = prices[s] * 0.98 if yesterday_limit_down[s] else prices[s]
                    fee = max(sell_price * positions[u, s] * 0.0013, 5.0)
                    cash[u, 0] += sell_price * positions[u, s] - fee
                    positions[u, s] = 0; cost_basis[u, s] = 0.0
            if signals[u, s] == 1 and prices[s] > 0.01 and atrs[s] > 0.01:
                shares = int(max(risk_budget / (2.0 * atrs[s]), 0))
                if shares >= 100:
                    shares = (shares // 100) * 100
                    total_cost = shares * prices[s] + max(shares*prices[s]*0.0003, 5.0)
                    if total_cost <= cash[u, 0]:
                        cash[u, 0] -= total_cost
                        positions[u, s] += shares
                        bought_today_out[u, s] = 1  # T+1标记
```

---

## 三、中观脑（regime_meso.py — ETF轮动核心）
```python
def rs_rank(close: np.ndarray) -> np.ndarray:
    """RS = 20日涨幅×0.7 + 60日涨幅×0.3"""
    mom_20 = close[-1] / close[-21] - 1
    mom_60 = close[-1] / close[-61] - 1
    return mom_20 * 0.7 + mom_60 * 0.3

class MesoBrain:
    def weekly_cycle(self, df, current_date, macro_regime):
        # 计算22只ETF的RS
        ranking = self._compute_rs_ranking(df)
        # 安全垫：全负RS→国债
        if all(r < 0 for r in ranking.values()):
            return {"status":"safety","action":"全仓国债ETF"}
        # 缓冲带：持仓跌出Top5才换
        for held in self.positions:
            if ranking.get(held["code"], -999) >= -len(ranking)*0.25:
                continue  # 还在Top5内
        # 换仓：买入新Top3
        ...
```

---

## 四、48组参数搜索空间
```python
# 6动量 x 4波动 x 2反转 = 48
mom_thresholds = [0.0, 0.01, 0.02, 0.03, 0.05, 0.07]
vol_thresholds = [0.01, 0.02, 0.03, 0.05]
rev_thresholds = [-0.02, -0.05]
```

---

## 五、价格告警集成（watchdog + price_checker）
```python
# 5秒轮询→读price_alerts.parquet→对比实时价
# 触达买入价/止损价/止盈价→自动推企业微信
def check():
    if not os.path.exists(ALERTS): return
    df = pl.read_parquet(ALERTS)
    for r in df.iter_rows(named=True):
        if r["direction"]=="buy" and not r["triggered_buy"]:
            push(f"[价格提醒] {r['name']}({r['code']})建议买入价{r['entry_price']}")
```

---

*GPT审查时如需查看任意完整文件，可以在对话中指定文件名，我会提供。*
