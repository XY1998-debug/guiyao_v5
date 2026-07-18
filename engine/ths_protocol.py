"""
归爻 — 同花顺 HTTPS API 客户端

使用同花顺侧边栏进程 (hxexternal) 的 HTTPS 接口。
Fiddler 抓包发现 eq.10jqka.com.cn 提供 RESTful API。
"""

import json, time, hashlib, logging
from typing import List, Tuple, Optional
from pathlib import Path

logger = logging.getLogger("ths_api")

BASE = "https://eq.10jqka.com.cn"


class THSClient:
    """
    同花顺 HTTPS API 客户端

    用法:
        client = THSClient("username", "password")
        if client.login():
            client.sync_watchlist(["000001", "000002"])
    """

    def __init__(self, username: str, password: str):
        self.username = username
        self._pwd = password
        self._session = self._make_session()
        self._token = ""
        self._logged_in = False

    def login(self) -> bool:
        """登录 → 获取 token"""
        import requests
        pwd_md5 = hashlib.md5(self._pwd.encode()).hexdigest()
        url = f"{BASE}/user/login/v1"
        payload = {
            "username": self.username,
            "password": pwd_md5,
            "client": "pc",
            "device_id": "thy_client_pc",
        }
        try:
            resp = requests.post(url, json=payload, timeout=15)
            data = resp.json()
            if data.get("status_code") == 0:
                self._token = data.get("data", {}).get("token", "")
                self._logged_in = bool(self._token)
                if self._logged_in:
                    logger.info("THS 登录成功")
                    return True
            logger.warning(f"THS 登录失败: {data.get('status_msg','')}")
        except Exception as e:
            logger.error(f"THS 登录异常: {e}")
        return False

    def sync_watchlist(self, codes: List[str]) -> bool:
        """全量替换自选股列表"""
        if not self._logged_in:
            logger.warning("未登录")
            return False
        import requests
        url = f"{BASE}/user/selfstock/sync"
        payload = {
            "token": self._token,
            "stock_list": [{"code": c, "market": self._detect_market(c)} for c in codes],
            "action": "replace",
        }
        try:
            resp = requests.post(url, json=payload, timeout=15)
            data = resp.json()
            ok = data.get("status_code") == 0
            logger.info(f"自选同步 {len(codes)} 只 → {'成功' if ok else '失败'}")
            return ok
        except Exception as e:
            logger.error(f"自选同步异常: {e}")
            return False

    def get_watchlist(self) -> List[str]:
        """获取当前自选股列表"""
        if not self._logged_in:
            return []
        import requests
        url = f"{BASE}/user/selfstock/list"
        try:
            resp = requests.post(url, json={"token": self._token}, timeout=15)
            data = resp.json()
            if data.get("status_code") == 0:
                return [s["code"] for s in data.get("data", [])]
        except:
            pass
        return []

    @staticmethod
    def _detect_market(code: str) -> int:
        return 1 if code.startswith(("6", "5")) else 0

    @staticmethod
    def _make_session():
        """创建统一的 requests session"""
        import requests
        s = requests.Session()
        s.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Content-Type": "application/json",
            "Origin": "https://eq.10jqka.com.cn",
        })
        return s


def sync_watchlist(username: str, password: str, codes: List[str]) -> Tuple[bool, str]:
    """一键同步（供 ths_sync.py 调用）"""
    client = THSClient(username, password)
    if not client.login():
        return False, "登录失败"
    ok = client.sync_watchlist(codes)
    return ok, "" if ok else "同步失败"


def get_watchlist(username: str, password: str) -> Tuple[bool, List[str]]:
    """获取当前自选股（供外部调用）"""
    client = THSClient(username, password)
    if not client.login():
        return False, []
    codes = client.get_watchlist()
    return True, codes


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    import sys
    cfg = json.load(open(Path(__file__).parent.parent / "config.json"))
    if len(sys.argv) > 1 and sys.argv[1] == "list":
        ok, codes = get_watchlist(cfg["ths_username"], cfg["ths_password"])
        print(f"自选股: {codes}" if ok else "失败")
    else:
        ok, msg = sync_watchlist(cfg["ths_username"], cfg["ths_password"],
                                  ["000001", "000002", "600519"])
        print(f"同步: {'成功' if ok else msg}")
