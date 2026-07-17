"""
持仓数据定时同步任务
每天 9:00 & 15:00 从 quantpilot.db 读取持仓，通过 mootdx 下载最新日线
"""
import sqlite3, polars as pl
from pathlib import Path
from datetime import datetime, timedelta
from mootdx.quotes import Quotes

DB = Path.home() / "quantpilot" / "data" / "quantpilot.db"
DST = Path.home() / "guiyao_v5" / "data" / "positions_sync.parquet"

def sync_positions():
    DST.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB))
    rows = conn.execute(
        "SELECT code, name, cost, shares, buy_date, current_price FROM live_positions"
    ).fetchall()
    conn.close()

    if not rows:
        print("[持仓同步] 无持仓数据")
        return

    codes = list(set(r[0] for r in rows))
    print(f"[持仓同步] {datetime.now():%H:%M} {len(codes)} 只持仓: {codes}")

    q = Quotes.factory(market="std")
    frames = []
    for code in codes:
        try:
            df = q.bars(symbol=code, frequency=9, start=0, count=100)
            if df is not None and len(df) > 0:
                df = pl.DataFrame({
                    "code": [code] * len(df),
                    "date": df["datetime"].str[:10].tolist(),
                    "open": df["open"].astype(float).tolist(),
                    "high": df["high"].astype(float).tolist(),
                    "low":  df["low"].astype(float).tolist(),
                    "close":df["close"].astype(float).tolist(),
                    "volume": df["volume"].astype(float).tolist(),
                    "amount": df["amount"].astype(float).tolist(),
                })
                frames.append(df)
                print(f"  {code}: {len(df)} 条")
        except Exception as e:
            print(f"  {code}: FAIL - {e}")

    if frames:
        pl.concat(frames).write_parquet(str(DST))
        print(f"  已存储: {DST} ({DST.stat().st_size:,}B)")

if __name__ == "__main__":
    sync_positions()
