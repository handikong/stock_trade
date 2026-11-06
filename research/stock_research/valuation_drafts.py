"""从已存数据生成有证据的估值草稿；不调用AI、不获取更多财报、不确认假设。"""
import json
import re
from bisect import bisect_right
from datetime import date, timedelta
from decimal import Decimal, localcontext
from pathlib import Path

from .config import settings
from .financials import METRICS
from .store import Security, Session, digest, latest, save_snapshot
from .valuation import calculate, number
from .market import cache_state

VERSION="automatic-valuation-draft-independent-models-v2"


def archive_json(name):
    if not re.fullmatch(r"[0-9a-f]{64}\.json",name or ""):
        raise ValueError("估值基线缺少有效SEC原始档案")
    return json.loads((settings.storage/"documents"/name).read_text())


def policy():
    path=Path(settings.valuation_policy_file).expanduser().resolve()
    value=json.loads(path.read_text())
    if not isinstance(value,dict) or not isinstance(value.get("enabled"),bool):
        raise ValueError("估值配置需要enabled布尔值")
    if not value["enabled"]:
        return value
    if value.get("review_status")!="draft" or not value.get("source_note"):
        raise ValueError("自动估值必须保留draft及假设依据，不能自动确认为正式估值")
    for key,low,high in (("forecast_years",2,15),("pe_lookback_sessions",30,1500),("pe_minimum_samples",20,1000),("max_financial_age_days",30,400)):
        if type(value.get(key)) is not int or not low<=value[key]<=high:
            raise ValueError(f"估值配置{key}超出范围")
    if value["pe_minimum_samples"]>value["pe_lookback_sessions"]:
        raise ValueError("PE最低样本数不能超过观察窗口")
    floor,ceiling=(number(value.get(k),k) for k in ("growth_floor","growth_ceiling"))
    if not Decimal(-1)<floor<=ceiling<=1:
        raise ValueError("预测增长边界需要满足-1<下限<=上限<=1")
    rows=value.get("scenarios")
    if not isinstance(rows,list) or len(rows)!=3 or len({r.get("name") for r in rows if isinstance(r,dict)})!=3:
        raise ValueError("请配置三个名称不同的估值草稿情景")
    for row in rows:
        if not row.get("name") or not 0<=number(row.get("pe_quantile"),"PE分位")<=1:
            raise ValueError("估值情景名称或PE分位无效")
        for key in ("growth_delta","wacc_delta","terminal_growth"):
            number(row.get(key),key)
    return value


