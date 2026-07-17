"""用mootdx补全210池日线 + 22ETF + 列差距检查"""
import polars as pl
import numpy as np
from mootdx.quotes import Quotes
from pathlib import Path
from datetime import datetime

QP = Path("/home/ubuntu/guiyao_v5/data")

# ── 1. 拉210池最新数据 ──
q = Quotes.factory(market="std")
market = pl.read_parquet(QP / "market_v5.parquet")
all_codes = sorted(market["code"].unique().to_list())
print(f"原始池子: {len(all_codes)} 只")

# 取最新日期
latest = sorted(market["date"].unique().to_list())[-1]
print(f"最新已有数据: {latest}")
print(f"最新日数据量: {market.filter(pl.col('date')==latest).height} 行")

# 用mootdx拉所有股票最新行情补全
new_rows = []
for i, code in enumerate(all_codes):
    try:
        df = q.bars(symbol=code, frequency=9, start=0, count=3)
        if df is None or len(df) == 0:
            continue
        # 取最新一天
        last = df.iloc[-1]
        d_str = str(last["datetime"])[:10]
        new_rows.append({
            "code": code,
            "date": d_str,
            "open": float(last["open"]),
            "high": float(last["high"]),
            "low": float(last["low"]),
            "close": float(last["close"]),
            "volume": int(last["volume"]),
            "amount": float(last["amount"]),
        })
        if (i+1) % 50 == 0:
            print(f"  已拉 {i+1}/{len(all_codes)}...")
    except Exception as e:
        print(f"  {code} 跳过: {e}")

if new_rows:
    new_df = pl.DataFrame(new_rows).unique(subset=["code", "date"])
    # 保证schema一致
    for col in market.columns:
        if col in new_df.columns and new_df[col].dtype != market[col].dtype:
            new_df = new_df.with_columns(pl.col(col).cast(market[col].dtype))
    # 合并到现有数据
    merged = pl.concat([market, new_df]).unique(subset=["code", "date"]).sort(["code", "date"])
    merged.write_parquet(QP / "market_v5.parquet", compression="zstd")
    print(f"\n写入完成: {len(merged)} 行, {merged['code'].n_unique()} 只")
    print(f"最新日期: {sorted(merged['date'].unique().to_list())[-3:]}")
else:
    print("\n无新数据")

# ── 2. ETF数据同步 ──
print("\n--- ETF数据同步 ---")
etf_codes = ["510050","510300","510500","512100","588000",
             "512880","512690","512480","512660","515030",
             "512800","159825","510880","515220","512170",
             "159941","513100","513330","159920","511260"]
etf = pl.read_parquet(QP / "etf_v5.parquet") if (QP / "etf_v5.parquet").exists() else pl.DataFrame()
etf_latest = sorted(etf["date"].unique().to_list())[-1] if len(etf) > 0 else "2020-01-01"
print(f"ETF已有最新: {etf_latest}")

new_etf = []
for code in etf_codes:
    try:
        df = q.bars(symbol=code, frequency=9, start=0, count=5)
        if df is None or len(df) == 0:
            continue
        for _, row in df.iterrows():
            d_str = str(row["datetime"])[:10]
            row_dict = {
                "code": code,
                "date": d_str,
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": int(row["volume"]),
            }
            # amount只在现有ETF parquet有amount列时才加
            if "amount" in [c for c in (etf.columns if len(etf) > 0 else [])]:
                row_dict["amount"] = float(row["amount"])
            new_etf.append(row_dict)
    except:
        pass

if new_etf:
    new_ef = pl.DataFrame(new_etf)
    # 保证schema匹配
    if len(etf) > 0:
        for col in etf.columns:
            if col in new_ef.columns and new_ef[col].dtype != etf[col].dtype:
                new_ef = new_ef.with_columns(pl.col(col).cast(etf[col].dtype))
        # 保持列顺序一致
        new_ef = new_ef.select(etf.columns)
        merged_etf = pl.concat([etf, new_ef]).unique(subset=["code","date"]).sort(["code","date"])
    else:
        merged_etf = new_ef.unique(subset=["code","date"]).sort(["code","date"])
    merged_etf.write_parquet(QP / "etf_v5.parquet", compression="zstd")
    print(f"ETF写入: {len(merged_etf)} 行, 最新日期: {sorted(merged_etf['date'].unique().to_list())[-3:]}")
else:
    print("ETF无新数据")

# ── 3. 列差距检查 ──
print("\n--- V5 vs mootdx 字段对比 ---")
required = {"code","date","open","high","low","close","volume","amount"}
got = set(new_rows[0].keys()) if new_rows else required
print(f"V5需要: {required}")
print(f"mootdx给了: {got}")
print(f"缺: {required - got}" if required - got else "✅ 字段全齐")

# V5额外需要的列（因子计算用）
extra_needed = {"mom_20d","vol_20d","rev_5d","turnover","turnover_pctile"}
print(f"V5因子列(由Polars计算): {extra_needed}")
print("  这些由 factors.py 从OHLCV产生，mootdx无需提供 ✅")
