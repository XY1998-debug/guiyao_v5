"""获取10只股票的最近一周1分钟K线数据（CSV输出）"""
import sys, os
import pandas as pd
from datetime import datetime, timedelta
from mootdx.quotes import Quotes

STOCKS = [
    ("600519", "贵州茅台"),
    ("000858", "五粮液"),
    ("600036", "招商银行"),
    ("000001", "平安银行"),
    ("600900", "长江电力"),
    ("002594", "比亚迪"),
    ("300750", "宁德时代"),
    ("601318", "中国平安"),
    ("600887", "伊利股份"),
    ("000333", "美的集团"),
]

client = Quotes.factory(market="std", multithread=True, heartbeat=True)

all_dfs = []
errors = []

for code, name in STOCKS:
    try:
        symbol = f"{code}.SH" if code.startswith("6") else f"{code}.SZ"
        print(f"Fetching {name}({code}) ...", file=sys.stderr)
        bars = client.bars(symbol=symbol, frequency=8, offset=2400)
        if bars is None or (isinstance(bars, pd.DataFrame) and bars.empty):
            print(f"  Empty result", file=sys.stderr)
            errors.append(code)
            continue

        df = bars.copy()
        col_map = {}
        for col in df.columns:
            c = str(col).lower()
            if c in ("date", "datetime"):
                col_map[col] = "trade_date"
            elif c in ("open", "high", "low", "close"):
                col_map[col] = c
            elif c in ("vol", "volume"):
                col_map[col] = "volume"
            elif c == "amount":
                col_map[col] = "amount"
        df = df.rename(columns=col_map)
        df = df[list(col_map.values())].copy()
        df["code"] = code
        df["name"] = name
        df["trade_date"] = df["trade_date"].astype(str)
        for c in ["open", "high", "low", "close", "volume", "amount"]:
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)
        all_dfs.append(df)
        print(f"  OK -> {len(df)} bars", file=sys.stderr)
    except Exception as e:
        print(f"  Error: {e}", file=sys.stderr)
        errors.append(code)

if not all_dfs:
    print("ERROR: No data fetched", file=sys.stderr)
    sys.exit(1)

result = pd.concat(all_dfs, ignore_index=True)

# 过滤最近一周
result["dt"] = pd.to_datetime(result["trade_date"])
latest = result["dt"].max()
week_ago = latest - timedelta(days=7)
result = result[result["dt"] >= week_ago].copy()
result = result.drop(columns=["dt"])

cols = [c for c in ["code", "name", "trade_date", "open", "high", "low", "close", "volume", "amount"] if c in result.columns]
result = result[cols]
result = result.sort_values(["code", "trade_date"]).reset_index(drop=True)

# 输出到stdout（CSV格式）
result.to_csv(sys.stdout, index=False, encoding="utf-8", float_format="%.2f")

print(f"\n---\nStats: {len(result)} rows, {result['code'].nunique()} stocks, {result['trade_date'].min()} ~ {result['trade_date'].max()}", file=sys.stderr)
if errors:
    print(f"Errors: {errors}", file=sys.stderr)