class Ledger:
    """保留同一事实的各次披露；历史倍数只使用价格当日之前已披露的数字。"""
    def __init__(self, raw, cik):
        self.cik=cik
        self.rows={}
        concepts=raw.get("facts",{}).get("us-gaap",{})
        for metric,(_,tags) in METRICS.items():
            values=[]
            unit="shares" if metric.startswith("shares_") else "USD/shares" if metric=="eps_diluted" else "USD"
            for priority,tag in enumerate(tags):
                for row in concepts.get(tag,{}).get("units",{}).get(unit,[]):
                    if row.get("form") in ("10-Q","10-K","10-Q/A","10-K/A") and row.get("filed") and row.get("end"):
                        values.append({**row,"tag":tag,"unit":unit,"priority":priority})
            self.rows[metric]=values

    def periods(self, metric, as_of):
        selected={}
        for row in self.rows[metric]:
            if row["filed"]>as_of or row["end"]>as_of:
                continue
            key=(row.get("start"),row["end"])
            previous=selected.get(key)
            if previous is None or (row["filed"],-row["priority"])>(previous["filed"],-previous["priority"]):
                selected[key]=row
        return selected

    def get(self, metric, period, as_of):
        value=self.periods(metric,as_of).get(period)
        if value is None:
            raise ValueError(f"估值补充基线缺少{metric}：{period}")
        return value

    def bundle(self, as_of):
        income=self.periods("net_income",as_of)
        candidates=[r for r in income.values() if r.get("start") and 60<=days(r)<=380]
        if not candidates:
            raise ValueError("尚无可构造TTM的净利润期间")
        current=max(candidates,key=lambda r:(r["end"],days(r)))
        if days(current)>=330:
            return [current]
        annuals=[r for r in candidates if days(r)>=330 and
            date.fromisoformat(r["end"])+timedelta(days=1)==date.fromisoformat(current["start"])]
        if not annuals:
            raise ValueError("TTM需要上一完整财年，不能将两季直接年化")
        annual=max(annuals,key=lambda r:r["end"])
        prior=[r for r in candidates if r["start"]==annual["start"] and abs(days(r)-days(current))<=7
            and r["end"]<annual["end"]]
        if not prior:
            raise ValueError("TTM需要上年同期累计数据，不能只使用本年累计值")
        return [annual,current,min(prior,key=lambda r:abs(days(r)-days(current)))]

    def ttm(self, metric, bundle, as_of, weighted=False):
        rows=[self.get(metric,(b["start"],b["end"]),as_of) for b in bundle]
        weights=[1] if len(rows)==1 else [1,1,-1]
        if len({r["tag"] for r in rows})!=1:
            raise ValueError(f"{metric}的TTM组成使用不同会计概念，需先核对")
        duration=sum(w*days(r) for w,r in zip(weights,rows))
        if not 330<=duration<=380:
            raise ValueError("TTM组成期间不接近完整年度，拒绝混算")
        if weighted:
            value=sum(number(r["val"],metric)*w*days(r) for w,r in zip(weights,rows))/duration
        else:
            value=sum(number(r["val"],metric)*w for w,r in zip(weights,rows))
        return value,rows

    def eps(self, as_of):
        bundle=self.bundle(as_of)
        income,_=self.ttm("net_income",bundle,as_of)
        shares,_=self.ttm("shares_diluted",bundle,as_of,weighted=True)
        if shares<=0 or income<=0:
            raise ValueError("TTM净利润或稀释股数非正，PE不适用")
        return income/shares

    def evidence(self, row):
        accession=row.get("accn","")
        return {"id":f"sec:{accession}:{row['tag']}:{row.get('start')}:{row['end']}",
            "concept":row["tag"],"start":row.get("start"),"end":row["end"],"filed":row["filed"],
            "value":str(row["val"]),"unit":row["unit"],
            "source_url":f"https://www.sec.gov/Archives/edgar/data/{int(self.cik)}/{accession.replace('-','')}/{accession}-index.html"}


def days(row):
    return (date.fromisoformat(row["end"])-date.fromisoformat(row["start"])).days+1


def quantile(values, fraction):
    values=sorted(values)
    position=(len(values)-1)*fraction
    left=int(position); right=min(left+1,len(values)-1)
    return values[left]+(values[right]-values[left])*(position-left)


def quote_number(value):
    # 行情供应商浮点尾数可能超过输入精度限制，先以Decimal规整，原始报价仍保留在行情档案。
    return number(Decimal(str(value)).quantize(Decimal("0.000000000001")),"行情报价")


def generate(security_id, force=False):
    # 用户切换手工估值；即使旧配置enabled=true或force，也不恢复自动估值。
    return {"skipped":"自动估值已停用，请录入并确认手工EPS/PE或DCF假设", "status":"disabled"}


def _generate_legacy_entry(security_id, force=False):
    """停用前实现留档，不由研究或通知调用。"""
    with localcontext() as ctx:
        ctx.prec=32
        return _generate(security_id)


def component_warnings(summary):
    """缓存命中也保留模型缺口，不能因第二次查询而把部分结果显示为完整成功。"""
    names={"pe":"当前PE","eps":"EPS×PE","dcf":"DCF"}
    return [{"model":key,"message":f"{names[key]}：{part['message']}"}
            for key,part in summary.get("components",{}).items() if part["status"]!="draft"]


def _text(value):
    return str(value.quantize(Decimal("0.000000000001")))


