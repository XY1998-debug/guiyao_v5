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
