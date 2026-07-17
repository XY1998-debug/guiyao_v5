# -*- coding: utf-8 -*-
"""GY Daily Analysis Engine — 4 Deep Modules: debate / morning / midday / evening

V2.1 (2026-07-13): 
  - 交易日历集成 (P1-3)
  - 复盘三分模型 (P1-2)
  - 周总结模块 (P1-1)
"""
import json, re, logging, sqlite3
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger("quantpilot.daily_tasks")
DB = "/home/ubuntu/quantpilot/data/quantpilot.db"

def _db():
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row; return c

def _llm():
    from config import load_config; from agent.client import LLMClient
    return LLMClient(load_config(), model_key="primary")

def _today():
    c = _db()
    try:
        r = c.execute("SELECT trade_date FROM daily_kline WHERE code='000001' ORDER BY trade_date DESC LIMIT 1").fetchone()
        return r["trade_date"] if r else datetime.now().strftime("%Y-%m-%d")
    finally: c.close()

def _trading(date_str: str) -> bool:
    """检查是否为交易日 (P1-3: 基于同步后的 trading_calendar)"""
    c = _db()
    try:
        r = c.execute("SELECT trade_date FROM trading_calendar WHERE trade_date=?", (date_str,)).fetchone()
        return bool(r)
    finally: c.close()

def _is_holiday(date_str: str) -> Optional[str]:
    """检查是否为节假日"""
    c = _db()
    try:
        r = c.execute("SELECT name FROM holidays WHERE trade_date=?", (date_str,)).fetchone()
        return r["name"] if r else None
    finally: c.close()

def _name(code):
    c = _db()
    try:
        r = c.execute("SELECT name FROM instruments WHERE code=?", (code,)).fetchone()
        return r["name"] if r else code
    finally: c.close()

def _k(code, days=120):
    c = _db()
    try:
        rows = c.execute(
            "SELECT trade_date,open,high,low,close,volume FROM daily_kline WHERE code=? ORDER BY trade_date DESC LIMIT ?",
            (code, days)).fetchall()
        return [dict(r) for r in reversed(rows)]
    finally: c.close()

def _pos():
    c = _db()
    try: return [dict(r) for r in c.execute("SELECT * FROM live_positions").fetchall()]
    finally: c.close()

def _call(name, **kw):
    from agent.tools import TOOL_DISPATCH
    fn = TOOL_DISPATCH.get(name)
    if not fn: return {"error": "not found"}
    try:
        r = fn(**kw)
        return json.loads(r) if isinstance(r, str) else r
    except Exception as e:
        return {"error": str(e)}


# ====================================================================
# PROMPTS
# ====================================================================

BULL = """你是归爻辩论多头(Bull)。为{code}({name})构建最强看多理由。
【K线】{kline_block}
【技术】{technicals}
【板块】{sector_context}
【市场】{market_env}
输出: 核心论点3-5条 + 技术面 + 资金面 + 目标价 + 风险"""

BEAR = """你是归爻辩论空头(Bear)。为{code}({name})构建最强看空理由。
【K线】{kline_block}
【技术】{technicals}
【板块】{sector_context}
【市场】{market_env}
输出: 核心论点3-5条 + 技术风险 + 资金预警 + 支撑 + 反指"""

JUDGE = """你是归爻辩论裁判(Judge)。{code}({name})辩论已完成。
【多头】{bull_args}
【空头】{bear_args}
【板块】{sector_context}|【市场】{market_env}
输出纯JSON: {"decision":"buy|sell|hold","confidence":0.X,"entry_price":X,"stop_loss":X,"targets":[X],"risk_level":"low|medium|high","rationale":"理由","strategy_match":"战法"}"""

MORNING = """你是归爻早盘策略师。生成完整交易预案。
【市场】{market_data}
【板块】{sector_data}
【涨停】{limit_up_data}
【候选】{screened_stocks}
【持仓】{positions}
输出: 市场阶段+情绪温度 → 今日主线2-3个 → 精选3-5只(含买入区间/止损/仓位/战法) → 持仓建议 → 风控"""

MIDDAY = """你是归爻午盘修正师。评估上午并调整下午策略。
【盘面】{am_summary}
【持仓】{positions}
【热力图】{sector_heat}
输出: 偏差分析 + 下午调整 + 极简清单"""

# ── P1-2: 三分复盘模型 ──

