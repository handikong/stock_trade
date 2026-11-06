import hashlib
import json
from datetime import date, datetime, timezone
import yfinance as yf
from .config import settings
from .documents import archive, fetch_public, saved_document
from .market import clean, frame_records
from .report_scope import fact_periods, recent_filings, scope_config, scoped_facts
from .providers import failure_detail, fmp_get, yahoo_call
from .store import Session, Security, latest, save_snapshot, stamp

METRICS={
    "revenue":("营收",["RevenueFromContractWithCustomerExcludingAssessedTax","Revenues","SalesRevenueNet","Revenue"]),
    "net_income":("净利润",["NetIncomeLoss","ProfitLoss"]),
    "ebit":("经营利润",["OperatingIncomeLoss","ProfitLossFromOperatingActivities"]),
    "eps_diluted":("稀释EPS",["EarningsPerShareDiluted","DilutedEarningsLossPerShare"]),
    "shares_diluted":("加权稀释股数",["WeightedAverageNumberOfDilutedSharesOutstanding","WeightedAverageNumberOfDilutedSharesOutstandingAdjustedForShareSplit"]),
    "cfo":("经营现金流",["NetCashProvidedByUsedInOperatingActivities","CashFlowsFromUsedInOperatingActivities"]),
    "capex":("资本开支",["PaymentsToAcquirePropertyPlantAndEquipment","PurchaseOfPropertyPlantAndEquipment"]),
    "cash":("现金及等价物",["CashAndCashEquivalentsAtCarryingValue","CashAndCashEquivalents"]),
    "debt_current":("流动长期债务",["LongTermDebtCurrent"]),
    "debt_noncurrent":("非流动长期债务",["LongTermDebtNoncurrent"]),
    "gross_profit":("毛利润",["GrossProfit"]),
    "pretax_income":("税前利润",["IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest","IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments"]),
    "income_tax":("所得税费用",["IncomeTaxExpenseBenefit"]),
    "depreciation_amortization":("折旧摊销",["DepreciationDepletionAndAmortization","DepreciationDepletionAndAmortizationPropertyPlantAndEquipment","DepreciationAmortizationAndAccretionNet"]),
    "stock_compensation":("股份支付费用",["ShareBasedCompensation"]),
    "assets":("总资产",["Assets"]),
    "liabilities":("总负债",["Liabilities"]),
    "equity":("股东权益",["StockholdersEquity","StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"]),
    "current_assets":("流动资产",["AssetsCurrent"]),
    "current_liabilities":("流动负债",["LiabilitiesCurrent"]),
    "receivables":("应收账款净额",["AccountsReceivableNetCurrent"]),
    "inventory":("存货",["InventoryNet"]),
    "payables":("应付账款",["AccountsPayableCurrent"]),
    "short_term_borrowings":("短期借款/商业票据",["ShortTermBorrowings","CommercialPaper"]),
    "shares_outstanding":("期末普通股股数",["CommonStockSharesOutstanding"]),
}


def raw_archive(value, suffix="json"):
    raw=json.dumps(value,ensure_ascii=False,default=str).encode()
    filename=hashlib.sha256(raw).hexdigest()+"."+suffix
    (settings.storage/"documents"/filename).write_bytes(raw)
    return filename


def sec_json(url):
    if not settings.sec_user_agent.strip():
        raise ValueError("请在.env填写SEC_USER_AGENT（应用名称与联系邮箱），再获取SEC原始披露")
    raw,_,_=fetch_public(url,limit=40_000_000)
    return json.loads(raw)


def normalize_facts(data, cik: str, report_periods: list[str] | None = None) -> list[dict]:
    result=[]
    for namespace, concepts in data.get("facts",{}).items():
        if namespace not in ("us-gaap","ifrs-full"):
            continue
        for metric,(label,tags) in METRICS.items():
            selected=next((t for t in tags if t in concepts),None)
            if not selected:
                continue
            for unit,values in concepts[selected].get("units",{}).items():
                by_period={}
                for fact in values:
                    if not fact.get("end") or fact.get("val") is None:
                        continue
                    end=date.fromisoformat(fact["end"])
                    start=fact.get("start")
                    days=(end-date.fromisoformat(start)).days if start else None
                    period="instant" if days is None else "quarter" if 65<=days<=110 else "annual" if 300<=days<=400 else "cumulative"
                    key=(start,fact["end"],unit)
                    current=by_period.get(key)
                    # 当期在线使用最新已披露修订；旧快照始终保留供未来回放。
                    if current and (current.get("filed","")>fact.get("filed","")):
                        continue
                    by_period[key]=fact
                for (start,end,_),fact in by_period.items():
                    days=(date.fromisoformat(end)-date.fromisoformat(start)).days if start else None
                    period="instant" if days is None else "quarter" if 65<=days<=110 else "annual" if 300<=days<=400 else "cumulative"
                    accession=fact.get("accn","")
                    result.append({"id":f"sec:{accession}:{namespace}:{selected}:{start}:{end}:{unit}",
                        "metric":metric,"label":label,"value":str(fact["val"]),"unit":unit,
                        "start":start,"end":end,"period_type":period,"filed":fact.get("filed"),
                        "fiscal_year":fact.get("fy"),"fiscal_period":fact.get("fp"),"form":fact.get("form"),
                        "basis":"GAAP" if namespace=="us-gaap" else "IFRS",
                        "source_tag":f"{namespace}:{selected}","source":"SEC XBRL",
                        "source_url":f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-','')}/{accession}-index.html",
                        "availability_precision":"filing_date_only","status":"reported"})
    return scoped_facts(result,report_periods)