def _growth(ledger, bundle, as_of, evidence):
    current=bundle[1] if len(bundle)==3 else bundle[0]
    now=ledger.get("revenue",(current["start"],current["end"]),as_of)
    if len(bundle)==3:
        previous=ledger.get("revenue",(bundle[2]["start"],bundle[2]["end"]),as_of)
    else:
        annuals=[r for r in ledger.periods("revenue",as_of).values() if r.get("start")
                 and 330<=days(r)<=380 and r["end"]<now["start"]]
        if not annuals:
            raise ValueError("缺少上年可比营收，无法生成增长草稿")
        previous=max(annuals,key=lambda r:r["end"])
    evidence.extend(ledger.evidence(r) for r in (now,previous))
    if number(previous["val"],"上年营收")<=0:
        raise ValueError("上年可比营收非正，不能外推增长")
    return number(now["val"],"当期营收")/number(previous["val"],"上年营收")-1


def _pe_history(ledger, bars, cfg):
    # 每个历史价格只用当日之前披露的盈利，不能拿当前盈利覆盖历史。
    states={}; multiples=[]; sample=[]
    filing_days=sorted({r["filed"] for m in ("net_income","shares_diluted") for r in ledger.rows[m]})
    for bar in bars[-cfg["pe_lookback_sessions"]:]:
        index=bisect_right(filing_days,bar["date"])-1
        if index>=0 and filing_days[index]==bar["date"]:
            index-=1
        if index<0:
            continue
        known=filing_days[index]
        if known not in states:
            try:
                states[known]=ledger.eps(known)
            except ValueError:
                states[known]=None
        if states[known] is None:
            continue
        multiple=quote_number(bar["close"])/states[known]
        if multiple>0:
            multiples.append(multiple)
            sample.append({"date":bar["date"],"known_by":known,"eps":str(states[known]),"pe":str(multiple)})
    return multiples,sample


def _scenario_growth(scenario, growth, cfg):
    return max(number(cfg["growth_floor"],"增长下限"),min(number(cfg["growth_ceiling"],"增长上限"),
               growth+number(scenario["growth_delta"],"增长扰动")))


def _eps_model(current_eps, multiples, growth, horizon_years, end, target, cfg, profile):
    if len(multiples)<cfg["pe_minimum_samples"]:
        raise ValueError("可追溯历史PE样本不足，请补齐历史基线或明确输入PE情景")
    rows=[]; assumptions=[]
    for scenario in cfg["scenarios"]:
        g=_scenario_growth(scenario,growth,cfg)
        multiple=quantile(multiples,number(scenario["pe_quantile"],"PE分位"))
        rows.append({"name":scenario["name"],"eps":_text(current_eps*(1+g)**horizon_years),"pe":_text(multiple)})
        assumptions.append({**scenario,"projected_growth":_text(g),"pe":_text(multiple)})
    payload={"method":"eps","currency":profile["currency"],"convention":"target_ttm","target_date":target.isoformat(),
        "eps_basis":"SEC GAAP净利润/加权稀释股数派生TTM；固定利润率及股数的预测草稿",
        "period_note":f"由{end}已披露TTM桥接并投影至{target.isoformat()}目标TTM；未将Yahoo FY1直接当作NTM，也未将两季年化",
        "source_note":cfg["source_note"]+"；PE取历史当时可知盈利匹配报价的分位。","scenarios":rows}
    return calculate(payload,profile),assumptions