PASS1 = """你是归爻预测验证师(Pass1/3)。验证今日预测。
【持仓盈亏】{position_pnl}
【预测数据】{predictions}
【实盘价格】{prices}
输出JSON: {"total":N,"correct":N,"accuracy":X,"summary":"一句话总结"}"""

PASS2 = """你是归爻操作评价师(Pass2/3)。结合验证结果评价操作。
【验证结果】{pass1_result}
【持仓盈亏】{position_pnl}
【盘面】{market_summary}
【模拟盘】{sim_performance}
【板块】{sector_final}
【涨停】{limit_up}
输出JSON: {"operations":[{"code":"","score":"A~D","reason":""}],"missed_ops":[],"sim_compare":"总结"}"""

PASS3 = """你是归爻教训师(Pass3/3)。提炼智慧 + 自选清理。
【操作评价】{pass2_result}
【验证结果】{pass1_result}
【持仓盈亏】{position_pnl}
【自选池】{watchlist_status}
输出JSON: {"lesson":"核心教训","lesson_details":"详细","watchlist_cleanup":[{"code":"","reason":""}],"tomorrow_focus":["方向"],"checklist":["条目"]}"""

# ── P1-1: 周总结 ──

WEEKLY = """你是归爻周度策略师。聚合本周数据深度复盘。
【盈亏】{weekly_pnl}
【交易】{weekly_trades}
【预测】{prediction_accuracy}
【策略进化】{strategy_evo}
【板块轮动】{sector_rotation}
【教训】{weekly_lessons}
分析: 盈亏归因/战法评估/执行质量/市场回顾/下周预判
输出Markdown: 盈亏概览→归因→战法→执行→教训→下周预判"""


# ====================================================================
# 1. DEEP DEBATE
# ====================================================================

def deep_debate(code: str) -> str:
    try:
        name = _name(code)
        klines = _k(code, 120)
        if len(klines) < 20:
            return json.dumps({"error": f"{code} kline too few ({len(klines)})"}, ensure_ascii=False)
        recent = klines[-20:]
        lines = ["Date       Open   High   Low    Close    Vol", "-"*55]
        for k in recent:
            lines.append(f"{k['trade_date']} {k['open']:7.2f} {k['high']:7.2f} {k['low']:7.2f} {k['close']:7.2f} {int(k['volume']//100):>6}")
        kline_block = "\n".join(lines)
        tech = _call("calc_technical", code=code, days=60)
        sector = _call("sector_ranking", limit=5)
        env = _call("detect_market_environment")
        closes = [k["close"] for k in klines[-60:]]
        avg = sum(closes)/len(closes) if closes else 1
        vol = f"{(max(closes)-min(closes))/avg*100:.1f}%"
        client = _llm()
        ctx = {"code": code, "name": name, "kline_block": kline_block,
               "technicals": json.dumps(tech, ensure_ascii=False)[:2000],
               "sector_context": json.dumps(sector, ensure_ascii=False)[:1000],
               "market_env": json.dumps(env, ensure_ascii=False)[:500]}
        bull = client.chat([{"role":"user","content": BULL.format(**ctx)}])
        bear = client.chat([{"role":"user","content": BEAR.format(**ctx)}])
        ba, be = str(bull.get("content",""))[:2500], str(bear.get("content",""))[:2500]
        br = client.chat([{"role":"user","content": f"看多分析:\n{ba[:1500]}\n空头反驳:\n{be[:800]}\n逐条反驳空头。"}])
        ber = client.chat([{"role":"user","content": f"看空分析:\n{be[:1500]}\n多头反驳:\n{ba[:800]}\n逐条反驳多头。"}])
        jctx = {**ctx, "volatility": vol, "bull_args": ba[:2000], "bear_args": be[:2000]}
        judge = client.chat([{"role":"user","content": JUDGE.format(**jctx)}])
        jt = str(judge.get("content",""))
        decision = {"decision":"hold","confidence":0}
        jm = re.search(r'\{[^{}]*"decision"[^{}]*\}', jt, re.DOTALL)
        if jm:
            try: decision = json.loads(jm.group())
            except: pass
        if decision.get("confidence",0) > 0.5:
            try:
                d = "bullish" if decision.get("decision")=="buy" else "bearish"
                _call("save_prediction", code=code, direction=d,
                      target_price=decision.get("targets",[0])[0] if decision.get("targets") else None,
                      stop_loss=decision.get("stop_loss"), timeframe_days=5,
                      reasoning=str(decision.get("rationale",""))[:200],
                      confidence=decision.get("confidence",0.5))
            except: pass
        return json.dumps({"code":code,"name":name,"bull":ba[:1200],"bear":be[:1200],
                          "bull_rebuttal":str(br.get("content",""))[:600],
                          "bear_rebuttal":str(ber.get("content",""))[:600],
                          "decision":decision}, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"deep_debate: {e}")
        return json.dumps({"error":str(e)}, ensure_ascii=False)


