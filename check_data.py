"""检查 mootdx 数据覆盖"""
from mootdx.quotes import Quotes
import pandas as pd
import sys

q = Quotes.factory(market='std')

# 1. mootdx能返回啥
df = q.bars(symbol='000001', frequency=9, start=0, count=5)
print("=== mootdx 日线字段 ===")
print(list(df.columns))
print(df.head(2).to_string())

# 2. ETF能拉到吗
df2 = q.bars(symbol='510050', frequency=9, start=0, count=5)
print("\n=== ETF(510050) 样例 ===")
print(list(df2.columns))
print(df2.head(2).to_string())

# 3. V5日线需要 vs mootdx能给的
print("\n=== V5 data_layer.py 需要的字段 ===")
print("code, date, open, high, low, close, volume, amount")
print("mootdx 有: datetime, open, high, low, close, volume, amount ✅")
print("\n差异: mootdx 的datetime = V5 的date(需截取前10字符)")

# 4. V5 parquet已有多少有效数据
import polars as pl
df3 = pl.read_parquet("/home/ubuntu/guiyao_v5/data/market_v5.parquet")
print(f"\n=== V5 market_v5.parquet 现状 ===")
print(f"总行数: {len(df3)}")
print(f"股票数: {df3['code'].n_unique()}")
print(f"日期范围: {df3['date'].min()} ~ {df3['date'].max()}")
print(f"最新日期: {sorted(df3['date'].unique().to_list())[-5:]}")

# 5. 最新交易日是否有210池数据
from datetime import datetime
today = datetime.now()
latest = sorted(df3['date'].unique().to_list())[-1]
latest_df = df3.filter(pl.col('date') == latest)
print(f"\n最新交易日({latest})数据量: {len(latest_df)} 行")
print(f"代码示例: {latest_df['code'].to_list()[:5]}")

# 6. ETF数据状况
df4 = pl.read_parquet("/home/ubuntu/guiyao_v5/data/etf_v5.parquet")
print(f"\n=== ETF parquet 现状 ===")
print(f"总行数: {len(df4)}")
print(f"ETF数: {df4['code'].n_unique()}")
print(f"日期范围: {df4['date'].min()} ~ {df4['date'].max()}")
latest_etf = sorted(df4['date'].unique().to_list())[-1]
print(f"最新ETF数据: {latest_etf}")
