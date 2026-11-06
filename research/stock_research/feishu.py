"""飞书群自定义机器人适配：凭据仅从服务端配置取，不在异常中暴露URL。"""
import base64
import hashlib
import hmac
import json
import logging
import re
import time
from urllib.parse import urlparse

import httpx

from .config import settings


class DeliveryFailure(Exception):
    def __init__(self, message, status):
        super().__init__(message)
        self.status = status


def destination(required=True):
    url = settings.feishu_webhook_url.get_secret_value().strip()
    if not url:
        if required:
            raise ValueError("请先在本地.env配置FEISHU_WEBHOOK_URL；不要将地址写进代码或日志")
        return None
    try:
        parsed = urlparse(url)
        valid_port = parsed.port in (None, 443)
    except ValueError:
        raise ValueError("飞书Webhook格式无效，需要飞书官方HTTPS群自定义机器人地址") from None
    if not (parsed.scheme == "https" and parsed.hostname == "open.feishu.cn" and valid_port
            and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment
            and re.fullmatch(r"/open-apis/bot/v2/hook/[A-Za-z0-9_-]{8,160}", parsed.path)):
        raise ValueError("飞书Webhook格式无效，需要飞书官方HTTPS群自定义机器人地址")
    return hashlib.sha256(url.encode()).hexdigest()


def send(card):
    destination()
    body = {"msg_type": "interactive", "card": card}
    secret = settings.feishu_signing_secret.get_secret_value()
    if secret:
        timestamp = str(int(time.time()))
        key = f"{timestamp}\n{secret}".encode()
        body.update(timestamp=timestamp, sign=base64.b64encode(hmac.new(key, b"", hashlib.sha256).digest()).decode())
    if len(json.dumps(body, ensure_ascii=False).encode("utf-8")) > 18000:
        raise DeliveryFailure("通知正文超过本地18KB限额，未发送", "FAILED")
    # httpx INFO会记录完整URL，httpcore DEBUG可能带请求路径；群Webhook不能进入日志。
    for name in ("httpx", "httpcore", "httpcore.connection", "httpcore.http11", "httpcore.http2"):
        logging.getLogger(name).setLevel(logging.WARNING)
    try:
        with httpx.Client(timeout=settings.feishu_timeout_seconds, trust_env=False, follow_redirects=False) as client:
            response = client.post(settings.feishu_webhook_url.get_secret_value().strip(), json=body)
    except (httpx.ConnectError, httpx.ConnectTimeout):
        raise DeliveryFailure("无法建立飞书连接，本次未取得接收确认", "FAILED") from None
    except Exception:
        # 读超时可能是已发送但响应丢失，不能自动当失败重发；原异常可能包含Webhook。
        raise DeliveryFailure("飞书发送结果无法确认，请先查看群消息；不会自动重发", "UNKNOWN") from None
    if response.status_code != 200:
        raise DeliveryFailure(f"飞书返回HTTP {response.status_code}，未确认接收；请核对群消息和配置", "UNKNOWN")
    try:
        value = response.json()
        code = value.get("code", value.get("StatusCode")) if isinstance(value, dict) else None
    except Exception:
        code = None
    if type(code) is not int:
        raise DeliveryFailure("飞书响应格式无法确认接收结果，请核对群消息", "UNKNOWN")
    if code != 0:
        raise DeliveryFailure(f"飞书明确拒绝消息，业务码{code}；请检查机器人安全设置", "FAILED")
    return {"accepted": True, "code": code}
