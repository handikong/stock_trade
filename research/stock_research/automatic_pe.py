"""独立PE来源与缓存。只读估值指标，不抓财报、不查证券身份、不调用AI。"""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
import fcntl
import json
import logging
import re

import httpx
import yfinance as yf
from bs4 import BeautifulSoup
from sqlalchemy import select

from .config import settings
from .providers import ProviderFailure, failure_detail, yahoo_call
from .store import Security, Session, Snapshot, digest, latest, save_snapshot, stamp
from .valuation import number

logger = logging.getLogger(__name__)
INDUSTRY_URL = "https://pages.stern.nyu.edu/adamodar/New_Home_Page/datafile/pedata.html"


def policy():
    p = json.loads(Path(settings.automatic_pe_policy_file).expanduser().read_text())
    if not isinstance(p, dict) or p.get("version") != "automatic-pe-v1" or type(p.get("enabled")) is not bool:
        raise ValueError("自动PE配置版本或enabled无效")
    for k, lo, hi in (("minimum_samples", 4, 100), ("cache_hours", 1, 8760),
                      ("failure_cooldown_minutes", 1, 1440), ("maximum_sample_age_days", 1, 730),
                      ("industry_maximum_age_days", 1, 730)):
        if type(p.get(k)) is not int or not lo <= p[k] <= hi:
            raise ValueError("自动PE配置范围错误：" + k)
    years = p.get("history_years")
    if not isinstance(years, list) or not years or any(type(y) is not int or not 1 <= y <= 10 for y in years):
        raise ValueError("PE历史窗口需要1至10年的整数列表")
    if type(p.get("primary_years")) is not int or p["primary_years"] not in years:
        raise ValueError("PE主窗口必须包含于history_years")
    c = p.get("input_convention") or {}; y = p.get("yahoo_definition") or {}
    if not isinstance(c, dict) or not isinstance(y, dict):
        raise ValueError("EPS约定与Yahoo口径配置需要对象")
    if c.get("period_type") != "ntm" or c.get("origin") != "user_research_scenarios" or not re.fullmatch(r"[A-Z]{3}", str(c.get("currency", ""))):
        raise ValueError("三值EPS默认约定需要明确币种、ntm期间和用户研究情景来源")
    bases = ("unspecified", "gaap_diluted", "adjusted_diluted", "normalized_diluted")
    if c.get("earnings_basis") not in bases or y.get("earnings_basis") not in bases or y.get("forward_eps_reference") not in ("unspecified", "ntm", "fy1"):
        raise ValueError("自动PE盈利/预测期间定义配置无效")
    if not isinstance(p.get("industries"), dict):
        raise ValueError("行业代理映射需要对象")
    for symbol, row in p["industries"].items():
        if not isinstance(row, dict) or not row.get("name") or not row.get("note"):
            raise ValueError("行业映射需要name和代理依据note")
    return p


def _day():
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo(settings.research_display_timezone)).date()


def _anniversary(day, years):
    try:
        return day.replace(year=day.year + years)
    except ValueError:
        return day.replace(year=day.year + years, day=28)


def _fresh(item, cfg):
    if not item:
        return False
    try:
        acquired = datetime.fromisoformat(item.payload["fetched_at"])
        return timedelta(0) <= datetime.now(timezone.utc) - acquired <= timedelta(hours=cfg["cache_hours"])
    except (ValueError, KeyError, TypeError):
        return False


def _quantile(values, q):
    ordered = sorted(values)
    with localcontext() as ctx:
        ctx.prec = 48
        index = Decimal(len(ordered) - 1) * q
        low = int(index); high = min(low + 1, len(ordered) - 1)
        return str(ordered[low] + (ordered[high] - ordered[low]) * (index - low))


