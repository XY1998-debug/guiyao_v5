"""企业微信消息处理器 — 归爻 (GY) 统一 Agent"""

import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger("quantpilot.wechat")
TZ = ZoneInfo("Asia/Shanghai")

# 全局 Agent 实例（统一归爻，不再分 QP/CC）
_agent = None
_config = None


def init_agent(config: dict):
    """初始化 Agent（延迟加载）"""
    global _config
    _config = config


def _get_agent(user_id: str = ""):
    """获取归爻 Agent 实例（单例，全能力）"""
    global _agent
    model_key = _get_user_model(user_id)
    if _agent is None:
        from agent.agent import QuantPilotAgent
        # admin_mode=True: 开放全部工具（写代码/重启/数据同步等）
        _agent = QuantPilotAgent(_config, admin_mode=True, user_id=user_id, model_key=model_key)
    elif _agent.model_key != model_key:
        from agent.agent import QuantPilotAgent
        _agent = QuantPilotAgent(_config, admin_mode=True, user_id=user_id, model_key=model_key)
    return _agent


# 用户模型记忆: user_id → "primary" | "flash"
_user_model: dict[str, str] = {}


def _get_user_model(user_id: str) -> str:
    return _user_model.get(user_id, "flash")


async def handle_message(user_id: str, user_name: str, content: str, msg_type: str = "text") -> dict:
    """处理用户消息，返回回复内容"""
    content = content.strip()

    # ── 命令处理 ──
    if content.startswith("/"):
        return await _handle_command(user_id, content)

    # ── 帮助 ──
    if content.lower() in ("帮助", "help", "?"):
        return await _handle_help()

    # ── reset ──
    if content in ("重置", "reset", "清空对话"):
        ag = _get_agent(user_id)
        ag.reset()
        return {"reply": "对话已重置，归爻随时待命。", "msgtype": "text"}

    # ── Agent 对话 ──
    try:
        model_key = _get_user_model(user_id)
        ag = _get_agent(user_id)
        response = ag.chat(content)

        # Pro 模式标记
        if model_key == "primary":
            response += "\n\n---\n💎 Pro 模式 · `/flash` 切回 Flash 省成本"

        if len(response.encode("utf-8")) > 1800:
            pass  # 交由 send_text_message 自动分片

        return {"reply": response, "msgtype": "text"}
    except Exception as e:
        logger.error(f"归爻处理失败: {e}")
        return {"reply": f"处理出错了: {e}\n请稍后重试或输入「重置」", "msgtype": "text"}


async def _handle_command(user_id: str, cmd: str) -> dict:
    """处理斜杠命令"""
    parts = cmd.split(maxsplit=1)
    command = parts[0].lower()
    args = parts[1] if len(parts) > 1 else ""

    if command == "/help":
        return await _handle_help()

    elif command == "/gy" and args:
        return await _handle_gy(user_id, args)

    elif command in ("/model", "/pro", "/flash"):
        model_key = "flash" if command == "/flash" else \
                     "primary" if command == "/pro" else \
                     args.strip() or "primary"
        return await _handle_model_switch(user_id, model_key)

    elif command == "/health":
        from agent.tools import system_health_check
        result = system_health_check()
        data = json.loads(result)
        lines = [f"**{k}**: {v}" for k, v in data.items()]
        return {"reply": "## 系统健康检查\n" + "\n".join(lines), "msgtype": "markdown"}

    elif command == "/calendar":
        from src.calendar import get_calendar
        cal = get_calendar()
        s = cal.get_status()
        return {"reply": f"""## 交易日历
> {s['today']}

- 今日: **{'交易日' if s['is_trading_day'] else '休市'}**
- 总交易日: {s['total_trading_days']}
- 下一个交易日: {s['next_trading_day']}
- 范围: {s['date_range']}""", "msgtype": "markdown"}

    elif command == "/position":
        from agent.tools import view_portfolio
        result = json.loads(view_portfolio())
        positions = result.get("positions", [])
        if not positions:
            return {"reply": "当前实盘空仓", "msgtype": "text"}
        lines = ["## 当前持仓"]
        for p in positions:
            lines.append(f"- **{p['name']}**({p['code']}) 成本{p['cost']:.2f} x{p['shares']}股")
        return {"reply": "\n".join(lines), "msgtype": "markdown"}

    elif command == "/alert":
        from agent.tools import view_alerts
        result = json.loads(view_alerts(limit=5))
        alerts = result.get("alerts", [])
        if not alerts:
            return {"reply": "暂无告警", "msgtype": "text"}
        lines = ["## 最新告警"]
        for a in alerts:
            lines.append(f"- [{a['created_at'][:16]}] {a['alert_type']}: {a['name']}({a['code']})")
        return {"reply": "\n".join(lines), "msgtype": "markdown"}

    elif command == "/watchlist":
        from agent.tools import list_watchlist
        result = json.loads(list_watchlist())
        wl = result.get("watchlist", [])
        if not wl:
            return {"reply": "盯盘列表为空", "msgtype": "text"}
        lines = ["## 盯盘列表"]
        for w in wl:
            lines.append(f"- {w['name']}({w['code']}) {'🔔' + w.get('condition','') if w.get('condition') else ''}")
        return {"reply": "\n".join(lines), "msgtype": "markdown"}

    elif command == "/search" and args:
        from agent.tools import web_search
        result = json.loads(web_search(args, max_results=3))
        results = result.get("results", [])
        if not results:
            return {"reply": f"未找到「{args}」相关信息", "msgtype": "text"}
        lines = [f"## 搜索: {args}"]
        for r in results:
            lines.append(f"- [{r['title']}]({r['url']})")
        return {"reply": "\n".join(lines), "msgtype": "markdown"}

    elif command == "/sync":
        from agent.tools import sync_trading_calendar
        result = json.loads(sync_trading_calendar())
        return {"reply": f"交易日历已同步: {result.get('trading_days', 0)} 个交易日", "msgtype": "text"}

    elif command == "/update":
        return await _handle_update()

    elif command == "/source":
        try:
            from src.sources.manager import DataSourceManager
            from config import config as cfg
            mgr = DataSourceManager(cfg)
            health = mgr.health_check()
            lines = ["## 数据源状态"]
            ok_count = 0
            for name, s in health.items():
                icon = "✅" if s == "ok" else "❌"
                lines.append(f"- {icon} {name}: {s}")
                if s == "ok":
                    ok_count += 1
            lines.append(f"\n{ok_count}/{len(health)} 可用")
            return {"reply": "\n".join(lines), "msgtype": "markdown"}
        except Exception as e:
            return {"reply": f"数据源查询失败: {e}", "msgtype": "text"}

    else:
        return {"reply": f"未知命令: {command}\n输入 /help 查看帮助", "msgtype": "text"}


