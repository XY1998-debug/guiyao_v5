# 归爻 V5 — 4_消息推送 (v10 终版)
## wechat/crypto.py
`python
"""企业微信消息加解密 — WXBizMsgCrypt

兼容企业微信官方加解密算法。
来源: https://developer.work.weixin.qq.com/document/path/90968
"""

import base64
import hashlib
import random
import struct
import socket
import string
from typing import Optional

from Crypto.Cipher import AES


class WXBizMsgCrypt:
    """企业微信消息加解密工具"""

    def __init__(self, token: str, encoding_aes_key: str, corp_id: str):
        self.token = token
        self.corp_id = corp_id
        self.aes_key = base64.b64decode(encoding_aes_key + "=")

    def verify_url(self, msg_signature: str, timestamp: str, nonce: str, echostr: str) -> tuple[int, str]:
        """URL 验证：解密 echostr 并返回明文"""
        try:
            signature = self._sha1(self.token, timestamp, nonce, echostr)
            if signature != msg_signature:
                return (-1, "签名验证失败")
            plain = self._decrypt(echostr)
            return (0, plain.decode("utf-8"))
        except Exception as e:
            return (-1, str(e))

    def decrypt_msg(self, msg_signature: str, timestamp: str, nonce: str, post_data: str) -> tuple[int, str]:
        """解密消息 XML"""
        import xml.etree.ElementTree as ET
        try:
            xml_tree = ET.fromstring(post_data)
            encrypt = xml_tree.find("Encrypt")
            if encrypt is None:
                return (-1, "XML 中未找到 Encrypt 节点")
            encrypt_text = encrypt.text

            signature = self._sha1(self.token, timestamp, nonce, encrypt_text)
            if signature != msg_signature:
                return (-1, "签名验证失败")

            plain = self._decrypt(encrypt_text)
            return (0, plain.decode("utf-8"))
        except Exception as e:
            return (-1, str(e))

    def encrypt_msg(self, reply_xml: str, nonce: str, timestamp: str = None) -> str:
        """加密回复消息"""
        import time
        if timestamp is None:
            timestamp = str(int(time.time()))

        raw = self._random_str(16).encode("utf-8") + struct.pack("I", socket.htonl(len(reply_xml))) + reply_xml.encode("utf-8") + self.corp_id.encode("utf-8")
        # PKCS7 padding
        block_size = 32
        pad = block_size - len(raw) % block_size
        raw += bytes([pad] * pad)

        cipher = AES.new(self.aes_key, AES.MODE_CBC, self.aes_key[:16])
        encrypted = cipher.encrypt(raw)
        encrypt_text = base64.b64encode(encrypted).decode("utf-8")

        signature = self._sha1(self.token, timestamp, nonce, encrypt_text)

        return f"""<xml>
<Encrypt><![CDATA[{encrypt_text}]]></Encrypt>
<MsgSignature><![CDATA[{signature}]]></MsgSignature>
<TimeStamp>{timestamp}</TimeStamp>
<Nonce><![CDATA[{nonce}]]></Nonce>
</xml>"""

    def _decrypt(self, encrypt_text: str) -> bytes:
        """AES 解密"""
        cipher = AES.new(self.aes_key, AES.MODE_CBC, self.aes_key[:16])
        plain = cipher.decrypt(base64.b64decode(encrypt_text))
        # 去掉 PKCS7 padding
        pad = plain[-1]
        content = plain[16:-pad]  # 去掉前16字节随机串
        # 读取消息长度（4字节大端）
        msg_len = socket.ntohl(struct.unpack("I", content[:4])[0])
        msg = content[4:4 + msg_len]
        return msg

    def _sha1(self, *args) -> str:
        """SHA1 签名"""
        raw = "".join(sorted(args))
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def _random_str(length: int) -> str:
        return "".join(random.choice(string.ascii_letters + string.digits) for _ in range(length))

`

## wechat/handler.py
`python
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

`

