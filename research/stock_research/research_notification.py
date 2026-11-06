"""用户显式发起的单股研究快照；无需信号，不占正式信号投递去重。"""
import json
import logging
import os
import re
from uuid import uuid4

from . import feishu, market, signal_research
from .config import settings
from .notification_cards import FORMAT, buy_card
from .providers import failure_detail
from .service import security_for
from .store import stamp

logger = logging.getLogger(__name__)


def _write(path, record, *, initial=False):
    content = json.dumps(record,ensure_ascii=False,indent=2)+"\n"
    if initial:
        with path.open("x",encoding="utf-8") as stream:
            stream.write(content)
        return
    temporary = path.with_name(path.name+"."+uuid4().hex+".tmp")
    try:
        with temporary.open("x",encoding="utf-8") as stream:
            stream.write(content)
        os.replace(temporary,path)
    finally:
        temporary.unlink(missing_ok=True)


def run(symbol, *, refresh=False, send=False):
    if type(refresh) is not bool or type(send) is not bool:
        raise ValueError("refresh和send必须为显式布尔值")
    symbol = symbol.strip().upper().removeprefix("$")
    if not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,14}",symbol) or symbol.endswith((".SS",".SZ",".BJ")):
        raise ValueError("研究快照仅支持单只美股代码，例如GOOG")
    warnings = []
    try:
        destination_key = feishu.destination(required=send)
    except ValueError:
        if send:
            raise
        destination_key = None
        warnings.append("Webhook配置无效；当前仅预览，未请求飞书")
    security = security_for(symbol,create=True)
    if refresh:
        logger.info("%s 更新研究快照行情；不扫描策略、不抓财务或请求模型",symbol)
        try:
            market.refresh_market(security.id)
        except Exception as exc:
            warnings.append("行情更新未完成："+failure_detail(exc)["message"])
    research = signal_research.prepare(symbol)
    warnings.extend(w["message"] for w in research.get("warnings",[]))
    context = research["context"]
    if context["cache_state"] != "fresh":
        warnings.append(context["cache_message"] or "行情待更新，本条不发布旧支撑压力")
    request_id = uuid4().hex; created_at = stamp()
    card = buy_card(symbol,research)
    result = {"operation":"RESEARCH_SNAPSHOT","mode":"SEND" if send else "PREVIEW","symbol":symbol,
        "request_id":request_id,"created_at":created_at,"status":"PREVIEW","card":card,
        "warnings":warnings,"error":"","audit_path":None,"versions":research["versions"],"card_format":FORMAT}
    if not send:
        result["message"] = "仅预览，未发送；加--send才发送一条研究快照，不影响正式信号去重"
        return result
    folder = settings.storage/"research-notifications"
    folder.mkdir(parents=True,exist_ok=True)
    path = folder/(request_id+".json")
    record = {**result,"destination_key":destination_key,"status":"SENDING","research":research}
    # 档案不可写则不发送；不把手动快照伪装为BUY，也不改变SignalDelivery。
    _write(path,record,initial=True)
    result["audit_path"] = str(path)
    logger.info("正在发送%s研究快照，编号%s；无需策略信号",symbol,request_id)
    interrupted = False
    try:
        feishu.send(card)
        status = "SENT"; error = ""
    except feishu.DeliveryFailure as exc:
        status = exc.status; error = str(exc)
    except KeyboardInterrupt:
        status = "UNKNOWN"; error = "发送中断，请核对群里的快照编号，不自动重试"; interrupted = True
    except Exception:
        status = "UNKNOWN"; error = "发送结果无法确认，请核对群消息，不自动重试"
    record.update(status=status,error=error,finished_at=stamp())
    try:
        _write(path,record)
    except OSError:
        status = "UNKNOWN"; error = "发送后档案更新失败，请核对群消息及本地SENDING档案"
    result.update(status=status,error=error,finished_at=record["finished_at"],
        message="SENT仅表示飞书确认接收，不代表已读；每次显式执行发送一条新编号快照，FAILED/UNKNOWN不自动重试。")
    if interrupted:
        result["interrupted"] = True
    logger.info("%s研究快照结果%s；编号%s",symbol,status,request_id)
    return result
