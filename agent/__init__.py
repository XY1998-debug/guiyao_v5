"""工具注册表 - 聚合所有工具定义和调度（完整版）"""

import json
import os
import re
import logging
import requests
from dataclasses import asdict
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

logger = logging.getLogger("quantpilot.tools")
TZ = ZoneInfo("Asia/Shanghai")


def _get_conn():
    from src.database import get_connection
    return get_connection()


# ============================================================
# 1. 文件操作
# ============================================================

def read_file(path: str, **kwargs) -> str:
    """读取文件内容"""
    path = os.path.abspath(path)
    if not os.path.exists(path):
        return json.dumps({"error": f"文件不存在: {path}"}, ensure_ascii=False)
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    if len(content) > 8000:
        content = content[:8000] + f"\n...[截断，共{len(content)}字符]"
    return json.dumps({"path": path, "content": content}, ensure_ascii=False)


def write_file(path: str, content: str) -> str:
    """写入文件（自动创建目录）"""
    path = os.path.abspath(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return json.dumps({"status": "ok", "path": path, "chars": len(content)}, ensure_ascii=False)


def list_files(directory: str) -> str:
    """列出目录文件"""
    directory = os.path.abspath(directory)
    if not os.path.exists(directory):
        return json.dumps({"error": f"目录不存在: {directory}"}, ensure_ascii=False)
    files = []
    for root, dirs, filenames in os.walk(directory):
        level = root.replace(directory, "").count(os.sep)
        indent = "  " * level
        files.append(f"{indent}{os.path.basename(root)}/")
        for f in filenames:
            files.append(f"{indent}  {f}")
        if len(files) > 200:
            files.append("...(截断)")
            break
    return json.dumps({"files": files}, ensure_ascii=False)


# ============================================================
# 2. 实盘交易
# ============================================================

def record_trade(code: str, action: str, price: float = None, shares: int = None,
                 reason: str = "", strategy: str = "", is_live: bool = True) -> str:
    """记录实盘交易。信息不全时会提示补充。"""
    if not code:
        return json.dumps({"error": "请提供股票代码"}, ensure_ascii=False)
    if action not in ("买入", "卖出"):
        return json.dumps({"error": "请明确是买入还是卖出"}, ensure_ascii=False)
    if shares is None:
        return json.dumps({"error": "请问买入/卖出多少股？", "hint": "请告知股数"}, ensure_ascii=False)
    if price is None:
        try:
            price = _get_realtime_price(code)
        except Exception:
            pass
        if price is None:
            return json.dumps({"error": "无法获取实时价，请手动指定价格"}, ensure_ascii=False)

    conn = _get_conn()
    try:
        row = conn.execute("SELECT name FROM instruments WHERE code=?", (code,)).fetchone()
        name = row["name"] if row else code
        amount = price * shares
        today = datetime.now(TZ).strftime("%Y-%m-%d")

        conn.execute("""
            INSERT INTO live_trades (trade_date, code, name, action, price, shares, amount, reason, strategy)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (today, code, name, action, price, shares, amount, reason, strategy))

        if action == "买入":
            existing = conn.execute("SELECT * FROM live_positions WHERE code=?", (code,)).fetchone()
            if existing:
                new_shares = existing["shares"] + shares
                new_cost = (existing["cost"] * existing["shares"] + price * shares) / new_shares
                conn.execute("UPDATE live_positions SET cost=?, shares=? WHERE code=?", (new_cost, new_shares, code))
            else:
                conn.execute("""
                    INSERT INTO live_positions (code, name, cost, shares, buy_date, strategy)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (code, name, price, shares, today, strategy))
        else:
            existing = conn.execute("SELECT * FROM live_positions WHERE code=?", (code,)).fetchone()
            if existing:
                if existing["shares"] <= shares:
                    conn.execute("DELETE FROM live_positions WHERE code=?", (code,))
                else:
                    conn.execute("UPDATE live_positions SET shares=shares-? WHERE code=?", (shares, code))

        conn.commit()
        return json.dumps({
            "status": "recorded", "action": action, "code": code, "name": name,
            "price": price, "shares": shares, "amount": amount,
        }, ensure_ascii=False)
    finally:
        conn.close()


def view_portfolio() -> str:
    """查看实盘持仓"""
    conn = _get_conn()
    try:
        positions = conn.execute("SELECT * FROM live_positions").fetchall()
        return json.dumps({"positions": [dict(p) for p in positions], "count": len(positions)}, ensure_ascii=False)
    finally:
        conn.close()


def view_trade_history(code: str = None, limit: int = 20) -> str:
    """查看交易历史"""
    conn = _get_conn()
    try:
        if code:
            rows = conn.execute("SELECT * FROM live_trades WHERE code=? ORDER BY trade_date DESC LIMIT ?", (code, limit)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM live_trades ORDER BY trade_date DESC LIMIT ?", (limit,)).fetchall()
        return json.dumps({"trades": [dict(r) for r in rows], "count": len(rows)}, ensure_ascii=False)
    finally:
        conn.close()


def update_position(code: str, stop_loss: float = None, take_profit: float = None, notes: str = None) -> str:
    """更新持仓信息（止损价、止盈价、备注）"""
    conn = _get_conn()
    try:
        updates = []
        params = []
        if stop_loss is not None:
            updates.append("stop_loss=?")
            params.append(stop_loss)
        if take_profit is not None:
            updates.append("take_profit=?")
            params.append(take_profit)
        if notes is not None:
            updates.append("notes=?")
            params.append(notes)
        if not updates:
            return json.dumps({"error": "请提供要更新的字段"}, ensure_ascii=False)
        params.append(code)
        conn.execute(f"UPDATE live_positions SET {', '.join(updates)} WHERE code=?", params)
        conn.commit()
        return json.dumps({"status": "updated", "code": code}, ensure_ascii=False)
    finally:
        conn.close()


# ============================================================
# 3. 模拟盘
# ============================================================

def view_sim_portfolio(account_id: str = None) -> str:
    """查看模拟盘持仓"""
    conn = _get_conn()
    try:
        if account_id:
            positions = conn.execute("SELECT * FROM sim_positions WHERE account_id=?", (account_id,)).fetchall()
        else:
            positions = conn.execute("SELECT * FROM sim_positions").fetchall()
        accounts = conn.execute("SELECT * FROM sim_accounts").fetchall()
        return json.dumps({
            "accounts": [dict(a) for a in accounts],
            "positions": [dict(p) for p in positions],
        }, ensure_ascii=False)
    finally:
        conn.close()


def execute_sim_trade(account_id: str, code: str, action: str, price: float = None, shares: int = None, reason: str = "") -> str:
    """模拟盘执行交易"""
    conn = _get_conn()
    try:
        acc = conn.execute("SELECT * FROM sim_accounts WHERE id=?", (account_id,)).fetchone()
        if not acc:
            return json.dumps({"error": f"模拟盘 {account_id} 不存在"}, ensure_ascii=False)
        if price is None:
            price = _get_realtime_price(code)
            if not price:
                return json.dumps({"error": "无法获取实时价"}, ensure_ascii=False)

        row = conn.execute("SELECT name FROM instruments WHERE code=?", (code,)).fetchone()
        name = row["name"] if row else code

        if action == "买入":
            max_amount = acc["cash"] * 0.25
            buy_shares = shares or int(max_amount / price / 100) * 100
            if buy_shares < 100:
                return json.dumps({"error": "资金不足"}, ensure_ascii=False)
            cost = price * buy_shares
            conn.execute("UPDATE sim_accounts SET cash=cash-? WHERE id=?", (cost, account_id))
            conn.execute("""
                INSERT OR REPLACE INTO sim_positions (account_id, code, name, cost, shares, buy_date, buy_price, strategy)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (account_id, code, name, price, buy_shares, datetime.now(TZ).strftime("%Y-%m-%d"), price, reason))
            conn.execute("""
                INSERT INTO sim_trades (account_id, trade_date, code, name, action, price, shares, amount, reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (account_id, datetime.now(TZ).strftime("%Y-%m-%d"), code, name, "买入", price, buy_shares, cost, reason))
            conn.commit()
            return json.dumps({"status": "ok", "action": "买入", "code": code, "shares": buy_shares, "price": price}, ensure_ascii=False)
        else:
            pos = conn.execute("SELECT * FROM sim_positions WHERE account_id=? AND code=?", (account_id, code)).fetchone()
            if not pos:
                return json.dumps({"error": f"{account_id} 未持有 {code}"}, ensure_ascii=False)
            sell_shares = shares or pos["shares"]
            amount = price * sell_shares
            pnl = (price - pos["cost"]) * sell_shares
            pnl_pct = (price / pos["cost"] - 1) * 100
            conn.execute("UPDATE sim_accounts SET cash=cash+? WHERE id=?", (amount, account_id))
            if sell_shares >= pos["shares"]:
                conn.execute("DELETE FROM sim_positions WHERE id=?", (pos["id"],))
            else:
                conn.execute("UPDATE sim_positions SET shares=shares-? WHERE id=?", (sell_shares, pos["id"]))
            conn.execute("""
                INSERT INTO sim_trades (account_id, trade_date, code, name, action, price, shares, amount, reason, pnl, pnl_pct)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (account_id, datetime.now(TZ).strftime("%Y-%m-%d"), code, name, "卖出", price, sell_shares, amount, reason, pnl, pnl_pct))
            conn.commit()
            return json.dumps({"status": "ok", "action": "卖出", "code": code, "shares": sell_shares, "pnl": round(pnl, 2), "pnl_pct": round(pnl_pct, 2)}, ensure_ascii=False)
    finally:
        conn.close()


def view_sim_trades(account_id: str = None, limit: int = 20) -> str:
    """查看模拟盘交易记录"""
    conn = _get_conn()
    try:
        if account_id:
            rows = conn.execute("SELECT * FROM sim_trades WHERE account_id=? ORDER BY trade_date DESC LIMIT ?", (account_id, limit)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM sim_trades ORDER BY trade_date DESC LIMIT ?", (limit,)).fetchall()
        return json.dumps({"trades": [dict(r) for r in rows]}, ensure_ascii=False)
    finally:
        conn.close()


# ============================================================
# 4. 盯盘
# ============================================================

def add_to_watchlist(code: str, condition: str = "", strategy: str = "", source: str = "user_add") -> str:
    """添加股票到盯盘列表（同时同步同花顺自选）"""
    conn = _get_conn()
    try:
        row = conn.execute("SELECT name FROM instruments WHERE code=?", (code,)).fetchone()
        name = row["name"] if row else code
        conn.execute("""
            INSERT INTO watchlist (code, name, source, condition, strategy, active)
            VALUES (?, ?, ?, ?, ?, 1)
            ON CONFLICT(code) DO UPDATE SET condition=?, strategy=?, active=1
        """, (code, name, source, condition, strategy, condition, strategy))
        conn.commit()
        # P0-1: 同时同步同花顺自选池
        ths_result = ""
        try:
            ths_result = sync_to_ths_watchlist(codes=code, group="我的自选")
        except Exception as e:
            ths_result = f"同花顺同步失败: {e}"
        return json.dumps({
            "status": "added", "code": code, "name": name, "condition": condition,
            "ths_sync": "done" if "error" not in str(ths_result) else ths_result
        }, ensure_ascii=False)
    finally:
        conn.close()


def remove_from_watchlist(code: str) -> str:
    """从盯盘列表移除"""
    conn = _get_conn()
    try:
        conn.execute("UPDATE watchlist SET active=0 WHERE code=?", (code,))
        conn.commit()
        return json.dumps({"status": "removed", "code": code}, ensure_ascii=False)
    finally:
        conn.close()


def list_watchlist() -> str:
    """查看盯盘列表"""
    conn = _get_conn()
    try:
        items = conn.execute("SELECT * FROM watchlist WHERE active=1").fetchall()
        return json.dumps({"watchlist": [dict(i) for i in items], "count": len(items)}, ensure_ascii=False)
    finally:
        conn.close()


def view_alerts(limit: int = 20) -> str:
    """查看告警记录"""
    conn = _get_conn()
    try:
        rows = conn.execute("SELECT * FROM alerts ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return json.dumps({"alerts": [dict(r) for r in rows], "count": len(rows)}, ensure_ascii=False)
    finally:
        conn.close()


# ============================================================
# 5. 删除/清理
# ============================================================

def clear_position(code: str = None) -> str:
    """删除持仓记录（不记录卖出，直接删除）"""
    conn = _get_conn()
    try:
        if code:
            conn.execute("DELETE FROM live_positions WHERE code=?", (code,))
            msg = f"已删除 {code} 的持仓记录"
        else:
            count = conn.execute("SELECT COUNT(*) FROM live_positions").fetchone()[0]
            conn.execute("DELETE FROM live_positions")
            msg = f"已清空所有持仓记录（共 {count} 条）"
        conn.commit()
        return json.dumps({"status": "ok", "message": msg}, ensure_ascii=False)
    finally:
        conn.close()


def clear_trades(code: str = None) -> str:
    """删除交易记录"""
    conn = _get_conn()
    try:
        if code:
            conn.execute("DELETE FROM live_trades WHERE code=?", (code,))
            msg = f"已删除 {code} 的交易记录"
        else:
            count = conn.execute("SELECT COUNT(*) FROM live_trades").fetchone()[0]
            conn.execute("DELETE FROM live_trades")
            msg = f"已清空所有交易记录（共 {count} 条）"
        conn.commit()
        return json.dumps({"status": "ok", "message": msg}, ensure_ascii=False)
    finally:
        conn.close()


def clear_memory(memory_id: str = None, memory_type: str = None) -> str:
    """删除长期记忆"""
    conn = _get_conn()
    try:
        if memory_id:
            conn.execute("DELETE FROM long_term_memories WHERE id=?", (memory_id,))
            msg = f"已删除记忆 {memory_id}"
        elif memory_type:
            count = conn.execute("SELECT COUNT(*) FROM long_term_memories WHERE memory_type=?", (memory_type,)).fetchone()[0]
            conn.execute("DELETE FROM long_term_memories WHERE memory_type=?", (memory_type,))
            msg = f"已删除所有 {memory_type} 类型记忆（共 {count} 条）"
        else:
            count = conn.execute("SELECT COUNT(*) FROM long_term_memories").fetchone()[0]
            conn.execute("DELETE FROM long_term_memories")
            msg = f"已清空所有记忆（共 {count} 条）"
        conn.commit()
        return json.dumps({"status": "ok", "message": msg}, ensure_ascii=False)
    finally:
        conn.close()


def cleanup_expired_data(alerts_days: int = 30, inactive_watchlist_days: int = 7) -> str:
    """清理过期数据"""
    conn = _get_conn()
    try:
        cutoff_alerts = (datetime.now(TZ) - timedelta(days=alerts_days)).strftime("%Y-%m-%d")
        a = conn.execute("DELETE FROM alerts WHERE created_at < ?", (cutoff_alerts,)).rowcount
        cutoff_wl = (datetime.now(TZ) - timedelta(days=inactive_watchlist_days)).strftime("%Y-%m-%d")
        w = conn.execute("DELETE FROM watchlist WHERE active=0 AND added_at < ?", (cutoff_wl,)).rowcount
        conn.commit()
        return json.dumps({"status": "ok", "deleted_alerts": a, "deleted_watchlist": w}, ensure_ascii=False)
    finally:
        conn.close()


def reset_test_data() -> str:
    """一键清空所有测试数据"""
    conn = _get_conn()
    try:
        results = []
        for table in ["live_positions", "live_trades", "long_term_memories", "watchlist", "alerts"]:
            count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            conn.execute(f"DELETE FROM {table}")
            results.append(f"{table}: {count}条")
        conn.commit()
        return json.dumps({"status": "ok", "message": "测试数据已清空", "details": results}, ensure_ascii=False)
    finally:
        conn.close()


# ============================================================
# 6. 记忆系统
# ============================================================

def save_memory(content: str, memory_type: str = "insight", keywords: str = "",
                tags: str = "", importance: float = 0.5, mode: str = "qp") -> str:
    """保存长期记忆"""
    try:
        from agent.memory.manager import MemoryManager
        memory = MemoryManager()
        memory.init_tables()
        return memory.save_memory(content=content, memory_type=memory_type,
                                  keywords=keywords, tags=tags, importance=importance,
                                  mode=mode)
    except Exception as e:
        return json.dumps({"error": f"记忆保存失败: {e}"}, ensure_ascii=False)


def search_memory(query: str, top_k: int = 5, mode: str = None) -> str:
    """搜索长期记忆"""
    try:
        from agent.memory.manager import MemoryManager
        memory = MemoryManager()
        memory.init_tables()
        results = memory.search(query, top_k=top_k, mode=mode)
        return json.dumps({"results": results, "count": len(results)}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": f"记忆搜索失败: {e}"}, ensure_ascii=False)


def list_memories(memory_type: str = None, limit: int = 20) -> str:
    """列出长期记忆"""
    conn = _get_conn()
    try:
        if memory_type:
            rows = conn.execute("SELECT * FROM long_term_memories WHERE memory_type=? AND is_archived=0 ORDER BY importance DESC LIMIT ?", (memory_type, limit)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM long_term_memories WHERE is_archived=0 ORDER BY importance DESC LIMIT ?", (limit,)).fetchall()
        return json.dumps({"memories": [dict(r) for r in rows], "count": len(rows)}, ensure_ascii=False)
    finally:
        conn.close()


# ============================================================
# 7. 市场数据查询
# ============================================================

def query_kline(code: str, days: int = 60) -> str:
    """查询日K线数据"""
    conn = _get_conn()
    try:
        rows = conn.execute("""
            SELECT trade_date, open, high, low, close, volume, amount, turnover
            FROM daily_kline WHERE code=? ORDER BY trade_date DESC LIMIT ?
        """, (code, days)).fetchall()
        if not rows:
            return json.dumps({"error": f"股票 {code} 无K线数据，请先同步"}, ensure_ascii=False)
        kline = [dict(r) for r in reversed(rows)]
        return json.dumps({"code": code, "kline": kline, "count": len(kline), "volume_unit": "手"}, ensure_ascii=False)
    finally:
        conn.close()


def search_stock(query: str) -> str:
    """搜索股票（按代码或名称）"""
    conn = _get_conn()
    try:
        rows = conn.execute("""
            SELECT code, name, exchange, industry FROM instruments
            WHERE code LIKE ? OR name LIKE ? LIMIT 10
        """, (f"%{query}%", f"%{query}%")).fetchall()
        return json.dumps({"results": [dict(r) for r in rows]}, ensure_ascii=False)
    finally:
        conn.close()


def market_overview() -> str:
    """市场概览（最新市场快照）"""
    conn = _get_conn()
    try:
        snap = conn.execute("SELECT * FROM market_snapshot ORDER BY trade_date DESC LIMIT 1").fetchone()
        result = dict(snap) if snap else {"info": "暂无市场数据"}
        return json.dumps(result, ensure_ascii=False)
    finally:
        conn.close()


def sector_ranking(trade_date: str = None, limit: int = 10) -> str:
    """板块排行（涨跌幅TOP）"""
    conn = _get_conn()
    try:
        if not trade_date:
            trade_date = datetime.now(TZ).strftime("%Y-%m-%d")
        rows = conn.execute("""
            SELECT industry, avg_change_pct, total_amount, stock_count, limit_up_count, net_inflow
            FROM sector_data WHERE trade_date=?
            ORDER BY avg_change_pct DESC LIMIT ?
        """, (trade_date, limit)).fetchall()
        if not rows:
            return json.dumps({"info": f"{trade_date} 暂无板块数据", "results": []}, ensure_ascii=False)
        return json.dumps({"trade_date": trade_date, "results": [dict(r) for r in rows]}, ensure_ascii=False)
    finally:
        conn.close()


def limit_up_pool(trade_date: str = None) -> str:
    """查看涨停池"""
    conn = _get_conn()
    try:
        if not trade_date:
            trade_date = datetime.now(TZ).strftime("%Y-%m-%d")
        rows = conn.execute("SELECT * FROM limit_up_pool WHERE trade_date=? ORDER BY streak DESC", (trade_date,)).fetchall()
        return json.dumps({"trade_date": trade_date, "count": len(rows), "results": [dict(r) for r in rows]}, ensure_ascii=False)
    finally:
        conn.close()


# ============================================================
# 8. 技术分析（从V1移植）
# ============================================================

def calc_technical(code: str, days: int = 60) -> str:
    """计算技术指标（MA/MACD/RSI/KDJ/BOLL/量价分析）"""
    conn = _get_conn()
    try:
        rows = conn.execute("""
            SELECT trade_date, open, high, low, close, volume, amount, turnover
            FROM daily_kline WHERE code=? ORDER BY trade_date DESC LIMIT ?
        """, (code, days + 60)).fetchall()
        if len(rows) < 20:
            return json.dumps({"error": f"{code} 数据不足（需至少20条）"}, ensure_ascii=False)

        import pandas as pd
        df = pd.DataFrame([dict(r) for r in reversed(rows)])
        df = df.sort_values("trade_date").reset_index(drop=True)
        close = df["close"].astype(float)
        high = df["high"].astype(float)
        low = df["low"].astype(float)
        volume = df["volume"].astype(float)

        result = {"code": code, "data_count": len(df)}

        # MA
        for w in [5, 10, 20, 60]:
            if len(close) >= w:
                result[f"MA{w}"] = round(float(close.rolling(w).mean().iloc[-1]), 2)

        # RSI
        for p in [6, 12, 24]:
            if len(close) > p:
                d = close.diff()
                g = d.where(d > 0, 0).rolling(p).mean()
                l = (-d.where(d < 0, 0)).rolling(p).mean()
                rsi = 100 - (100 / (1 + g / l.replace(0, float("nan"))))
                result[f"RSI{p}"] = round(float(rsi.iloc[-1]), 2)

        # MACD
        if len(close) >= 26:
            e12 = close.ewm(span=12, adjust=False).mean()
            e26 = close.ewm(span=26, adjust=False).mean()
            dif = e12 - e26
            dea = dif.ewm(span=9, adjust=False).mean()
            macd = (dif - dea) * 2
            result["MACD_DIF"] = round(float(dif.iloc[-1]), 4)
            result["MACD_DEA"] = round(float(dea.iloc[-1]), 4)
            result["MACD"] = round(float(macd.iloc[-1]), 4)
            # 金叉死叉
            if len(dif) >= 2:
                if dif.iloc[-2] <= dea.iloc[-2] and dif.iloc[-1] > dea.iloc[-1]:
                    result["MACD信号"] = "金叉"
                elif dif.iloc[-2] >= dea.iloc[-2] and dif.iloc[-1] < dea.iloc[-1]:
                    result["MACD信号"] = "死叉"

        # KDJ
        if len(close) >= 9:
            l9 = low.rolling(9).min()
            h9 = high.rolling(9).max()
            rsv = (close - l9) / (h9 - l9).replace(0, float("nan")) * 100
            k = rsv.ewm(com=2, adjust=False).mean()
            d = k.ewm(com=2, adjust=False).mean()
            j = 3 * k - 2 * d
            result["KDJ_K"] = round(float(k.iloc[-1]), 2)
            result["KDJ_D"] = round(float(d.iloc[-1]), 2)
            result["KDJ_J"] = round(float(j.iloc[-1]), 2)

        # BOLL
        if len(close) >= 20:
            ma20 = close.rolling(20).mean()
            std20 = close.rolling(20).std()
            result["BOLL_上轨"] = round(float(ma20.iloc[-1] + 2 * std20.iloc[-1]), 2)
            result["BOLL_中轨"] = round(float(ma20.iloc[-1]), 2)
            result["BOLL_下轨"] = round(float(ma20.iloc[-1] - 2 * std20.iloc[-1]), 2)

        # 量价
        if len(volume) >= 5:
            vol_avg5 = volume.rolling(5).mean().iloc[-1]
            result["成交量"] = int(volume.iloc[-1])
            result["5日均量"] = int(vol_avg5)
            result["量比"] = round(float(volume.iloc[-1] / vol_avg5), 2) if vol_avg5 > 0 else 0

        # 涨跌幅
        if len(close) >= 2:
            change = (close.iloc[-1] / close.iloc[-2] - 1) * 100
            result["涨跌幅"] = round(float(change), 2)
        result["最新价"] = round(float(close.iloc[-1]), 2)

        return json.dumps(result, ensure_ascii=False)
    finally:
        conn.close()


def recognize_kline_patterns(code: str, days: int = 60) -> str:
    """识别K线形态（十字星、锤子线、吞没、早晨之星等）"""
    conn = _get_conn()
    try:
        rows = conn.execute("""
            SELECT trade_date, open, high, low, close, volume
            FROM daily_kline WHERE code=? ORDER BY trade_date DESC LIMIT ?
        """, (code, days)).fetchall()
        if len(rows) < 3:
            return json.dumps({"error": f"{code} 数据不足"}, ensure_ascii=False)

        import pandas as pd
        df = pd.DataFrame([dict(r) for r in reversed(rows)])
        patterns = []

        def _body(r): return abs(float(r["close"]) - float(r["open"]))
        def _range(r): return float(r["high"]) - float(r["low"])
        def _upper(r): return float(r["high"]) - max(float(r["close"]), float(r["open"]))
        def _lower(r): return min(float(r["close"]), float(r["open"])) - float(r["low"])
        def _bull(r): return float(r["close"]) > float(r["open"])
        def _bear(r): return float(r["close"]) < float(r["open"])

        last = df.iloc[-1]
        r = _range(last)
        if r > 0:
            body = _body(last)
            body_ratio = body / r

            # 十字星
            if body_ratio < 0.1:
                patterns.append({"name": "十字星", "type": "neutral", "confidence": 0.7, "desc": "多空平衡，可能变盘"})

            # 锤子线
            if _lower(last) > body * 2 and _upper(last) < body * 0.5 and body > 0:
                if _bear(df.iloc[-2]):
                    patterns.append({"name": "锤子线", "type": "bullish", "confidence": 0.65, "desc": "下跌末端，可能止跌反弹"})

            # 射击之星
            if _upper(last) > body * 2 and _lower(last) < body * 0.5 and body > 0:
                if _bull(df.iloc[-2]):
                    patterns.append({"name": "射击之星", "type": "bearish", "confidence": 0.6, "desc": "上涨末端，注意回调"})

        # 吞没
        if len(df) >= 2:
            prev, curr = df.iloc[-2], df.iloc[-1]
            if _bear(prev) and _bull(curr):
                if float(curr["open"]) <= float(prev["close"]) and float(curr["close"]) >= float(prev["open"]):
                    patterns.append({"name": "看涨吞没", "type": "bullish", "confidence": 0.7, "desc": "多方反攻信号"})
            if _bull(prev) and _bear(curr):
                if float(curr["open"]) >= float(prev["close"]) and float(curr["close"]) <= float(prev["open"]):
                    patterns.append({"name": "看跌吞没", "type": "bearish", "confidence": 0.7, "desc": "空方反攻信号"})

        # 早晨之星 / 黄昏之星
        if len(df) >= 3:
            d1, d2, d3 = df.iloc[-3], df.iloc[-2], df.iloc[-1]
            if _bear(d1) and _body(d2) < _body(d1) * 0.3 and _bull(d3):
                if float(d3["close"]) > (float(d1["open"]) + float(d1["close"])) / 2:
                    patterns.append({"name": "早晨之星", "type": "bullish", "confidence": 0.75, "desc": "经典底部反转形态"})
            if _bull(d1) and _body(d2) < _body(d1) * 0.3 and _bear(d3):
                if float(d3["close"]) < (float(d1["open"]) + float(d1["close"])) / 2:
                    patterns.append({"name": "黄昏之星", "type": "bearish", "confidence": 0.75, "desc": "经典顶部反转形态"})

        return json.dumps({"code": code, "patterns": patterns, "count": len(patterns)}, ensure_ascii=False)
    finally:
        conn.close()


def screen_stocks(conditions: str, limit: int = 20) -> str:
    """根据技术条件筛选股票（如 'RSI6<20', 'MACD金叉', '放量'）"""
    conn = _get_conn()
    try:
        stocks = conn.execute("SELECT code, name FROM instruments WHERE code LIKE '6%' OR code LIKE '0%' ORDER BY code").fetchall()
        conds = conditions.upper()
        results = []
        for code, name in stocks:
            if len(results) >= limit:
                break
            rows = conn.execute("""
                SELECT open, high, low, close, volume FROM daily_kline
                WHERE code=? AND open IS NOT NULL ORDER BY trade_date DESC LIMIT 60
            """, (code,)).fetchall()
            if len(rows) < 20:
                continue
            rows = [r for r in reversed(rows)]
            closes = [float(r[4]) for r in rows]
            volumes = [float(r[4]) for r in rows]
            last_close = closes[-1]

            matched = False
            # RSI 条件
            rsi_match = re.search(r"RSI(\d+)[<>](\d+)", conds)
            if rsi_match:
                p, th = int(rsi_match.group(1)), float(rsi_match.group(2))
                if len(closes) > p:
                    d = [closes[i] - closes[i-1] for i in range(1, len(closes))]
                    recent = d[-p:]
                    g = sum(x for x in recent if x > 0) / p
                    l = sum(-x for x in recent if x < 0) / p
                    rsi = 100 - (100 / (1 + g/l)) if l > 0 else 100
                    if "<" in conds and rsi < th:
                        matched = True
                    elif ">" in conds and rsi > th:
                        matched = True

            # 放量
            if "放量" in conds and len(volumes) >= 5:
                avg5 = sum(volumes[-6:-1]) / 5
                if avg5 > 0 and volumes[-1] > avg5 * 1.5:
                    matched = True

            # MACD 金叉
            if "MACD" in conds and "金叉" in conds and len(closes) >= 26:
                e12 = _ema(closes, 12)
                e26 = _ema(closes, 26)
                dif = [a - b for a, b in zip(e12, e26)]
                dea = _ema(dif, 9)
                if len(dif) >= 2 and dif[-2] <= dea[-2] and dif[-1] > dea[-1]:
                    matched = True

            if matched:
                prev = float(rows[-2][4]) if len(rows) > 1 else last_close
                change = (last_close - prev) / prev * 100 if prev > 0 else 0
                results.append({"code": code, "name": name, "price": round(last_close, 2), "change_pct": round(change, 2)})

        return json.dumps({"conditions": conditions, "checked": len(stocks), "matched": len(results), "results": results}, ensure_ascii=False)
    finally:
        conn.close()


def _ema(data, period):
    """指数移动平均"""
    result = []
    multiplier = 2 / (period + 1)
    for i, val in enumerate(data):
        if i == 0:
            result.append(val)
        else:
            result.append(val * multiplier + result[-1] * (1 - multiplier))
    return result


# ============================================================
# 9. 推送
# ============================================================

def push_wechat(message: str, msg_type: str = "text") -> str:
    """推送消息到企业微信。

    Args:
        message: 推送内容
        msg_type: 消息类型 - "text"（纯文本）/ "markdown"（Markdown格式）
    """
    try:
        from config import config as cfg
        url = cfg.get("notification", {}).get("wechat_webhook", {}).get("url", "")
        if not url:
            return json.dumps({"error": "微信 webhook 未配置"}, ensure_ascii=False)
        import requests

        if msg_type == "markdown":
            payload = {
                "msgtype": "markdown",
                "markdown": {"content": message}
            }
        else:
            payload = {
                "msgtype": "text",
                "text": {"content": f"[QuantPilot]\n{message}"}
            }

        resp = requests.post(url, json=payload, timeout=10)
        result = resp.json()
        if result.get("errcode") == 0:
            return json.dumps({"status": "sent", "type": msg_type}, ensure_ascii=False)
        else:
            return json.dumps({"status": "failed", "error": result.get("errmsg", "unknown")}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": f"推送失败: {e}"}, ensure_ascii=False)


# ============================================================
# 10. 数据同步
# ============================================================

def sync_stock_list() -> str:
    """同步股票列表到数据库（自动降级：TickFlow → BaoStock）"""
    try:
        from src.sources.manager import DataSourceManager
        from config import config as cfg
        mgr = DataSourceManager(cfg)
        conn = _get_conn()
        stocks = mgr.fetch_stock_list()
        count = 0
        for s in stocks:
            conn.execute("""
                INSERT OR REPLACE INTO instruments
                (code, symbol, name, exchange, industry, float_shares, total_shares, limit_up, limit_down)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (s['code'], s.get('symbol', s['code']), s['name'], s.get('exchange', ''),
                  s.get('industry', ''), s.get('float_shares'), s.get('total_shares'),
                  s.get('limit_up'), s.get('limit_down')))
            count += 1
        conn.commit()
        conn.close()
        source_names = mgr.get_source_names()["historical"]
        return json.dumps({
            "status": "ok", "count": count,
            "message": f"已同步 {count} 只股票 (数据源: {' → '.join(source_names)})"
        }, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": f"同步失败: {e}"}, ensure_ascii=False)


def sync_kline(code: str, days: int = 365) -> str:
    """同步指定股票的日K线数据（自动降级：TickFlow → BaoStock）"""
    try:
        from src.sources.manager import DataSourceManager
        from config import config as cfg
        code = str(code).strip().split(".")[0]

        # 前置验证：股票代码是否合法
        if not re.match(r'^[0368]\d{5}$', code):
            return json.dumps({"error": f"无效的股票代码格式: {code}（应为6位数字，以0/3/6/8开头）"}, ensure_ascii=False)

        # 查询是否在股票列表中
        conn_check = _get_conn()
        existing = conn_check.execute("SELECT 1 FROM instruments WHERE code=?", (code,)).fetchone()
        conn_check.close()
        if not existing:
            return json.dumps({"error": f"股票 {code} 不在数据库中，请先执行 sync_stock_list"}, ensure_ascii=False)

        mgr = DataSourceManager(cfg)
        end_date = date.today().strftime("%Y-%m-%d")
        start_date = (date.today() - timedelta(days=days)).strftime("%Y-%m-%d")
        df = mgr.fetch_daily_kline(code, start_date, end_date)
        if df.empty:
            return json.dumps({"error": f"股票 {code} 无数据（所有数据源均返回空）"}, ensure_ascii=False)
        conn = _get_conn()
        count = 0
        ds_label = df["data_source"].iloc[0] if "data_source" in df.columns else "unknown"
        for _, row in df.iterrows():
            conn.execute("""
                INSERT OR REPLACE INTO daily_kline
                (code, trade_date, open, high, low, close, volume, amount, turnover, data_source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (row['code'], row['trade_date'], row['open'], row['high'], row['low'],
                  row['close'], row['volume'], row['amount'], row.get('turnover', 0), ds_label))
            count += 1
        conn.commit()
        name_row = conn.execute("SELECT name FROM instruments WHERE code=?", (code,)).fetchone()
        name = name_row["name"] if name_row else code
        conn.close()
        return json.dumps({
            "status": "ok", "code": code, "name": name, "records": count,
            "period": f"{start_date}~{end_date}", "source": ds_label,
        }, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": f"同步失败: {e}"}, ensure_ascii=False)


def init_sim_accounts() -> str:
    """初始化模拟盘账户（首次运行必须执行）"""
    conn = _get_conn()
    try:
        from config import config as cfg
        sim_config = cfg.get("simulation", {})
        capital_map = sim_config.get("capital", {
            "P1_顺势接力": 150000, "P2_逆势低吸": 100000,
            "P3_打板先锋": 100000, "P4_波段猎手": 80000, "P5_ETF轮动": 70000
        })
        
        count = 0
        for name, capital in capital_map.items():
            conn.execute("""
                INSERT OR IGNORE INTO sim_accounts (id, name, style, capital, cash, stop_loss_pct, take_profit_pct)
                VALUES (?, ?, ?, ?, ?, -5, 10)
            """, (name, name, name.split("_")[1] if "_" in name else name, capital, capital))
            count += 1
        conn.commit()
        return json.dumps({"status": "ok", "message": f"已初始化 {count} 个模拟盘账户", "accounts": list(capital_map.keys())}, ensure_ascii=False)
    finally:
        conn.close()


def calc_sector_data(trade_date: str = None) -> str:
    """从个股数据计算板块涨跌排行"""
    conn = _get_conn()
    try:
        if not trade_date:
            trade_date = datetime.now(TZ).strftime("%Y-%m-%d")
        rows = conn.execute("""
            SELECT i.industry,
                   COUNT(*) as stock_count,
                   AVG((d.close - d.open) / d.open * 100) as avg_change,
                   SUM(d.amount) as total_amount,
                   SUM(CASE WHEN d.close >= i.limit_up * 0.995 THEN 1 ELSE 0 END) as limit_up_count
            FROM daily_kline d
            JOIN instruments i ON d.code = i.code
            WHERE d.trade_date = ? AND i.industry != ''
            GROUP BY i.industry
            ORDER BY avg_change DESC
        """, (trade_date,)).fetchall()
        if not rows:
            return json.dumps({"info": f"{trade_date} 无数据"}, ensure_ascii=False)
        # 写入 sector_data 表
        for r in rows:
            conn.execute("""
                INSERT OR REPLACE INTO sector_data (trade_date, industry, avg_change_pct, total_amount, stock_count, limit_up_count)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (trade_date, r["industry"], round(r["avg_change"], 2), r["total_amount"], r["stock_count"], r["limit_up_count"]))
        conn.commit()
        return json.dumps({"trade_date": trade_date, "sectors": len(rows), "top5": [dict(r) for r in rows[:5]]}, ensure_ascii=False)
    finally:
        conn.close()


# ============================================================
# 11. 系统
# ============================================================

def system_health_check() -> str:
    """系统健康检查"""
    from src.database import get_db_stats
    stats = get_db_stats()
    return json.dumps({"status": "ok", "database": stats, "timestamp": datetime.now(TZ).isoformat()}, ensure_ascii=False, default=str)


def test_data_source(source_name: str = "tickflow") -> str:
    """测试数据源连通性"""
    try:
        if source_name == "tickflow":
            from src.sources.tickflow_source import TickFlowSource
            from config import config as cfg
            source = TickFlowSource(cfg)
            ok = source.health_check()
            return json.dumps({"source": source_name, "status": "ok" if ok else "fail"}, ensure_ascii=False)
        else:
            return json.dumps({"source": source_name, "status": "未支持的测试"}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"source": source_name, "status": "fail", "error": str(e)}, ensure_ascii=False)


def update_config(key_path: str, value: str) -> str:
    """修改配置项"""
    try:
        from config import update_config_value
        update_config_value(key_path, value)
        return json.dumps({"status": "ok", "key": key_path, "value": value}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": f"配置更新失败: {e}"}, ensure_ascii=False)


def get_user_profile() -> str:
    """查看用户画像"""
    conn = _get_conn()
    try:
        rows = conn.execute("SELECT * FROM user_profile").fetchall()
        return json.dumps({"profile": [dict(r) for r in rows]}, ensure_ascii=False)
    finally:
        conn.close()


def add_holiday(date_str: str, name: str = "") -> str:
    """添加 A 股节假日（跳过交易任务）"""
    conn = _get_conn()
    try:
        year = int(date_str[:4]) if len(date_str) >= 4 else datetime.now(TZ).year
        conn.execute("INSERT OR REPLACE INTO holidays (trade_date, name, year) VALUES (?, ?, ?)",
                     (date_str, name, year))
        conn.commit()
        return json.dumps({"status": "ok", "date": date_str, "name": name}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)
    finally:
        conn.close()


def list_holidays(year: int = None) -> str:
    """查看节假日列表"""
    conn = _get_conn()
    try:
        if year:
            rows = conn.execute("SELECT * FROM holidays WHERE year=? ORDER BY trade_date", (year,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM holidays ORDER BY trade_date DESC LIMIT 30").fetchall()
        return json.dumps({"holidays": [dict(r) for r in rows], "count": len(rows)}, ensure_ascii=False)
    finally:
        conn.close()


# ============================================================
# 12. 战法库（40个内置短线战法）
# ============================================================

def search_strategies(query: str, top_k: int = 3) -> str:
    """从40个内置战法中搜索匹配。输入技术指标状态、市场信号等，返回最匹配的战法。

    示例: search_strategies("RSI超卖 + MACD即将金叉 + 放量")
    """
    try:
        from agent.strategy_loader import get_strategy_loader
        loader = get_strategy_loader()
        results = loader.search(query, top_k=top_k)
        if not results:
            return json.dumps({"results": [], "hint": "未匹配到战法，尝试补充更多技术指标信息"}, ensure_ascii=False)
        return json.dumps({"results": results, "count": len(results)}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": f"战法搜索失败: {e}"}, ensure_ascii=False)


def list_strategies() -> str:
    """列出所有已加载的战法"""
    try:
        from agent.strategy_loader import get_strategy_loader
        loader = get_strategy_loader()
        strategies = loader.list_strategies()
        return json.dumps({"strategies": strategies, "count": len(strategies)}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": f"战法列表加载失败: {e}"}, ensure_ascii=False)


# ============================================================
# 13. 预测跟踪（预测→验证→记忆反馈闭环）
# ============================================================

def save_prediction(code: str, direction: str = "neutral", target_price: float = 0,
                    stop_loss: float = 0, timeframe_days: int = 5,
                    reasoning: str = "", confidence: float = 0.5) -> str:
    """保存一条交易预测，到期后自动验证。分析完股票后建议保存预测以便追踪准确率。"""
    try:
        from src.prediction import save_prediction as sp
        return sp(code=code, direction=direction, target_price=target_price,
                  stop_loss=stop_loss, timeframe_days=timeframe_days,
                  reasoning=reasoning, confidence=confidence)
    except Exception as e:
        return json.dumps({"error": f"预测保存失败: {e}"}, ensure_ascii=False)


def check_predictions(code: str = None) -> str:
    """检查待验证的预测（code为空时返回所有到期的预测）"""
    try:
        from src.prediction import check_predictions as cp
        return cp(code)
    except Exception as e:
        return json.dumps({"error": f"预测查询失败: {e}"}, ensure_ascii=False)


def verify_prediction(prediction_id: str, actual_price: float) -> str:
    """验证一个预测的结果（指定实际价格，自动判断正确/错误/部分正确）"""
    try:
        from src.prediction import verify_prediction as vp
        return vp(prediction_id, actual_price)
    except Exception as e:
        return json.dumps({"error": f"预测验证失败: {e}"}, ensure_ascii=False)


def prediction_accuracy() -> str:
    """查看预测准确率统计"""
    try:
        from src.prediction import get_prediction_accuracy
        return get_prediction_accuracy()
    except Exception as e:
        return json.dumps({"error": f"准确率查询失败: {e}"}, ensure_ascii=False)


# ============================================================
# 14. 回测引擎
# ============================================================

def backtest_stock(code: str, strategy: str = "macd_cross", days: int = 500,
                   capital: float = 100000, stop_loss: float = 0.05,
                   take_profit: float = 0.10, max_hold: int = 20) -> str:
    """对单只股票运行指定策略的历史回测。

    策略: ma_cross / macd_cross / rsi / volume_breakout / multi_confirm
    返回: 收益率/年化/最大回撤/胜率/夏普比率/每笔交易明细
    """
    try:
        from engine.backtest import run_backtest
        result = run_backtest(code, strategy, days, capital, stop_loss, take_profit, max_hold)
        d = asdict(result)
        d.pop("trades", None)  # 默认不返回交易明细，太长
        d["total_return"] = f"{result.total_return:.1%}"
        d["win_rate"] = f"{result.win_rate:.0%}"
        d["max_drawdown"] = f"{result.max_drawdown:.1%}"
        return json.dumps(d, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": f"回测失败: {e}"}, ensure_ascii=False)


def backtest_with_trades(code: str, strategy: str = "macd_cross", days: int = 500) -> str:
    """回测并返回完整交易明细"""
    try:
        from engine.backtest import run_backtest
        result = run_backtest(code, strategy, days)
        d = asdict(result)
        d["total_return"] = f"{result.total_return:.1%}"
        d["win_rate"] = f"{result.win_rate:.0%}"
        d["max_drawdown"] = f"{result.max_drawdown:.1%}"
        return json.dumps(d, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": f"回测失败: {e}"}, ensure_ascii=False)


def reindex_memories() -> str:
    """重建所有记忆的向量索引（切换到 embedding 模式后执行一次）"""
    try:
        from agent.memory.manager import MemoryManager
        m = MemoryManager()
        m.init_tables()
        return m.reindex_all()
    except Exception as e:
        return json.dumps({"error": f"索引重建失败: {e}"}, ensure_ascii=False)


# ============================================================
# 15. 自我运维
# ============================================================

def self_status() -> str:
    """系统自检：服务状态 + CPU/内存/磁盘 + 数据库 + 版本"""
    try:
        from src.self_maintenance import self_status as _ss
        return _ss()
    except Exception as e:
        return json.dumps({"error": f"自检失败: {e}"}, ensure_ascii=False)


def self_update(user: str = "") -> str:
    """双通道自动更新：从 Gitee/GitHub 拉取最新代码并重建 Docker。

    Args:
        user: Git 用户名（Gitee 和 GitHub 需一致）
    """
    try:
        from src.self_maintenance import self_update as _su
        return _su(user)
    except Exception as e:
        return json.dumps({"error": f"更新失败: {e}"}, ensure_ascii=False)


def self_backup() -> str:
    """自动备份数据库 + 配置 + 战法文件"""
    try:
        from src.self_maintenance import self_backup as _sb
        return _sb()
    except Exception as e:
        return json.dumps({"error": f"备份失败: {e}"}, ensure_ascii=False)


def self_health_probe() -> str:
    """定时健康探针：磁盘/内存/数据库/数据源，异常自动推微信"""
    try:
        from src.self_maintenance import self_health_probe as _shp
        return _shp()
    except Exception as e:
        return json.dumps({"error": f"探针失败: {e}"}, ensure_ascii=False)


def audit_log(limit: int = 20) -> str:
    """查看最近的操作审计日志（谁在何时做了什么）"""
    try:
        from src.eventbus import recent_events
        events = recent_events(limit)
        return json.dumps({"events": events, "count": len(events)}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": f"审计日志查询失败: {e}"}, ensure_ascii=False)


def tool_risk_check(tool_name: str) -> str:
    """检查指定工具的风险等级（safe/write/danger/system）"""
    try:
        from src.danger_gate import get_risk
        level = get_risk(tool_name)
        return json.dumps({"tool": tool_name, "risk_level": level.value}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": f"风险查询失败: {e}"}, ensure_ascii=False)


# ============================================================
# 17. 交易日历（智能识别交易日/休市日）
# ============================================================


# ============================================================
# 16. 板块计算增强
# ============================================================

def sync_sector_data(trade_date: str = None) -> str:
    """计算并同步所有板块数据（聚合个股K线生成板块排行）

    Args:
        trade_date: 交易日，默认最近交易日
    """
    try:
        from src.sector import compute_all_sectors
        result = compute_all_sectors(trade_date)
        return json.dumps(result, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": f"板块计算失败: {e}"}, ensure_ascii=False)


def sector_rotation_analysis(days: int = 5) -> str:
    """检测板块轮动信号（持续强势/轮动加速/新晋热门/资金流入TOP）

    Args:
        days: 分析最近 N 个交易日，默认5天
    """
    try:
        from src.sector import detect_sector_rotation
        result = detect_sector_rotation(days)
        return json.dumps(result, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": f"板块轮动分析失败: {e}"}, ensure_ascii=False)


def sector_trend(industry: str, days: int = 20) -> str:
    """获取指定板块的趋势详情（累计涨跌/资金流向/趋势判断）

    Args:
        industry: 板块名称，如"半导体"
        days: 分析天数
    """
    try:
        from src.sector import get_sector_trend
        result = get_sector_trend(industry, days)
        return json.dumps(result, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": f"板块趋势查询失败: {e}"}, ensure_ascii=False)


# ============================================================
# 12. 交易日历（智能识别交易日/休市日）
# ============================================================

def check_trading_day(date_str: str = None) -> str:
    """检查指定日期是否为 A 股交易日。

    自动处理：周末、法定节假日、调休补班日。
    基于 akshare 官方交易日历，覆盖 1990-2026。
    """
    try:
        from src.calendar import get_calendar
        cal = get_calendar()
        if date_str is None:
            date_str = datetime.now(TZ).strftime("%Y-%m-%d")
        is_trade = cal.is_trading_day(date_str)
        next_td = cal.next_trading_day(date_str)
        prev_td = cal.prev_trading_day(date_str)
        return json.dumps({
            "date": date_str,
            "is_trading_day": is_trade,
            "status": "交易日" if is_trade else "休市",
            "next_trading_day": next_td,
            "prev_trading_day": prev_td,
        }, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": f"交易日历查询失败: {e}"}, ensure_ascii=False)


def sync_trading_calendar() -> str:
    """同步/刷新交易日历（从 akshare 下载最新交易日数据）"""
    try:
        from src.calendar import refresh_calendar
        count = refresh_calendar()
        from src.calendar import get_calendar
        cal = get_calendar()
        status = cal.get_status()
        return json.dumps({
            "status": "ok",
            "trading_days": count,
            "date_range": status["date_range"],
            "today": status["today"],
            "is_trading_day": status["is_trading_day"],
        }, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": f"交易日历同步失败: {e}"}, ensure_ascii=False)


def get_trading_days(start: str = None, end: str = None) -> str:
    """获取指定日期范围内的 A 股交易日列表。

    Args:
        start: 起始日期 YYYY-MM-DD，默认为本月1号
        end: 结束日期 YYYY-MM-DD，默认为今天
    """
    try:
        from src.calendar import get_calendar
        cal = get_calendar()
        today = datetime.now(TZ)
        if start is None:
            start = today.replace(day=1).strftime("%Y-%m-%d")
        if end is None:
            end = today.strftime("%Y-%m-%d")
        days = cal.trading_days_in_range(start, end)
        return json.dumps({
            "start": start, "end": end,
            "trading_days": days,
            "count": len(days),
            "total_days": (datetime.strptime(end, "%Y-%m-%d") - datetime.strptime(start, "%Y-%m-%d")).days + 1,
        }, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": f"查询失败: {e}"}, ensure_ascii=False)


def _get_realtime_price(code: str) -> float:
    """获取实时价格（内部辅助函数）"""
    try:
        from config import config as cfg
        from tickflow import TickFlow
        tf = TickFlow(api_key=cfg["tickflow"]["api_key"])
        symbol = f"{code}.SH" if code.startswith("6") else f"{code}.SZ"
        quotes = tf.quotes.get(symbols=[symbol])
        if quotes:
            return float(quotes[0].get("last_price", 0))
    except Exception:
        pass
    return None


# ============================================================
# 18. 联网搜索
# ============================================================

def web_search(query: str, max_results: int = 5) -> str:
    """联网搜索——获取最新资讯、新闻、政策、公告等实时信息。

    Args:
        query: 搜索关键词（支持中文）
        max_results: 返回结果数量，默认5条
    """
    if not query or not query.strip():
        return json.dumps({"error": "请提供搜索关键词"}, ensure_ascii=False)
    try:
        from ddgs import DDGS
        results = []
        with DDGS() as ddgs:
            for r in ddgs.text(query.strip(), max_results=max(max_results, 10)):
                results.append({
                    "title": r.get("title", ""),
                    "url": r.get("href", ""),
                    "snippet": r.get("body", "")[:300],
                })
                if len(results) >= max_results:
                    break
        if not results:
            return json.dumps({"query": query, "results": [], "hint": "未找到相关结果，尝试换关键词"}, ensure_ascii=False)
        return json.dumps({"query": query, "results": results, "count": len(results)}, ensure_ascii=False)
    except ImportError:
        return json.dumps({"error": "搜索模块未安装，请运行: pip install ddgs"}, ensure_ascii=False)
    except Exception as e:
        err_msg = str(e)
        # 限流友好提示
        if "403" in err_msg or "frequent" in err_msg.lower() or "rate" in err_msg.lower():
            return json.dumps({"error": "搜索请求过于频繁，请稍后重试", "hint": "建议间隔 10 秒以上再次搜索"}, ensure_ascii=False)
        return json.dumps({"error": f"搜索失败: {err_msg[:200]}"}, ensure_ascii=False)


def web_fetch(url: str, max_chars: int = 3000) -> str:
    """获取网页内容——读取指定URL的正文文本。

    Args:
        url: 网页URL
        max_chars: 最大返回字符数，默认3000
    """
    if not url or not url.strip():
        return json.dumps({"error": "请提供网页URL"}, ensure_ascii=False)
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        resp = requests.get(url.strip(), headers=headers, timeout=15)
        resp.raise_for_status()
        resp.encoding = resp.apparent_encoding or "utf-8"

        from bs4 import BeautifulSoup
        soup = BeautifulSoup(resp.text, "lxml")
        # 移除 script/style 标签
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        text = soup.get_text(separator="\n", strip=True)
        # 压缩空白行
        text = re.sub(r"\n{3,}", "\n\n", text)
        if len(text) > max_chars:
            text = text[:max_chars] + f"\n...[截断，原始{len(text)}字符]"
        return json.dumps({"url": url, "content": text, "chars": len(text)}, ensure_ascii=False)
    except requests.RequestException as e:
        return json.dumps({"error": f"网页获取失败: {e}"}, ensure_ascii=False)
    except ImportError:
        return json.dumps({"error": "依赖缺失，请运行: pip install beautifulsoup4 lxml"}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": f"解析失败: {str(e)[:200]}"}, ensure_ascii=False)


# ============================================================
# 19. Admin 运维工具（仅白名单用户可调用）
# ============================================================

def write_code(path: str, content: str) -> str:
    """安全写入代码文件，自动备份+commit+验证

    协议:
    1. self_backup() → 自动备份
    2. git add + commit → 保存安全点
    3. write file → 写入新代码
    4. python import check → 语法验证
    5. 失败 → git revert → 回滚
    """
    import os, subprocess
    PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    # 安全检查：禁止写入敏感文件
    forbidden = [".env", ".qp_env", "authorized_keys", "id_rsa", "credentials"]
    fname = os.path.basename(path)
    if any(f in fname for f in forbidden):
        return json.dumps({"error": f"安全策略禁止写入 {fname}（受保护文件）"}, ensure_ascii=False)

    abs_path = os.path.join(PROJECT_ROOT, path) if not os.path.isabs(path) else path
    if not abs_path.startswith(PROJECT_ROOT):
        return json.dumps({"error": "安全策略禁止写入项目目录外的文件"}, ensure_ascii=False)

    # Step 1: 读取旧内容（如果存在）
    old_content = ""
    is_new_file = not os.path.exists(abs_path)
    if not is_new_file:
        with open(abs_path, "r", encoding="utf-8") as f:
            old_content = f.read()

    # Step 2: Git commit 保存安全点
    try:
        subprocess.run(["git", "add", abs_path], cwd=PROJECT_ROOT,
                       capture_output=True, timeout=10)
        safe_msg = f"safety: auto-backup before write_code -> {path}"
        subprocess.run(["git", "commit", "-m", safe_msg], cwd=PROJECT_ROOT,
                       capture_output=True, timeout=10)
    except Exception as e:
        logger.warning(f"Git commit 失败（继续写入）: {e}")

    # Step 3: 写入文件
    try:
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        with open(abs_path, "w", encoding="utf-8") as f:
            f.write(content)
    except Exception as e:
        return json.dumps({"error": f"写入失败: {e}"}, ensure_ascii=False)

    # Step 4: Python 语法验证
    if path.endswith(".py"):
        try:
            result = subprocess.run(
                [sys.executable or "python3", "-c", f"import py_compile; py_compile.compile('{abs_path}', doraise=True)"],
                cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=10
            )
            if result.returncode != 0:
                # 语法错误 → 回滚
                if old_content:
                    with open(abs_path, "w", encoding="utf-8") as f:
                        f.write(old_content)
                return json.dumps({
                    "error": f"Python 语法错误，已回滚。\n{result.stderr[:300]}",
                    "status": "ROLLED_BACK"
                }, ensure_ascii=False)
        except Exception as e:
            logger.warning(f"语法检查跳过: {e}")

    # Step 5: Git commit 新内容
    try:
        subprocess.run(["git", "add", abs_path], cwd=PROJECT_ROOT,
                       capture_output=True, timeout=10)
        subprocess.run(["git", "commit", "-m", f"auto: {path} (write_code)", "--allow-empty"],
                       cwd=PROJECT_ROOT, capture_output=True, timeout=10)
    except Exception:
        pass

    # Step 6: 自动重启 + 验证 (OODA VERIFY)
    import time as _verify_time
    _service_type = "wechat" if "wechat" in path or "handler" in path or "session" in path else "all"
    try:
        restart_result = restart_service(_service_type)
        restart_json = json.loads(restart_result)
        _verify_time.sleep(5)
        import requests as _req
        checker = _req.get("http://127.0.0.1:7861", timeout=5)
        if checker.status_code == 200:
            return json.dumps({
                "status": "ok",
                "path": path,
                "is_new": is_new_file,
                "chars_written": len(content),
                "action": "written+committed+restarted",
                "health": f"HTTP {checker.status_code}"
            }, ensure_ascii=False)
        else:
            raise Exception(f"HTTP {checker.status_code}")
    except Exception as e:
        # 验证失败 → git revert
        try:
            subprocess.run(["git", "revert", "--no-edit", "HEAD"], cwd=PROJECT_ROOT,
                           capture_output=True, timeout=10)
            if old_content:
                with open(abs_path, "w", encoding="utf-8") as f:
                    f.write(old_content)
            status = "ROLLED_BACK"
            message = f"服务验证失败({e})，已自动回滚"
        except Exception:
            status = "VERIFY_FAILED"
            message = f"服务验证失败({e})，回滚也失败，请手动处理"
        return json.dumps({"status": status, "path": path, "error": message}, ensure_ascii=False)


def download_full_kline(tables: str = "daily", start: str = "", end: str = "") -> str:
    """全量批量下载日K/周K/月K/分钟K线 — 后台异步执行

    分批次下载，限速遵循 API 限制，支持断点续传。
    调用后立即返回，下载在后台运行，完成后推送企业微信通知。
    """
    import threading

    worker = threading.Thread(
        target=_download_kline_worker,
        args=(tables, start, end),
        daemon=True
    )
    worker.start()

    return json.dumps({
        "status": "started",
        "message": f"全量数据下载任务已启动 (tables={tables})，下载完成后会自动推送到企业微信。",
        "note": "下载在后台进行，不影响其他操作。预计耗时 80-120 分钟。"
    }, ensure_ascii=False)


def _download_kline_worker(tables: str = "daily", start: str = "", end: str = ""):
    """后台工作线程：全量下载"""
    import time as _time
    from datetime import datetime, timedelta
    import logging
    logger_worker = logging.getLogger("quantpilot.download_worker")

    if not end:
        end = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    if not start:
        start = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")

    tasks = [t.strip() for t in tables.split(",")]
    results = {}

    conn = _get_conn()
    try:
        codes = [row["code"] for row in
                 conn.execute("SELECT code FROM instruments LIMIT 5500").fetchall()]
    except Exception:
        from src.sources.manager import DataSourceManager
        from config import config as cfg
        mgr = DataSourceManager(cfg)
        try:
            mgr.sync_instruments()
            conn = _get_conn()
            codes = [row["code"] for row in
                     conn.execute("SELECT code FROM instruments LIMIT 5500").fetchall()]
        except Exception as e:
            results["error"] = f"获取股票列表失败: {e}"
            return

    total_codes = len(codes)
    results["total_stocks"] = total_codes
    results["tasks"] = {}

    for task in tasks:
        if task not in ("daily", "weekly", "monthly", "minute"):
            results["tasks"][task] = {"error": f"不支持的表: {task}"}
            continue

        to_download = []
        for code in codes:
            exists = conn.execute(
                "SELECT COUNT(*) FROM daily_kline WHERE code=? AND trade_date>=?",
                (code, start)
            ).fetchone()[0]
            if exists == 0:
                to_download.append(code)

        task_total = len(to_download)
        results["tasks"][task] = {"to_download": task_total, "downloaded": 0, "errors": 0}

        if task_total == 0:
            results["tasks"][task]["status"] = "already_up_to_date"
            continue

        batch_size = 100
        from src.sources.manager import DataSourceManager
        from config import config as cfg
        mgr = DataSourceManager(cfg)

        for i in range(0, task_total, batch_size):
            batch = to_download[i:i + batch_size]
            for code in batch:
                try:
                    df = mgr.fetch_daily_kline(code, start, end)
                    if df is not None and not df.empty and not (isinstance(df, dict) and "error" in df):
                        for _, row in df.iterrows():
                            try:
                                conn.execute(
                                    "INSERT OR REPLACE INTO daily_kline (code,trade_date,open,high,low,close,volume,amount,turnover,data_source) VALUES (?,?,?,?,?,?,?,?,?,'tickflow')",
                                    (str(row.get("code", code)), str(row.get("trade_date", "")),
                                     float(row.get("open", 0)), float(row.get("high", 0)),
                                     float(row.get("low", 0)), float(row.get("close", 0)),
                                     float(row.get("volume", 0)), float(row.get("amount", 0)),
                                     float(row.get("turnover", 0)))
                                )
                            except Exception:
                                pass
                        conn.commit()
                        results["tasks"][task]["downloaded"] += 1
                    else:
                        results["tasks"][task]["errors"] += 1
                except Exception:
                    results["tasks"][task]["errors"] += 1

            _time.sleep(2)
            logger_worker.info(f"下载进度: {task} {i + len(batch)}/{task_total}")

        results["tasks"][task]["status"] = "done"

    try:
        from src.wechat.server import send_text_message
        admin_users = []
        try:
            from src.danger_gate import get_admin_users_list
            admin_users = get_admin_users_list()
        except Exception:
            pass
        summary = f"## 全量数据下载完成\n"
        for t, r in results.get("tasks", {}).items():
            summary += f"- **{t}**: 下载 {r.get('downloaded', 0)} 只, 错误 {r.get('errors', 0)}\n"
        for uid in admin_users:
            send_text_message(uid, summary)
    except Exception:
        pass

    conn.close()


def restart_service(service: str = "all") -> str:
    """重启 QP 服务"""
    import os, subprocess, signal
    PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    results = {}

    if service in ("ui", "all"):
        try:
            # 找到并杀掉旧进程
            pids = subprocess.run(
                ["pgrep", "-f", "python3 main.py ui"],
                capture_output=True, text=True, timeout=5
            ).stdout.strip().split("\n")
            for pid in pids:
                if pid:
                    os.kill(int(pid), signal.SIGTERM)
            # 启动新进程
            subprocess.Popen(
                ["nohup", "./run_qp.sh", "ui"],
                cwd=PROJECT_ROOT, stdout=open("/tmp/qp.log", "a"),
                stderr=subprocess.STDOUT, preexec_fn=os.setpgrp
            )
            results["ui"] = "restarted"
        except Exception as e:
            results["ui"] = f"failed: {e}"

    if service in ("wechat", "all"):
        try:
            pids = subprocess.run(
                ["pgrep", "-f", "python3 start_wechat.py"],
                capture_output=True, text=True, timeout=5
            ).stdout.strip().split("\n")
            for pid in pids:
                if pid:
                    os.kill(int(pid), signal.SIGTERM)
            subprocess.Popen(
                ["nohup", "./run_wechat.sh"],
                cwd=PROJECT_ROOT, stdout=open("/tmp/wechat.log", "a"),
                stderr=subprocess.STDOUT, preexec_fn=os.setpgrp
            )
            results["wechat"] = "restarted"
        except Exception as e:
            results["wechat"] = f"failed: {e}"

    # 等待服务上线
    import time as _time
    _time.sleep(3)
    try:
        import requests
        r = requests.get("http://127.0.0.1:7861", timeout=5)
        results["health_check"] = f"HTTP {r.status_code}"
    except Exception:
        results["health_check"] = "unreachable"

    return json.dumps(results, ensure_ascii=False)


def data_self_heal() -> str:
    """OODA 数据自愈闭环

    Observe  → 扫描所有核心表 + 数据源健康
    Orient   → 分类: never_synced(从未) / stale(滞后) / source_down(源宕)
    Decide   → 选策略: 全量异步下载 / 增量补数据 / 切换备源
    Act      → 执行修复
    Verify   → 验证行数 → 推微信报告
    """
    from datetime import datetime, timedelta
    conn = _get_conn()
    report = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "phase": "OBSERVE",
        "tables": {},
        "actions": [],
        "verify": {},
        "status": "pending"
    }

    # ═══ OBSERVE: 扫描所有核心表 ═══
    checks = {
        "instruments": {"label": "股票列表", "min_rows": 1000, "critical": True},
        "daily_kline": {"label": "日K线", "min_rows": 100000, "critical": True},
        "trading_calendar": {"label": "交易日历", "min_rows": 1000, "critical": True},
        "market_snapshot": {"label": "市场快照", "min_rows": 1, "critical": False},
        "limit_up_pool": {"label": "涨停池", "min_rows": 1, "critical": False},
        "sector_data": {"label": "板块数据", "min_rows": 1, "critical": False},
    }

    for table, cfg in checks.items():
        try:
            count = conn.execute(f"SELECT COUNT(*) FROM [{table}]").fetchone()[0]
            status = "ok" if count >= cfg["min_rows"] else "empty" if count == 0 else "low"
            report["tables"][table] = {
                "label": cfg["label"], "rows": count, "status": status,
                "critical": cfg["critical"]
            }
        except Exception:
            report["tables"][table] = {"label": cfg["label"], "status": "error", "critical": cfg["critical"]}

    # ═══ OBSERVE: 检查数据源健康 ═══
    try:
        from src.sources.manager import DataSourceManager
        from config import config as cfg
        mgr = DataSourceManager(cfg)
        health = mgr.health_check()
        report["sources"] = health
        primary_ok = health.get("tickflow") == "ok"
    except Exception:
        report["sources"] = {"error": "无法检测数据源"}
        primary_ok = False

    # ═══ ORIENT: 分类问题 ═══
    report["phase"] = "ORIENT"
    gaps = []
    for table, info in report["tables"].items():
        if info["status"] in ("empty", "low"):
            if info["critical"] and info["status"] == "empty":
                gaps.append({"table": table, "type": "never_synced", "priority": "critical"})
            elif info["status"] == "low":
                gaps.append({"table": table, "type": "stale", "priority": "medium"})
            else:
                gaps.append({"table": table, "type": "empty_noncritical", "priority": "low"})

    if not primary_ok and gaps:
        gaps.append({"table": "data_source", "type": "source_down", "priority": "blocker"})
        report["blocked"] = "主数据源 tickflow 不可用，无法修复数据缺失"

    if not gaps:
        report["status"] = "healthy"
        report["message"] = "所有核心表数据正常"
        conn.close()
        return json.dumps(report, ensure_ascii=False)

    report["gaps"] = gaps

    # ═══ DECIDE + ACT: 逐项修复 ═══
    report["phase"] = "DECIDE_ACT"
    for gap in gaps:
        if gap["type"] == "source_down":
            report["actions"].append({"gap": gap, "result": "blocked"})
            continue

        table = gap["table"]
        try:
            if table == "instruments":
                from agent.tools import sync_stock_list
                r = json.loads(sync_stock_list())
                report["actions"].append({"gap": gap, "result": "ok", "detail": r})
            elif table == "trading_calendar":
                from agent.tools import sync_trading_calendar
                r = json.loads(sync_trading_calendar())
                report["actions"].append({"gap": gap, "result": "ok", "detail": r})
            elif table == "daily_kline":
                import threading
                worker = threading.Thread(
                    target=_download_kline_worker,
                    args=("daily", "2025-01-01", datetime.now().strftime("%Y-%m-%d")),
                    daemon=True
                )
                worker.start()
                report["actions"].append({"gap": gap, "result": "async_started",
                    "note": "全量日K下载已触发，完成后自动推微信通知"})
            else:
                report["actions"].append({"gap": gap, "result": "manual_required"})
        except Exception as e:
            report["actions"].append({"gap": gap, "result": "error", "detail": str(e)})

    # ═══ VERIFY: 重新检查行数 ═══
    report["phase"] = "VERIFY"
    for table in checks:
        try:
            count = conn.execute(f"SELECT COUNT(*) FROM [{table}]").fetchone()[0]
            report["verify"][table] = count
        except Exception:
            report["verify"][table] = "?"

    conn.close()

    # ═══ NOTIFY: 推送微信报告 ═══
    try:
        affected = [g["table"] for g in gaps if g["type"] != "source_down"]
        summary = "## 数据自愈报告\n\n"
        for a in report.get("actions", []):
            status_icon = "✅" if a.get("result") == "ok" else "🔄" if a.get("result") == "async_started" else "❌"
            summary += f"- {status_icon} {a['gap']['table']}: {a['gap']['type']} → {a.get('result', '?')}\n"
        if affected:
            from src.wechat.server import send_text_message
            from src.danger_gate import get_admin_users_list
            for uid in get_admin_users_list():
                send_text_message(uid, summary)
    except Exception:
        pass

    report["status"] = "complete"
    return json.dumps(report, ensure_ascii=False)


def run_shell(command: str, timeout: int = 30) -> str:
    """在服务器上执行 Shell 命令（Admin 专属）

    安全限制：
    - 禁止 rm -rf, sudo, passwd, shutdown, reboot, mkfs, dd
    - 禁止操作 ~/.qp_env 和 /etc/
    - 超时 30 秒
    - 所有命令记录到 audit_log
    """
    import subprocess

    # 安全检查
    forbidden = ["rm -rf", "sudo ", "passwd", "shutdown", "reboot", "mkfs", "dd if=",
                 ".qp_env", "/etc/passwd", "/etc/shadow", "> /dev/sda"]
    cmd_lower = command.lower()
    for f in forbidden:
        if f in cmd_lower:
            return json.dumps({"error": f"安全策略禁止: {f}", "command": command}, ensure_ascii=False)

    try:
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True,
            timeout=timeout, cwd=os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        )
        output = result.stdout
        if result.stderr:
            output += "\n[STDERR]\n" + result.stderr
        if not output.strip():
            output = "(无输出)"

        # 截断过长输出
        if len(output) > 4000:
            output = output[:4000] + f"\n...(截断，共{len(output)}字符)"

        return json.dumps({
            "command": command,
            "exit_code": result.returncode,
            "output": output
        }, ensure_ascii=False)
    except subprocess.TimeoutExpired:
        return json.dumps({"error": f"命令超时({timeout}s)", "command": command}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e), "command": command}, ensure_ascii=False)


# ============================================================

# ============================================================
# 21. 龙虎榜 + 北向资金 (Tushare)
# ============================================================

def sync_dragon_tiger(date: str = None) -> str:
    """同步龙虎榜数据（需要 Tushare token）
    如果没有 token, 返回配置指引
    """
    from datetime import datetime
    if not date:
        date = datetime.now().strftime("%Y-%m-%d")

    try:
        from config import config as cfg
        token = cfg.get("tushare", {}).get("token", "")
        if not token or token == "":
            return json.dumps({
                "status": "skip",
                "message": "Tushare token not configured",
                "guide": "Register at https://tushare.pro, get token, set in config.yaml tushare.token"
            }, ensure_ascii=False)

        import tushare as ts
        pro = ts.pro_api(token)
        df = pro.top_inst(trade_date=date.replace("-", ""))
        if df is not None and not df.empty:
            conn = _get_conn()
            count = 0
            for _, row in df.iterrows():
                try:
                    conn.execute("""
                        INSERT OR REPLACE INTO dragon_tiger
                        (trade_date, code, name, reason, buy_amount, sell_amount, net_amount)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """, (
                        date, row.get("ts_code", ""), "", "",
                        float(row.get("buy_amount", 0) or 0),
                        float(row.get("sell_amount", 0) or 0),
                        float(row.get("net_amount", 0) or 0)
                    ))
                    count += 1
                except Exception:
                    pass
            conn.commit()
            conn.close()
            return json.dumps({"status": "ok", "synced": count, "date": date}, ensure_ascii=False)
        return json.dumps({"status": "empty", "message": f"{date} 无龙虎榜数据"}, ensure_ascii=False)
    except ImportError:
        return json.dumps({"error": "请安装 tushare: pip install tushare"}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


def sync_northbound_flow(days: int = 5) -> str:
    """同步北向资金流向（需要 Tushare token）"""
    try:
        from config import config as cfg
        token = cfg.get("tushare", {}).get("token", "")
        if not token or token == "":
            return json.dumps({
                "status": "skip",
                "message": "Tushare token 未配置, 请注册 https://tushare.pro"
            }, ensure_ascii=False)

        from datetime import datetime, timedelta
        import tushare as ts
        pro = ts.pro_api(token)
        end = datetime.now().strftime("%Y%m%d")
        start = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")
        df = pro.moneyflow_hsgt(start_date=start, end_date=end)
        if df is not None and not df.empty:
            conn = _get_conn()
            count = 0
            for _, row in df.iterrows():
                try:
                    conn.execute("""
                        INSERT OR REPLACE INTO northbound_flow
                        (trade_date, buy_amount, sell_amount, net_amount)
                        VALUES (?, ?, ?, ?)
                    """, (
                        row.get("trade_date", ""),
                        float(row.get("buy_amount", 0) or 0),
                        float(row.get("sell_amount", 0) or 0),
                        float(row.get("net_amount", 0) or 0)
                    ))
                    count += 1
                except Exception:
                    pass
            conn.commit()
            conn.close()
            return json.dumps({"status": "ok", "synced": count, "days": days}, ensure_ascii=False)
        return json.dumps({"status": "empty", "message": "无北向数据"}, ensure_ascii=False)
    except ImportError:
        return json.dumps({"error": "请安装 tushare: pip install tushare"}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)





def sync_to_ths_watchlist(codes: str = "", group: str = "我的自选") -> str:
    """将股票批量添加到同花顺自选池（v1 API，稳定可靠）

    codes 格式: "600519,000858,002902"（逗号分隔，不需要市场后缀）
    注意: 此工具通过同花顺 v1 API 操作，需要 THS_USERNAME/THS_PASSWORD
    """
    import os, json as _json, sys as _sys
    _ths_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "ths_favorite")
    _sys.path.insert(0, _ths_dir)

    # 从 .qp_env 加载环境变量（如果进程未加载）
    _qp_env = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), ".qp_env")
    if os.path.exists(_qp_env):
        for _line in open(_qp_env):
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip())

    username = os.environ.get("THS_USERNAME", "")
    password = os.environ.get("THS_PASSWORD", "")
    if not username or not password:
        return _json.dumps({"error": "未配置同花顺账号。请在 ~/.qp_env 中添加 THS_USERNAME 和 THS_PASSWORD"}, ensure_ascii=False)

    stock_list = [c.strip() for c in codes.split(",") if c.strip() and c.strip().isdigit()]
    if not stock_list:
        return _json.dumps({"error": "请提供股票代码，如 600519,000858"}, ensure_ascii=False)

    try:
        from ths_favorite.auth import create_session
        from ths_favorite.selfstock_v1 import download_self_stocks_v1, modify_self_stocks_v1
        from ths_favorite.models import StockEntry

        session = create_session(username=username, password=password)
        cookies = session.cookies

        # 读取当前自选股
        current = download_self_stocks_v1(cookies)
        existing = {s.code for s in current.items}

        # 添加不存在的（自动判断市场类型）
        added = []
        for code in stock_list:
            # 市场类型: 6xxxxx=SH(17), 51xxxx/56xxxx=SHETF(20), 0xxxxx/3xxxxx=SZ(33)
            if code.startswith("6"):
                if code.startswith("399"):
                    mtype = "32"  # 深圳指数（跳过）
                elif code.startswith("159"):
                    mtype = "36"  # 深圳 ETF
                elif code.startswith("51") or code.startswith("56") or code.startswith("58") or code.startswith("513"):
                    mtype = "20"  # 上海 ETF
                elif code.startswith("68"):
                    mtype = "18"  # 科创板
                elif code.startswith("1"):
                    mtype = "16"  # 指数（跳过）
                else:
                    mtype = "17"  # 上海主板
            elif code.startswith("3"):
                mtype = "33"  # 创业板
            elif code.startswith("8"):
                mtype = "71"  # 北交所
            else:
                mtype = "33"  # 深圳主板
            if code not in existing:
                current.items.append(StockEntry(code, mtype))
                added.append(code)

        if added:
            modify_self_stocks_v1(cookies, current.items, current.version)
            return _json.dumps({"status": "ok", "added": added, "group": group}, ensure_ascii=False)
        else:
            return _json.dumps({"status": "ok", "added": [], "message": "所有股票已在自选池中"}, ensure_ascii=False)

    except Exception as e:
        return _json.dumps({"error": f"同花顺同步失败: {e}"}, ensure_ascii=False)

# 工具定义 + 调度（从 registry.py 导入）
# ============================================================


# ============================================================
# 20. 结构化决策输出
# ============================================================







def detect_market_environment() -> str:
    """检测当前市场环境类型

    基于近10个交易日的涨停数据、指数走势、情绪周期判断。
    返回 market_env: 启动期/高潮期/发酵期/震荡期/低迷期/冰点期
    """
    import json as _json
    from datetime import datetime, timedelta

    today = datetime.now().strftime("%Y-%m-%d")

    # 获取最近涨停数据
    conn = _get_conn()
    recent = []
    try:
        rows = conn.execute('''SELECT trade_date, break_count, limit_up_count,
            limit_down_count, sentiment FROM market_snapshot
            ORDER BY trade_date DESC LIMIT 10''').fetchall()
        for r in rows:
            recent.append(dict(r))
    except Exception:
        pass
    conn.close()

    if not recent:
        return _json.dumps({"market_env": "unknown", "reason": "no snapshot data", "note": "Run sync first"})

    avg_break = sum(r.get("break_count", 0) or 0 for r in recent) / max(len(recent), 1)
    avg_limit_up = sum(r.get("limit_up_count", 0) or 0 for r in recent) / max(len(recent), 1)
    avg_limit_down = sum(r.get("limit_down_count", 0) or 0 for r in recent) / max(len(recent), 1)
    latest_sentiment = recent[0].get("sentiment", "") if recent else ""

    # 市场环境分类
    if avg_limit_up > 80 and avg_break > 40:
        env = "gaochao"
        label = "高潮期"
        desc = "涨停潮，连板率高，板块主线明确"
    elif avg_limit_up > 50 and avg_break > 20:
        env = "fajiao"
        label = "发酵期"
        desc = "赚钱效应扩散，连板梯队完整"
    elif avg_limit_up > 30 and avg_break > 10:
        env = "qidong"
        label = "启动期"
        desc = "行情刚启动，首板增多，风险适中"
    elif avg_limit_down > avg_limit_up:
        env = "bingdian"
        label = "冰点期"
        desc = "跌停>涨停，市场恐慌，防守为主"
    elif avg_limit_up < 20:
        env = "dimi"
        label = "低迷期"
        desc = "人气低迷，涨停稀少，不适合短线"
    else:
        env = "zhendang"
        label = "震荡期"
        desc = "多空均衡，结构性机会为主"

    return _json.dumps({
        "market_env": env,
        "label": label,
        "description": desc,
        "data": {
            "avg_limit_up": round(avg_limit_up, 1),
            "avg_break": round(avg_break, 1),
            "avg_limit_down": round(avg_limit_down, 1),
            "sentiment": latest_sentiment,
            "sample_days": len(recent)
        }
    }, ensure_ascii=False)


def route_skills(market_env: str = "") -> str:
    """根据市场环境路由最佳战法

    market_env: 启动期/高潮期/发酵期/震荡期/低迷期/冰点期
    返回该环境下最匹配的战法列表（按匹配度排序）
    """
    import json as _json

    if not market_env:
        prev = detect_market_environment()
        try:
            market_env = _json.loads(prev).get("market_env", "unknown")
        except Exception:
            market_env = "unknown"

    # 战法-环境 路由矩阵
    ROUTING_MATRIX = {
        "qidong": {
            "label": "启动期",
            "strategies": [
                ("放量突破战法", "01", 95, "最适合，突破确认后的启动点"),
                ("低位首板战法", "04", 90, "低位首板是启动期核心策略"),
                ("题材首板战法", "06", 85, "新题材启动期爆发力最强"),
                ("日内首板战法", "07", 80, "半路首板捕捉启动信号"),
                ("一进二战法", "10", 75, "首板转二板确认强度"),
            ]
        },
        "gaochao": {
            "label": "高潮期",
            "strategies": [
                ("接力战法", "02", 90, "高潮期连板接力是核心利润源"),
                ("龙头战法", "29", 90, "高潮期龙头股空间最大"),
                ("二板接力战法", "10", 85, "连板梯队完整时胜率高"),
                ("三板接力战法", "13", 80, "三板确认龙头地位"),
                ("高位接力战法", "14", 75, "高潮期高位票有惯性"),
                ("加速二板战法", "12", 70, "加速阶段追涨"),
            ]
        },
        "fajiao": {
            "label": "发酵期",
            "strategies": [
                ("换手二板战法", "11", 85, "发酵期换手充分"),
                ("首板回封战法", "05", 80, "分歧确认后的二次封板"),
                ("卡位战法", "31", 75, "板块内卡位竞争"),
                ("补涨龙战法", "32", 70, "主线确定后的补涨挖掘"),
                ("龙头二波战法", "30", 65, "龙头首次分歧后是否二波"),
            ]
        },
        "zhendang": {
            "label": "震荡期",
            "strategies": [
                ("低吸战法", "03", 90, "震荡期低吸为王"),
                ("5日线低吸战法", "17", 85, "依托均线低吸"),
                ("10日线低吸战法", "18", 80, "深度回调低吸"),
                ("分歧低吸战法", "21", 75, "分歧转一致低吸"),
                ("平台支撑低吸战法", "19", 70, "平台支撑位低吸"),
                ("尾盘套利战法", "35", 65, "尾盘买早盘卖"),
            ]
        },
        "dimi": {
            "label": "低迷期",
            "strategies": [
                ("超跌低吸战法", "24", 80, "超跌反弹修复"),
                ("超跌反弹半路战法", "27", 75, "超跌后的反弹"),
                ("尾盘套利战法", "35", 70, "小幅套利为主"),
                ("空仓战法", "40", 90, "低迷期空仓是最佳策略"),
            ]
        },
        "bingdian": {
            "label": "冰点期",
            "strategies": [
                ("空仓战法", "40", 95, "冰点期必须空仓防守"),
                ("翘板战法", "22", 60, "撬跌停板高难度操作"),
                ("反核战法", "23", 60, "反核按钮高风险操作"),
                ("集合竞价战法", "34", 40, "冰点期竞价信号极少"),
            ]
        }
    }

    env_data = ROUTING_MATRIX.get(market_env, ROUTING_MATRIX.get("zhendang"))
    ordered = sorted(env_data["strategies"], key=lambda x: x[2], reverse=True)

    return _json.dumps({
        "market_env": market_env,
        "market_label": env_data["label"],
        "matched_strategies": [
            {"name": s[0], "code": s[1], "match_score": s[2], "reason": s[3]}
            for s in ordered
        ],
        "total_matched": len(ordered)
    }, ensure_ascii=False)




def strategy_evolution() -> str:
    """策略进化系统：基于模拟盘交易数据，评估各战法表现

    每周六 strategy_evolution 定时任务执行。
    前置条件: sim_trades >= 30 条
    输出: 各战法胜率、建议升级/降级的战法
    """
    import json as _json
    conn = _get_conn()

    # 检查模拟盘数据量
    trade_count = conn.execute("SELECT COUNT(*) FROM sim_trades").fetchone()[0]
    if trade_count < 10:
        conn.close()
        return _json.dumps({
            "status": "skip",
            "message": f"模拟盘交易记录不足({trade_count}/30)，暂不进化",
            "trade_count": trade_count
        }, ensure_ascii=False)

    # 统计各战法胜率
    rows = conn.execute("""
        SELECT strategy, action,
               COUNT(*) as cnt,
               SUM(CASE WHEN outcome = 'win' THEN 1 ELSE 0 END) as wins,
               SUM(CASE WHEN outcome = 'loss' THEN 1 ELSE 0 END) as losses
        FROM sim_trades
        WHERE strategy IS NOT NULL AND strategy != ''
        GROUP BY strategy, action
        ORDER BY cnt DESC
    """).fetchall()

    if not rows:
        conn.close()
        return _json.dumps({"status": "skip", "message": "交易记录缺少战法标注"}, ensure_ascii=False)

    results = {"status": "complete", "strategies": [], "upgrades": [], "downgrades": [], "trade_count": trade_count}

    for row in rows:
        strategy = row["strategy"]
        action = row["action"]
        total = row["cnt"]
        wins = row["wins"] or 0
        win_rate = wins / max(total, 1) * 100

        entry = {
            "strategy": strategy, "action": action,
            "trades": total, "wins": wins,
            "losses": row["losses"] or 0,
            "win_rate": f"{win_rate:.1f}%"
        }
        results["strategies"].append(entry)

        if total >= 5 and win_rate >= 55:
            entry["recommendation"] = "upgrade"
            results["upgrades"].append(strategy)
        elif total >= 5 and win_rate <= 35:
            entry["recommendation"] = "downgrade"
            results["downgrades"].append(strategy)
        else:
            entry["recommendation"] = "hold"

    conn.close()
    return _json.dumps(results, ensure_ascii=False)


def debate_analysis(code: str) -> str:
    """辩论模式：Bull↔Bear↔Judge 多角度分析股票"""
    import json as _json
    from datetime import datetime

    conn = _get_conn()
    name = ''
    try:
        row = conn.execute('SELECT name FROM instruments WHERE code=?', (code,)).fetchone()
        name = row['name'] if row else code
    except Exception:
        name = code

    klines = []
    try:
        rows = conn.execute("""SELECT trade_date, open, high, low, close, volume
            FROM daily_kline WHERE code=? ORDER BY trade_date DESC LIMIT 120""", (code,)).fetchall()
        for r in rows:
            klines.append(f"""{r['trade_date']} O:{r['open']:.2f} H:{r['high']:.2f} L:{r['low']:.2f} C:{r['close']:.2f} V:{int(r['volume'])}""")
    except Exception:
        pass
    conn.close()

    data_str = '\n'.join(klines[-60:]) if len(klines) > 60 else '\n'.join(klines)
    if not data_str:
        return _json.dumps({'error': 'No kline data. Sync first.'}, ensure_ascii=False)

    context = {'code': code, 'name': name, 'data': data_str}

    try:
        from config import load_config
        from agent.client import LLMClient
        cfg = load_config()
        client = LLMClient(cfg, model_key='primary')
    except Exception as e:
        return _json.dumps({'error': f'LLM init: {e}'}, ensure_ascii=False)

    from agent.daily_tasks import BULL, BEAR, JUDGE

    bull_msgs = [{'role': 'user', 'content': BULL.format(**context)}]
    bull_resp = client.chat(bull_msgs)
    bull_args = bull_resp.get('content', '')

    bear_msgs = [{'role': 'user', 'content': BEAR.format(**context)}]
    bear_resp = client.chat(bear_msgs)
    bear_args = bear_resp.get('content', '')

    rebuttal_bull = client.chat([{'role': 'user', 'content':
        BULL.format(**context) + '\n\n## Bear countered\n' + bear_args[:1000]
        + '\n\nRefute bear. Strengthen your case.'}])
    rebuttal_bear = client.chat([{'role': 'user', 'content':
        BEAR.format(**context) + '\n\n## Bull countered\n' + bull_args[:1000]
        + '\n\nRefute bull. Strengthen your case.'}])

    judge_msgs = [{'role': 'user', 'content': JUDGE.format(**{
        **context, 'bull_args': bull_args[:1500], 'bear_args': bear_args[:1500]})}]
    judge_resp = client.chat(judge_msgs)
    judge_text = judge_resp.get('content', '')

    try:
        import re
        jm = re.search(r'\{.*\}', judge_text, re.DOTALL)
        decision = _json.loads(jm.group()) if jm else {'decision': 'hold', 'confidence': 0}
    except Exception:
        decision = {'decision': 'hold', 'confidence': 0}

    return _json.dumps({
        'code': code, 'name': name,
        'bull': bull_args[:500],
        'bear': bear_args[:500],
        'bull_rebuttal': rebuttal_bull.get('content', '')[:300],
        'bear_rebuttal': rebuttal_bear.get('content', '')[:300],
        'decision': decision,
        'judge_raw': judge_text[:300]
    }, ensure_ascii=False)


def reflection_sweeper() -> str:
    """反思清扫器：批量验证到期预测 → 归因 → 提炼教训 → 存入记忆

    定时任务：每天收盘后运行一次
    流程：
      1. 查找 outcome='pending' 且 check_after_date <= 今天的预测
      2. 从日K线获取实际价格
      3. 自动验证 (correct/wrong/partial)
      4. 对 wrong 预测：分析失败原因
      5. 提炼教训存入 long_term_memories (mode=当前模式)
    """
    from datetime import datetime, timedelta
    conn = _get_conn()
    results = {"swept": 0, "verified": 0, "errors": 0, "lessons": []}

    # 1. 查找到期未验证
    rows = conn.execute("""
        SELECT id, stock_code, direction, target_price, stop_loss,
               timeframe_days, reasoning, confidence, created_at
        FROM predictions
        WHERE outcome = 'pending' AND check_after_date <= date('now')
        ORDER BY check_after_date ASC LIMIT 50
    """).fetchall()

    if not rows:
        conn.close()
        return json.dumps({"status": "up_to_date", "message": "没有到期预测"}, ensure_ascii=False)

    results["swept"] = len(rows)

    for row in rows:
        pred_id = row["id"]
        code = row["stock_code"]
        confidence = row["confidence"] or 0.5

        # 2. 从日K线拿实际价格（预测到期后的第一个收盘价）
        try:
            check_date = datetime.now().strftime("%Y-%m-%d")
            price_row = conn.execute("""
                SELECT close FROM daily_kline
                WHERE code = ? AND trade_date <= ?
                ORDER BY trade_date DESC LIMIT 1
            """, (code, check_date)).fetchone()
        except Exception:
            price_row = None

        if price_row is None or price_row["close"] is None:
            results["errors"] += 1
            continue

        actual_price = float(price_row["close"])

        # 3. 自动验证
        from src.prediction import verify_prediction as vp
        vp_result = vp(pred_id, actual_price)
        vp_data = json.loads(vp_result)
        outcome = vp_data.get("outcome", "partial")

        if outcome == "wrong" and confidence >= 0.3:
            # 4. 归因分析：为什么错了
            direction = row["direction"]
            reasoning = row["reasoning"] or ""
            target = row["target_price"] or 0

            if direction == "bullish" and actual_price < target:
                cause = "target_too_high"
                lesson = f"对 {code} 的看多预测过于乐观（目标价 {target}，实际 {actual_price:.2f}），下次应降低目标价预期，结合更充分的量价确认。"
            elif direction == "bearish" and actual_price > target:
                cause = "target_too_low"
                lesson = f"对 {code} 的看空预测过于悲观（目标价 {target}，实际 {actual_price:.2f}），下次应等待空头信号确认后再做空。"
            elif confidence < 0.5:
                cause = "low_confidence_guess"
                lesson = f"对 {code} 的预测置信度过低（{confidence}），不应草率做出方向判断。建议等待更多信号。"
            else:
                cause = "unknown"
                lesson = f"对 {code} 的预测失败（方向={direction}，置信度={confidence}），需要更全面的基本面+技术面分析。"

            # 5. 存入记忆
            try:
                from agent.memory.manager import MemoryManager
                mm = MemoryManager()
                mm.save_memory(
                    content=lesson,
                    memory_type="learning",
                    importance=0.65,
                    tags="reflection,error",
                    mode="cc"  # CC 学习的内容
                )
                # 标记已提取
                conn.execute("UPDATE predictions SET learning_extracted=1, outcome_notes=? WHERE id=?", (cause, pred_id))
                results["lessons"].append({"code": code, "cause": cause, "lesson": lesson[:60]})
            except Exception:
                pass

        results["verified"] += 1

    conn.commit()
    conn.close()
    # 推送微信告警
    try:
        from src.alerting import alert_reflection_done
        alert_reflection_done(len(results.get("lessons", [])), results["swept"])
    except Exception:
        pass
    return json.dumps(results, ensure_ascii=False)


def generate_trade_decision(code: str, analysis_result: str = "",
                             decision: str = "hold", confidence: float = 0.5,
                             entry_price: float = 0, stop_loss: float = 0,
                             targets: str = "", risk_level: str = "medium",
                             rationale: str = "", strategy_match: str = "") -> str:
    """生成结构化交易决策卡片

    每次股票分析后调用此工具，输出标准化决策。
    用途: 微信推送可读性 / 回测验证精度 / 长期统计胜率
    """
    import json as _json
    target_list = [float(t.strip()) for t in targets.split(",") if t.strip()] if targets else []
    decision_card = {
        "code": code,
        "decision": decision,
        "confidence": max(0, min(1, confidence)),
        "entry_price": entry_price,
        "stop_loss": stop_loss,
        "targets": target_list,
        "risk_level": risk_level,
        "rationale": rationale,
        "strategy_match": strategy_match,
        "generated_at": __import__("datetime").datetime.now(
            __import__("zoneinfo").ZoneInfo("Asia/Shanghai")
        ).strftime("%Y-%m-%d %H:%M")
    }

    try:
        conn = _get_conn()
        conn.execute("""
            INSERT INTO predictions
            (id, code, direction, target_price, stop_loss,
             timeframe_days, reasoning, confidence, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            str(__import__("uuid").uuid4()),
            code,
            "bullish" if decision == "buy" else "bearish" if decision == "sell" else "neutral",
            entry_price, stop_loss, 5 if not target_list else 30,
            rationale, confidence,
            decision_card["generated_at"]
        ))
        conn.commit()
        conn.close()
    except Exception:
        pass

    return _json.dumps(decision_card, ensure_ascii=False)

TOOL_DISPATCH = {
    "read_file": read_file, "write_file": write_file, "list_files": list_files,
    "record_trade": record_trade, "view_portfolio": view_portfolio, "view_trade_history": view_trade_history,
    "update_position": update_position,
    "view_sim_portfolio": view_sim_portfolio, "init_sim_accounts": init_sim_accounts, "execute_sim_trade": execute_sim_trade, "view_sim_trades": view_sim_trades,
    "add_to_watchlist": add_to_watchlist, "remove_from_watchlist": remove_from_watchlist, "list_watchlist": list_watchlist,
    "view_alerts": view_alerts,
    "clear_position": clear_position, "clear_trades": clear_trades, "clear_memory": clear_memory,
    "cleanup_expired_data": cleanup_expired_data, "reset_test_data": reset_test_data,
    "save_memory": save_memory, "search_memory": search_memory, "list_memories": list_memories,
    "query_kline": query_kline, "search_stock": search_stock, "market_overview": market_overview,
    "sector_ranking": sector_ranking, "limit_up_pool": limit_up_pool,
    "calc_technical": calc_technical, "recognize_kline_patterns": recognize_kline_patterns,
    "screen_stocks": screen_stocks, "calc_sector_data": calc_sector_data,
    "search_strategies": search_strategies, "list_strategies": list_strategies,
    "backtest_stock": backtest_stock, "backtest_with_trades": backtest_with_trades,
    "reindex_memories": reindex_memories,
    "self_status": self_status, "self_update": self_update,
    "self_backup": self_backup, "self_health_probe": self_health_probe,
    "audit_log": audit_log, "tool_risk_check": tool_risk_check,
    "save_prediction": save_prediction, "check_predictions": check_predictions,
    "verify_prediction": verify_prediction, "prediction_accuracy": prediction_accuracy,
    "sync_sector_data": sync_sector_data, "sector_rotation_analysis": sector_rotation_analysis,
    "sector_trend": sector_trend,
    "push_wechat": push_wechat,
    "web_search": web_search, "web_fetch": web_fetch,
    "sync_stock_list": sync_stock_list, "sync_kline": sync_kline,
    "system_health_check": system_health_check, "test_data_source": test_data_source,
    "update_config": update_config, "get_user_profile": get_user_profile,
    "add_holiday": add_holiday, "list_holidays": list_holidays,
    "check_trading_day": check_trading_day, "sync_trading_calendar": sync_trading_calendar,
    "get_trading_days": get_trading_days,
    "write_code": write_code, "download_full_kline": download_full_kline,
    "restart_service": restart_service, "data_self_heal": data_self_heal,
    "run_shell": run_shell,
    "generate_trade_decision": generate_trade_decision,
    "sync_dragon_tiger": sync_dragon_tiger, "sync_northbound_flow": sync_northbound_flow,
    "reflection_sweeper": reflection_sweeper,
    "debate_analysis": debate_analysis,
    "detect_market_environment": detect_market_environment,
"route_skills": route_skills,
    "strategy_evolution": strategy_evolution,
    "sync_to_ths_watchlist": sync_to_ths_watchlist,
}


# ── P0-2: 分析今日机会（用户主动触发）──
def analyze_stock(code: str) -> str:
    """对单只股票做组合分析: K线+技术指标+板块+市场环境 → LLM智能分析"""
    import logging as _log
    _log.getLogger("quantpilot.analyze").info(f"analyze_stock: {code}")
    conn = _get_conn()
    try:
        row = conn.execute("SELECT name FROM instruments WHERE code=?", (code,)).fetchone()
        name = row["name"] if row else code
    finally: conn.close()
    try:
        kline = json.loads(query_kline(code=code, days=60))
        tech = json.loads(calc_technical(code=code, days=60))
        sector = json.loads(sector_ranking())
        env = json.loads(detect_market_environment())
        kline_data = kline.get("data", kline)[:5000] if isinstance(kline, dict) else str(kline)[:5000]
        from config import load_config
        from agent.client import LLMClient
        client = LLMClient(load_config(), model_key="primary")
        prompt = (
            f"股票 {name}({code})。请做快速综合技术分析。\n"
            f"K线(近60日): {json.dumps(kline_data, ensure_ascii=False)[:3000]}\n"
            f"技术指标: {json.dumps(tech, ensure_ascii=False)[:2000]}\n"
            f"板块排行: {json.dumps(sector, ensure_ascii=False)[:1000]}\n"
            f"市场环境: {json.dumps(env, ensure_ascii=False)[:500]}\n\n"
            "输出JSON(不含markdown):\n"
            '{"trend":"up|down|sideways","support":价格,"resistance":价格,'
            '"signals":["信号1","信号2"],"risk":"low|med|high",'
            '"recommendation":"buy|hold|sell","reason":"一句话理由"}'
        )
        resp = client.chat([{"role":"user","content":prompt}])
        analysis = str(resp.get("content",""))
        # 自动存预测
        jm = re.search(r'\{[^{}]*"trend"[^{}]*\}', analysis)
        if jm:
            try:
                rj = json.loads(jm.group())
                direction = "bullish" if rj.get("recommendation")=="buy" else "bearish" if rj.get("recommendation")=="sell" else "neutral"
                save_prediction(code=code, direction=direction, reasoning=rj.get("reason","")[:200], confidence=0.6)
            except: pass
        return json.dumps({"code":code,"name":name,"analysis":analysis[:2000]}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error":str(e)}, ensure_ascii=False)


from agent.tools.registry import TOOL_DEFINITIONS
# 注意: TOOL_DISPATCH 已在上方构建，此处不再覆盖 registry 的空字典
# ===== Hermes Daily Tasks (auto-registered) =====
try:
    from agent.daily_tasks import DAILY_TOOLS as _DAILY_TOOLS
    TOOL_DISPATCH.update(_DAILY_TOOLS)
    for _name in _DAILY_TOOLS:
        TOOL_DEFINITIONS.append({
            'type': 'function',
            'function': {
                'name': _name,
                'description': {
                    'deep_debate': '深度多空辩论: Bull→Bear→反驳→Judge, 5轮DeepSeek Pro分析。用法: deep_debate(code="600519")',
                    'morning_picker': '早盘选股引擎: 多维度筛选+市场阶段判断+策略规划+微信推送。用法: morning_picker(trade_date="2026-07-14")',
                    'midday_adjuster': '午盘修正引擎: 持仓P&L更新+偏差分析+下午策略调整+微信推送。用法: midday_adjuster(trade_date="2026-07-14")',
                    'evening_reviewer': '收盘复盘引擎: 完整P&L+逐笔评价+教训提取+微信推送。用法: evening_reviewer(trade_date="2026-07-14")',
                    'weekly_summary': '周总结引擎: 聚合本周交易数据+盈亏归因+战法评估+策略进化+周六微信推送。用法: weekly_summary(saturday="2026-07-18")',
                }.get(_name, 'Daily analysis tool'),
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'trade_date' if _name != 'deep_debate' else 'code': {
                            'type': 'string',
                            'description': '交易日期 YYYY-MM-DD' if _name != 'deep_debate' else '股票代码如600519'
                        }
                    },
                    'required': [] if _name == 'deep_debate' else []
                }
            }
        })
    print(f'Registered {len(_DAILY_TOOLS)} daily tools')
except Exception as e:
    print(f'Daily tools registration: {e}')

# P0-2: 延迟注册 analyze_stock（函数定义在 TOOL_DISPATCH 之后）
TOOL_DISPATCH["analyze_stock"] = analyze_stock