## wechat/server.py
`python
"""企业微信回调服务器 — FastAPI 实现"""

import json
import logging
import xml.etree.ElementTree as ET
from urllib.parse import unquote

from fastapi import FastAPI, Request, Query
from fastapi.responses import PlainTextResponse, Response

from src.wechat.crypto import WXBizMsgCrypt
from src.wechat.handler import handle_message, init_agent
from src.wechat.session import session_manager

logger = logging.getLogger("quantpilot.wechat.server")

app = FastAPI(title="QuantPilot WeChat Callback")
_crypt: WXBizMsgCrypt = None
_config: dict = None
_access_token: str = None
_access_token_expire: float = 0


def init_wechat(config: dict):
    """初始化企业微信配置"""
    global _crypt, _config
    _config = config

    wechat = config.get("wechat", {})
    token = wechat.get("token", "")
    aes_key = wechat.get("encoding_aes_key", "")
    corp_id = wechat.get("corp_id", "")

    if not all([token, aes_key, corp_id]):
        logger.error("企业微信配置不完整，请检查 config.yaml 中 wechat 段")
        raise RuntimeError("企业微信配置不完整")

    _crypt = WXBizMsgCrypt(token, aes_key, corp_id)
    init_agent(config)
    logger.info("企业微信回调服务已初始化")


# ═══════════════════════════════════════════════
# 回调接口
# ═══════════════════════════════════════════════

@app.get("/wechat/callback")
async def verify_url(
    msg_signature: str = Query(..., alias="msg_signature"),
    timestamp: str = Query(...),
    nonce: str = Query(...),
    echostr: str = Query(...),
):
    """企业微信 URL 验证（GET 请求）"""
    if _crypt is None:
        return PlainTextResponse("server not ready", status_code=500)

    ret, plain = _crypt.verify_url(msg_signature, timestamp, nonce, unquote(echostr))
    if ret != 0:
        logger.error(f"URL 验证失败: {plain}")
        return PlainTextResponse("verify failed", status_code=403)

    logger.info("URL 验证成功")
    return PlainTextResponse(plain)


@app.post("/wechat/callback")
async def receive_message(
    request: Request,
    msg_signature: str = Query(..., alias="msg_signature"),
    timestamp: str = Query(...),
    nonce: str = Query(...),
):
    """接收企业微信消息（POST 请求）"""
    if _crypt is None:
        return PlainTextResponse("server not ready", status_code=500)

    body = await request.body()
    body_str = body.decode("utf-8")

    # 解密
    ret, plain = _crypt.decrypt_msg(msg_signature, timestamp, nonce, body_str)
    if ret != 0:
        logger.error(f"消息解密失败: {plain}")
        return PlainTextResponse("decrypt failed", status_code=403)

    # 解析 XML
    try:
        xml_tree = ET.fromstring(plain)
        msg_type = xml_tree.find("MsgType").text if xml_tree.find("MsgType") is not None else "unknown"
        from_user = xml_tree.find("FromUserName").text if xml_tree.find("FromUserName") is not None else "unknown"
        to_user = xml_tree.find("ToUserName").text if xml_tree.find("ToUserName") is not None else "unknown"
        content = xml_tree.find("Content").text if xml_tree.find("Content") is not None else ""
        create_time = xml_tree.find("CreateTime").text if xml_tree.find("CreateTime") is not None else "0"
    except Exception as e:
        logger.error(f"XML 解析失败: {e}")
        return PlainTextResponse("", status_code=200)  # 返回空，不报错

    logger.info(f"收到消息: from={from_user}, type={msg_type}, content={content[:50]}")
    # Admin 调试：输出 UserID 到 stderr（确保日志可捕获）
    import sys
    print(f"[WECHAT USERID] {from_user}", file=sys.stderr, flush=True)

    if msg_type != "text" or not content:
        return PlainTextResponse("", status_code=200)

    session_manager.get_or_create(from_user)

    # 异步回复：先返回200，后台处理
    import threading
    thr = threading.Thread(target=_async_reply, args=(from_user, content), daemon=True)
    thr.start()
    return PlainTextResponse("")

def _async_reply(user_id: str, content: str):
    try:
        import asyncio
        loop = asyncio.new_event_loop()
        result = loop.run_until_complete(handle_message(user_id, user_id, content))
        reply = result.get("reply", "")
        if reply:
            send_text_message(user_id, reply)
        loop.close()
    except Exception as e:
        logger.error(f"异步回复失败: {e}")


# ═══════════════════════════════════════════════
# 主动推送 API
# ═══════════════════════════════════════════════

def _get_access_token() -> str:
    """获取企业微信 access_token（缓存 2 小时）"""
    global _access_token, _access_token_expire
    import time as _time

    now = _time.time()
    if _access_token and now < _access_token_expire:
        return _access_token

    if _config is None:
        raise RuntimeError("企业微信未初始化")

    wechat = _config.get("wechat", {})
    corp_id = wechat.get("corp_id", "")
    secret = wechat.get("agent_secret", "")

    import requests
    resp = requests.get(
        "https://qyapi.weixin.qq.com/cgi-bin/gettoken",
        params={"corpid": corp_id, "corpsecret": secret},
        timeout=10,
    )
    data = resp.json()
    if data.get("errcode") != 0:
        raise RuntimeError(f"获取 access_token 失败: {data}")

    _access_token = data["access_token"]
    _access_token_expire = now + data.get("expires_in", 7200) - 300  # 提前5分钟刷新
    return _access_token


def _chunk_message(content: str, max_bytes: int = 1700) -> list:
    """将长消息按字节数分片，避免截断在多字节字符中间"""
    chunks = []
    while content:
        encoded = content.encode("utf-8")
        if len(encoded) <= max_bytes:
            chunks.append(content)
            break
        # 找到该字节数内的安全截断点
        truncated = encoded[:max_bytes]
        content = truncated.decode("utf-8", errors="ignore")
        # 从 content 向回找最近的自然断句位置
        wrap = max(content.rfind("\n"), content.rfind("。"), content.rfind("，"), content.rfind(". "))
        if wrap > 50:
            chunks.append(content[:wrap+1])
            rest = content[wrap+1:]
        else:
            chunks.append(content)
            rest = content[len(content):]
        content = rest
    return chunks


def send_text_message(user_id: str, content: str):
    """主动发送文本消息给指定用户（自动分片长消息）"""
    if not content:
        return

    chunks = _chunk_message(content)
    import time as _time

    for i, chunk in enumerate(chunks):
        try:
            token = _get_access_token()
            wechat = _config.get("wechat", {})
            agent_id = wechat.get("agent_id", 0)

            import requests
            resp = requests.post(
                f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={token}",
                json={
                    "touser": user_id,
                    "msgtype": "text",
                    "agentid": agent_id,
                    "text": {"content": chunk},
                },
                timeout=10,
            )
            result = resp.json()
            if result.get("errcode") != 0:
                logger.error(f"分片{i+1}/{len(chunks)}发送失败: {result}")
            else:
                logger.info(f"分片{i+1}/{len(chunks)}已发送: len={len(chunk)}")
        except Exception as e:
            logger.error(f"发送消息异常: {e}")

        if i < len(chunks) - 1:
            _time.sleep(1)  # 各分片间隔1秒，避免企微限频


def send_markdown_message(user_id: str, content: str):
    """主动发送 Markdown 消息"""
    if not content:
        return

    try:
        token = _get_access_token()
        wechat = _config.get("wechat", {})
        agent_id = wechat.get("agent_id", 0)

        import requests
        resp = requests.post(
            f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={token}",
            json={
                "touser": user_id,
                "msgtype": "markdown",
                "agentid": agent_id,
                "markdown": {"content": content},
            },
            timeout=10,
        )
        result = resp.json()
        if result.get("errcode") != 0:
            logger.error(f"Markdown 发送失败: {result}")
    except Exception as e:
        logger.error(f"发送 Markdown 异常: {e}")


def notify_daily_report(user_ids: list[str], report: str):
    """推送每日报告给指定用户列表"""
    for uid in user_ids:
        send_markdown_message(uid, report)


# ═══════════════════════════════════════════════
# 监控接口
# ═══════════════════════════════════════════════

@app.get("/wechat/status")
async def wechat_status():
    """企业微信服务状态"""
    return {
        "status": "running" if _crypt else "not_initialized",
        "active_sessions": session_manager.active_count,
        "has_access_token": bool(_access_token),
        "config": {
            "has_corp_id": bool(_config and _config.get("wechat", {}).get("corp_id")),
            "has_agent_id": bool(_config and _config.get("wechat", {}).get("agent_id")),
        } if _config else {},
    }

`

