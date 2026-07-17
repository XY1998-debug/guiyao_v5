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