def dynamic_is_fresh(item):
    """仅用于预期、日程、修订等动态快照；财报档案没有时间有效期。"""
    return bool(item and item.payload.get("fetched_at") and
        (datetime.now(timezone.utc)-datetime.fromisoformat(item.payload["fetched_at"])).total_seconds()<settings.disclosure_cache_hours*3600)


def refresh_sec(security_id: str,force=False):
    return {"status":"disabled","skipped":"SEC财务自动获取已停用，请手工录入"}


def _refresh_sec_legacy(security_id: str,force=False):
    with Session() as session:
        security=session.get(Security,security_id)
        if security.profile.get("type")=="ETF":
            return {"skipped":"ETF使用基金档案，不获取公司EPS财报"}
        profile=dict(security.profile)
        symbol=security.symbol
        current=latest(session,security_id,"financials")
        filings=latest(session,security_id,"filings")
        if (not force and current and current.payload.get("source")=="SEC XBRL"
                and current.payload.get("scope")==scope_config() and filings
                and current.payload.get("scope_status")=="confirmed_against_filings"
                and filings.payload.get("scope")==scope_config() and not filings.payload.get("errors")
                and current.payload.get("facts") and filings.payload.get("items")
                and all((settings.storage/"documents"/name).is_file() for name in
                    (current.payload.get("raw_file", ""),filings.payload.get("submissions_raw_file", "")))
                and all(saved_document(security_id,"filing",row["url"],row["period_end"],row["accession"])
                    for row in filings.payload["items"])):
            # 已记录的完整财报永久复用，新一期后续由财报发布事件写入；force保留为人工同步入口。
            return {"cached":True,"retention":"permanent"}
    cik=profile.get("cik")
    if not cik:
        listing=sec_json("https://www.sec.gov/files/company_tickers.json")
        match=next((v for v in listing.values() if v["ticker"].upper().replace("-",".")==symbol.replace("-",".")),None)
        if not match:
            raise ValueError("SEC映射未找到对应CIK，请核对股份类别；可导入官方材料继续研究")
        cik=str(match["cik_str"]).zfill(10)
        with Session() as session:
            security=session.get(Security,security_id)
            security.profile={**security.profile,"cik":cik,"sec_name":match["title"]}
            session.commit()
    data=sec_json(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json")
    raw_file=raw_archive(data)
    facts=normalize_facts(data,cik)
    if not facts:
        raise ValueError("SEC未返回可提取的最近财报期标准指标，已保留原有财务快照")
    with Session() as session:
        save_snapshot(session,security_id,"financials",{"source":"SEC XBRL","fetched_at":stamp(),
            "retention":"permanent",
            "facts":facts,"raw_file":raw_file,"scope":scope_config(),"report_periods":fact_periods(facts),
            "scope_status":"inferred_from_facts",
            "warnings":["仅保留最近财报期的标准概念，不包含所有公司自定义指标；单季与累计期间分开，不自行拼凑EPS"]})
        session.commit()
    submissions=sec_json(f"https://data.sec.gov/submissions/CIK{cik}.json")
    submissions_file=raw_archive(submissions)
    recent=submissions.get("filings",{}).get("recent",{})
    manifest=recent_filings(recent)
    for row in manifest:
        row["url"]=f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{row['accession'].replace('-','')}/{row.pop('primary_document')}"
    if not manifest:
        raise ValueError("SEC近期索引中没有可确认的定期财报；8-K/6-K不自动当作季报，请使用公司原文补充")
    report_periods=sorted({r["period_end"] for r in manifest},reverse=True)
    # 原始批量响应完整归档，研究快照只保留与选中财报期间一致的事实。
    facts=normalize_facts(data,cik,report_periods)
    if not facts:
        raise ValueError("最新定期财报与结构化财务期间暂未对齐，已保存取得的指标，尚不能确认资料齐全")
    with Session() as session:
        current=latest(session,security_id,"financials")
        selected={**current.payload,"facts":facts,"report_periods":report_periods,
            "scope_status":"confirmed_against_filings"}
        save_snapshot(session,security_id,"financials",selected)
        session.commit()
    downloaded=[]; errors=[]
    if len(report_periods)<settings.financial_report_count:
        errors.append({"message":f"仅找到{len(report_periods)}个定期财报期间，少于设置的{settings.financial_report_count}期"})
    for row in manifest:
        try:
            existing=saved_document(security_id,"filing",row["url"],row["period_end"],row["accession"])
            if existing:
                downloaded.append(existing)
                continue
            raw,ctype,url=fetch_public(row["url"])
            downloaded.append(archive(security_id,"filing",f"{row['form']} · {row['period_end'] or row['filed']}",
                raw,url.rsplit("/",1)[-1],source_url=url,published_at=row["accepted_at"] or row["filed"],
                period_end=row["period_end"],content_type=ctype,metadata={"accession":row["accession"],"form":row["form"]}))
        except Exception:
            errors.append({"url":row["url"],"message":"文件暂未归档，可稍后从披露索引重试"})
    with Session() as session:
        save_snapshot(session,security_id,"filings",{"fetched_at":stamp(),"items":manifest,
            "retention":"permanent",
            "archived_ids":[d["id"] for d in downloaded],"errors":errors,"scope":scope_config(),
            "report_periods":report_periods,"submissions_raw_file":submissions_file})
        session.commit()
    return {"facts":len(facts),"archived":len(downloaded),"warnings":errors}


def refresh_vendor_financials(security_id: str,force=False):
    return {"status":"disabled","skipped":"第三方财务自动获取已停用，请手工录入"}


def _refresh_vendor_financials_legacy(security_id: str,force=False):
    with Session() as session:
        security=session.get(Security,security_id)
        symbol=security.symbol
        if security.profile.get("type")=="ETF":
            return {"skipped":True}
        current=latest(session,security_id,"financials")
        if current and current.payload.get("source")=="SEC XBRL":
            return {"skipped":"保留SEC主数据，不用第三方财务覆盖"}
        if not force and current and current.payload.get("facts") and current.payload.get("scope")==scope_config():
            return {"cached":True,"retention":"permanent"}
    ticker=yf.Ticker(symbol)
    facts=[];warnings=[]
    # 辅助来源也取季度三表；不能分别取两年和两季后混成“最近两次”。
    for frequency,operation,fn in [("quarter","get_income_stmt",ticker.get_income_stmt),
                            ("quarter","get_cashflow",ticker.get_cashflow),("instant","get_balance_sheet",ticker.get_balance_sheet)]:
        try:
            frame=yahoo_call(security_id,operation,lambda:fn(freq="quarterly"))
        except Exception as exc:
            warnings.append(failure_detail(exc,operation));continue
        for label,row in frame.iterrows():
            for end,value in row.items():
                value=clean(value)
                if value is None:
                    continue
                facts.append({"id":f"yf:{label}:{end}:{frequency}","metric":str(label),"label":str(label),
                    "value":value,"end":str(end.date()),"start":None,"period_type":frequency,
                    "basis":"供应商口径，待核对","unit":"未知，见字段名称与原始报表",
                    "source":"Yahoo Finance","source_url":f"https://finance.yahoo.com/quote/{symbol}/financials/",
                    "filed":None,"status":"vendor_unverified"})
    facts=scoped_facts(facts)
    if not facts:
        return {"warnings":warnings+[{"message":"供应商未返回财务表，可配置SEC或导入原始资料；原缓存保留"}]}
    with Session() as session:
        save_snapshot(session,security_id,"financials",{"source":"Yahoo Finance","facts":facts,
            "retention":"permanent",
            "fetched_at":stamp(),"scope":scope_config(),"report_periods":fact_periods(facts),
            "warnings":["第三方财务仅作辅助，季度期间/单位与GAAP口径需核对；尚未获取官方原文"]})
        session.commit()
    return {"facts":len(facts),"warnings":warnings}


def refresh_estimates(security_id: str,force=False):
    return {"status":"disabled","skipped":"预期与日程自动获取已停用，请手工录入"}


def _refresh_estimates_legacy(security_id: str,force=False):
    with Session() as session:
        security=session.get(Security,security_id)
        symbol=security.symbol
        if security.profile.get("type")=="ETF":
            return {"skipped":True}
        old=latest(session,security_id,"estimates")
        reported=latest(session,security_id,"financials")
        reported_periods=(reported.payload.get("report_periods") or fact_periods(reported.payload.get("facts",[]))) if reported else []
        cutoff=max(reported_periods) if reported_periods else None
        components={kind:latest(session,security_id,kind) for kind in ("earnings_calendar","eps_revisions","eps_trend")}
    ticker=yf.Ticker(symbol)
    warnings=[];results={}
    fresh_estimates=bool(old and (datetime.now(timezone.utc)-datetime.fromisoformat(old.payload["captured_at"])).total_seconds()<settings.disclosure_cache_hours*3600)
    if not force and fresh_estimates:
        results["estimates"]={"cached":True}
    else:
        try:
            estimates=frame_records(yahoo_call(security_id,"get_earnings_estimate",ticker.get_earnings_estimate))
            if not estimates:
                raise ValueError("Yahoo未返回一致预期，保留已有快照")
            source="Yahoo Finance";source_url=f"https://finance.yahoo.com/quote/{symbol}/analysis/"
        except Exception as exc:
            warnings.append(failure_detail(exc,"get_earnings_estimate"))
            estimates=[]
            if settings.fmp_api_key.get_secret_value():
                try:
                    source="FMP";source_url="https://site.financialmodelingprep.com/developer/docs/stable/analyst-estimates"
                    # 已结束但尚未公布的季度仍需要前瞻，不能用“期末早于今天”将其误删。
                    for period in ("quarter","annual"):
                        rows=fmp_get("analyst-estimates",symbol=symbol,period=period,page=0,limit=20)
                        future=sorted((r for r in rows if r.get("symbol")==symbol and r.get("date")
                            and (r["date"]>cutoff if cutoff else r["date"]>=date.today().isoformat())),key=lambda r:r["date"])
                        estimates.extend({**clean(r),"period":r["date"]+" · "+period,"provider_period":period,
                            "avg":clean(r.get("epsAvg")),"low":clean(r.get("epsLow")),"high":clean(r.get("epsHigh")),
                            "numberOfAnalysts":clean(r.get("numAnalystsEps"))} for r in future[:4])
                    if not estimates:
                        warnings.append({"message":"FMP没有返回当前所需目标期间的预期，保留缺失，不使用过去已公布期间的估计充数"})
                except Exception as backup_exc:
                    warnings.append(failure_detail(backup_exc,"analyst-estimates"))
            else:
                warnings.append({"message":"Yahoo预期不可用，且未配置FMP_API_KEY；一致预期未补齐","category":"not_configured"})
        if estimates:
            payload={"source":source,"captured_at":stamp(),"fetched_at":stamp(),"earnings":estimates,
                "basis":"供应商未明确GAAP/non-GAAP时保持未知；季度/年度分开，目标期按最近已取得财报过滤，不用于自动历史超预期计算",
                "source_url":source_url,"warnings":[]}
            with Session() as session:
                save_snapshot(session,security_id,"estimates",payload);session.commit()
            results["estimates"]={"rows":len(estimates),"source":source}
    # 日程、修订、趋势各自有快照/有效期；主预期失败不能阻断这些请求，失败也不清空旧值。
    for kind,operation,fn in [("earnings_calendar","get_calendar",ticker.get_calendar),
            ("eps_revisions","get_eps_revisions",ticker.get_eps_revisions),("eps_trend","get_eps_trend",ticker.get_eps_trend)]:
        if not force and dynamic_is_fresh(components[kind]):
            results[kind]={"cached":True};continue
        try:
            value=yahoo_call(security_id,operation,fn)
            value=clean(value or {}) if kind=="earnings_calendar" else frame_records(value)
            if not value:
                raise ValueError(f"{operation}未返回有效资料，保留已有快照")
            with Session() as session:
                save_snapshot(session,security_id,kind,{"source":"Yahoo Finance","fetched_at":stamp(),
                    "captured_at":stamp(),"data":value,"confirmed":False,
                    "message":"供应商辅助资料，日程尚未由公司确认，不自动冻结前瞻"});session.commit()
            results[kind]={"updated":True}
        except Exception as exc:
            warnings.append(failure_detail(exc,operation))
            if kind=="earnings_calendar" and settings.fmp_api_key.get_secret_value():
                try:
                    rows=fmp_get("earnings",symbol=symbol)
                    future=sorted((r for r in rows if r.get("symbol")==symbol and r.get("date","")>=date.today().isoformat()),key=lambda r:r["date"])
                    if not future:
                        raise ValueError("FMP尚未返回未来财报日程，不能将历史公布日当作下一次日期")
                    with Session() as session:
                        save_snapshot(session,security_id,kind,{"source":"FMP","fetched_at":stamp(),"captured_at":stamp(),
                            "data":clean(future[:4]),"confirmed":False,
                            "message":"供应商日程尚未由公司确认，不自动冻结前瞻"});session.commit()
                    results[kind]={"updated":True,"source":"FMP"}
                except Exception as backup_exc:
                    warnings.append(failure_detail(backup_exc,"earnings"))
    return {"components":results,"warnings":warnings,"cached":bool(results) and all(r.get("cached") for r in results.values())}
