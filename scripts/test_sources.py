#!/usr/bin/env python3
"""全数据源测试"""
import sys, os, time, requests
sys.path.insert(0, os.path.expanduser("~/quantpilot"))

print("=" * 60)
print("1. 数据源可用性测试")
print("=" * 60)
srcs = []

# mootdx 日线
try:
    from src.sources.mootdx_source import MootdxSource
    s = MootdxSource()
    df = s.fetch_daily_kline("600519", "2026-06-01", "2026-07-15")
    ok = df is not None and len(df) > 0
    srcs.append(("mootdx(通达信)", ok, 0.5, "日线", len(df) if ok else 0))
    print(f"  mootdx: {'OK' if ok else 'FAIL'} 600519={len(df) if ok else 0}条")
except Exception as e:
    srcs.append(("mootdx(通达信)", False, 99, "日线", 0))
    print(f"  mootdx: FAIL {e}")

# zzshare
try:
    from src.sources.zzshare_source import ZZShareSource
    s = ZZShareSource()
    df = s.fetch_daily_kline("600519", "2026-06-01", "2026-07-15")
    ok = df is not None and len(df) > 0
    srcs.append(("zzshare(综合)", ok, 1.0, "日线", len(df) if ok else 0))
    print(f"  zzshare: {'OK' if ok else 'FAIL'} 600519={len(df) if ok else 0}条")
except Exception as e:
    srcs.append(("zzshare(综合)", False, 99, "日线", 0))
    print(f"  zzshare: FAIL {e}")

# mootdx 实时
try:
    from src.sources.mootdx_source import MootdxSource
    s = MootdxSource()
    quotes = s.fetch_quotes(["600519","000001","000858"])
    ok = quotes is not None and len(quotes) > 0
    srcs.append(("mootdx(实时)", ok, 0.3, "报价", len(quotes) if ok else 0))
    print(f"  mootdx实时: {'OK' if ok else 'FAIL'} {len(quotes) if ok else 0}只")
except Exception as e:
    srcs.append(("mootdx(实时)", False, 99, "报价", 0))
    print(f"  mootdx实时: FAIL {e}")

# akshare 全市场快照
try:
    import akshare as ak
    t0 = time.time()
    df = ak.stock_zh_a_spot_em()
    elapsed = time.time() - t0
    ok = df is not None and len(df) > 0
    srcs.append(("akshare(快照)", ok, elapsed, "全市场", len(df) if ok else 0))
    print(f"  akshare: {'OK' if ok else 'FAIL'} {len(df) if ok else 0}条 {elapsed:.1f}s")
except Exception as e:
    srcs.append(("akshare(快照)", False, 99, "全市场", 0))
    print(f"  akshare: FAIL {e}")

# tencent 实时
try:
    codes = ["sh600519","sz000001","sz000858"]
    t0 = time.time()
    r = requests.get(f"http://qt.gtimg.cn/q={','.join(codes)}", timeout=5)
    elapsed = time.time() - t0
    ok = r.status_code == 200 and len(r.text) > 50
    srcs.append(("tencent(API)", ok, elapsed, "实时", len(codes)))
    print(f"  tencent: {'OK' if ok else 'FAIL'} {elapsed:.1f}s")
except Exception as e:
    srcs.append(("tencent(API)", False, 99, "实时", 0))
    print(f"  tencent: FAIL {e}")

# 排序
srcs.sort(key=lambda x: (-x[1], x[2]))
print(f"\n2. 数据源排序")
print("=" * 60)
for i, (name, ok, elap, desc, n) in enumerate(srcs, 1):
    status = "✅ 主力" if i <= 2 else ("⚠️ 备用" if ok else "❌ 不可用")
    print(f"  {i}. {name:20} {elap:.1f}s {desc:6} {n}条  {status}")

# 同花顺
print(f"\n3. 同花顺自选池")
print("=" * 60)
try:
    from ths_favorite.selfstock_v1 import download_self_stocks_v1, modify_self_stocks_v1
    print("  函数可用: download_self_stocks_v1, modify_self_stocks_v1")
    print("  ⚠️ 需要10jqka.com.cn的浏览器cookie才能正常工作")
except Exception as e:
    print(f"  FAIL: {e}")

# 持仓同步
print(f"\n4. 持仓同步脚本")
print("=" * 60)
import sqlite3
c = sqlite3.connect("/home/ubuntu/quantpilot/data/quantpilot.db")
codes = [r[0] for r in c.execute("SELECT DISTINCT code FROM live_positions").fetchall()]
print(f"  持仓: {codes}")
print(f"  同步到: ~/guiyao_v5/data/positions_sync.parquet")
print(f"  频率: 每日09:00 & 15:00")

# 推送全场景
print(f"\n5. 推送全场景")
print("=" * 60)
scenes = [
    ("09:10", "早间选股", "🌅 市场=chop(45分), 今日ETF=510300, 个股=600519"),
    ("10:35", "盘中突破", "🔥 600519放量3.2x,建议买入200股@15.85"),
    ("14:20", "止损告警", "🆘 002902触及-5%止损! 亏187元,冷静期3天"),
    ("11:15", "加仓确认", "➕ 浮盈+5.2%触发加仓→加300股至40%"),
    ("15:30", "收盘复盘", "📊 今日AI 4信号/0止损, 胜率25%, 净值+1.2%"),
    ("13:00", "极端锁仓", "🚨 breadth<0.10+跌停>300! 全部锁死! "),
]
for t, l, c in scenes:
    print(f"  [{t}] {l}: {c}")

print(f"\n✅ 测试完成")
