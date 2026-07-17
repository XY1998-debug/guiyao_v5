# 归爻 V5 — P0 工程蓝图 (BLUEPRINT)

> **蓝图编号**：V5-P0-FINAL
> **签发日期**：2026-07-15
> **签发依据**：终审专家最终裁定 + 执行专家两次质询修正
> **阅读顺序**：先读 `CONSTITUTION.md`，再读本文档

---

## 一、系统全景数据流

```
┌──────────────────────────────────────────────────────────────────┐
│                        每日执行流程                               │
│                                                                  │
│  9:00  ┌──────────┐                                              │
│        │ 数据拉取  │  mootdx: 日线增量更新 (210池)                │
│        │          │  akshare: 中证1000快照 (breadth用)           │
│        │          │  × 超时降级: akshare失败了用昨日缓存          │
│        └────┬─────┘                                              │
│             ↓                                                    │
│  9:05  ╔══════════╗                                              │
│        ║ 超时检查  ║  如果当前时间 > 9:25 → 触发"防御性盲跑"       │
│        ║ DEADLINE  ║  跳过所有买入，仅执行持仓止损检查 + 告警      │
│        ╚════┬═════╝                                              │
│             ↓ (未超时则继续)                                      │
│  9:10  ┌──────────┐                                              │
│        │ 市场状态  │  4维打分 + 非对称防抖                        │
│        │          │  → bull/chop/bear                            │
│        └────┬─────┘                                              │
│             ↓                                                    │
│  9:12  ┌──────────┐                                              │
│        │ 因子计算  │  Polars Lazy: rev_5d, low_vol, mom_20d       │
│        │          │  + turnover_pctile + MGS正交化               │
│        └────┬─────┘                                              │
│             ↓                                                    │
│  9:13  ┌──────────┐                                              │
│        │ 参数搜索  │  200宇宙 Numba @njit 并行                   │
│        │          │  → Walk-Forward 60天验证                     │
│        │          │  → Kelly 门控 → 通过宇宙取最优参数            │
│        └────┬─────┘                                              │
│             ↓                                                    │
│  9:14  ┌──────────┐                                              │
│        │ 仓位分配  │  股数 = min(风险, 上限, 现金)                │
│        │          │  + 资金保护 + VaR + 回撤惩罚                  │
│        └────┬─────┘                                              │
│             ↓                                                    │
│  9:15  ┌──────────┐                                              │
│        │ 信号输出  │  高开>2% / 低开<-3% 过滤                    │
│        │          │  T+1 当日有效                                │
│        │          │  → 同花顺同步: Top 10 候选 + 触发信号股票     │
│        └──────────┘                                              │
│                                                                  │
│  15:00 ┌──────────┐                                              │
│        │ 晚间复盘  │  实盘执行 vs 信号建议对比                    │
│        │          │  滑点/犹豫统计 → 记忆桥接                    │
│        └──────────┘                                              │
└──────────────────────────────────────────────────────────────────┘
```

---

## 二、模块清单与接口

### 模块 1：数据层 (`engine/data_layer.py`)

| 项目 | 说明 |
|------|------|
| **职责** | 统一数据接口：历史日线（mootdx Parquet 缓存）+ 实时快照（akshare）+ 中证1000 breadth |
| **技术** | mootdx (历史), akshare (快照), Parquet (缓存), Polars (拼接) |

**210 池维护规则**（双轨制）：
- **月度全量筛选**：每月末按"最近120天动量最强 + 日均成交额>2000万"在全市场筛选210只
- **日常硬性剔除**：池内股票触发以下任一立即踢出（下月再评估）：
  - 被 ST 或 *ST
  - 连续 2 个交易日一字跌停
  - 单日跌幅 > 15% 且成交额异常放大（>20日均量 3倍）

**流通股本缓存**（周末低频更新）：
- 每周六通过 akshare 拉取全市场最新流通股本 → 本地 Parquet
- 日常用 `turnover = volume / float_shares` 自行计算
- 历史回测直接用 akshare `stock_zh_a_hist`（自带换手率字段）

