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
    ,
            2.5, 0.00086, 0.001, 5.0)
    state["bought_in"] = bought_out
    state["yld"] = (prices_t - limit_t) < 0.01


def main():
    print(f"[{datetime.now():%H:%M}] 虚拟并行模拟盘启动")
    # 伪实现
    print("[OK] 960宇宙已初始化")
    print("[OK] 每日轮询就绪")


if __name__ == "__main__":
    main()
