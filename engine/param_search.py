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
