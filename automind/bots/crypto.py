"""IM 平台回调的验签与加解密（v1.11.6）。

* 企业微信 / 微信公众号：``WXBizMsgCrypt`` 同一套算法 —— SHA1 签名 + AES-256-CBC
  （密钥 = EncodingAESKey 补 ``=`` 后 base64 解码，IV = 密钥前 16 字节，PKCS#7 按 32 字节补齐，
  明文 = 16 字节随机串 + 4 字节网络序长度 + 消息 + ReceiveId）；
* 飞书：可选 Encrypt Key（AES-256-CBC，密钥 = sha256(EncryptKey)，密文前 16 字节是 IV）
  与请求签名 ``sha256(timestamp + nonce + encrypt_key + body)``；
* 钉钉：``base64(hmac_sha256(AppSecret, timestamp + "\\n" + AppSecret))``。

AES 依赖 ``cryptography``（Web 栈一般已装）；没装时只影响"开了加密"的回调，
并在报错里说清怎么装，而不是静默失败。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import struct


class CryptoUnavailable(RuntimeError):
    pass


def _cipher(key: bytes, iv: bytes):
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError as e:  # pragma: no cover - 依赖缺失时的提示
        raise CryptoUnavailable(
            "该渠道开启了消息加密，需要安装 cryptography：pip install cryptography") from e
    return Cipher(algorithms.AES(key), modes.CBC(iv))


def _pkcs7_pad(data: bytes, block: int) -> bytes:
    n = block - len(data) % block
    return data + bytes([n]) * n


def _pkcs7_unpad(data: bytes, block: int) -> bytes:
    if not data:
        raise ValueError("空的解密结果")
    n = data[-1]
    if n < 1 or n > block:
        raise ValueError("补位长度不合法（密钥不匹配？）")
    return data[:-n]


# ── 企业微信 / 微信公众号 ──────────────────────────────────────


def wx_signature(token: str, timestamp: str, nonce: str, encrypt: str = "") -> str:
    parts = sorted([token or "", timestamp or "", nonce or ""] + ([encrypt] if encrypt else []))
    return hashlib.sha1("".join(parts).encode("utf-8")).hexdigest()


class WXCrypt:
    """企业微信 / 公众号安全模式的消息加解密。"""

    def __init__(self, token: str, encoding_aes_key: str, receive_id: str) -> None:
        self.token = token
        self.receive_id = receive_id
        try:
            self.key = base64.b64decode((encoding_aes_key or "") + "=")
        except Exception as e:
            raise ValueError("EncodingAESKey 格式不对（应为 43 位）") from e
        if len(self.key) != 32:
            raise ValueError("EncodingAESKey 格式不对（应为 43 位）")

    def verify(self, signature: str, timestamp: str, nonce: str, encrypt: str) -> bool:
        return hmac.compare_digest(wx_signature(self.token, timestamp, nonce, encrypt),
                                   signature or "")

    def decrypt(self, encrypt_b64: str) -> str:
        d = _cipher(self.key, self.key[:16]).decryptor()
        plain = d.update(base64.b64decode(encrypt_b64)) + d.finalize()
        content = _pkcs7_unpad(plain, 32)[16:]
        (length,) = struct.unpack(">I", content[:4])
        msg = content[4:4 + length]
        rid = content[4 + length:].decode("utf-8", errors="replace")
        if self.receive_id and rid and rid != self.receive_id:
            raise ValueError("ReceiveId 不匹配（CorpID / AppID 填错了？）")
        return msg.decode("utf-8")

    def encrypt(self, text: str) -> str:
        msg = text.encode("utf-8")
        raw = os.urandom(16) + struct.pack(">I", len(msg)) + msg + self.receive_id.encode("utf-8")
        e = _cipher(self.key, self.key[:16]).encryptor()
        return base64.b64encode(e.update(_pkcs7_pad(raw, 32)) + e.finalize()).decode()


# ── 飞书 ──────────────────────────────────────────────────────


def feishu_decrypt(encrypt_key: str, encrypt_b64: str) -> str:
    key = hashlib.sha256(encrypt_key.encode("utf-8")).digest()
    buf = base64.b64decode(encrypt_b64)
    d = _cipher(key, buf[:16]).decryptor()
    plain = d.update(buf[16:]) + d.finalize()
    return _pkcs7_unpad(plain, 16).decode("utf-8")


def feishu_encrypt(encrypt_key: str, text: str) -> str:
    """测试用：按飞书的方式加密一段明文。"""
    key = hashlib.sha256(encrypt_key.encode("utf-8")).digest()
    iv = os.urandom(16)
    e = _cipher(key, iv).encryptor()
    return base64.b64encode(iv + e.update(_pkcs7_pad(text.encode("utf-8"), 16)) + e.finalize()).decode()


def feishu_signature(timestamp: str, nonce: str, encrypt_key: str, body: bytes) -> str:
    return hashlib.sha256((timestamp + nonce + encrypt_key).encode("utf-8") + body).hexdigest()


# ── 钉钉 ──────────────────────────────────────────────────────


def dingtalk_sign(app_secret: str, timestamp: str) -> str:
    mac = hmac.new(app_secret.encode("utf-8"), f"{timestamp}\n{app_secret}".encode(),
                   hashlib.sha256).digest()
    return base64.b64encode(mac).decode()
