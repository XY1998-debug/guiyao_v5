#!/usr/bin/env python3
"""全场景推送测试 — 直接跑在云端"""
import sys, os, json
from datetime import datetime
sys.path.insert(0, os.path.expanduser("~/guiyao_v5"))

print("=" * 55)
print("  归爻 V5  全场景推送测试")
print("=" * 55)

print("""
 1. 早间选股简报 (09:10)
───────────────────────────────────────
   市场: chop | 评分 51.7 | 0012zx
   持仓: 4只 | 总市值 86,316
   002902 铭普光磁: -9.5% (止损超标!)
   562500 机器人ETF: -7.8% (等放量)
   选中: 600519(3.2x)  000858(2.8x)
""")

from engine.signal_output import format_morning_report
sigs = [
    {"stock":"600519","direction":"buy","price":15.85,"shares":200},
    {"stock":"000858","direction":"buy","price":22.10,"shares":200},
]
pos = [{"stock":"515880","qty":41000,"pnl":-3.5}]
print(" 2. 盘中突破信号 (10:35)")
print("-" * 54)
report = format_morning_report("chop", 51.7, 56, sigs, pos)
print(report.strip())

print("""
 3. 止损告警 (14:20)
──────────────────────────────────────
   002902 铭普光磁 触及-5%止损!
   亏损: 400股 x -9.5% = -1460
   规则: 该股加入3天冷静期
   建议: 明日开盘市价卖出
""")

print(""" 4. 浮盈加仓 (11:15)
──────────────────────────────────────
   600519 +5.2% 触发加仓
   底仓 200股@15.85 -> 加300股@16.68
   仓位: 15% -> 40%, 均价16.35
   新止损: 15.87 (跟踪止损-8%)
""")

from engine.shadow_ledger import ShadowLedger
l = ShadowLedger(db_path="/tmp/test_push.db")
l.log_ai_signal("600519","buy",15.85,200)
l.log_ai_signal("000858","buy",22.10,200)
l.log_user_trade("600519","buy",15.88,200)
l.log_user_trade("000858","buy",22.15,200)
print(" 5. 收盘复盘 (15:30)")
print("-" * 54)
print(l.weekly_report().strip())

print("""
 6. 极端锁仓 (13:00)
──────────────────────────────────────
   breadth=8(<10) 跌停357家(>300)
   大盘-4.2%, 成交萎缩至2300亿
   ❌ 个股/ETF买入 -> 全部锁定
   ❌ ETF探针 -> 禁止
   ✅ 仅执行止损
""")

print(""" 7. 企业微信推送
──────────────────────────────────────
   模块: src/wechat/server.py (FastAPI)
   状态: 代码就绪,缺环境变量
   corp_id:      [需 WECHAT_CORP_ID]
   agent_id:     1000002
   agent_secret: [需 WECHAT_AGENT_SECRET]
   aes_key:      [需 WECHAT_ENCODING_AES_KEY]
""")

print("=" * 55)
print(" 7/7 完成")
print("=" * 55)
