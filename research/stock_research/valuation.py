from datetime import date
from decimal import Decimal, InvalidOperation, localcontext
import re


def number(value, name: str) -> Decimal:
    try:
        d = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f"{name}需要有效数值")
    if not d.is_finite():
        raise ValueError(f"{name}不能是空值或无穷数")
    if abs(d)>Decimal("1e18") or (d!=0 and d.as_tuple().exponent < -12):
        raise ValueError(f"{name}数量级或精度超出范围，请统一为元、百万等合适单位")
    return d


def calculate(payload: dict, security: dict) -> dict:
    context=payload.get("security_context")
    if context is not None:
        if not isinstance(context,dict) or context.get("confirmed") is not True or not context.get("source_note"):
            raise ValueError("security_context需要confirmed=true及source_note，不能自动猜测币种或模型适用性")
        security=dict(security)
        for key in ("type","currency","sector"):
            value=context.get(key)
            if not value:
                continue
            known=security.get(key)
            if known and known!="UNKNOWN" and known!=value:
                raise ValueError(f"估值声明的{key}与已有行情口径冲突，请先核对")
            security[key]=value
    if security.get("currency") and not re.fullmatch(r"[A-Z]{3}",security["currency"]):
        raise ValueError("估值币种需要明确的三位币种代码")
    if security.get("type")!="EQUITY" or not security.get("currency"):
        raise ValueError("缺少公司估值适用性或币种；使用行情附带字段或在security_context明确确认，不另行查询身份")
    if security.get("type") in ("ETF", "MUTUALFUND"):
        raise ValueError("ETF不适用公司EPS或DCF估值，请使用基金研究资料")
    if payload.get("currency") != security.get("currency"):
        raise ValueError("本版估值币种必须与证券报价币种一致，不自动混用汇率与折现假设")
    if not payload.get("source_note", "").strip():
        raise ValueError("请填写假设依据，不能把手工情景当成实际财报")
    kind = payload.get("method")
    if kind == "eps":
        return eps(payload)
    if kind == "dcf":
        if "financial" in str(security.get("sector", "")).lower():
            raise ValueError("金融行业不适用本版经营公司FCFF模型")
        return dcf(payload)
    raise ValueError("请选择EPS倍数或DCF方法")


def eps(p: dict) -> dict:
    convention = p.get("convention")
    if convention not in ("current_ttm", "target_ttm"):
        raise ValueError("请选择当前TTM估值或目标时点TTM情景")
    target = date.fromisoformat(p["target_date"]) if convention=="target_ttm" else date.today()
    if convention == "target_ttm" and target <= date.today():
        raise ValueError("目标时点需要晚于今天")
    if not p.get("eps_basis") or not p.get("period_note"):
        raise ValueError("请说明EPS口径和对应期间，不能混用GAAP、non-GAAP或FY/TTM")
    rows = []
    for item in p.get("scenarios", []):
        e, pe = number(item.get("eps"), "EPS"), number(item.get("pe"), "PE")
        if e < Decimal("0.000001") or pe <= 0:
            raise ValueError("EPS非正/过于接近零或PE非正时，本版倍数估值不适用")
        rows.append({"name": str(item.get("name", "情景")), "eps": str(e), "pe": str(pe), "price": str(e*pe)})
    if len(rows) != 3:
        raise ValueError("请分别填写保守、基准、乐观三个情景")
    return {"method": "eps", "target_date": str(target), "convention": convention,
        "scenarios": rows, "currency": p["currency"], "assumptions": p,
        "status": "scenario", "message": "这是有条件的估值情景，不是未来一年实际最高/最低价格预测"}


def dcf(p: dict) -> dict:
    if not p.get("nonfinancial_confirmed"):
        raise ValueError("请确认公司适用非金融经营公司的FCFF框架")
    w, g = number(p.get("wacc"), "WACC"), number(p.get("terminal_growth"), "长期增长率")
    if not (Decimal(0) < w < Decimal(1)) or g <= Decimal(-1) or w <= g:
        raise ValueError("WACC应在0与1之间，并大于长期增长率")
    shares = number(p.get("shares"), "股数")
    cash, debt, claims = [number(p.get(k), label) for k,label in
        [("cash","非经营现金"),("debt","债务"),("other_claims","其他优先索取权")]]
    if shares <= 0 or min(cash,debt,claims) < 0:
        raise ValueError("股数必须为正，现金、债务与其他索取权不能为负")
    years = p.get("years", [])
    if not 2 <= len(years) <= 15:
        raise ValueError("请填写2至15年的完整预测路径")
    flows = []
    for row in years:
        ebit, tax, da, capex, working = [number(row.get(k), label) for k,label in
            [("ebit","EBIT"),("tax_rate","税率"),("da","折旧摊销"),("capex","资本开支"),("delta_wc","营运资本增加")]]
        if not 0 <= tax <= 1 or min(da,capex) < 0:
            raise ValueError("税率需要在0与1之间，折旧摊销和资本开支不能为负")
        flows.append(ebit*(1-tax)+da-capex-working)
    if flows[-1] <= 0:
        raise ValueError("稳定期现金流非正，不适用本版永续增长终值")
    if not p.get("terminal_policy_note"):
        raise ValueError("请说明稳定期增长、再投资及股票薪酬/稀释处理假设")
    with localcontext() as ctx:
        ctx.prec = 32
        terminal = flows[-1]*(1+g)/(w-g)
        operating = sum(f/(1+w)**(i+1) for i,f in enumerate(flows)) + terminal/(1+w)**len(flows)
        equity = operating+cash-debt-claims
        if equity <= 0:
            raise ValueError("当前假设下普通股权益非正，不能给出有效正价格")
        sensitivity=[]
        for dw in (Decimal("-0.01"),Decimal(0),Decimal("0.01")):
            for dg in (Decimal("-0.005"),Decimal(0),Decimal("0.005")):
                sw,sg=w+dw,g+dg
                if sw <= sg or sw <= 0:
                    continue
                tv=flows[-1]*(1+sg)/(sw-sg)
                ev=sum(f/(1+sw)**(i+1) for i,f in enumerate(flows))+tv/(1+sw)**len(flows)
                value=(ev+cash-debt-claims)/shares
                sensitivity.append({"wacc":str(sw),"growth":str(sg),"price":str(value) if value>0 else None,
                    "status":"scenario" if value>0 else "普通股权益非正"})
        target = None
        if p.get("year_one_state"):
            state=p["year_one_state"]
            sc,sd,so,ss=[number(state.get(k),k) for k in ("cash","debt","other_claims","shares")]
            if min(sc,sd,so)<0 or ss<=0:
                raise ValueError("一年后资产负债与股数假设无效")
            ev1=sum(f/(1+w)**i for i,f in enumerate(flows[1:],1))+terminal/(1+w)**(len(flows)-1)
            if ev1+sc-sd-so<=0:
                raise ValueError("一年后假设下普通股权益非正，不生成有效正价格")
            target={"price":str((ev1+sc-sd-so)/ss),"label":"12个月目标时点情景","state":state}
        return {"method":"dcf","currency":p["currency"],"status":"scenario",
            "price":str(equity/shares),"operating_value":str(operating),"fcff":[str(f) for f in flows],
            "terminal_share":str((terminal/(1+w)**len(flows))/operating),
            "target":target,"sensitivity":sensitivity,"assumptions":p,
            "message":"今日内在价值与一年后情景分别计算；没有一年后资产负债/股数假设时不生成目标时点价格"}