# ====================================================================
# 2. MORNING PICKER + 节假日跳过
# ====================================================================

def morning_picker(trade_date: str = "") -> str:
    try:
        today = trade_date or _today() or datetime.now().strftime("%Y-%m-%d")
        holiday = _is_holiday(today)
        if holiday:
            return json.dumps({"status":"skip","reason":f"节假日:{holiday}"}, ensure_ascii=False)
        if not _trading(today):
            return json.dumps({"status":"skip","reason":f"{today}非交易日"}, ensure_ascii=False)
        market = _call("market_overview")
        sector = _call("sector_ranking", trade_date=today, limit=10)
        limit_up = _call("limit_up_pool", trade_date=today)
        candidates = []
        try:
            rsi = _call("screen_stocks", conditions="RSI14<35 AND volume_ratio>1.5", limit=10)
            if isinstance(rsi, dict) and "stocks" in rsi: candidates.extend(rsi["stocks"])
        except: pass
        try:
            macd = _call("screen_stocks", conditions="MACD_golden_cross AND close>MA60", limit=10)
            if isinstance(macd, dict) and "stocks" in macd: candidates.extend(macd["stocks"])
        except: pass
        seen, unique = set(), []
        for c in candidates:
            sid = str(c) if isinstance(c,str) else c.get("code","")
            if sid and sid not in seen: seen.add(sid); unique.append(c)
        positions = _pos()
        ps = "\n".join([f"- {p['stock_name']}({p['stock_code']}) x{p['shares']} 成本{p['avg_cost']:.2f}" for p in positions[:10]]) if positions else "空仓"
        client = _llm()
        prompt = MORNING.format(
            market_data=json.dumps(market,ensure_ascii=False)[:1500],
            sector_data=json.dumps(sector,ensure_ascii=False)[:1500],
            limit_up_data=json.dumps(limit_up,ensure_ascii=False)[:1000],
            screened_stocks=json.dumps(unique[:15],ensure_ascii=False)[:2000],
            positions=ps)
        resp = client.chat([{"role":"user","content":prompt}])
        analysis = str(resp.get("content",""))
        _call("push_wechat", message=analysis[:1900], msg_type="markdown")
        return json.dumps({"date":today,"candidates":len(unique),"analysis":analysis[:2000]}, ensure_ascii=False)
    except Exception as e:
        logger.error(f"morning_picker: {e}")
        return json.dumps({"error":str(e)}, ensure_ascii=False)


# ====================================================================
# 3. MIDDAY ADJUSTER
# ====================================================================

def midday_adjuster(trade_date: str = "") -> str:
    try:
        today = trade_date or _today() or datetime.now().strftime("%Y-%m-%d")
        if not _trading(today):
            return json.dumps({"status":"skip","reason":f"{today}非交易日"}, ensure_ascii=False)
        sector = _call("sector_ranking", trade_date=today, limit=15)
        env = _call("detect_market_environment")
        positions = _pos()
        pl = []
        for p in positions[:5]:
            k = _k(p["code"],3)
            if k:
                lc = k[-1]["close"]
                pct = f"{(lc/p['avg_cost']-1)*100:+.1f}%" if p["cost"] else "?"
                pl.append(f"- {p['stock_name']}({p['stock_code']}) 成本{p['avg_cost']:.2f} 现{lc:.2f} {pct}")
        ps = "\n".join(pl) if pl else "空仓"
        client = _llm()
        prompt = MIDDAY.format(am_summary=json.dumps(env,ensure_ascii=False)[:1000], positions=ps,
                               sector_heat=json.dumps(sector,ensure_ascii=False)[:1500])
        resp = client.chat([{"role":"user","content":prompt}])
        analysis = str(resp.get("content",""))
        _call("push_wechat", message=analysis[:1900], msg_type="markdown")
        return json.dumps({"date":today,"analysis":analysis[:1500]}, ensure_ascii=False)
    except Exception as e:
        logger.error(f"midday_adjuster: {e}")
        return json.dumps({"error":str(e)}, ensure_ascii=False)