**核心函数签名**：
```python
def update_daily_kline(codes: list[str]) -> pl.DataFrame:
    """增量更新日线 Parquet，仅追加新数据。返回完整 DataFrame"""

def fetch_csi1000_snapshot(timeout: float = 10.0) -> pl.DataFrame:
    """akshare 'stock_zh_a_spot_em' 获取中证1000成分股当日快照。
    缓存60日均线值到 Parquet，每日仅更新当日价格。返回含 close, ma60, pct_change
    超时或失败 → 返回昨日缓存快照 (stale=True 标记)"""

def compute_breadth(snapshot: pl.DataFrame) -> float:
    """中证1000成分股 close>ma60 的比例。0-1 之间的值。
    停牌股 (close 为 NaN) 从分母中剔除，不作任何假设。"""
    valid = snapshot.filter(pl.col("close").is_not_nan())
    return (valid.filter(pl.col("close") > pl.col("ma60")).height / valid.height)

# API 降级
def get_snapshot_with_fallback() -> tuple[pl.DataFrame, bool]:
    try:
        df = fetch_csi1000_snapshot(timeout=10)
        if df.is_empty():
            raise ValueError("空数据")
        save_cached_snapshot(df)
        return df, False  # stale=False
    except Exception:
        return load_cached_snapshot(), True  # stale=True
```

**内存预算**：中证1000快照约 2MB，Parquet 缓存均线值 < 5MB。


### 模块 2：市场状态检测 (`engine/regime.py`)

| 项目 | 说明 |
|------|------|
| **职责** | 4维打分 → bull/chop/bear 三态输出 |
| **输入** | breadth(中证1K), breadth(210池), 趋势评分, 波动率分位 |
| **输出** | `bull` / `chop` / `bear` + 置信度 + 建议仓位上限 |

**评分规则**（最终执行标准 — 含防抖机制）：
```python
# ═══════════════════════════════════════════════════════
# 4 维评分公式 (0-100分)
# ═══════════════════════════════════════════════════════

# 1. 趋势评分 (0-40分，分段阶跃 + tanh)
trend_score = 20  # 基础分
if idx > idx_ma20:      trend_score += 5   # 站上20日均线
if idx > idx_ma60:      trend_score += 15  # 站上60日均线（阶跃大奖励）
trend_score = min(40, trend_score + 10 * np.tanh(idx_20d_ret * 100))

# 2. 波动率评分 (0-30分，低波高分)
vol_pctile = (vol_20d.rank() / vol_20d.count())  # 当前波动率在历史中的分位
vol_score = 30 * (1.0 - vol_pctile)

# 3. 宽度评分 (0-30分，中证1K + 210池加权)
breadth_score = 30 * (breadth_csi1000 * 0.7 + breadth_210 * 0.3)

# 4. 总分
total = trend_score + vol_score + breadth_score  # 0-100

# ═══════════════════════════════════════════════════════
# 非对称防抖 (Anti-Flutter) — 防止震荡市频繁切换
# ═══════════════════════════════════════════════════════

# 向下切换 (Bull/Chop → Bear): 零延迟，breadth<35 次日开盘无条件执行
# 向上切换 (Bear → Chop/Bull): 强制平滑，连续3天总分>65 或 3日均线>65

if total >= 65 and (consecutive_up_days >= 3 or ma3_total >= 65):
    regime = "bull"
elif total < 35:
    regime = "bear"
    consecutive_up_days = 0  # 跌入熊市立即重置向上计数器
else:
    regime = "chop"

# 极端熔断: 跌停>300家 或 breadth_csi1000<0.10 → 强制锁死一切买入(含ETF探针)

# 熊市仓位: 完全空仓 + 模拟运行(Paper Trading) + 可选ETF探针(1手510300)
```