def _pe_value(raw):
    # 供应商浮点可能超过12位小数；在适配层统一为12位，保留原始样本另行追溯。
    try:
        value = Decimal(str(raw))
        if not value.is_finite() or abs(value) > Decimal("1e18"):
            raise ValueError
        with localcontext() as ctx:
            ctx.prec = 48
            return number(str(value.quantize(Decimal("0.000000000001"))), "PE")
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError("PE样本不是有限有效数值") from None


def _fetch_history(security_id, symbol, cfg):
    today = _day(); start = _anniversary(today, -max(cfg["history_years"]))
    endpoint = f"https://query2.finance.yahoo.com/ws/fundamentals-timeseries/v1/finance/timeseries/{symbol}"
    # 0.2.66无公开估值历史方法；将SDK私有网络接口隔离在此，复用其Cookie/代理/429处理。
    ticker = yf.Ticker(symbol)
    params = {"symbol":symbol, "type":"quarterlyPeRatio,quarterlyForwardPeRatio",
              "period1":int(datetime.combine(start, datetime.min.time(), timezone.utc).timestamp()),
              "period2":int(datetime.combine(today + timedelta(days=1), datetime.min.time(), timezone.utc).timestamp())}
    response = yahoo_call(security_id, "PE估值时间序列（非财报）", lambda:ticker._data.get_raw_json(endpoint, params=params, timeout=30))
    series = response.get("timeseries") or {}
    if series.get("error") or not isinstance(series.get("result"), list):
        raise ValueError("Yahoo未返回可用PE序列，已保留旧缓存")
    samples = {}; raw_values = {}; invalid = 0
    for item in series["result"]:
        for key, metric in (("quarterlyForwardPeRatio", "forward"), ("quarterlyPeRatio", "ttm")):
            for point in item.get(key) or []:
                try:
                    day = date.fromisoformat(point["asOfDate"])
                    value = _pe_value(point["reportedValue"]["raw"])
                    if not start <= day <= today or value <= 0:
                        invalid += 1; continue
                    k = (metric, day.isoformat())
                    if k in samples and samples[k] != str(value):
                        raise ValueError("同日同指标PE存在冲突记录，不能任意取值")
                    samples[k] = str(value)
                    raw_values[k] = str(point["reportedValue"]["raw"])
                except (KeyError, TypeError, ValueError) as exc:
                    if str(exc).startswith("同日同指标"):
                        raise
                    invalid += 1
    if not samples:
        raise ValueError("Yahoo历史PE为空；不会用今日EPS除历史股价补造")
    return {"fetched_at":stamp(), "source":"Yahoo fundamentals-timeseries", "source_url":f"https://finance.yahoo.com/quote/{symbol}/key-statistics/",
            "frequency":"quarterly", "requested_start":start.isoformat(), "requested_end":today.isoformat(),
            "invalid_samples":invalid, "samples":[{"metric":m,"date":d,"value":v,"raw_value":raw_values[(m,d)]} for (m,d),v in sorted(samples.items())],
            "definition_note":"供应商季度估值序列，不宣称为日频、完整五年或无历史修订的时点快照"}


