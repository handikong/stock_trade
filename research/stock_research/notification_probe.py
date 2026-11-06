"""无需股票信号的机器人连通性诊断；每次显式发送仅一条，独立归档，不自动重试。"""
import json
import logging
import os
from datetime import datetime, timezone
from uuid import uuid4

from . import feishu
from .config import settings

logger = logging.getLogger(__name__)


def stamp():
    return datetime.now(timezone.utc).isoformat()


def _write(path, value, *, initial=False):
    content = json.dumps(value,ensure_ascii=False,indent=2)+"\n"
    if initial:
        with path.open("x",encoding="utf-8") as stream:
            stream.write(content)
    else:
        temporary = path.with_name(path.name+"."+uuid4().hex+".tmp")
        try:
            with temporary.open("x",encoding="utf-8") as stream:
                stream.write(content)
            os.replace(temporary,path)
        finally:
            temporary.unlink(missing_ok=True)


def run(*, send=False):
    if type(send) is not bool:
        raise ValueError("send必须为显式布尔值")
    probe_id = uuid4().hex
    created_at = stamp()
    card = {"header":{"template":"blue","title":{"tag":"plain_text","content":"股票系统 · 飞书连通性测试"}},
            "elements":[{"tag":"div","text":{"tag":"plain_text","content":
                f"这是一条手动发起的连通性测试消息，无需股票信号。\n测试编号：{probe_id}\n生成时间UTC：{created_at}\n此消息不是买卖信号，不代表真实成交。"}}]}
    result = {"mode":"SEND" if send else "PREVIEW","operation":"CONNECTION_TEST","probe_id":probe_id,
              "status":"PREVIEW","created_at":created_at,"card":card,"warnings":[],"error":"","audit_path":None}
    if not send:
        try:
            result["configured"] = feishu.destination(required=False) is not None
        except ValueError:
            result["configured"] = False
            result["warnings"].append("Webhook配置无效；预览未发送，请核对本地配置")
        result["message"] = "仅预览，未请求飞书、行情或模型，未写正式信号与测试投递档案；加--send才发送"
        return result
    destination_key = feishu.destination()
    folder = settings.storage/"notification-probes"
    folder.mkdir(parents=True,exist_ok=True)
    path = folder/(probe_id+".json")
    record = {"probe_id":probe_id,"created_at":created_at,"destination_key":destination_key,
              "status":"SENDING","card":card,"error":""}
    # 先记发送尝试；无法保留档案时，不发出消息。没有证券/股票池/正式信号副作用。
    _write(path,record,initial=True)
    result["audit_path"] = str(path)
    logger.info("正在发送一条飞书连通性测试；编号%s，不写正式信号去重表",probe_id)
    interrupted = False
    try:
        feishu.send(card)
        status = "SENT"; error = ""
    except feishu.DeliveryFailure as exc:
        status = exc.status; error = str(exc)
    except KeyboardInterrupt:
        status = "UNKNOWN"; error = "发送被中断，送达无法确认，请先核对群消息"; interrupted = True
    except Exception:
        status = "UNKNOWN"; error = "发送结果无法确认，请先核对群消息，不会自动重试"
    record.update(status=status,error=error,finished_at=stamp())
    try:
        _write(path,record)
    except OSError:
        status = "UNKNOWN"; error = "发送后档案更新失败，请先核对群消息和本地SENDING记录，不会自动重试"
    result.update(status=status,error=error,finished_at=record["finished_at"],
        message="SENT仅表示飞书接口确认接收，请核对群里的测试编号；不会自动重试。档案停在SENDING亦按送达未知处理。")
    if interrupted:
        result["interrupted"] = True
    logger.info("连通性测试结果%s；编号%s%s",status,probe_id,"；"+error if error else "")
    return result