**关键变量持久化**：
`consecutive_up_days` 和 `ma3_total` 必须存入 SQLite 表 `regime_state`，跨重启保持：
```sql
CREATE TABLE regime_state (
    date TEXT PRIMARY KEY,
    total_score REAL,
    regime TEXT,
    consecutive_up_days INTEGER,
    ma3_total REAL
);

**关键设计决策**：
- 210池 breadth 权重 0.3、中证1000 breadth 权重 0.7：通过加权融合而非只看全市场，保留了210池对微观强势的感知
- 趋势评分采用分段阶跃 + tanh 平滑：阶跃捕捉60日线突破的博弈突变，tanh 提供连续平滑


### 模块 3：因子计算 (`engine/factors.py`)

| 项目 | 说明 |
|------|------|
| **职责** | 计算因子 → MGS 正交化 → 输出因子矩阵 |
| **因子数** | P0: 4个（mom_20d, vol_20d, rev_5d, turnover_pctile）+ P1: EP-TTM |

**换手率因子（终审裁定采纳的截面百分位方案）**：
```python
def calc_turnover_pctile(df: pl.DataFrame) -> pl.Series:
    """当日换手率在全市场的截面百分位排名。
    免疫市值、行业影响。>0.95 分位 = 异常活跃，需特别关注。"""
    return df.select(
        pl.col("turnover").rank("ordinal") / pl.col("turnover").count()
    ).over("date")
```

**MGS 正交化** (Modified Gram-Schmidt，修正 Classical GS 数值不稳定)：
```python
def mgs_orthogonalize(factor_matrix: np.ndarray) -> np.ndarray:
    """Modified Gram-Schmidt，输入 (stocks, factors)，输出正交因子矩阵"""
```
注意：MGS 在 Polars 中不能用 Lazy 表达式实现，必须提取 NumPy 数组后处理。

**内存预算**：210股 × 5因子 × 827天 ≈ 8.6MB（Polars Lazy 下常驻更少）。


### 模块 4：回测内核 (`engine/backtest_kernel_v3.py`)

| 项目 | 说明 |
|------|------|
| **职责** | V3 sandbox.py 修正版：T+1 锁定、分离 total_capital/cash、cost_basis |
| **技术** | Numba @njit, parallel=True, prange |
| **来源** | 专家二 `backtest_kernel_v3` 代码（终审直接采纳） |

**关键修正**（相对于 V3 原版 `sandbox.py`）：

| 修正项 | V3 原版 | P0 版本 |
|--------|---------|---------|
| 本金语义 | `cash` 既参与计算又当本金 | `total_capital`(不变，算股数) + `cash`(可扣减) |
| 卖出后现金 | 立即可用于当天再买 | 卖出回款入 cash，可用于当天买入（买入端无 T+1 限制）。买入的股票标记 bought_today，明天才能卖 |
| 持仓成本 | 不记录 | `cost_basis` 数组记录每笔持仓成本（计算盈亏用） |
| T+1 校验 | 无 | 今日买入的股票标记 `bought_today`，明天才能卖 |

**核心函数签名**：
```python
@njit(parallel=True, cache=True)
def backtest_kernel_v3(
    prices, signals, atrs, limit_down,   # (S,) float32 arrays
    total_capital, cash,                 # (U,1) float64
    pos, cost_basis,                     # (U,S) int32/float64
    bought_today_in,                     # (U,S) int8: 昨天的锁定标记 (只读)
    bought_today_out,                    # (U,S) int8: 今天的锁定标记 (写入)
) -> None:
```

**`bought_today` 生命周期**（修复跨日传递 Bug）：
```python
# Python 层 (orchestrator) 管理跨日传递
bought_yesterday = np.zeros((U, S), dtype=np.int8)
bought_today = np.zeros((U, S), dtype=np.int8)

for day_idx in range(total_days):
    # 内核读取 bought_yesterday (昨天买的今天不能卖)
    # 内核写入 bought_today (今天买的明天不能卖)
    backtest_kernel_v3(..., bought_yesterday, bought_today)
    # 轮换
    bought_yesterday = bought_today.copy()
    bought_today.fill(0)
```

**跌停处理**（业务降级判定——不检查成交量）：
```python
# Python 层 (orchestrator) 管理 limit_down 和 yesterday_was_limit_down
yesterday_limit_down = np.zeros(S, dtype=bool)
today_limit_down = np.zeros(S, dtype=bool)

for day_idx in range(total_days):
    # 内核内：卖出前检查
    # 只要收盘价 ≈ 跌停价（0.01元容差）→ 视为流动性枯竭，不可卖出
    # 连续跌停后首次打开次日卖出的惩罚：额外 2% 滑点
    backtest_kernel_v3(..., yesterday_limit_down, today_limit_down)
    # 轮换
    yesterday_limit_down = today_limit_down.copy()
