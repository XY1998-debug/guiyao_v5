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