# ====================================================================
# 4. EVENING REVIEWER — 复盘三分 (P1-2) + 自选清理 (P0-4)
# ====================================================================

def evening_reviewer(trade_date: str = "") -> str:
    try:
        today = trade_date or _today() or datetime.now().strftime("%Y-%m-%d")
        if not _trading(today):
            return json.dumps({"status":"skip","reason":f"{today}非交易日"}, ensure_ascii=False)
        positions = _pos()
        pl_all, total = [], 0
        for p in positions[:10]:
            k = _k(p["code"],5)
            if k:
                lc = k[-1]["close"]
                pnl = (lc-p["cost"])*p["shares"]
                total += pnl
                pct = f"{(lc/p['avg_cost']-1)*100:+.1f}%"
                pl_all.append(f"- {p['stock_name']}({p['stock_code']}) 成本{p['avg_cost']:.2f} 现{lc:.2f} {pnl:+.0f}({pct})")
        ps = "\n".join(pl_all) if pl_all else "空仓"
        ps += f"\n\n总盈亏: {total:+.0f}元"
        market = _call("market_overview")
        sector = _call("sector_ranking", trade_date=today, limit=10)
        limit_up = _call("limit_up_pool", trade_date=today)
        sim = _call("view_sim_portfolio")
        client = _llm()
        # Pass 1: 预测验证
        pred_data = _call("prediction_accuracy")
        p1_prompt = PASS1.format(
            position_pnl=ps[:1500],
            predictions=json.dumps(pred_data,ensure_ascii=False)[:1500],
            prices=json.dumps([{"code":p["code"],"name":p.get("name",""),"date":today} for p in positions],ensure_ascii=False)[:1500])
        p1 = client.chat([{"role":"user","content":p1_prompt}])
        p1t = str(p1.get("content",""))
        # Pass 2: 操作评价
        p2_prompt = PASS2.format(
            pass1_result=p1t[:2000], position_pnl=ps[:1500],
            market_summary=json.dumps(market,ensure_ascii=False)[:1000],
            sim_performance=json.dumps(sim,ensure_ascii=False)[:800],
            sector_final=json.dumps(sector,ensure_ascii=False)[:1000],
            limit_up=json.dumps(limit_up,ensure_ascii=False)[:500])
        p2 = client.chat([{"role":"user","content":p2_prompt}])
        p2t = str(p2.get("content",""))
        # Pass 3: 教训 + 自选清理
        watchlist_status = "无"
        try:
            wl = _call("list_watchlist")
            if isinstance(wl,list) and len(wl)>0:
                watchlist_status = json.dumps([{"code":w.get("code",""),"name":w.get("name","")} for w in wl],ensure_ascii=False)[:500]
        except: pass
        p3_prompt = PASS3.format(pass1_result=p1t[:1500],pass2_result=p2t[:2000],position_pnl=ps[:1000],watchlist_status=watchlist_status)
        p3 = client.chat([{"role":"user","content":p3_prompt}])
        p3t = str(p3.get("content",""))
        # 教训提取
        cleaned = []
        try:
            jm = re.search(r'\{[^{}]*"lesson"[^{}]*\}', p3t, re.DOTALL)
            if jm:
                rj = json.loads(jm.group())
                lesson = rj.get("lesson","")
                lesson_dtls = rj.get("lesson_details","")
                if lesson:
                    _call("save_memory", content=f"{lesson}\n{lesson_dtls}"[:400], memory_type="learning", importance=0.8, tags="daily_review")
                # P0-4: 自选清理
                wl_cleanup = rj.get("watchlist_cleanup",[])
                for item in wl_cleanup:
                    code = item.get("code","")
                    reason = item.get("reason","")
                    if code and reason:
                        _call("remove_from_watchlist", code=code)
                        cleaned.append(f"{code}({reason})")
        except Exception as e:
            logger.warning(f"Pass3解析: {e}")
        # 推送
        push_msg = f"## {today} 收盘复盘(三分)\n\n### Pass1 预测验证\n{p1t[:500]}\n\n### Pass2 操作评价\n{p2t[:500]}\n\n### Pass3 教训\n{p3t[:600]}"
        if cleaned:
            push_msg += f"\n\n**自选已清理**: {', '.join(cleaned)}"
        _call("push_wechat", message=push_msg[:1900], msg_type="markdown")
        try: _call("reflection_sweeper")
        except: pass
        return json.dumps({"date":today,"total_pnl":total,"positions":len(positions),
                          "pass1":p1t[:500],"pass2":p2t[:500],"pass3":p3t[:500],
                          "cleaned":len(cleaned)}, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"evening_reviewer: {e}")
        return json.dumps({"error":str(e)}, ensure_ascii=False)


