"""
归爻 — 全市场数据下载器
==================================
下载全A股日线 + ETF + 行业标签 + 交易日历
数据源: mootdx(通达信) / akshare(东方财富)
输出: H:\归爻\data\market_full.parquet

注意: 建议交易时段 (9:30-15:00) 运行，非交易时段可能连不上。

用法: 双击 run.bat → 选 5 → 回车
     或: .venv\Scripts\python.exe scripts\download_full_market.py
"""

import os, time, sys, warnings
from pathlib import Path
sys.path.insert(0, r"H:\归爻")
warnings.filterwarnings("ignore")

DATA = Path(r"H:\归爻\data")
DATA.mkdir(parents=True, exist_ok=True)

def step(n, label):
    print(f"\n[{n}/5] {label}")
    print("-" * 40)

# ── 辅助: 已有数据统计 ──
import polars as pl
existing = list(DATA.glob("*.parquet"))
print(f"已存数据: {len(existing)} 个 parquet 文件")
for f in existing:
    try:
        df = pl.read_parquet(f)
        print(f"  {f.name}: {len(df)} 行, {df.columns}")
    except:
        print(f"  {f.name}: (无法读取)")

# ── 1. 扩展股票池（从210→全市场）──
step(1, "获取全A股列表 + 扩池")
from mootdx.quotes import Quotes
client = Quotes.factory(market="std")
all_codes = []
for m in [0, 1]:
    for s in client.stocks(market=m):
        c = s.get("code", "")
        if c and c.startswith(("000","001","002","003","300","600","601","603","605","688")):
            all_codes.append(c)
print(f"  全市场: {len(all_codes)} 只股票")
with open(DATA / "all_codes.txt", "w") as f:
    f.write("\n".join(sorted(set(all_codes))))

# ── 2. 增量下载日线 ──
step(2, "下载日线 K 线（增量续传）")
import pandas as pd
codes = sorted(set(all_codes))
target = len(codes)

# 检查已有数据，跳过已下载的
done_file = DATA / "market_full.parquet"
done_codes = set()
if done_file.exists():
    try:
        done_codes = set(pl.read_parquet(done_file)["code"].unique().to_list())
        print(f"  已有 {len(done_codes)} 只股票，需补 {target - len(done_codes)} 只")
    except:
        pass

todo = [c for c in codes if c not in done_codes]
chunks = []
failed = 0
for i, code in enumerate(todo):
    try:
        df = client.bars(symbol=code, frequency=9, offset=0, start=0, count=2000)
        if df is not None and len(df) > 0:
            df["code"] = code
            chunks.append(df)
    except:
        failed += 1
    if (i + 1) % 100 == 0:
        print(f"  进度: {i+1}/{len(todo)} (失败{failed})", end="\r")
print(f"\n  本次新增: {len(chunks)} 只, 失败 {failed}")

if chunks:
    new = pd.concat(chunks, ignore_index=True)
    new.columns = [c.lower() for c in new.columns]
    new = new.rename(columns={"vol": "volume"})
    if done_file.exists() and done_codes:
        old = pl.read_parquet(done_file)
        merged = pl.concat([old, pl.from_pandas(new)], how="diagonal_relaxed")
        merged.unique(subset=["code", "date"]).sort(["code", "date"]).write_parquet(done_file)
    else:
        new.to_parquet(done_file)
    print(f"  合并后: {pl.read_parquet(done_file).height} 行, {pl.read_parquet(done_file)['code'].n_unique()} 只")

# ── 3. 交易日历 ──
step(3, "交易日历")
if done_file.exists():
    df = pl.read_parquet(done_file)
    dates = sorted(df["date"].unique().to_list())
    cal = pl.DataFrame({"trade_date": dates, "is_trading_day": 1})
    cal.write_parquet(DATA / "trading_calendar.parquet")
    print(f"  {len(dates)} 天 ({dates[0]} → {dates[-1]})")

# ── 4. 行业标签 ──
step(4, "行业标签（akshare）")
try:
    import akshare as ak
    indu = ak.stock_board_industry_name_em()
    indu.to_parquet(DATA / "industry_list.parquet")
    print(f"  {len(indu)} 个行业")

    tags = []
    for board in indu["board_name"].tolist()[:60]:
        try:
            m = ak.stock_board_industry_cons_em(symbol=board)
            for _, r in m.iterrows():
                tags.append({"code": r["代码"][2:], "industry": board})
            time.sleep(0.2)
        except:
            pass
    if tags:
        tag_df = pl.DataFrame(tags).unique()
        tag_df.write_parquet(DATA / "stock_tags_full.parquet")
        print(f"  {len(tag_df)} 条标签")
except Exception as e:
    print(f"  ❌ 下载失败: {e}（可非交易时段重试）")

# ── 5. ETF 全量 ──
step(5, "ETF 日线")
try:
    from mootdx.quotes import Quotes
    client = Quotes.factory(market="std")
    etf_list = []
    for m in [0, 1]:
        for s in client.stocks(market=m):
            c = s.get("code", "")
            if c.startswith(("51", "15", "16", "18", "56", "58")):
                etf_list.append(c)
    print(f"  ETF 数量: {len(etf_list)}")

    etf_chunks = []
    for code in etf_list:
        try:
            df = client.bars(symbol=code, frequency=9, offset=0, start=0, count=2000)
            if df is not None and len(df) > 0:
                df["code"] = code
                etf_chunks.append(df)
        except:
            pass
        if len(etf_chunks) % 50 == 0 and etf_chunks:
            print(f"  已下载 {len(etf_chunks)} 只", end="\r")
    if etf_chunks:
        e = pd.concat(etf_chunks, ignore_index=True)
        e.columns = [c.lower() for c in e.columns]
        e = e.rename(columns={"vol": "volume"})
        e.to_parquet(DATA / "etf_full.parquet")
        print(f"\n  ETF 完成: {len(e)} 行, {e['code'].nunique()} 只")
except Exception as ex:
    print(f"  ❌ ETF 下载失败: {ex}")

print("\n" + "=" * 50)
print("完成！数据在 H:\\归爻\\data\\")
print("=" * 50)
