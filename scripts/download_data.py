"""
归爻 V5 — 批量下载 A 股日线（mootdx 通达信）
8 线程并行，约 60-90 秒完成 300 只股票
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import time, polars as pl, pandas as pd
from mootdx.quotes import Quotes

DATA_DIR = Path(__file__).parent.parent / "data"
TARGET = 300
THREADS = 8
DAYS = 800


def get_stock_list():
    codes = set()
    for m in range(2):
        try:
            s = Quotes.factory(market="std").stocks(market=m)
            if s is not None:
                for _, r in s.iterrows():
                    c = str(r.get("code", ""))
                    n = r.get("name", "")
                    if len(c) == 6 and c.isdigit() and not n.startswith(("ST","*ST","N","C","U")):
                        if c.startswith(("6","0","3")):
                            codes.add(c)
        except:
            pass
    return sorted(codes)[:TARGET]


def download_one(code):
    """下载单只股票日线并转为标准格式"""
    try:
        df = Quotes.factory(market="std").bars(
            symbol=code, frequency=9, start=0, count=DAYS
        )
        if df is None or len(df) == 0:
            return None

        # mootdx 返回列: open,close,high,low,vol,amount,year,month,day,hour,minute,datetime,volume
        dates = df["datetime"].str.slice(0, 10).values
        data = {
            "code": [code] * len(df),
            "date": dates,
            "open": df["open"].values,
            "high": df["high"].values,
            "low": df["low"].values,
            "close": df["close"].values,
            "volume": df["volume"].values.astype(int),
            "amount": df["amount"].values.astype(int),
        }
        result = pd.DataFrame(data)

        # 跳空剔除：涨跌超过 50% 的极可能是停牌后复牌
        prev = result["close"].shift(1)
        jump = abs(result["close"] / prev - 1) > 0.5
        result = result[~jump]

        return pl.from_pandas(result)
    except Exception:
        return None


def main():
    print("=" * 50)
    print("归爻 V5 — 批量日线下载")
    print("=" * 50)

    print("\n[1] 获取股票列表...")
    codes = get_stock_list()
    print(f"  {len(codes)} 只")

    print(f"\n[2] 下载 ({THREADS} 线程)...")
    batch = codes[:TARGET]
    all_dfs = []
    done = 0
    t0 = time.time()

    with ThreadPoolExecutor(max_workers=THREADS) as ex:
        fs = {ex.submit(download_one, c): c for c in batch}
        for f in as_completed(fs):
            done += 1
            r = f.result()
            if r is not None:
                all_dfs.append(r)
            if done % 50 == 0 or done == len(batch):
                print(f"  {done}/{len(batch)} 成功{len(all_dfs)} {time.time()-t0:.0f}s")

    if all_dfs:
        merged = pl.concat(all_dfs).sort(["code", "date"])
        out = DATA_DIR / "market_v5.parquet"
        merged.write_parquet(out, compression="zstd")
        print(f"\n[3] 保存: {merged['code'].n_unique()}只 {len(merged)}行 {merged['date'].n_unique()}天")
        print(f"  用时: {time.time()-t0:.0f}s")
    else:
        print("\n[3] 失败: 无数据")

    print("完成")


if __name__ == "__main__":
    main()
