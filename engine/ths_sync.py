"""
归爻 — 同花顺协议同步模块 (THS Sync)

功能：
  1. 自动同步自选股（每日开盘前将候选股推入同花顺自选股列表）
  2. 下单接口骨架（预留，后续扩展）
  3. 失效自动检测 + 企微通知

使用前准备工作：
  1. 在 PC 上打开 Fiddler（或 Wireshark）
  2. 登录同花顺客户端 → 添加一只股票到自选股
  3. 抓到对应的 HTTP/TCP 请求，将 URL、Headers、Payload 填入下方常量
  4. 部署到云端 → 每天自动调用 sync_watchlist()

协议依据：https://github.com/limitget/THS（同花顺 APP 逆向工程）
"""

import json, time, hashlib, logging
from typing import List, Optional
from pathlib import Path
from datetime import datetime

logger = logging.getLogger("ths_sync")

# ════════════════════════════════════════════
# 以下常量需通过 Fiddler 抓包后填写
#
# 抓包步骤:
#   1. 启动 Fiddler, 设置解密 HTTPS
#   2. 打开同花顺 PC 客户端, 登录账号
#   3. 添加一只股票到自选股
#   4. 在 Fiddler 中找到对应的 POST/GET 请求
#   5. 将 URL、Headers、Body 格式填入下方
# ════════════════════════════════════════════

# 同花顺 API 基础地址（抓包后替换）
THS_HOST = "https://api.10jqka.com.cn"  # 占位，需实际抓包确认

# 自选股同步接口
WATCHLIST_URL = THS_HOST + "/user/selfstock/sync"  # 占位

# 固定请求头（抓包后替换）
WATCHLIST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Content-Type": "application/json",
    "Cookie": "请在此处填写从 Fiddler 抓到的 Cookie",  # ← 需填写
}

# 签名密钥（可能不需要，占位）
SIGN_KEY = ""


def _default_payload(codes: List[str]) -> dict:
    """构造自选股同步请求体（占位，实际格式需抓包确认）"""
    return {
        "stock_list": codes,
        "timestamp": int(time.time()),
        "action": "replace",  # replace=全量替换, append=追加
    }


# 请求体构造函数（如需自定义格式，替换此函数）
build_payload = _default_payload


# ════════════════════════════════════════════
# 核心类
# ════════════════════════════════════════════

class THSSyncError(Exception):
    """同花顺同步失败"""
    pass


