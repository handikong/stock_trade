"""沪深 A 股人民币估值资料，独立于美股来源。"""
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
import re
from zoneinfo import ZoneInfo

import httpx

from .stockanalysis_mg7 import ORIGIN, USER_AGENT, Reader, StopFetch, _forecast, _number, _ratios, _tables

BEIJING = ZoneInfo("Asia/Shanghai")
BOARDS = {"SH":r"(?:60[0135]|688)[0-9]{3}", "SZ":r"(?:00[0-3]|30[01])[0-9]{3}"}
EXCHANGES = {"SH":"sha", "SZ":"she"}


def symbol(value):
    text = value.strip().upper()
    match = re.fullmatch(r"([0-9]{6})(?:\.(SH|SS|SZ))?", text)
    if match:
        code, suffix = match.groups()
        suffix = "SH" if suffix == "SS" else suffix
    else:
        match = re.fullmatch(r"(SH|SZ)([0-9]{6})", text)
        if not match:
            raise ValueError("请输入沪深 A 股代码，例如 600519.SH 或 300750.SZ")
        suffix, code = match.groups()
    venue = next((key for key, pattern in BOARDS.items() if re.fullmatch(pattern,code)), None)
    if venue is None or (suffix and suffix != venue):
        raise ValueError("仅支持沪深普通 A 股，代码和交易所需一致")
    return f"{code}.{venue}"


def _annual_pe(page):
    for rows in _tables(page):
        header = rows[0]
        fields = {row[0]:row[1:] for row in rows[1:]}
        if header[0] != "Fiscal Year" or not ("PE Ratio" in fields or "Forward PE" in fields):
            continue
        labels = header[1:]
        result = {}
        for label, key in (("PE Ratio","trailing_pe"),("Forward PE","forward_pe")):
            numbers = fields.get(label,[])
            if len(numbers) != len(labels):
                continue
            result[key] = {period:str(value) for period,raw in zip(labels,numbers)
                           if (value := _number(raw)) is not None and value > 0}
        if result:
            return result
    raise ValueError("未找到公开年度 PE 表")


def research(value):
    code = symbol(value)
    ticker, venue = code.split(".")
    base = f"{ORIGIN}/quote/{EXCHANGES[venue]}/{ticker}"
    as_of = datetime.now(BEIJING).date()
    urls = {"eps":base+"/forecast/","annual_pe":base+"/financials/ratios/",
            "quarterly_forward_pe":base+"/financials/ratios/?p=quarterly"}
    result = {"symbol":code,"currency":"CNY","as_of":as_of.isoformat(),"source":"Stock Analysis",
              "urls":urls,"warnings":[],"scenarios":[]}
    with httpx.Client(timeout=18,follow_redirects=False,headers={
            "User-Agent":USER_AGENT,"Accept":"text/html, text/plain"}) as client:
        reader = Reader(client)
        for key, parse in (("eps",_forecast),("annual_pe",_annual_pe),
                           ("quarterly_forward_pe",lambda page:_ratios(page,as_of))):
            try:
                parsed = parse(reader.page(urls[key]))
                if key == "eps" and (parsed["currency_per_share"] != "CNY"
                                     or parsed["currency_source"] != "financial_currency_label"):
                    raise ValueError("预测 EPS 未明确标为人民币")
                result[key] = parsed
            except StopFetch as exc:
                result["warnings"].append(str(exc))
                break
            except ValueError as exc:
                result["warnings"].append(f"{key} · {exc}")
    eps, pe = result.get("eps") or {}, result.get("quarterly_forward_pe") or {}
    pairs = [(eps.get(e),pe.get(p)) for e,p in (("low","p25"),("mean","p50"),("high","p75"))]
    if (eps.get("period_end") or "") >= as_of.isoformat() and all(e and p and Decimal(e)>0 for e,p in pairs):
        result["scenarios"] = [{"name":name,"price":str((Decimal(e)*Decimal(p)).quantize(Decimal("0.01"),
                                 rounding=ROUND_HALF_UP))} for name,(e,p) in zip(("保守","基准","乐观"),pairs)]
        result["warnings"].append("财年 EPS × 历史 Forward PE 仅为情景参考，PE 分母口径待核对")
    else:
        result["warnings"].append("未取得完整且有效的人民币 EPS 和季度 Forward PE 三档，估值区间留空")
    return result
