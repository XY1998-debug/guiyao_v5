# 归爻 V5.P1 执行引擎
import numpy as np
import polars as pl
from dataclasses import dataclass
from typing import Optional

MARKET_TYPE = {
    "600":"主板","601":"主板","603":"主板","605":"主板",
    "000":"主板","001":"主板","002":"主板",
    "300":"创业板","301":"创业板",
    "688":"科创板","689":"科创板",
}
LIMIT_RATIO = {"主板":0.10,"创业板":0.20,"科创板":0.20}

def _get_market(code):
    return MARKET_TYPE.get(str(code)[:3], "主板")

def gate_macro_veto(regime, stype="stock"):
    if regime == "bear": return False
    if regime == "chop" and stype == "stock": return False
    return True

@dataclass
class PriceSuggestion:
    entry_price: float = 0.0
    stop_loss: float = 0.0
    take_profit: float = 0.0
    position_pct: float = 0.0
    regime: str = "chop"
    market_type: str = "主板"

class ExecutionEngine:
    GAMMA = {"bull":1.2,"chop":1.0,"bear":-1.0}
    SLIPPAGE = {"high":0.001,"mid":0.002,"low":0.003}
    PRICE_CAGE = 0.02
    # 股票/ETF分策略止损止盈（仲裁专家终裁）
    STOCK_MSL = 2.5; STOCK_RRR = 3.5
    ETF_MSL = 1.5; ETF_RRR = 2.0
    GAMMA = {"bull":1.2,"chop":1.0,"bear":-1.0}


    def calculate(self, code, signal, price, atr, atr20,
                  entry_price=0, position_pct=0, regime="chop", vol_rank="mid", asset_type="stock"):
        if not gate_macro_veto(regime, asset_type):
            return None
        mkt = _get_market(code)
        atr_safe = atr20 if (atr>3*atr20 and atr20>0.01) else atr
        if regime == chr(98)+chr(101)+chr(97)+chr(114): return None
        if signal == 1:
            gamma = self.GAMMA.get(regime, 1.0)
            m_sl = self.STOCK_MSL if asset_type=="stock" else self.ETF_MSL
            rr = self.STOCK_RRR if asset_type=="stock" else self.ETF_RRR
            sl = round(price - atr_safe * m_sl, 2)
            tp = round(price + (price - sl) * rr, 2)
            entry = round(price * (1 + self.SLIPPAGE.get(vol_rank,0.002)), 2)
            
            return PriceSuggestion(entry, round(sl,2), tp, 0.10, regime, mkt)
        if signal == -1 and entry_price > 0:
            pnl = (price - entry_price) / entry_price
            trail = 0.05 if pnl<0.10 else (0.04 if pnl<0.30 else 0.03)
            sl = round(entry_price * (1 - trail), 2)
            return PriceSuggestion(0, sl, 0, position_pct, regime, mkt)
        return None

CONFIGS = {
    "train": {"slip":0.0,"style":"close","t1p":False,"cage":False},
    "virtual": {"slip":"random","style":"next_open","t1p":True,"t1pct":0.02,"cage":True,"grace":21},
    "sim": {"slip":"live","style":"auction+chase","t1p":True,"cage":True},
    "shadow": {"slip":"live","style":"auction+chase","t1p":False,"cage":True},
    "live": {"slip":"real","style":"auction+chase","t1p":False,"cage":True},
}

def apply_t1_penalty(df):
    if "entry_price" not in df.columns:
        return df
    return df.with_columns(
        pl.when((pl.col("exit_price")<pl.col("entry_price")*0.95)&(pl.col("holding_days")<=1))
        .then(pl.col("pnl")-pl.col("shares")*pl.col("entry_price")*0.02)
        .otherwise(pl.col("pnl")).alias("pnl_adjusted")
    )
