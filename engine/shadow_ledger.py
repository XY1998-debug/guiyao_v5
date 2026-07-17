"""
归爻 V5 P0 — 影子账本 (Shadow Ledger)

每日在后台模拟执行所有 AI 信号（15%仓位），不受用户主观过滤影响。
与实盘账本对比，量化"人脑过滤"的价值。

用法：
  from engine.shadow_ledger import ShadowLedger
  ledger = ShadowLedger()
  ledger.log_signal(signal)       # AI 发出信号时调用
  ledger.log_execution(trade)     # 用户实际执行时调用
  report = ledger.weekly_report() # 周日自动生成对比报告
"""

import sqlite3
import json
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional

LEDGER_DB = Path(__file__).parent.parent / "data" / "shadow_ledger.db"


class ShadowLedger:

    def __init__(self, db_path: str = str(LEDGER_DB)):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                signal_time TEXT NOT NULL,
                stock_code TEXT NOT NULL,
                direction TEXT NOT NULL CHECK(direction IN ('buy','sell')),
                trigger_price REAL,
                target_qty INTEGER,
                source TEXT DEFAULT 'AI'  -- 'AI' or 'USER'
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                signal_id INTEGER,
                trade_time TEXT NOT NULL,
                stock_code TEXT NOT NULL,
                direction TEXT NOT NULL,
                price REAL NOT NULL,
                shares INTEGER NOT NULL,
                fee REAL DEFAULT 0,
                source TEXT DEFAULT 'AI',  -- 'SHADOW' or 'USER'
                FOREIGN KEY (signal_id) REFERENCES signals(id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS weekly_report (
                week_start TEXT PRIMARY KEY,
                shadow_pnl REAL,
                shadow_trades INTEGER,
                user_pnl REAL,
                user_trades INTEGER,
                verdict TEXT  -- 'USER_WINS', 'AI_WINS', 'TIE'
            )
        """)
        conn.commit()
        conn.close()

    def log_ai_signal(self, stock: str, direction: str, price: float, qty: int) -> int:
        """AI 发出买入/卖出信号时记录"""
        conn = sqlite3.connect(self.db_path)
        cur = conn.execute(
            "INSERT INTO signals (signal_time, stock_code, direction, trigger_price, target_qty, source) VALUES (?,?,?,?,?,'AI')",
            (datetime.now().isoformat(), stock, direction, price, qty)
        )
        conn.commit()
        signal_id = cur.lastrowid
        conn.close()
        return signal_id

    def log_shadow_execution(self, signal_id: int, stock: str, direction: str,
                              price: float, shares: int, fee: float = 0):
        """AI 模拟执行（影子账本）"""
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO trades (signal_id, trade_time, stock_code, direction, price, shares, fee, source) VALUES (?,?,?,?,?,?,?,'SHADOW')",
            (signal_id, datetime.now().isoformat(), stock, direction, price, shares, fee)
        )
        conn.commit()
        conn.close()

    def log_user_trade(self, stock: str, direction: str, price: float, shares: int, fee: float = 0):
        """用户实际交易记录（实盘账本）"""
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO trades (trade_time, stock_code, direction, price, shares, fee, source) VALUES (?,?,?,?,?,?,'USER')",
            (datetime.now().isoformat(), stock, direction, price, shares, fee)
        )
        conn.commit()
        conn.close()

    def weekly_report(self) -> str:
        """生成每周对比报告"""
        conn = sqlite3.connect(self.db_path)
        this_week = (datetime.now() - timedelta(days=datetime.now().weekday())).strftime("%Y-%m-%d")

        # 本周影子账本
        shadow = conn.execute("""
            SELECT COUNT(*), COALESCE(SUM(t.shares * t.price * (1 - 0.0013)), 0)
            FROM trades t WHERE t.source='SHADOW' AND t.trade_time >= ?
        """, (this_week,)).fetchone()

        # 本周实盘
        user = conn.execute("""
            SELECT COUNT(*), COALESCE(SUM(t.shares * t.price * (1 - 0.0013)), 0)
            FROM trades t WHERE t.source='USER' AND t.trade_time >= ?
        """, (this_week,)).fetchone()

        shadow_cnt, shadow_val = shadow
        user_cnt, user_val = user

        verdict = "TIE"
        if shadow_val > user_val and user_cnt > 0:
            verdict = "AI_WINS"
        elif user_val > shadow_val and user_cnt > 0:
            verdict = "USER_WINS"

        # 存储
        conn.execute(
            "INSERT OR REPLACE INTO weekly_report (week_start, shadow_pnl, shadow_trades, user_pnl, user_trades, verdict) VALUES (?,?,?,?,?,?)",
            (this_week, shadow_val, shadow_cnt, user_val, user_cnt, verdict)
        )
        conn.commit()
        conn.close()

        lines = [
            f"\n{'='*50}",
            f"📊 影子账本 vs 实盘账本 — 周报 ({this_week})",
            f"{'='*50}",
        ]
        if shadow_cnt > 0:
            lines.append(f"  AI  模拟: {shadow_cnt} 笔 | 盈亏 {shadow_val:+.0f}")
        else:
            lines.append(f"  AI  模拟: 本周无信号")

        if user_cnt > 0:
            lines.append(f"  用户实盘: {user_cnt} 笔 | 盈亏 {user_val:+.0f}")
        else:
            lines.append(f"  用户实盘: 无执行记录")

        if verdict == "AI_WINS":
            lines.append(f"\n  ⚠️ AI  跑赢实盘，建议下周严格执行 AI 信号。")
        elif verdict == "USER_WINS":
            lines.append(f"\n  ✅ 用户跑赢 AI，主观判断有效。")
        else:
            if user_cnt == 0 and shadow_cnt > 0:
                lines.append(f"\n  ⚠️ 本周全部信号未执行，请检查执行力。")
            elif shadow_cnt == 0:
                lines.append(f"\n  — 本周无有效信号。")

        lines.append("=" * 50)
        return "\n".join(lines)
