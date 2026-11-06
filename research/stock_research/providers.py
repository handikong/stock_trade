"""数据商请求边界：限流状态不冒充行情缓存，日志不包含凭据。"""
import logging
from datetime import datetime, timedelta
import httpx
from sqlalchemy import select
from yfinance.exceptions import YFRateLimitError
from .config import settings
from .store import Session, Snapshot, now, save_snapshot

logger=logging.getLogger(__name__)
FMP_BASE="https://financialmodelingprep.com/stable/"


class ProviderFailure(Exception):
    def __init__(self, message, provider, category="unavailable", operation="", retry_at=None, http_status=None):
        super().__init__(message)
        self.provider=provider
        self.category=category
        self.operation=operation
        self.retry_at=retry_at
        self.http_status=http_status


def failure_detail(exc, operation=""):
    result={"message":str(exc) if isinstance(exc,(ProviderFailure,ValueError)) else "上游请求或解析失败，原有数据保留",
        "error_type":type(exc).__name__}
    if isinstance(exc,YFRateLimitError):
        result.update({"provider":"Yahoo Finance","category":"rate_limited",
            "message":"Yahoo触发限流，当前步骤未取得新数据；请稍后重试"})
    for key in ("provider","category","operation","retry_at","http_status"):
        value=getattr(exc,key,None)
        if value is not None and value!="":
            result[key]=value
    if operation:
        result.setdefault("operation",operation)
    response_status=getattr(getattr(exc,"response",None),"status_code",None)
    if isinstance(response_status,int) and 100<=response_status<=599:
        result.setdefault("http_status",response_status)
    return result


def yahoo_call(security_id, operation, fn):
    with Session() as session:
        state=session.scalar(select(Snapshot).where(Snapshot.kind=="provider_yahoo_limit")
            .order_by(Snapshot.created_at.desc(),Snapshot.id.desc()).limit(1))
    retry_at=state.payload.get("retry_at") if state else None
    if retry_at and datetime.fromisoformat(retry_at)>now():
        raise ProviderFailure("Yahoo处于限流冷却期，本次未重复发送请求；其他数据步骤继续执行",
            "Yahoo Finance","rate_limited",operation,retry_at)
    logger.info("[Yahoo] 正在执行 %s",operation)
    try:
        return fn()
    except YFRateLimitError as exc:
        retry_at=(now()+timedelta(seconds=settings.yahoo_cooldown_seconds)).isoformat()
        with Session() as session:
            save_snapshot(session,security_id,"provider_yahoo_limit",{"retry_at":retry_at,"operation":operation})
            session.commit()
        raise ProviderFailure("Yahoo触发限流，已暂停重复请求；稍后重试或配置可用的备用数据源",
            "Yahoo Finance","rate_limited",operation,retry_at) from exc


def fmp_get(endpoint, **params):
    key=settings.fmp_api_key.get_secret_value()
    if not key:
        raise ProviderFailure("未配置FMP_API_KEY，备用分析师预期/电话会接口不可用；也可导入有权使用的原文",
            "FMP","not_configured",endpoint)
    try:
        with httpx.Client(timeout=35,trust_env=False,follow_redirects=False) as client:
            response=client.get(FMP_BASE+endpoint,params={**params,"apikey":key})
        if response.status_code>=400:
            category="permission" if response.status_code in (401,402,403) else "rate_limited" if response.status_code==429 else "unavailable"
            raise ProviderFailure(f"FMP请求未成功（HTTP {response.status_code}），请核对密钥、对应接口权限与额度",
                "FMP",category,endpoint,http_status=response.status_code)
        payload=response.json()
        if not isinstance(payload,list):
            raise ProviderFailure("FMP未返回预期列表，可能缺少接口权限或返回了上游错误","FMP","invalid_response",endpoint)
        return payload
    except ProviderFailure:
        raise
    except Exception as exc:
        # 请求URL包含API密钥，不能向外透传httpx异常全文。
        raise ProviderFailure(f"FMP网络或解析失败（{type(exc).__name__}）","FMP","unavailable",endpoint) from exc