async def _handle_model_switch(user_id: str, model_key: str) -> dict:
    """切换 LLM 模型"""
    if model_key not in ("primary", "flash"):
        return {"reply": f"未知模型: {model_key}。可用: pro, flash", "msgtype": "text"}
    global _agent
    _agent = None
    old_model = _user_model.get(user_id, "flash")
    _user_model[user_id] = model_key
    names = {"primary": "DeepSeek V4 Pro", "flash": "DeepSeek V4 Flash"}
    hint = ""
    if model_key == "primary":
        hint = "\n⚠️ Pro 模式每条回复末尾会标记提醒，用完记得 `/flash` 切回。"
    elif old_model == "primary":
        hint = "\n✅ 已从 Pro 切回 Flash，回复不再带标记。"
    return {"reply": f"已切换到 **{names[model_key]}**{hint}", "msgtype": "markdown"}


async def _handle_gy(user_id: str, content: str) -> dict:
    """调归爻引擎（GYEngine 快速通道）"""
    try:
        from gy_client import get_engine
        engine = get_engine(model_key="flash")
        reply = engine.chat(content, user_id=user_id)
        if not reply:
            reply = "(归爻无输出)"
        if len(reply.encode("utf-8")) > 1900:
            reply = reply[:800] + "\n\n...(回复过长已截断)"
        return {"reply": reply, "msgtype": "text"}
    except Exception as e:
        return {"reply": f"[GY] 错误: {e}\n输入「/gy reset」重置对话", "msgtype": "text"}


async def _handle_update() -> dict:
    """更新部署: git pull + 智能重启"""
    try:
        import subprocess, os
        result = subprocess.run(
            ["bash", "deploy.sh"],
            cwd=os.path.expanduser("~/quantpilot"),
            capture_output=True, text=True, timeout=30
        )
        output = result.stdout[-800:]
        return {"reply": f"## 部署结果\n```\n{output}\n```", "msgtype": "markdown"}
    except subprocess.TimeoutExpired:
        return {"reply": "部署超时，请到服务器查看日志 /tmp/qp.log", "msgtype": "text"}
    except Exception as e:
        return {"reply": f"部署失败: {e}", "msgtype": "text"}


async def _handle_help() -> dict:
    """帮助信息"""
    from src.calendar import get_calendar
    cal = get_calendar()
    status = "交易日" if cal.is_trading_day() else "休市"

    model_tag = _get_user_model("")
    model_name = {"primary": "DeepSeek V4 Pro", "flash": "DeepSeek V4 Flash"}
    default_note = " *(默认)*" if model_tag == "flash" else ""

    return {"reply": f"""## 归爻 (GY) 帮助
> 身份: YangJie 的个人大总管
> 今日: {status} | {datetime.now(TZ).strftime('%Y-%m-%d %H:%M')}

**直接对话:**
输入股票代码/名称或问题，如:
"分析贵州茅台" / "MACD金叉选股" / "半导体板块怎么样"
也支持运维指令: "重启服务" / "下载全量日K" / "数据自愈"

**模型切换:**
- `/pro` 用 Pro ({model_name['primary']})
- `/flash` 用 Flash ({model_name['flash']}){default_note}
- 当前: **{model_name[model_tag]}**

**快捷命令:**
- `/health` 系统健康检查
- `/position` 当前持仓
- `/alert` 最新告警
- `/watchlist` 盯盘列表
- `/calendar` 交易日历
- `/search 关键词` 联网搜索
- `/sync` 同步交易日历
- `/source` 数据源状态
- `/update` 更新部署
- `/gy 问题` 调归爻快速通道
""", "msgtype": "markdown"}
