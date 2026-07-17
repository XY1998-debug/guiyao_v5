"""
归爻 V5 P0 — 市场状态检测 (宏观脑)
仲裁终裁版本 (2026-07-17)
  BULL_THRESH=70, BEAR_THRESH=25, BULL_EXIT=65
  宽度一票否决 + ma3 判定 + Bull 滞后退出 + 趋势分割修正
"""
import numpy as np

VOL_SLOPE = 600

class RegimeDetector:
    BULL_THRESH = 70
    BEAR_THRESH = 25
    BULL_EXIT = 65
    WIDTH_VETO = 12
    TREND_PROTECT = 28
    TREND_CAP = 15

    def detect(self, idx_close=None, idx_ma20=None, idx_ma60=None,
               idx_20d_ret=None, vol_20d=0.0, breadth_csi1000=0.5,
               breadth_210=0.5, streak_days=0, ma3_total=0.0,
               prev_regime="chop"):
        trend = 20
        if idx_close is not None and idx_ma20 is not None and idx_ma60 is not None:
            if idx_close > idx_ma20:
                trend += 5
            if idx_close > idx_ma60:
                trend += 15
            if idx_20d_ret is not None:
                trend = min(40, trend + 10 * np.tanh(max(-3, min(3, idx_20d_ret)) * 100))
        trend = max(0, min(40, trend))
        raw_vol = 30 * max(0, 1.0 - vol_20d / 0.05)
        if trend >= self.TREND_PROTECT:
            vol_score = 30.0
        elif trend < self.TREND_CAP:
            vol_score = min(raw_vol, 20.0)
        else:
            vol_score = raw_vol
        width_score = 30 * (breadth_csi1000 * 0.7 + breadth_210 * 0.3)
        total = trend + vol_score + width_score
        if width_score < self.WIDTH_VETO - 0.01:
            return {"regime":"bear","total":round(total,1),"confidence":1.0,"max_positions":0,"max_single":0.0}
        if total < self.BEAR_THRESH:
            return {"regime":"bear","total":round(total,1),"confidence":min(1.0,(25-total)/25),"max_positions":0,"max_single":0.0}
        if prev_regime == "bear":
            return {"regime":"chop","total":round(total,1),"confidence":min(1.0,(total-25)/45),"max_positions":3,"max_single":0.20}
        in_bull = (total >= self.BULL_THRESH or ma3_total >= self.BULL_THRESH)
        if prev_regime == "bull" and total < self.BULL_EXIT:
            in_bull = False
        if in_bull:
            return {"regime":"bull","total":round(total,1),"confidence":min(1.0,(total-70)/30),"max_positions":5,"max_single":0.25}
        return {"regime":"chop","total":round(total,1),"confidence":min(1.0,(total-25)/45),"max_positions":3,"max_single":0.20}

    @staticmethod
    def factor_weights(regime):
        if regime == "bull":
            return {"mom":0.40,"vol":0.35,"rev":0.25}
        if regime == "bear":
            return {"mom":0.10,"vol":0.80,"rev":0.10}
        return {"mom":0.25,"vol":0.50,"rev":0.25}