# ====================================================================
# 5. WEEKLY SUMMARY — 周总结 (P1-1)
# ====================================================================

def weekly_summary(saturday: str = None) -> str:
    try:
        if saturday:
            t = datetime.strptime(saturday,"%Y-%m-%d")
        else:
            t = datetime.now()+timedelta(days=(5-datetime.now().weekday()))
        monday = t - timedelta(days=t.weekday())
        week_days = []
        for i in range(5):
            d = monday+timedelta(days=i)
            ds = d.strftime("%Y-%m-%d")
            if _trading(ds): week_days.append(ds)
        if len(week_days)==0:
            return json.dumps({"status":"skip","reason":"本周无交易日"}, ensure_ascii=False)
        monday_str, friday_str = week_days[0], week_days[-1]
        conn = _db()
        try:
            trades = conn.execute("SELECT * FROM live_trades WHERE trade_date BETWEEN ? AND ? ORDER BY trade_date",
                                  (monday_str,friday_str)).fetchall()
            total_buy = sum(t["amount"] for t in trades if t["action"]=="买入")
            total_sell = sum(t["amount"] for t in trades if t["action"]=="卖出")
            trades_summary = f"买入{total_buy:+.0f}/卖出{total_sell:+.0f}/共{len(trades)}笔"
            trades_detail = "\n".join([f"- {t['trade_date']} {t['code']} {t['action']} {t['price']:.2f}x{t['shares']}" for t in trades[:20]]) or "无"
            positions = _pos()
            pl_total = 0
            pl_list = []
            for p in positions:
                k = _k(p["code"],5)
                if k:
                    lc = k[-1]["close"]
                    pnl = (lc-p["cost"])*p["shares"]
                    pl_total += pnl
                    pl_list.append(f"- {p['stock_name']}({p['stock_code']}) {pnl:+.0f}元")
            weekly_pnl = f"总盈亏:{pl_total:+.0f}元\n"+"\n".join(pl_list) if pl_list else "空仓"
            lessons = conn.execute("SELECT content FROM long_term_memories WHERE tags LIKE '%daily_review%' ORDER BY updated_at DESC LIMIT 5").fetchall()
            weekly_lessons = "\n".join([f"- {l['content'][:200]}" for l in lessons]) or "无"
            sector = _call("sector_rotation_analysis", days=5)
        finally: conn.close()
        evo = _call("strategy_evolution")
        pred_acc = _call("prediction_accuracy")
        client = _llm()
        prompt = WEEKLY.format(
            weekly_pnl=weekly_pnl[:1500], weekly_trades=trades_detail[:1500],
            prediction_accuracy=json.dumps(pred_acc,ensure_ascii=False)[:1000],
            strategy_evo=json.dumps(evo,ensure_ascii=False)[:1500],
            sector_rotation=json.dumps(sector,ensure_ascii=False)[:1500],
            weekly_lessons=weekly_lessons[:1000])
        resp = client.chat([{"role":"user","content":prompt}])
        analysis = str(resp.get("content",""))
        m = re.search(r"核心教训[：:]\s*(.+?)(?:\n|$)", analysis)
        if m:
            _call("save_memory", content=m.group(1), memory_type="learning", importance=0.9, tags="weekly_review")
        _call("push_wechat", message=analysis[:1900], msg_type="markdown")
        return json.dumps({"week_start":monday_str,"week_end":friday_str,"trading_days":len(week_days),
                          "total_trades":len(trades or []),"total_pnl":pl_total,
                          "analysis":analysis[:2000]}, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"weekly_summary: {e}")
        return json.dumps({"error":str(e)}, ensure_ascii=False)


# ====================================================================
# EXPORT
# ====================================================================
DAILY_TOOLS = {
    "deep_debate": deep_debate,
    "morning_picker": morning_picker,
    "midday_adjuster": midday_adjuster,
    "evening_reviewer": evening_reviewer,
    "weekly_summary": weekly_summary,
}