def _dcf_models(ledger, bundle, as_of, end, target, growth, cfg, profile, symbol, sic, evidence, base):
    if not sic.isdigit() or 6000<=int(sic)<=6999:
        raise ValueError("公司行业未知或属于金融/地产行业，不自动套用经营公司FCFF")
    # DCF专属字段在本分支读取，不影响PE及EPS模型；也不依赖历史PE样本数。
    for metric in ("revenue","ebit","income_tax","pretax_income","depreciation_amortization","capex","stock_compensation"):
        value,rows=ledger.ttm(metric,bundle,as_of)
        base[metric]=value; evidence.extend(ledger.evidence(r) for r in rows)
    for metric in ("cash","debt_current","debt_noncurrent","short_term_borrowings","shares_outstanding","receivables","inventory","payables"):
        row=ledger.get(metric,(None,end),as_of)
        base[metric]=number(row["val"],metric);evidence.append(ledger.evidence(row))
    if base["revenue"]<=0 or base["pretax_income"]<=0 or base["shares_outstanding"]<=0:
        raise ValueError("营收、税前利润或期末股数无效，自动DCF不适用")
    if min(base[k] for k in ("cash","debt_current","debt_noncurrent","short_term_borrowings","depreciation_amortization","capex","stock_compensation"))<0:
        raise ValueError("现金、债务或投资费用出现异常负值，请先核对会计口径")
    tax=base["income_tax"]/base["pretax_income"]
    if not 0<=tax<=1:
        raise ValueError("TTM有效税率不适用，需明确正常化税率假设")
    symbol_cfg=cfg.get("symbols",{}).get(symbol,{})
    if not (symbol_cfg.get("wacc") is not None and str(symbol_cfg.get("sic"))==sic
            and symbol_cfg.get("wacc_source",{}).get("url")):
        raise ValueError("缺少本股票匹配行业编码的有来源WACC配置，不猜测折现率")
    debt=base["debt_current"]+base["debt_noncurrent"]+base["short_term_borrowings"]
    lag_years=Decimal((date.fromisoformat(as_of)-date.fromisoformat(end)).days)/Decimal("365.25")
    models=[]; rows=[]; assumptions=[]
    for scenario in cfg["scenarios"]:
        g=_scenario_growth(scenario,growth,cfg)
        assumptions.append({**scenario,"projected_growth":_text(g)})
        years=[]; revenue=base["revenue"]*(1+g)**lag_years
        nwc_ratio=(base["receivables"]+base["inventory"]-base["payables"])/base["revenue"]
        terminal=number(scenario["terminal_growth"],"终值增长")
        for i in range(cfg["forecast_years"]):
            annual_growth=g+(terminal-g)*Decimal(i)/Decimal(cfg["forecast_years"]-1)
            next_revenue=revenue*(1+annual_growth)
            years.append({"ebit":_text(next_revenue*base["ebit"]/base["revenue"]),"tax_rate":_text(tax),
                "da":_text(next_revenue*base["depreciation_amortization"]/base["revenue"]),
                "capex":_text(next_revenue*base["capex"]/base["revenue"]),"delta_wc":_text((next_revenue-revenue)*nwc_ratio)})
            revenue=next_revenue
        payload={"method":"dcf","currency":profile["currency"],"nonfinancial_confirmed":True,
            "wacc":_text(number(symbol_cfg["wacc"],"行业WACC")+number(scenario["wacc_delta"],"WACC扰动")),
            "terminal_growth":str(terminal),"shares":str(base["shares_outstanding"]),"cash":str(base["cash"]),"debt":str(debt),
            "other_claims":"0","years":years,"year_one_state":{"cash":str(base["cash"]),"debt":str(debt),"other_claims":"0","shares":str(base["shares_outstanding"])},
            "source_note":cfg["source_note"]+"；WACC为行业代理而非公司自身估计，见wacc_source；已披露财务先桥接至当前估值日，再预测未来现金流。",
            "terminal_policy_note":"末年增长收敛到g，终值沿同一经营利润率与DA/capex/NWC代理比例增长；SBC不加回，股数不变；现金债务保持基线，其他索取权假设为0。",
            "wacc_source":symbol_cfg["wacc_source"],"target_date":target.isoformat()}
        model=calculate(payload,profile)
        model["scenario_name"]=scenario["name"]
        models.append(model)
        rows.append({"name":scenario["name"],"current_price":model["price"],"target_price":model["target"]["price"],
                     "wacc":payload["wacc"],"terminal_growth":payload["terminal_growth"],"terminal_share":model["terminal_share"]})
    return models,rows,assumptions,symbol_cfg["wacc_source"].get("note","行业WACC代理参数需复核")


