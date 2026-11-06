"""指数候选来源适配：纳斯达克公开表与发行商SPY持仓代理，不搜索/猜测证券身份。"""
from datetime import datetime, timezone
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import re
from zipfile import ZipFile
import xml.etree.ElementTree as ET

from bs4 import BeautifulSoup
import httpx

from .config import ROOT, settings

NDX_URL = "https://www.nasdaq.com/articles/global-indexes/nasdaq-100-index"
SPY_URL = "https://www.ssga.com/library-content/products/fund-data/etfs/us/holdings-daily-us-en-spy.xlsx"
NS = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def _ndx(content):
    soup = BeautifulSoup(content, "html.parser")
    for table in soup.select("table"):
        rows = table.select("tr")
        if not rows or "security symbol" not in rows[0].get_text(" ", strip=True).lower():
            continue
        members = []
        for row in rows[1:]:
            cells = row.select("td")
            if len(cells) != 2:
                raise ValueError("纳指公开表结构变化，原名单保留")
            name, symbol = [c.get_text(" ", strip=True) for c in cells]
            if not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,14}", symbol):
                raise ValueError("纳指公开表包含未识别证券代码")
            members.append({"symbol": symbol.replace(".", "-"), "source_symbol": symbol, "name": name})
        return {"index": "NASDAQ100", "source_type": "official_public_constituent_table", "url": NDX_URL,
                "as_of": None, "members": members,
                "note": "纳斯达克官网公开表；未标明独立成分生效日，文章发布日期不作为名单日期。"}
    raise ValueError("未读取到纳斯达克完整公开表，拒绝用搜索摘要或前十名代替")


def _spy(content):
    # 标准库读取发行商固定格式XLSX，避免引入整套电子表格运行时。
    with ZipFile(BytesIO(content)) as archive:
        if sum(i.file_size for i in archive.infolist()) > 20_000_000:
            raise ValueError("持仓文件解压体积异常")
        strings = ["".join(item.itertext()) for item in ET.fromstring(archive.read("xl/sharedStrings.xml"))]
        rows = []
        root = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))
        for row in root.findall("s:sheetData/s:row", NS):
            cells = {}
            for cell in row:
                value = cell.findtext("s:v", default="", namespaces=NS)
                col = re.sub(r"\d", "", cell.attrib["r"])
                cells[col] = strings[int(value)] if cell.attrib.get("t") == "s" else value
            rows.append(cells)
    if rows[1].get("B") != "SPY" or rows[4].get("B") != "Ticker" or rows[4].get("H") != "Local Currency":
        raise ValueError("SPY持仓表结构变化，原名单保留")
    as_of = datetime.strptime(rows[2]["B"].removeprefix("As of "), "%d-%b-%Y").date().isoformat()
    members, excluded = [], []
    for row in rows[5:]:
        symbol = row.get("B", "").strip()
        identifier = row.get("C", "")
        if not identifier.isalnum() or len(identifier) != 9:
            continue
        item = {"symbol": symbol.replace(".", "-"), "source_symbol": symbol, "name": row.get("A", "")}
        if row.get("H") != "USD" or not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,14}", item["symbol"]):
            excluded.append(item)
        else:
            members.append(item)
    return {"index": "SP500", "source_type": "SPY_equity_holdings_proxy", "url": SPY_URL, "as_of": as_of,
            "members": members, "excluded_non_tradable_items": excluded,
            "note": "SPY发行商美元股票持仓作为标普500候选代理，非官方指数成分授权数据；排除现金/内部非交易项目。"}


def refresh(cfg):
    from .universe import validate_members, sync_lock
    directory = settings.storage / "universe" / "sources"
    directory.mkdir(parents=True, exist_ok=True)
    with sync_lock():
        sources = []
        # 代理沿用命令行环境；错误不透传HTTP完整请求，不触碰财务/身份接口。
        with httpx.Client(timeout=35, follow_redirects=True, headers={"User-Agent": "stock-research/0.1 public-index-reader"}) as client:
            for url, parser, suffix in ((NDX_URL, _ndx, "html"), (SPY_URL, _spy, "xlsx")):
                try:
                    response = client.get(url)
                    response.raise_for_status()
                except httpx.HTTPError:
                    raise ValueError("指数公开来源暂不可读，未改写名单；可继续使用尚有效的本地来源文件") from None
                if len(response.content) > 10_000_000:
                    raise ValueError("指数来源文件体积异常")
                raw = directory / (sha256(response.content).hexdigest() + "." + suffix)
                raw.write_bytes(response.content)
                source = parser(response.content)
                source.update(retrieved_at=datetime.now(timezone.utc).isoformat(), raw_file=str(raw))
                sources.append(source)
        data = {"version": "us-index-members-v1", "retrieved_at": datetime.now(timezone.utc).isoformat(), "sources": sources}
        validate_members(cfg, data)
        path = Path(cfg.members_file).expanduser()
        if not path.is_absolute():
            path = ROOT / path
        # 保留每版完整来源；两份资料都通过校验后才一次性发布新名单。
        version = directory / (sha256(json.dumps(data, sort_keys=True).encode()).hexdigest() + ".json")
        version.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        temporary = path.with_suffix(".pending")
        temporary.write_text(version.read_text())
        temporary.replace(path)
        return data
