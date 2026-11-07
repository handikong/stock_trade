"""大盘简报的官方来源，记录缺项与时点，不使用模型猜测新闻。"""
import csv
from datetime import date, datetime, timezone
from io import StringIO
import math
import re
from urllib.parse import urlparse
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
import httpx

from .external_briefs import _date, _plain, _value

VIX_SOURCE = "https://cdn-api.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv"
FOMC = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
BLS_CALENDAR = "https://www.bls.gov/schedule/news_release/bls.ics"
FEEDS = {
    "美联储货币政策": "https://www.federalreserve.gov/feeds/press_monetary.xml",
    "美国 CPI": "https://www.bls.gov/feed/cpi.rss",
    "美国就业报告": "https://www.bls.gov/feed/empsit.rss",
}
NY = ZoneInfo("America/New_York")
BJ = ZoneInfo("Asia/Shanghai")
MONTHS = {name:i for i,name in enumerate(
    "January February March April May June July August September October November December".split(),1)}


def _download(client, url):
    with client.stream("GET",url) as response:
        response.raise_for_status()
        data = bytearray()
        for chunk in response.iter_bytes():
            data.extend(chunk)
            if len(data)>1_500_000:
                raise ValueError("公开来源响应超过大小上限")
        return bytes(data)


def vix_history(client):
    rows = csv.DictReader(StringIO(_download(client,VIX_SOURCE).decode("utf-8-sig")))
    if not {"DATE","CLOSE"}.issubset(rows.fieldnames or []):
        raise ValueError("VIX CSV 缺少必要字段")
    prices = {}
    for row in rows:
        try:
            day = datetime.strptime(row["DATE"],"%m/%d/%Y").date().isoformat()
            value = float(row["CLOSE"])
        except (ValueError,TypeError):
            continue
        if math.isfinite(value) and value>0:
            prices[day] = value
    return prices


def _rss(raw, url, name, since, now):
    if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise ValueError("RSS 含不支持的实体声明")
    root = ET.fromstring(raw)
    if root.tag.rsplit("}",1)[-1].lower() not in ("rss","feed","rdf"):
        raise ValueError("未返回有效订阅源")
    rows = []
    host = urlparse(url).hostname
    for node in root.iter():
        if node.tag.rsplit("}",1)[-1].lower()!="item":
            continue
        published = _date(_value(node,("pubdate","published","date")))
        title = _plain(_value(node,("title",)))
        link = _value(node,("link",))
        address = urlparse(link)
        if not published or not since<=published<=now or not title or address.hostname!=host or address.scheme!="https":
            continue
        rows.append({"title":title[:180],"url":link,"source":name,"published_at":published.isoformat(),
                     "published_beijing":published.astimezone(BJ).isoformat(),
                     "summary":_plain(_value(node,("description","summary")))[:450]})
    return rows


def _bls(raw, now, through):
    text = re.sub(r"\r?\n[ \t]","",raw.decode("utf-8-sig"))
    if "BEGIN:VCALENDAR" not in text:
        raise ValueError("BLS 未返回有效日历")
    result = []
    recognized = 0
    for block in re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT",text,re.S):
        title = re.search(r"(?m)^SUMMARY(?:;[^:\r\n]+)?:([^\r\n]+)",block)
        when = re.search(r"(?m)^DTSTART([^:\r\n]*):(\d{8}T\d{6}Z?)\s*$",block)
        if not title or not when:
            continue
        name = ("美国 CPI" if re.search(r"Consumer Price Index",title[1],re.I) else
                "美国就业报告（非农）" if re.search(r"Employment Situation",title[1],re.I) else None)
        if not name:
            continue
        if when[2].endswith("Z"):
            published = datetime.strptime(when[2],"%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        else:
            if when[1] and when[1].strip(';') not in ("TZID=America/New_York","TZID=US/Eastern","TZID=US-Eastern",
                                                      "TZID=Eastern Standard Time","TZID=EST5EDT"):
                continue
            published = datetime.strptime(when[2],"%Y%m%dT%H%M%S").replace(tzinfo=NY)
        recognized += 1
        if now<published and published.astimezone(NY).date()<=through:
            result.append({"title":name,"source":"美国劳工统计局","url":BLS_CALENDAR,
                           "date_et":published.astimezone(NY).date().isoformat(),
                           "beijing_time":published.astimezone(BJ).isoformat()})
    if not recognized:
        raise ValueError("BLS 日历未识别到 CPI 或就业报告的发布时间")
    return result


def _fed(raw, now, through):
    soup = BeautifulSoup(raw,"html.parser")
    dates = soup.select(".fomc-meeting__date")
    if not dates:
        raise ValueError("美联储会议日历结构未识别")
    result = []
    recognized = 0
    for node in dates:
        month = node.find_previous(class_="fomc-meeting__month")
        if month is None:
            continue
        year = node.find_previous(string=re.compile(r"^\s*\d{4}\s+FOMC Meetings\s*$"))
        numbers = re.findall(r"\d+",node.get_text(strip=True))
        month_num = MONTHS.get(month.get_text(strip=True).split("/")[-1].strip())
        if not year or not numbers or not month_num:
            continue
        try:
            meeting = date(int(year.strip()[:4]),month_num,int(numbers[-1]))
        except ValueError:
            continue
        recognized += 1
        if now.astimezone(NY).date()<=meeting<=through:
            result.append({"title":"FOMC 会议末日","source":"美联储","url":FOMC,
                           "date_et":meeting.isoformat(),"beijing_time":None})
    if not recognized:
        raise ValueError("美联储会议日历未识别到有效会议日期")
    return result


def macro(client, since, now, through):
    news, events, warnings = [], [], []
    for name,url in FEEDS.items():
        try:
            news.extend(_rss(_download(client,url),url,name,since,now))
        except (httpx.HTTPError,ValueError,ET.ParseError):
            warnings.append(f"{name}订阅源暂不可用")
    for name,url,parse in (("BLS 发布日历",BLS_CALENDAR,_bls),("FOMC 会议日历",FOMC,_fed)):
        try:
            events.extend(parse(_download(client,url),now,through))
        except (httpx.HTTPError,ValueError):
            warnings.append(f"{name}暂不可用")
    news = sorted({n["url"]:n for n in news}.values(),key=lambda n:n["published_at"],reverse=True)[:5]
    events.sort(key=lambda e:(e["date_et"],e["title"]))
    return {"news":[{"id":f"N{i}",**n} for i,n in enumerate(news,1)],
            "events":[{"id":f"E{i}",**e} for i,e in enumerate(events[:5],1)],"warnings":warnings}
