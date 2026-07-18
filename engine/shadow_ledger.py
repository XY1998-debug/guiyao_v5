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
        conn.execute("""CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                signal_time TEXT NOT NULL,
                stock_code TEXT NOT NULL,
                direction TEXT NOT NULL CHECK(direction IN ('buy','sell')),
                trigger_price REAL, target_qty INTEGER,
                source TEXT DEFAULT 'AI'
            )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                signal_id INTEGER, trade_time TEXT NOT NULL,
                stock_code TEXT NOT NULL, direction TEXT NOT NULL,
                price REAL NOT NULL, shares INTEGER NOT NULL,
                fee REAL DEFAULT 0, pnl REAL DEFAULT 0,
                source TEXT DEFAULT 'AI',
                FOREIGN KEY (signal_id) REFERENCES signals(id)
            )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS weekly_report (
                week_start TEXT PRIMARY KEY,
                shadow_pnl REAL, shadow_trades INTEGER,
                shadow_closed_pnl REAL,
                user_pnl REAL, user_trades INTEGER,
                user_closed_pnl REAL,
                verdict TEXT
            )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS positions (
                stock_code TEXT NOT NULL, source TEXT NOT NULL,
                buy_time TEXT, buy_price REAL, shares INTEGER,
                PRIMARY KEY (stock_code, source)
            )""")
        conn.commit(); conn.close()

    # ── 记录 API ──

    def log_ai_signal(self, stock: str, direction: str, price: float, qty: int) -> int:
        conn = sqlite3.connect(self.db_path)
        cur = conn.execute(
            "INSERT INTO signals (signal_time, stock_code, direction, trigger_price, target_qty, source) VALUES (?,?,?,?,?,'AI')",
            (datetime.now().isoformat(), stock, direction, price, qty))
        conn.commit(); conn.close()
        return cur.lastrowid

    def log_shadow_execution(self, signal_id: int, stock: str, direction: str,
                              price: float, shares: int, fee: float = 0):
        conn = sqlite3.connect(self.db_path)
        pnl = self._calc_pnl(stock, "SHADOW", direction, price, shares, fee)
        conn.execute(
            "INSERT INTO trades (signal_id, trade_time, stock_code, direction, price, shares, fee, pnl, source) VALUES (?,?,?,?,?,?,?,?,'SHADOW')",
            (signal_id, datetime.now().isoformat(), stock, direction, price, shares, fee, pnl))
        self._update_position(conn, stock, "SHADOW", direction, price, shares)
        conn.commit(); conn.close()

    def log_user_trade(self, stock: str, direction: str, price: float, shares: int, fee: float = 0):
        conn = sqlite3.connect(self.db_path)
        pnl = self._calc_pnl(stock, "USER", direction, price, shares, fee)
        conn.execute(
            "INSERT INTO trades (trade_time, stock_code, direction, price, shares, fee, pnl, source) VALUES (?,?,?,?,?,?,?,'USER')",
            (datetime.now().isoformat(), stock, direction, price, shares, fee, pnl))
        self._update_position(conn, stock, "USER", direction, price, shares)
        conn.commit(); conn.close()

    # ── PnL & 持仓管理 ──

    def _calc_pnl(self, stock: str, source: str, direction: str,
                  price: float, shares: int, fee: float) -> float:
        """卖出时计算盈亏（FIFO），买入返回 0"""
        if direction == "buy":
            return 0.0
        conn = sqlite3.connect(self.db_path)
        row = conn.execute(
            "SELECT buy_price, shares FROM positions WHERE stock_code=? AND source=?",
            (stock, source)).fetchone()
        conn.close()
        if not row:
            return 0.0
        buy_price, held = row
        matched = min(shares, held)
        # 卖出盈亏 = (卖出价 - 买入价) × 股数 - 买入佣金 - 卖出佣金 - 印花税
        cost_buy_fee = max(matched * buy_price * 0.00008, 5.0)
        cost_sell_fee = max(matched * price * 0.00108, 5.0)
        return (price - buy_price) * matched - cost_buy_fee - cost_sell_fee

    def _update_position(self, conn, stock: str, source: str,
                          direction: str, price: float, shares: int):
        if direction == "buy":
            conn.execute(
                "INSERT OR REPLACE INTO positions (stock_code, source, buy_time, buy_price, shares) VALUES (?,?,?,?,?)",
                (stock, source, datetime.now().isoformat(), price,
                 shares + self._get_held(stock, source)))
        else:
            held = self._get_held(stock, source) - shares
            if held <= 0:
                conn.execute("DELETE FROM positions WHERE stock_code=? AND source=?", (stock, source))
            else:
                conn.execute(
                    "UPDATE positions SET shares=? WHERE stock_code=? AND source=?",
                    (held, stock, source))

    def _get_held(self, stock: str, source: str) -> int:
        conn = sqlite3.connect(self.db_path)
        row = conn.execute(
            "SELECT COALESCE(SUM(shares), 0) FROM positions WHERE stock_code=? AND source=?",
            (stock, source)).fetchone()
        conn.close()
        return row[0] if row else 0

    # ── 周报 ──

    def weekly_report(self) -> str:
        conn = sqlite3.connect(self.db_path)
        this_week = (datetime.now() - timedelta(days=datetime.now().weekday())).strftime("%Y-%m-%d")

        def stats(source: str):
            cnt, val, pnl = conn.execute("""
                SELECT COUNT(*), COALESCE(SUM(price * shares), 0),
                       COALESCE(SUM(pnl), 0)
                FROM trades WHERE source=? AND trade_time >= ?
            """, (source, this_week)).fetchone()
            return cnt, val, pnl

        sc, sv, sp = stats("SHADOW")
        uc, uv, up = stats("USER")

        verdict = "TIE"
        if sp > up and uc > 0: verdict = "AI_WINS"
        elif up > sp and uc > 0: verdict = "USER_WINS"

        conn.execute(
            "INSERT OR REPLACE INTO weekly_report (week_start, shadow_pnl, shadow_trades, shadow_closed_pnl, user_pnl, user_trades, user_closed_pnl, verdict) VALUES (?,?,?,?,?,?,?,?)",
            (this_week, sv, sc, sp, uv, uc, up, verdict))
        conn.commit(); conn.close()

        lines = [f"\n{'='*50}", f"📊 影子账本 vs 实盘 — 周报 ({this_week})", "="*50]
        if sc > 0:
            lines.append(f"  AI  模拟: {sc} 笔 | 已平仓盈亏 {sp:+.0f} | 成交额 {sv:,.0f}")
        else:
            lines.append(f"  AI  模拟: 本周无信号")
        if uc > 0:
            lines.append(f"  用户实盘: {uc} 笔 | 已平仓盈亏 {up:+.0f} | 成交额 {uv:,.0f}")
        else:
            lines.append(f"  用户实盘: 无执行记录")

        if verdict == "AI_WINS": lines.append(f"\n  ⚠️ AI 跑赢实盘，建议严格执行 AI 信号。")
        elif verdict == "USER_WINS": lines.append(f"\n  ✅ 用户跑赢 AI，主观判断有效。")
        elif uc == 0 and sc > 0: lines.append(f"\n  ⚠️ 本周信号未执行，请检查执行力。")
        elif sc == 0: lines.append(f"\n  — 本周无信号。")
        lines.append("=" * 50)
        return "\n".join(lines)