def _generate(security_id):
    cfg=policy()
    if not cfg["enabled"]:
        return {"skipped":"自动估值草稿已在配置中关闭"}
    with Session() as session:
        security=session.get(Security,security_id)
        if security.profile.get("type")!="EQUITY" or security.profile.get("currency")!="USD":
            return {"skipped":"自动草稿当前仅适用于有SEC US-GAAP数据的美元公司股票"}
        snapshots={k:latest(session,security_id,k) for k in ("market","financials","filings")}
        if not all(snapshots.values()):
            raise ValueError("自动估值需要先取得行情、SEC财务与披露索引")
        if cache_state(snapshots["market"])["state"]!="fresh":
            raise ValueError("行情缓存不是完整新鲜状态，先刷新行情再生成估值")
        market,financials,filings=(snapshots[k].payload for k in ("market","financials","filings"))
        if financials.get("source")!="SEC XBRL":
            raise ValueError("自动估值缺少可追溯的SEC财务基线")
        source_key={k:v.fingerprint for k,v in snapshots.items()}
        key=digest({"version":VERSION,"sources":source_key,"policy":cfg,"date":date.today().isoformat()})
        old=latest(session,security_id,"valuation_draft")
        if old and old.fingerprint==key:
            return {"cached":True,"snapshot_id":old.id,"warnings":component_warnings(old.payload)}
        profile=dict(security.profile); symbol=security.symbol
    if not market.get("coverage_ok"):
        raise ValueError("行情存在缺口，暂不生成自动估值草稿")
    raw=archive_json(financials.get("raw_file"))
    submissions=archive_json(filings.get("submissions_raw_file"))
    sic=str(submissions.get("sic") or "")
    # 保留原自动模板适用范围；本次拆分数据依赖，不扩大到尚未设计的行业模型。
    if not sic.isdigit() or 6000<=int(sic)<=6999:
        raise ValueError("当前自动估值模板尚不适用于行业未知或金融/地产公司，需独立行业模型")
    ledger=Ledger(raw,profile["cik"])
    as_of=date.today().isoformat(); target=date.today().replace(year=date.today().year+1) if (date.today().month,date.today().day)!=(2,29) else date(date.today().year+1,2,28)
    bundle=ledger.bundle(as_of)
    current=bundle[1] if len(bundle)==3 else bundle[0]
    end=current["end"]
    if (date.today()-date.fromisoformat(end)).days>cfg["max_financial_age_days"]:
        raise ValueError("最新财务期末超过配置的新鲜度上限，暂不生成自动估值")
    bars=[b for b in market.get("bars",[]) if b.get("complete")]
    # 历史报价与历史股数需要一致的拆股口径；未适配时明确停止，不混用。
    if any(number(b.get("split",0),"拆股") not in (0,1) for b in bars):
        raise ValueError("历史窗口存在拆股，自动PE历史样本需先统一价格与股数口径")
    evidence=[]; base={}; components={}; models=[]; assumptions=[]; dcf_assumptions=[]
    current_eps=None; current_pe=None; growth=None; growth_error=""; sample=[]; eps_model=None; dcf_rows=[]
    price=quote_number(market["last_price"])
    if price<=0:
        raise ValueError("最新报价非正，不能生成估值草稿")
    try:
        for metric in ("net_income","shares_diluted"):
            value,rows=ledger.ttm(metric,bundle,as_of,weighted=metric=="shares_diluted")
            base[metric]=value; evidence.extend(ledger.evidence(r) for r in rows)
        if base["net_income"]<=0 or base["shares_diluted"]<=0:
            raise ValueError("TTM净利润或稀释股数非正，PE不适用")
        current_eps=base["net_income"]/base["shares_diluted"]
        current_pe=price/current_eps
        components["pe"]={"status":"draft","message":"最新已披露派生TTM对应的当前PE；不是一年目标价"}
    except ValueError as exc:
        components["pe"]={"status":"missing","message":str(exc)}
    try:
        growth=_growth(ledger,bundle,as_of,evidence)
    except ValueError as exc:
        growth_error=str(exc)
    horizon_years=Decimal((target-date.fromisoformat(end)).days)/Decimal("365.25")
    try:
        if current_eps is None:
            raise ValueError(components["pe"]["message"])
        if growth is None:
            raise ValueError(growth_error)
        multiples,sample=_pe_history(ledger,bars,cfg)
        eps_model,assumptions=_eps_model(current_eps,multiples,growth,horizon_years,end,target,cfg,profile)
        models.append(eps_model)
        components["eps"]={"status":"draft","message":"三情景EPS×PE目标草稿；增长为机械假设"}
    except ValueError as exc:
        components["eps"]={"status":"missing","message":str(exc)}
    try:
        if growth is None:
            raise ValueError(growth_error)
        dcf_models,dcf_rows,dcf_assumptions,wacc_note=_dcf_models(ledger,bundle,as_of,end,target,growth,cfg,profile,symbol,sic,evidence,base)
        models.extend(dcf_models)
        components["dcf"]={"status":"draft","message":"三情景DCF草稿，今日价值与一年目标分别计算"}
    except ValueError as exc:
        components["dcf"]={"status":"missing","message":str(exc)}
        wacc_note=None
    warnings=["全部为未确认的机械估值草稿，不是未来一年价格最高/最低的保证范围或置信区间。",
        "TTM补充基线从已归档SEC批量原始数据读取上一财年和上年同期，不额外下载财报；最近两期研究范围保持不变，补充期间另列证据。",
        "TTM EPS为净利润除按期间天数重建的加权稀释股数，属于派生口径，可能与供应商累计季度EPS之和不同。",
        "历史PE分位不是未来合理PE的证明；增长沿用营收同比且利润率/税率/股数不变，尚未正常化一次性损益。",
        "DCF经营营运资本仅用贸易应收+存货-贸易应付作代理，未覆盖其他经营资产负债；现金仅计现金等价物，未计有价证券。",
        "股份支付保留在EBIT费用中、不加回，股数固定；其他优先索取权假设为0，需核对，不能当成已证实不存在。",
        "一年后现金、债务与股数暂按基线不变，不累加已分配现金流；一年后价格不含期间分红/派息。",
        "财务基线期末与估值日有滞后，使用同一增长假设桥接至目标日；桥接经营数据属于预测，不是尚未公布的新季度实际值。"]
    if wacc_note:
        warnings.append(wacc_note)
    for item in component_warnings({"components":components}):
        warnings.append(item["message"])
    unique={e["id"]:e for e in evidence}
    summary={"status":"draft","review_status":"draft","algorithm":VERSION,"as_of":as_of,"target_date":target.isoformat(),
        "currency":profile["currency"],"latest_price":str(price),"quote_date":market["last_complete_session"],
        "components":components,"partial":any(p["status"]!="draft" for p in components.values()),
        "financial_period_end":end,"derived_ttm_eps":_text(current_eps) if current_eps is not None else None,"current_ttm_pe":_text(current_pe) if current_pe is not None else None,
        "observed_revenue_growth":_text(growth) if growth is not None else None,"pe_sample_count":len(sample),
        "eps":{"scenarios":eps_model["scenarios"],"low":min(eps_model["scenarios"],key=lambda r:Decimal(r["price"]))["price"],"high":max(eps_model["scenarios"],key=lambda r:Decimal(r["price"]))["price"]} if eps_model else None,
        "dcf":{"scenarios":dcf_rows,"current_low":str(min(Decimal(r["current_price"]) for r in dcf_rows)),"current_high":str(max(Decimal(r["current_price"]) for r in dcf_rows)),
            "target_low":str(min(Decimal(r["target_price"]) for r in dcf_rows)),"target_high":str(max(Decimal(r["target_price"]) for r in dcf_rows))} if dcf_rows else None,
        "policy_file":str(Path(settings.valuation_policy_file).resolve()),"policy":cfg,"assumptions":assumptions,"dcf_assumptions":dcf_assumptions,
        "source_versions":source_key,"baseline":{k:str(v) for k,v in base.items()},"baseline_evidence":list(unique.values()),
        "supplemental_periods":sorted({r["end"] for r in unique.values()}),"historical_pe_samples":sample,"warnings":warnings,
        "message":"两种模型分别输出，不平均成目标价；自动草稿不可作为已通过账户风控的开仓结论。"}
    with Session() as session:
        input_snapshot=save_snapshot(session,security_id,"valuation_draft",summary)
        input_snapshot.fingerprint=key
        for model in models:
            model.update({"status":"draft","review_status":"draft","origin":"automatic","warnings":list(warnings)})
            model["input_snapshot_id"]=input_snapshot.id
            save_snapshot(session,security_id,"valuation",model)
        session.commit()
    return {"models":len(models),"review_status":"draft","snapshot_id":input_snapshot.id,"warnings":component_warnings(summary)}
