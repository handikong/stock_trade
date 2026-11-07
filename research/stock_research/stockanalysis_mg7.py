"""Stock Analysis 公开数据采集器及只读预览命令。"""
import argparse
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import json
from pathlib import Path
import re
import sys
import time
from urllib.robotparser import RobotFileParser
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
import httpx

SYMBOLS = ("AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "TSLA")
ORIGIN = "https://stockanalysis.com"
AGENT_NAME = "StockTradeResearchBot"
USER_AGENT = AGENT_NAME + "/0.1 (financial research; respects robots.txt)"
SOURCE_SYMBOLS = {"BRK-B":"BRK.B"}
MONTHS = {month:index for index,month in enumerate(
    "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split(),1)}


class StopFetch(ValueError):
    """访问限制导致整批停止，已读取的股票结果保留。"""


class Reader:
    def __init__(self, client):
        self.client = client
        url = ORIGIN + "/robots.txt"
        self.rules = RobotFileParser(url)
        self.last = 0.0
        self.delay = 2.0
        response = self._request(url)
        if response.status_code != 200 or not re.search(r"(?im)^user-agent\s*:",response.text):
            raise StopFetch("未能读取有效 robots.txt，不继续抓取")
        self.rules.parse(response.text.splitlines())
        delay = self.rules.crawl_delay(AGENT_NAME)
        rate = self.rules.request_rate(AGENT_NAME)
        self.delay = max(self.delay, delay or 0, rate.seconds / rate.requests if rate and rate.requests > 0 else 0)

    def _request(self,url):
        time.sleep(max(0.0, self.delay - (time.monotonic() - self.last)))
        self.last = time.monotonic()
        try:
            response = self.client.get(url)
        except httpx.HTTPError:
            raise StopFetch("Stock Analysis 网络请求失败；本批停止") from None
        if response.status_code in (401,403,429,503):
            raise StopFetch(f"Stock Analysis 返回 HTTP {response.status_code}；本批停止，不重试或绕过限制")
        return response

    def page(self,url):
        if not self.rules.can_fetch(AGENT_NAME,url):
            raise StopFetch("robots.txt 不允许访问所请求页面；本批停止")
        response = self._request(url)
        if response.status_code != 200:
            raise ValueError(f"该页面返回 HTTP {response.status_code}")
        return BeautifulSoup(response.content,"html.parser")


def _tables(page):
    for table in page.find_all("table"):
        rows = []
        for tr in table.find_all("tr"):
            row = [cell.get_text(" ",strip=True) for cell in tr.find_all(("th","td"),recursive=False)]
            if row:
                rows.append(row)
        if rows:
            yield rows


def _number(value):
    if value is None or not re.fullmatch(r"[+\-−]?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?",value):
        return None
    try:
        number = Decimal(value.replace("−","-").replace(",",""))
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def _date(value):
    match = re.search(r"([A-Z][a-z]{2}) (\d{1,2}), (\d{4})",value or "")
    if not match or match[1] not in MONTHS:
        return None
    try:
        return date(int(match[3]),MONTHS[match[1]],int(match[2]))
    except ValueError:
        return None


def _forecast(page):
    fiscal_ends = {}
    forecast = None
    for rows in _tables(page):
        header = rows[0]
        values = {row[0]:row[1:] for row in rows[1:]}
        if header[0] == "Fiscal Year" and "Period Ending" in values:
            fiscal_ends.update({label.removeprefix("FY "):_date(value)
                                for label,value in zip(header[1:],values["Period Ending"])})
        if header[0] == "EPS" and len(header) > 1 and re.fullmatch(r"\d{4}",header[1]):
            first = {label:_number(values[label][0]) if values.get(label) else None for label in ("Low","Avg","High")}
            forecast = (header[1],first)
    if forecast is None:
        raise ValueError("未找到公开财年 EPS 预估表")
    year, numbers = forecast
    end = fiscal_ends.get(year)
    available = [value for value in numbers.values() if value is not None]
    if available != sorted(available):
        raise ValueError("EPS 三档顺序无效")
    page_text = page.get_text(" ",strip=True)
    adjusted = "eps and forward pe are based on non-gaap adjusted numbers" in page_text.lower()
    currency = re.search(r"\bFinancial currency is ([A-Z]{3})\b",page_text)
    return {"period_type":"fiscal_year","fiscal_year":year,"period_end":end.isoformat() if end else None,
            "currency_per_share":currency[1] if currency else "USD",
            "currency_source":"financial_currency_label" if currency else "us_listing_default",
            "earnings_basis":"non_gaap_adjusted" if adjusted else "unknown",
            "low":str(numbers["Low"]) if numbers["Low"] is not None else None,
            "mean":str(numbers["Avg"]) if numbers["Avg"] is not None else None,
            "high":str(numbers["High"]) if numbers["High"] is not None else None}


def _percentile(values,p):
    pos = Decimal(len(values)-1) * p
    low = int(pos)
    high = min(low+1,len(values)-1)
    return values[low]+(values[high]-values[low])*(pos-low)


def _ratios(page,as_of):
    try:
        since = as_of.replace(year=as_of.year-5)
    except ValueError:
        since = as_of.replace(year=as_of.year-5,day=28)
    for rows in _tables(page):
        header = rows[0]
        fields = {row[0]:row[1:] for row in rows[1:]}
        if header[0] != "Fiscal Quarter" or "Forward PE" not in fields:
            continue
        periods, multiples = fields.get("Period Ending",[]),fields["Forward PE"]
        if len(header)-1 != len(periods) or len(periods) != len(multiples):
            raise ValueError("Forward PE 与季度日期列未对齐")
        samples, current, no_date = {}, None, 0
        for label,period,text in zip(header[1:],periods,multiples):
            number = _number(text)
            if label == "Current":
                current = number
                continue
            if number is None or number <= 0:
                continue
            day = _date(period)
            if day is None:
                no_date += 1
                continue
            if since <= day <= as_of:
                if day in samples:
                    raise ValueError("历史季度日期重复")
                samples[day] = number
        values = sorted(samples.values())
        quantiles = [str(_percentile(values,Decimal(p)).quantize(Decimal(".01"),rounding=ROUND_HALF_UP))
                     for p in (".25",".5",".75")] if len(values) >= 8 else [None,None,None]
        return {"sampling":"fiscal_quarter_end","lookback_start":since.isoformat(),"sample_count":len(values),
                "first_sample":min(samples).isoformat() if samples else None,
                "last_sample":max(samples).isoformat() if samples else None,
                "missing_exact_date":no_date,"forward_denominator":"unverified",
                "current":str(current) if current is not None else None,
                **dict(zip(("p25","p50","p75"),quantiles))}
    raise ValueError("未找到公开季度 Forward PE 表")


def collect(symbols=SYMBOLS, *, scope="mg7", on_row=None, allowed=None):
    now = datetime.now(timezone.utc)
    as_of = now.astimezone(ZoneInfo("Asia/Shanghai")).date()
    out = {"source":"Stock Analysis","fetched_at":now.isoformat(),"as_of":as_of.isoformat(),
           "mode":"research_preview","scope":scope,"requested_symbols":list(symbols),"stocks":[],"halted":None}
    with httpx.Client(timeout=18,follow_redirects=False,headers={
            "User-Agent":USER_AGENT,"Accept":"text/html, text/plain"}) as client:
        try:
            reader = Reader(client)
        except StopFetch as exc:
            out["halted"] = str(exc)
            return out
        for index,symbol in enumerate(symbols,1):
            if allowed is not None and not allowed():
                out["halted"] = "估值更新已停用或已过本周六窗口"
                break
            source_symbol = SOURCE_SYMBOLS.get(symbol,symbol)
            base = f"{ORIGIN}/stocks/{source_symbol.lower()}"
            row = {"symbol":symbol,"source_symbol":source_symbol,"forecast_url":base+"/forecast/",
                   "ratios_url":base+"/financials/ratios/?p=quarterly","errors":[],"quality_flags":[]}
            if on_row is None and (index == 1 or index % 10 == 0 or index == len(symbols)):
                print(f"公开数据预览 {index}/{len(symbols)}：{symbol}",file=sys.stderr,flush=True)
            for field,url,parse in (
                    ("eps",row["forecast_url"],_forecast),
                    ("forward_pe",row["ratios_url"],lambda page:_ratios(page,as_of))):
                try:
                    row[field] = parse(reader.page(url))
                except StopFetch as exc:
                    out["halted"] = str(exc)
                    row["errors"].append(str(exc))
                    break
                except ValueError as exc:
                    row["errors"].append(f"{field}：{exc}")
            if symbol == "BRK-B" and row.get("eps"):
                # 公开预测页当前呈现的金额疑似按A股每股计；保留原数作为证据，不自行折算。
                row["quality_flags"].append("BRK.B 预测页每股金额与B类报价尺度不一致，EPS不应用于估值")
            out["stocks"].append(row)
            if on_row is not None:
                on_row(row,datetime.now(timezone.utc).isoformat())
            if out["halted"]:
                break
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description="低频抓取公开 EPS 与 PE 预览，默认 MG7")
    parser.add_argument("--watchlist",action="store_true",help="只读当前活动关注池，取代默认 MG7")
    parser.add_argument("--output",type=Path,help="将 JSON 保存到新文件；不覆盖")
    args = parser.parse_args(argv)
    if args.output and args.output.exists():
        parser.error("输出文件已存在")
    symbols = SYMBOLS
    if args.watchlist:
        from .cli_helpers import storage_ready
        from .watchlist import entries, normalize_symbol

        if not storage_ready("watchlist_entries"):
            parser.error("尚无关注池，请先导入股票")
        symbols = tuple(normalize_symbol(row["symbol"]) for row in entries())
        if not symbols:
            parser.error("当前关注池为空")
    report = collect(symbols,scope="watchlist" if args.watchlist else "mg7")
    value = json.dumps(report,ensure_ascii=False,indent=2)
    if args.output:
        with args.output.open("x",encoding="utf-8") as output:
            output.write(value+"\n")
    print(value)
    return 1 if report["halted"] or any(row["errors"] for row in report["stocks"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