class THSSync:
    """
    同花顺协议同步器

    用法:
        from engine.ths_sync import THSSync
        ths = THSSync(username="your_phone", password="your_pwd")
        ths.sync_watchlist(["000001", "000002", "600519"])
    """

    def __init__(self, username: str = "", password: str = "",
                 cookie: str = "", host: str = THS_HOST):
        self.host = host
        self._username = username
        self._password = password
        self._cookie = cookie or WATCHLIST_HEADERS.get("Cookie", "")
        self._last_error = ""
        # 先试 TCP 协议，不行再试 HTTP
        self._use_tcp = bool(username and password)
        self._use_http = bool(self._cookie and "请在此处" not in self._cookie)
        self._configured = self._use_tcp or self._use_http

    # ── 公共方法 ──

    def sync_watchlist(self, codes: List[str]) -> bool:
        """
        同步自选股列表（全量替换）

        Args:
            codes: 股票代码列表，如 ["000001", "000002"]

        Returns:
            True  = 同步成功
            False = 同步失败

        失败时自动发送企微通知。
        """
        if not self._configured:
            logger.warning("THS 同步未配置，跳过。需设置账号密码或 cookie。")
            return False

        # 登录并同步
        if self._use_tcp:
            from engine.ths_protocol import sync_watchlist as ths_sync
            ok, err = ths_sync(self._username, self._password, codes)
            if ok:
                return True
            self._last_error = f"THS 失败: {err}"
            logger.warning(self._last_error)

        if self._use_http:
            payload = build_payload(codes)
            try:
                ok = self._call_api(WATCHLIST_URL, payload)
                if ok:
                    return True
            except THSSyncError as e:
                self._last_error = str(e)

        self._notify_failure(codes)
        return False

    def place_order(self, code: str, direction: str, price: float,
                    shares: int, broker: str = "银河") -> bool:
        """
        下单接口（骨架，待后续实现）

        Args:
            code:      股票代码
            direction: "buy" 或 "sell"
            price:     委托价格
            shares:    股数
            broker:    "银河" 或 "国泰海通"
        """
        logger.warning(f"下单接口尚未实现: {code} {direction} {shares}@{price}")
        return False

    @property
    def is_configured(self) -> bool:
        """是否已配置cookie"""
        return self._configured

    @property
    def last_error(self) -> str:
        return self._last_error

    # ── 内部方法 ──

    def _call_api(self, url: str, payload: dict) -> bool:
        """调用同花顺 API"""
        import requests
        try:
            resp = requests.post(
                url,
                json=payload,
                headers={
                    **WATCHLIST_HEADERS,
                    "Cookie": self._cookie,
                },
                timeout=15,
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("status_code") == 0 or data.get("code") == 0:
                    logger.info(f"THS 同步成功: {len(payload.get('stock_list',[]))} 只")
                    return True
                else:
                    raise THSSyncError(f"API 返回异常: {data}")
            elif resp.status_code in (401, 403):
                raise THSSyncError("Cookie 过期")
            else:
                raise THSSyncError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        except requests.exceptions.ConnectTimeout:
            raise THSSyncError("连接超时，同花顺服务器可能不可达")
        except requests.exceptions.SSLError:
            raise THSSyncError("SSL 证书错误，可能需更新 CA")
        except THSSyncError:
            raise
        except Exception as e:
            raise THSSyncError(f"请求异常: {e}")

    def _notify_failure(self, codes: List[str]):
        """同步失败 → 企微通知"""
        msg = (
            f"⚠️ 同花顺自选股同步失败\n"
            f"原因: {self._last_error}\n"
            f"待同步: {codes[:5]}... 共{len(codes)}只\n"
            f"解决: 在 Windows 电脑打开同花顺→F12抓新包→更新 Cookie\n"
            f"时间: {datetime.now().strftime('%m-%d %H:%M')}"
        )
        try:
            from wechat.server import send_text_message
            send_text_message("admin", msg)
        except ImportError:
            logger.warning(f"[本地调试] 企微通知: {msg}")

    # ── 配置管理 ──

    @classmethod
    def update_cookie(cls, cookie: str):
        """更新 Cookie（失效后调用）"""
        path = Path(__file__).parent.parent / "config.json"
        config = {}
        if path.exists():
            config = json.loads(path.read_text())
        config["ths_cookie"] = cookie
        path.write_text(json.dumps(config, ensure_ascii=False, indent=2))
        logger.info("THS Cookie 已更新")


# ════════════════════════════════════════════
# 便捷命令行接口
# ════════════════════════════════════════════

def sync_main(codes: List[str]):
    """每日调用入口"""
    import sys, json
    sys.path.insert(0, str(Path(__file__).parent.parent))
    cfg_path = Path(__file__).parent.parent / "config.json"
    if cfg_path.exists():
        cfg = json.loads(cfg_path.read_text())
        ths = THSSync(username=cfg.get("ths_username",""), password=cfg.get("ths_password",""))
    else:
        ths = THSSync()
    if not ths.is_configured:
        print("❌ THS 未配置。需在 config.json 中设置 ths_username/ths_password")
        return
    ok = ths.sync_watchlist(codes)
    print(f"{'✅' if ok else '❌'} THS 同步: {len(codes)} 只 → {'成功' if ok else '失败'}")


def update_cookie(cookie: str):
    """从命令行更新 cookie"""
    THSSync.update_cookie(cookie)
    print(f"✅ Cookie 已更新")


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 3 and sys.argv[1] == "cookie":
        update_cookie(sys.argv[2])
    elif len(sys.argv) >= 2:
        sync_main(sys.argv[1].split(","))
    else:
        print("用法:")
        print("  python engine/ths_sync.py 000001,000002,600519    # 同步自选股")
        print("  python engine/ths_sync.py cookie 'new_cookie'     # 更新cookie")