def _fetch_industry():
    # 固定公开域名；不读取模型提供的URL，不恢复身份查询或财报下载。
    with httpx.Client(timeout=30, trust_env=True, follow_redirects=False) as client:
        response = client.get(INDUSTRY_URL)
    if response.status_code != 200:
        raise ProviderFailure(f"行业PE公开表请求失败（HTTP {response.status_code}）", "NYU", http_status=response.status_code)
    if len(response.content) > 2_000_000:
        raise ValueError("行业PE页面超出解析大小限制")
    soup = BeautifulSoup(response.content, "html.parser")
    text = soup.get_text(" ", strip=True)
    match = re.search(r"as\s+of\s+([A-Za-z]+)\s+(20\d{2})", text, re.I)
    if not match:
        raise ValueError("行业PE数据日期不明确，未把抓取时间冒充数据日期")
    month = datetime.strptime(match[1], "%B").month
    period = f"{match[2]}-{month:02d}"
    tables = soup.find_all("table"); rows = []; headers = None
    for table in tables:
        for tr in table.find_all("tr"):
            cells = [re.sub(r"\s+", " ", c.get_text(" ", strip=True)) for c in tr.find_all(["td", "th"])]
            if not cells:
                continue
            if cells[0] == "Industry Name" and "Forward PE" in cells and "Number of firms" in cells:
                headers = cells; continue
            if headers is None or len(cells) != len(headers):
                continue
            row = dict(zip(headers, cells))
            try:
                count = int(row["Number of firms"].replace(",", ""))
                if count <= 0:
                    continue
            except (ValueError, KeyError):
                continue
            values = {}
            for column, name in (("Forward PE", "forward"), ("Trailing PE", "ttm")):
                try:
                    value = number(row[column].replace(",", ""), "行业PE")
                    if value > 0:
                        values[name] = str(value)
                except (ValueError, KeyError):
                    pass
            if values:
                rows.append({"industry":cells[0], "firms":count, **values})
    if not rows:
        raise ValueError("行业PE表结构变化或没有有效记录，旧缓存保留")
    return {"fetched_at":stamp(), "source":"NYU Damodaran US industry PE", "source_url":INDUSTRY_URL,
            "data_month":period, "date_precision":"month", "rows":rows,
            "definition_note":"来源发布的行业PE口径；非本程序计算的同行中位数；盈利口径及Forward期间定义未独立核实"}


def _refresh(security_id, kind, force, fetch, *, shared=False):
    if not re.fullmatch(r"[0-9a-f]{32}", security_id):
        raise ValueError("PE刷新需要有效证券档案id")
    cfg = policy()
    if not cfg["enabled"]:
        return {"skipped":"自动PE已关闭"}
    folder = settings.storage / "pe-locks"; folder.mkdir(parents=True, exist_ok=True)
    lock_key = kind if shared else kind + "-" + security_id
    with (folder / (lock_key + ".lock")).open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        with Session() as session:
            security = session.get(Security, security_id)
            if security is None:
                raise LookupError("股票档案不存在")
            if security.profile.get("type") in ("ETF", "MUTUALFUND"):
                return {"skipped":"基金不适用公司PE"}
            if shared and security.symbol not in cfg["industries"]:
                return {"skipped":"本股票尚无显式行业代理映射，不查询身份或猜行业"}
            filters = [Snapshot.kind == kind]
            if not shared:
                filters.append(Snapshot.security_id == security_id)
            old = session.scalar(select(Snapshot).where(*filters).order_by(Snapshot.created_at.desc(), Snapshot.id.desc()).limit(1))
            old_samples = old.payload.get("requested_start") if old and not shared else None
            covers = shared or (old_samples and old_samples <= _anniversary(_day(), -max(cfg["history_years"])).isoformat())
            if not force and _fresh(old, cfg) and covers:
                return {"cached":True}
            attempt_filters = [Snapshot.kind == kind + "_attempt"]
            if not shared:
                attempt_filters.append(Snapshot.security_id == security_id)
            last_attempt = session.scalar(select(Snapshot).where(*attempt_filters).order_by(Snapshot.created_at.desc(), Snapshot.id.desc()).limit(1))
            if last_attempt and datetime.fromisoformat(last_attempt.payload["retry_at"]) > datetime.now(timezone.utc):
                return {"warnings":[{"message":"PE来源处于失败冷却期，显式刷新也不绕过；旧档案保留"}]}
            symbol = security.symbol
        try:
            logger.info("正在获取%s的%s；不读取财报或调用模型", symbol, "行业PE" if shared else "历史PE")
            payload = fetch(security_id, symbol, cfg)
            with Session() as session:
                save_snapshot(session, security_id, kind, payload); session.commit()
            return {"cached":False}
        except Exception as exc:
            detail = failure_detail(exc)
            with Session() as session:
                save_snapshot(session, security_id, kind + "_attempt", {**detail,
                    "retry_at":(datetime.now(timezone.utc) + timedelta(minutes=cfg["failure_cooldown_minutes"])).isoformat()})
                session.commit()
            return {"warnings":[detail]}


