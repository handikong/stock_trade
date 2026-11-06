"""财报公共入口：命令行、网页刷新和后续财报发布事件共用，日程变动不代表发布。"""

import logging
from pathlib import Path
from time import perf_counter

from yfinance.exceptions import YFRateLimitError

from . import analysis, financials, materials
from .providers import failure_detail

logger=logging.getLogger(__name__)


def run_steps(security_id, force, steps, on_progress=None):
    """统一进度与安全错误记录，单项失败仍保留其他已经取得的结果。"""
    results={}; warnings=[]
    for percent,label,key,fn in steps:
        if on_progress:
            on_progress(percent,label)
        started=perf_counter()
        try:
            result=fn(security_id,force)
            results[key]=result
            status=("命中行情缓存，技术结构已按新参数重算" if result.get("technical_recomputed") else
                "复用永久保存的资料，不重复获取或分析" if result.get("cached") and result.get("retention")=="permanent" else
                "命中缓存" if result.get("cached") else result["skipped"] if result.get("skipped") else
                "处理结束，部分资料尚未取得" if result.get("warnings") else "更新完成")
            counts="，".join(f"{name}{result[field]}" for field,name in
                (("sessions","日线条数："),("facts","财务事实："),("archived","原文归档数：")) if field in result)
            logger.info("[%s] %s%s（耗时 %.2f 秒）",key,status,f"；{counts}" if counts else "",perf_counter()-started)
            for warning in result.get("warnings",[]):
                message=warning.get("message","资料未完整取得") if isinstance(warning,dict) else str(warning)
                warnings.append({**(warning if isinstance(warning,dict) else {}),"stage":key,"message":message})
                logger.warning("[%s] %s",key,message)
        except ValueError as exc:
            warnings.append({"stage":key,"message":str(exc)})
            logger.warning("[%s] %s（耗时 %.2f 秒）",key,exc,perf_counter()-started)
        except Exception as exc:
            warning={**failure_detail(exc),"stage":key}
            if isinstance(exc,YFRateLimitError):
                warning["category"]="rate_limited"
            trace=exc.__traceback__
            while trace:
                path=Path(trace.tb_frame.f_code.co_filename)
                if path.parent==Path(__file__).parent:
                    warning["error_location"]={"file":path.name,"function":trace.tb_frame.f_code.co_name,"line":trace.tb_lineno}
                trace=trace.tb_next
            status=getattr(getattr(exc,"response",None),"status_code",None)
            if isinstance(status,int) and 100<=status<=599:
                warning["http_status"]=status
            location=warning.get("error_location",{})
            logger.warning("[%s] %s（%s，位置 %s:%s，耗时 %.2f 秒）",key,warning["message"],type(exc).__name__,
                location.get("file","未知"),location.get("line","未知"),perf_counter()-started)
            warnings.append(warning)
    return {"steps":results,"warnings":warnings}


def process_financial_reports(security_id, *, force=False, analyze_reports=False, on_progress=None):
    # 用户已替换整条原件研究链路，保留公共名称供旧调用方兼容；force亦不恢复。
    return {"steps":{"financial_reports":{"status":"disabled","skipped":"自动读取财报和电话会议已停用，请手工录入研究数据"}},"warnings":[]}


def _process_financial_reports_legacy(security_id, *, force=False, analyze_reports=False, on_progress=None):
    """获取/补齐并永久归档；原文模型分析已停用，兼容参数不再发起分析。

    后续财报日历仅在公司实际发布/修订后调用force=True同步；只有预计日期变化不调用。
    force只影响来源同步；不请求DeepSeek，重复发布事件仍复用同版本原文。
    本入口不获取行情或当前市场预期，也不确认财报事件，不实现日历订阅。
    """
    result=run_steps(security_id,force,[
        (35,"正在获取官方财务与披露","sec",financials.refresh_sec),
        (55,"正在补充可获得的财务信息","vendor",financials.refresh_vendor_financials),
        (75,"正在获取业绩附件与管理层指引片段","materials",materials.refresh_materials),
    ],on_progress)
    result["steps"]["analysis"]={"skipped":analysis.DISABLED_MESSAGE,"status":"disabled","analysis_policy":analysis.ANALYSIS_POLICY}
    return result