```

**内存预算**：200宇宙 × 210股 × 4B ≈ 168KB（仅矩阵，不含因子）


### 模块 5：仓位管理 (`engine/position_sizer.py`)

| 项目 | 说明 |
|------|------|
| **职责** | 股数 = min(风险预算, 宏观上限, 现金约束) + VaR + 回撤惩罚 |
| **输出** | 具体交易指令（代码, 方向, 股数, 限价） |

**核心公式**（终审裁定融合版）：
```python
# 1. 风险预算股数（每笔 1.5% 本金 / 2倍ATR）
risk_shares = risk_budget  # total_capital × 0.015 / (2 × ATR)

# 2. 宏观上限（由市场状态决定）
bull:  max_positions=5, max_single=25%
chop:  max_positions=3, max_single=20%
bear:  max_positions=0 (空仓)

# 3. 现金约束
cash_shares = available_cash / (price × 1.0003) / 5  # 最多5个槽位

# 4. 股数取最小值 + 资金保护
shares = min(risk_shares, macro_shares, cash_shares)

# ── P0 关键修复：防止资金透支 ──
if shares >= 100:
    shares = (shares // 100) * 100  # 向下取整到 100 股整数倍
    min_cost = shares * price * 1.0003 + 5  # 含佣金
    if available_cash < min_cost:
        shares = 0  # 不够钱就一股不买，不强制
elif shares > 0:
    shares = 0  # 不足 1 手不买

# 5. T+1 VaR 5% 约束（过去250天5%分位日收益 × 持仓市值 < 总本金 5%）
if position_value * var_5pct > total_capital * 0.05:
    shares = int(shares * 0.7)  # 缩减30%

# 6. 回撤惩罚因子 R_adj
dd_pct = (peak_nv - current_nv) / peak_nv
if dd_pct > 0.05:
    R_adj = max(0.3, 1.0 - dd_pct * 3)  # 回撤>5%开始惩罚，min=0.3
    shares = int(shares * R_adj)
```

**信号执行窗口**：
- 高开 > 2% → 放弃买入（不追高）
- 低开 < -3% → 放弃买入（不接飞刀）
- T+1 窗口：当天信号仅当天有效，次日作废


### 模块 6：参数搜索 (`engine/param_search.py`)

| 项目 | 说明 |
|------|------|
| **职责** | 200 宇宙暴力搜索 + Walk-Forward 60天验证 + Kelly 二元门控 |
| **代码量** | < 60 行（不含内核） |

**搜索逻辑**：
```python
# 1. 生成 200 组随机参数
base_mom, base_vol, base_rev = 0.04, 0.02, 0.01
params = np.column_stack([
    rng.uniform(base_mom - 0.02, base_mom + 0.02, 200),  # 买入动量阈值
    rng.uniform(base_vol - 0.01, base_vol + 0.01, 200),  # 买入波动上限
    rng.uniform(-0.02, 0.02, 200),                       # 卖出阈值
])

# 2. 200 宇宙并行回测
for p in params:
    run_backtest(p)  # Numba kernel

# 3. Walker-Forward 验证（60天滚动窗口）
# 前540天训练 → 后60天验证
# 验证期夏普 > 0 且胜率 > 35% 才通过

# 4. Kelly 二元门控
# Full Kelly = (win_rate × avg_win - (1-win_rate) × avg_loss) / avg_win
# Kelly > 0.10 且 win_rate > 35% → 通过
```


### 模块 7：信号输出 (`engine/signal_output.py`)

| 项目 | 说明 |
|------|------|
| **职责** | 格式化交易建议 + 同花顺自选同步 + 记忆桥接 |
| **同步策略** | 210池是内部计算池，不推送。只同步到同花顺：当日 Top 10 候选股 + 已触发买入信号的股票 |
| **来源** | 复用 V2 的 `ths_favorite/selfstock_v1.py`（已存在，接口可能需更新）|

---

## 三、内存总体预算

| 组件 | 常驻内存 | 峰值内存 | 备注 |
|------|---------|---------|------|
| Polars 因子 LazyFrame | < 20MB | 50MB | streaming 模式下释放 |
| 中证1000 breadth 快照 | < 2MB | 5MB | akshare 解析 |
| Parquet 缓存 | 0MB | 10MB | 仅读取时占用 |
| Numba 内核矩阵 (200×210) | 0.2MB | 0.5MB | 极轻量 |
| Python 进程基础 | 50MB | 80MB | 含依赖 |
| **总计** | **< 80MB** | **< 150MB** | 云端 3.6GB 完全够用 |

---

## 四、Numba 兼容性约束（开发必读）

所有进入 Numba @njit 内核的数据必须满足：
1. **类型固定**：float64 / int32 / int8，禁 float16/bool
2. **C 连续**：`np.ascontiguousarray()` 确保内存布局
3. **无 Python 对象**：禁 list/dict/datetime/str/Polars
4. **数组维度固定**：`(U, S)` = (宇宙数, 股票数)，不可动态改变
5. **prange 内无竞态**：每个宇宙独立写入自己的行（`pos[u, s]`），宇宙间无交叉

---

## 五、P0 交付清单

| # | 模块 | 文件 | 预计行数 | 依赖 | 状态 |
|---|------|------|---------|------|:--:|
| 1 | 数据层 | `engine/data_layer.py` | ~175 | mootdx, akshare | ✅ |
| 2 | 市场状态 | `engine/regime.py` | ~75 | 数据层 | ✅ |
| 3 | 因子计算 | `engine/factors.py` | ~85 | Polars, 数据层 | ✅ |
| 4 | Numba 内核 | `engine/backtest_kernel_v3.py` | ~130 | Numba, NumPy | ✅ |
| 5 | 仓位管理 | `engine/position_sizer.py` | ~65 | 市场状态, Numba内核 | ✅ |
| 6 | 参数搜索 | `engine/param_search.py` | ~150 | 仓位管理 | ✅ |
| 7 | 信号输出 | `engine/signal_output.py` | ~65 | V2 ths_favorite | ✅ |
| 8 | 影子账本 | `engine/shadow_ledger.py` | ~130 | SQLite | ✅ |
| — | ETF轮动 | `strategies/etf_rotation.py` | ~260 | Polars, mootdx | ✅ |
| — | 个股突破 | `strategies/stock_breakout.py` | ~380 | Polars, mootdx | ✅ |
| — | 主入口 | `main.py` | ~145 | 以上全部 | ✅ |
| — | 工程宪法 | `CONSTITUTION.md` | — | — | ✅ |
| — | 工程蓝图 | `docs/BLUEPRINT.md` | — | — | ✅ |
| **合计** | — | **~1,500 行** | — | — |

---

## 六、关键设计决策理由（为什么这样做）

| 决策 | 理由 |
|------|------|
| 中证1000 breadth 权重 0.7 + 210池 0.3 | 全市场宏观系统性风险为主，210池微观强势感知为辅。两者加权而非二选一 |
| 趋势评分用分段阶跃 + tanh | 60日线突破有非连续的博弈突变（技术派集中跟风），阶跃奖励反映这种突变 |
| 截面百分位换手率 | 免市值分层维护，免行业影响校正，代码最简，鲁棒最强 |
| 暴力搜索 200 宇宙而非 fANOVA | 2-3 维参数空间下暴力搜索已能密集覆盖，fANOVA 过度设计 |
| 分离 total_capital 和 cash | 算股数用固定本金，扣除用实际现金。原版 V3 混用导致股数随亏损缩小（变相 martingale） |
| bought_today 数组 | T+1 约束的 Numba 实现。单日标记，次日清零 |

---

## 七、蓝图的不可变部分 vs 可调参数

| 不可变（改需重新评审） | 可调（配置文件即可） |
|------------------------|---------------------|
| 双脑架构 | 市场状态阈值 (65/35) |
| Numba @njit 内核 | 因子权重 (50/25/25 动态) |
| 仓位 min 架构 | VaR 窗口 (250天) |
| MGS 正交化 | 回撤惩罚系数 (0.05触发) |
| Kelly 二元门控阈值 (0.10/35%) | 宇宙数量 (200) |
| T+1 执行窗口 | Walk-Forward 窗口 (60天) |
| 跌停判定标准 (0.01容差) | 防抖连续天数 (3天) |
| 防御性盲跑超时 (9:25) | ETF探针开关 |

---

*蓝图签发。任何 Agent 在执行 P0 开发前必须先读 `CONSTITUTION.md` 和本文档。*
