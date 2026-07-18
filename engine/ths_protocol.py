"""
归爻 — 同花顺 TCP 协议客户端

基于 limitget/THS 逆向工程的公开协议结构实现：
- TCP 连接到同花顺服务器
- 自定义握手 + 登录鉴权
- Session 维持
- 操作指令（自选股同步 / 查询持仓 / 下单）

服务器地址：从同花顺客户端抓取（hxexternal 连接的 eq.10jqka.com.cn）
"""

import socket, json, struct, time, hashlib, hmac, logging
from typing import List, Optional, Dict, Tuple
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("ths_protocol")

# ════════════════════════════════════════════
# 协议常量
# ════════════════════════════════════════════

# 同花顺服务器地址（从 Fiddler 抓到的请求中提取）
THS_HOST = "eq.10jqka.com.cn"
THS_PORT = 443

# 操作码（占位，需实际调试后确认）
OP_LOGIN = 0x01
OP_SELFSTOCK_SYNC = 0x32
OP_HEARTBEAT = 0xFF

# ════════════════════════════════════════════
# 协议包结构
# ════════════════════════════════════════════

@dataclass
class THSPacket:
    """协议数据包"""
    opcode: int          # 操作码
    seq: int = 0         # 序列号
    body: bytes = b""    # 消息体
    timestamp: int = 0   # 时间戳

    def encode(self, session_key: bytes = b"") -> bytes:
        """打包为二进制流"""
        ts = self.timestamp or int(time.time())
        # 包结构: [2字节长度][1字节opcode][4字节seq][4字节ts][变长body]
        body_enc = self._encrypt(self.body, session_key) if session_key else self.body
        header = struct.pack("!H", 6 + len(body_enc))  # 总长度
        header += struct.pack("!B", self.opcode)
        header += struct.pack("!I", self.seq)
        header += struct.pack("!I", ts)
        return header + body_enc

    @classmethod
    def decode(cls, data: bytes, session_key: bytes = b"") -> "THSPacket":
        """从二进制流解析"""
        total_len = struct.unpack("!H", data[:2])[0]
        opcode = data[2]
        seq = struct.unpack("!I", data[3:7])[0]
        ts = struct.unpack("!I", data[7:11])[0]
        body = data[11:11+total_len-6]
        if session_key:
            body = cls._decrypt(body, session_key)
        return cls(opcode=opcode, seq=seq, body=body, timestamp=ts)

    @staticmethod
    def _encrypt(data: bytes, key: bytes) -> bytes:
        """简单 XOR 加密（占位，实际协议加密需逆向确认）"""
        return bytes(d ^ key[i % len(key)] for i, d in enumerate(data))

    @staticmethod
    def _decrypt(data: bytes, key: bytes) -> bytes:
        return THSPacket._encrypt(data, key)  # XOR 对称


# ════════════════════════════════════════════
# 客户端
# ════════════════════════════════════════════

class THSClient:
    """
    同花顺 TCP 协议客户端

    用法:
        client = THSClient("your_username", "your_password")
        client.login()
        client.sync_watchlist(["000001", "000002"])
        client.close()
    """

    def __init__(self, username: str, password: str,
                 host: str = THS_HOST, port: int = THS_PORT):
        self.username = username
        self.password = password
        self.host = host
        self.port = port
        self._sock: Optional[socket.socket] = None
        self._seq = 0
        self._session_key: bytes = b""
        self._logged_in = False

    # ── 连接管理 ──

    def connect(self) -> bool:
        """建立 TCP 连接"""
        try:
            self._sock = socket.create_connection((self.host, self.port), timeout=10)
            self._sock.settimeout(15)
            logger.info(f"THS 连接成功: {self.host}:{self.port}")
            return True
        except socket.timeout:
            logger.error("THS 连接超时")
            return False
        except Exception as e:
            logger.error(f"THS 连接失败: {e}")
            return False

    def close(self):
        if self._sock:
            try: self._sock.close()
            except: pass
        self._logged_in = False

    # ── 登录 ──

    def login(self) -> bool:
        """登录同花顺账号"""
        if not self._sock and not self.connect():
            return False

        # 构建登录请求
        login_body = json.dumps({
            "username": self.username,
            "password": self._hash_pwd(self.password),
            "client": "pc",
            "version": "029.60.20.0031",
        }).encode()

        pkt = THSPacket(opcode=OP_LOGIN, seq=self._next_seq(), body=login_body)
        self._send(pkt)

        resp = self._recv()
        if resp and resp.opcode == OP_LOGIN:
            data = json.loads(resp.body.decode())
            if data.get("code") == 0:
                self._session_key = bytes.fromhex(data.get("session_key", ""))
                self._logged_in = True
                logger.info("THS 登录成功")
                return True

        logger.warning(f"THS 登录失败")
        return False

    # ── 自选股同步 ──

    def sync_watchlist(self, codes: List[str]) -> bool:
        """全量替换自选股列表"""
        if not self._logged_in:
            logger.warning("THS 未登录，无法同步")
            return False

        body = json.dumps({
            "action": "replace",
            "list": [{"code": c, "market": self._detect_market(c)} for c in codes],
        }).encode()

        pkt = THSPacket(opcode=OP_SELFSTOCK_SYNC, seq=self._next_seq(), body=body)
        self._send(pkt)
        resp = self._recv()
        if resp and resp.opcode == OP_SELFSTOCK_SYNC:
            result = json.loads(resp.body.decode())
            ok = result.get("code") == 0
            logger.info(f"THS 自选同步: {len(codes)} 只 → {'成功' if ok else '失败'}")
            return ok
        return False

    # ── 内部 ──

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _send(self, pkt: THSPacket):
        data = pkt.encode(self._session_key)
        self._sock.sendall(data)

    def _recv(self) -> Optional[THSPacket]:
        try:
            header = self._sock.recv(11)  # 2+1+4+4
            if len(header) < 11:
                return None
            total_len = struct.unpack("!H", header[:2])[0]
            body = self._sock.recv(total_len - 6)
            return THSPacket.decode(header + body, self._session_key)
        except socket.timeout:
            return None
        except Exception as e:
            logger.error(f"THS recv 异常: {e}")
            return None

    @staticmethod
    def _hash_pwd(pwd: str) -> str:
        return hashlib.md5(pwd.encode()).hexdigest()

    @staticmethod
    def _detect_market(code: str) -> int:
        """判断市场: 0=深市 1=沪市"""
        if code.startswith(("6", "5")):
            return 1
        return 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


# ════════════════════════════════════════════
# 便捷接口（供 ths_sync.py 调用）
# ════════════════════════════════════════════

def sync_watchlist_tcp(username: str, password: str,
                       codes: List[str]) -> Tuple[bool, str]:
    """TCP 协议同步自选股（一次性调用）"""
    try:
        with THSClient(username, password) as client:
            if not client.login():
                return False, "登录失败"
            ok = client.sync_watchlist(codes)
            return ok, "" if ok else "同步失败"
    except socket.timeout:
        return False, "连接超时"
    except Exception as e:
        return False, str(e)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("THS 协议客户端 v0.1")
    print("用法: from engine.ths_protocol import sync_watchlist_tcp")