## wechat/session.py
`python
"""企业微信会话管理 — 多用户多轮对话上下文"""

import time
import threading
from typing import Optional


class Session:
    """单个用户的会话上下文"""

    def __init__(self, user_id: str):
        self.user_id = user_id
        self.created_at = time.time()
        self.last_active = time.time()
        self.message_count = 0

    def touch(self):
        self.last_active = time.time()
        self.message_count += 1

    def is_expired(self, ttl: int = 1800) -> bool:
        """30分钟无活动则过期"""
        return time.time() - self.last_active > ttl


class SessionManager:
    """会话管理器"""

    def __init__(self, ttl: int = 1800, max_sessions: int = 100):
        self._sessions: dict[str, Session] = {}
        self._ttl = ttl
        self._max = max_sessions
        self._lock = threading.Lock()

    def get_or_create(self, user_id: str) -> Session:
        with self._lock:
            # 清理过期
            expired = [k for k, v in self._sessions.items() if v.is_expired(self._ttl)]
            for k in expired:
                del self._sessions[k]

            # 已有会话
            if user_id in self._sessions:
                session = self._sessions[user_id]
                session.touch()
                return session

            # 超限清理最旧的
            if len(self._sessions) >= self._max:
                oldest = min(self._sessions.values(), key=lambda s: s.last_active)
                del self._sessions[oldest.user_id]

            session = Session(user_id)
            self._sessions[user_id] = session
            return session

    def remove(self, user_id: str):
        with self._lock:
            self._sessions.pop(user_id, None)

    @property

    def active_count(self) -> int:
        return len(self._sessions)


# 全局实例
session_manager = SessionManager()

`