def refresh_history(security_id, force=False):
    return {"skipped":"历史PE自动获取已停用，请手工录入"}


def refresh_industry(security_id, force=False):
    return {"skipped":"行业PE自动获取已停用，请手工录入"}


def cached(session, security_id, symbol, *, today=None):
    """只读缓存重算分位数，配置变动不伪造取得时间；不补网络请求。"""
    cfg = policy(); today = today or _day()
    result = {"policy_version":digest(cfg), "definition":cfg["yahoo_definition"], "history":[], "industry":None, "warnings":[]}
    if not cfg["enabled"]:
        result["warnings"].append("自动PE已关闭"); return result
    item = latest(session, security_id, "pe_history")
    if not _fresh(item, cfg):
        result["warnings"].append("历史PE缓存缺失或需更新")
    else:
        for years in sorted(set(cfg["history_years"])):
            start = _anniversary(today, -years).isoformat()
            for metric in ("forward", "ttm"):
                rows = [r for r in item.payload["samples"] if r["metric"]==metric and start <= r["date"] <= today.isoformat()]
                if len(rows) < cfg["minimum_samples"] or (today - date.fromisoformat(rows[-1]["date"])).days > cfg["maximum_sample_age_days"]:
                    continue
                values = [number(r["value"], "已存PE") for r in rows]
                result["history"].append({"years":years, "metric":metric, "count":len(rows), "requested_start":start,
                    "actual_start":rows[0]["date"], "actual_end":rows[-1]["date"], "frequency":item.payload["frequency"],
                    "low":_quantile(values, Decimal("0.25")), "median":_quantile(values, Decimal("0.5")), "high":_quantile(values, Decimal("0.75")),
                    "source_url":item.payload["source_url"], "snapshot_id":item.id, "fetched_at":item.payload["fetched_at"],
                    "partial_window":rows[0]["date"] > (_anniversary(today, -years) + timedelta(days=100)).isoformat()})
    candidates = [r for r in result["history"] if r["metric"]=="forward"] or [r for r in result["history"] if r["metric"]=="ttm"]
    result["primary"] = next((r for r in candidates if r["years"]==cfg["primary_years"]), candidates[-1] if candidates else None)
    if not result["primary"]:
        result["warnings"].append("历史PE有效样本不足，不补造分位数")
    elif result["primary"]["metric"] == "ttm":
        result["warnings"].append("Forward PE有效样本不足，当前使用历史TTM PE作为跨期倍数假设")
    if result["primary"] and result["primary"]["partial_window"]:
        result["warnings"].append("历史PE仅覆盖请求窗口的一部分，详见实际样本日期，不宣称完整多年历史")
    mapping = cfg["industries"].get(symbol)
    table = session.scalar(select(Snapshot).where(Snapshot.kind=="pe_industry_table")
        .order_by(Snapshot.created_at.desc(), Snapshot.id.desc()).limit(1))
    if mapping and _fresh(table, cfg):
        month = table.payload["data_month"]
        age = (today - date.fromisoformat(month + "-01")).days
        row = next((r for r in table.payload["rows"] if r["industry"]==mapping["name"]), None)
        if row and 0 <= age <= cfg["industry_maximum_age_days"]:
            metric = "forward" if row.get("forward") else "ttm"
            result["industry"] = {**row, "metric":metric, "value":row[metric], "data_month":month, "age_days_from_month_start":age,
                "source_url":table.payload["source_url"], "snapshot_id":table.id, "mapping_note":mapping["note"],
                "definition_note":table.payload["definition_note"]}
    if not result["industry"]:
        result["warnings"].append("行业PE缺失、需更新或尚无代理映射；历史估值仍可单独计算")
    return result
