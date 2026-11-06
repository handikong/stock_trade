"""独立手工录入入口：默认只预览，显式保存草稿或确认，不获取行情。"""
import logging
from pathlib import Path

from .cli import ChineseParser, configure_console_logging, dump
from .cli_helpers import emit, normalize_symbol, output_path, parse_input, storage_ready
from .console_tables import table

logger = logging.getLogger("stock_research.input_cli")


def parser():
    p = ChineseParser(description="录入GPT返回的手工研究JSON：预览→保存草稿/本人确认→查询与版本历史。", add_help=False)
    p.add_argument("-h", "--help", action="help", help="查看中文说明")
    p.add_argument("symbol", help="例如GOOG；必须与JSON中的大写股票代码一致")
    p.add_argument("--file", type=Path, help="读取JSON文件；-表示管道/粘贴，不填则直接粘贴")
    action = p.add_mutually_exclusive_group()
    action.add_argument("--save", action="store_true", help="保存未确认草稿，不取代当前确认版本")
    action.add_argument("--confirm", action="store_true", help="明确表示本人已核对本次内容，保存为确认版本")
    action.add_argument("--show", action="store_true", help="读取当前确认版本及最新草稿，不读取输入")
    action.add_argument("--history", action="store_true", help="查看手工输入版本历史，不读取输入")
    action.add_argument("--template", action="store_true", help="输出本股票空白JSON模板，需自行填写真实数据")
    p.add_argument("--expected-hash", help="确认/保存时校验之前预览的input_hash，防止文件内容变化")
    p.add_argument("--limit", type=int, default=20, help="history最多条数，默认20，范围1～1000")
    p.add_argument("--json", action="store_true", help="输出完整结构化结果；template始终输出JSON")
    p.add_argument("--details", action="store_true", help="附上标准化输入、来源和完整计算依据")
    p.add_argument("--output", type=Path, help="另存输出，拒绝覆盖；也可用于保存空白模板")
    return p


def existing_security(symbol):
    if not storage_ready("securities"):
        return None
    from sqlalchemy import select
    from .store import Security, Session
    with Session() as session:
        return session.scalar(select(Security).where(Security.symbol == symbol))


def render(result, *, details=False):
    from .research_tables import number, wrapped
    if result["mode"] == "HISTORY":
        return table(["版本ID", "状态", "资料日", "目标日", "保存时间UTC"],
            [[r["id"], "本人确认" if r["status"]=="confirmed" else "未确认草稿", r.get("as_of") or "—",
              r.get("target_date") or "—", r["created_at"]] for r in result["items"]]) + "\n"
    view = result["result"]
    lines = [f"{result['symbol']} · 手工研究录入 · {result['message']}"]
    if view.get("id"):
        lines.append("版本ID：" + view["id"])
    if view.get("input_hash"):
        lines.append("输入哈希：" + view["input_hash"])
    if view.get("status") in ("missing", "needs_review"):
        lines.append(view["message"])
    schema = (view.get("input") or {}).get("schema_version")
    if schema in ("manual-valuation-v2", "manual-eps-v3", "manual-eps-pe-v4"):
        if view.get("status") != "needs_review":
            if schema == "manual-eps-pe-v4":
                from .manual_multiples import render_lines
            elif schema == "manual-eps-v3":
                from .eps_only import render_lines
            else:
                from .compact_valuation import render_lines
            lines.extend(render_lines(view))
        if view.get("draft"):
            lines.append("已有未确认草稿，未覆盖当前确认版本；--json/--details可查看。")
        lines.append("预览不保存，--confirm表示本人已核对；不请求行情、模型或通知。")
        if details:
            lines.extend(["【标准化数据与完整依据】",dump(view)])
        return "\n".join(lines) + "\n"
    names = {"current_ttm":"实际TTM EPS", "pe":"观察PE", "eps":"EPS×PE", "dcf":"DCF"}
    rows = [[names.get(key,key), {"available":"资料可用", "missing":"待补", "not_applicable":"不适用"}.get(part["status"],part["status"]),
             str(len(part.get("missing_fields") or []))] for key,part in view.get("components",{}).items()]
    if rows:
        lines.append(table(["项目", "状态", "缺项数"], rows))
    data = view.get("input") or {}
    current = data.get("current_ttm") or {}; observed = data.get("observed_pe") or {}
    if current.get("eps") is not None:
        lines.append(wrapped(f"实际TTM EPS {current['eps']}；{current.get('period_start')}～{current.get('period_end')}；{current.get('eps_basis')}"))
    if observed.get("value") is not None:
        lines.append(wrapped(f"观察PE {observed['value']}；资料日{observed.get('as_of')}；{observed.get('basis')}"))
    if view.get("scenarios"):
        target = data.get("target") or {}
        lines.append(wrapped(f"目标日{target.get('date')}；EPS期间{target.get('eps_period_start')}～{target.get('eps_period_end')}；{target.get('eps_period_type')}；{target.get('eps_basis')}"))
        lines.append(table(["情景","手工EPS","目标PE","程序计算目标价格"],
            [[r["name"],number(r["eps"],4),number(r["pe"]),number(r["price"])] for r in view["scenarios"]],numeric=(1,2,3)))
    dcf = view.get("dcf_result")
    if dcf:
        lines.append(f"DCF资料日{dcf['as_of']}每股价值：{number(dcf['price'])}")
        if dcf.get("target"):
            lines.append(f"DCF目标日{dcf['target_date']}每股价值：{number(dcf['target']['price'])}")
    gaps = [*(view.get("base_missing_fields") or []), *((view.get("alignment_status") or {}).get("missing_fields") or [])]
    for key,part in view.get("components",{}).items():
        gaps.extend(part.get("missing_fields") or [])
    lines.extend(wrapped("待补："+gap) for gap in dict.fromkeys(gaps))
    for source in data.get("sources") or []:
        lines.append(wrapped(f"来源[{source['id']}] {source['title']}；{source['url']}"))
    note = (data.get("target") or {}).get("normalization_note")
    if note:
        lines.append(wrapped("盈利质量/正常化说明："+note))
    lines.extend(wrapped("输入风险说明："+risk) for risk in data.get("risks") or [])
    if view.get("draft"):
        lines.append("已有未确认草稿，未覆盖当前确认版本；--json/--details可查看。")
    lines.append("本入口不获取行情、不调用模型、不发送通知；预览不保存，草稿不代表本人确认。")
    if details:
        lines.extend(["【标准化数据与完整依据】",dump(view)])
    return "\n".join(lines) + "\n"


def main(argv=None):
    p = parser(); args = p.parse_args(argv); configure_console_logging()
    if args.json and args.details:
        p.error("--json已含完整数据，不与--details同时使用")
    if args.file and any((args.show,args.history,args.template)):
        p.error("--show/--history/--template不读取输入，不能与--file混用")
    if args.expected_hash and not (args.save or args.confirm):
        p.error("--expected-hash仅与--save或--confirm一起使用")
    if not 1 <= args.limit <= 1000:
        p.error("--limit范围为1～1000")
    try:
        symbol = normalize_symbol(args.symbol); path = output_path(args.output)
        security = existing_security(symbol)
        if args.template:
            template = {"eps_conservative":None,"eps_base":None,"eps_optimistic":None,
                        "pe_low":None,"pe_median":None,"pe_high":None,"industry_pe":None}
            logger.info("三档EPS＋三档历史PE，industry_pe可选；股票由命令指定，不获取PE")
            emit(dump(template)+"\n", path)
            return 0
        from . import manual_valuation
        from .store import Session, Snapshot, serialize_snapshot
        if args.show or args.history:
            if security is None or not storage_ready("research_snapshots"):
                raise LookupError("没有本股票手工档案，请先粘贴JSON预览，再用--save或--confirm保存")
            with Session() as session:
                if args.history:
                    from sqlalchemy import select
                    items = session.scalars(select(Snapshot).where(Snapshot.security_id==security.id, Snapshot.kind=="manual_valuation")
                        .order_by(Snapshot.created_at.desc(),Snapshot.id.desc()).limit(args.limit))
                    rows = [{"id":s.id,"status":s.payload["status"],"created_at":serialize_snapshot(s)["created_at"],
                             "as_of":s.payload["input"].get("as_of"),"target_date":s.payload["input"].get("target",{}).get("date")} for s in items]
                    result = {"symbol":symbol,"mode":"HISTORY","items":rows}
                else:
                    view = manual_valuation.read(session,security)
                    result = {"symbol":symbol,"mode":"SHOW","message":"当前确认状态","result":view}
        else:
            data = parse_input(args.file)
            logger.info("正在校验手工数据并计算预览，不请求行情、财报或模型")
            preview = manual_valuation.evaluate(data, symbol, security.profile if security else {})
            if security is not None:
                from .eps_only import attach
                with Session() as session:
                    preview = attach(preview, session, security)
            if args.expected_hash and args.expected_hash != preview["input_hash"]:
                raise ValueError("输入与指定预览哈希不一致，请重新核对")
            mode = "PREVIEW"; view = preview; message = "仅预览，尚未保存"
            if args.save or args.confirm:
                from .store import initialize
                from .service import security_for
                initialize()
                security = security_for(symbol,create=True)
                view = manual_valuation.save(security.id, data, confirm=args.confirm, expected_hash=preview["input_hash"])
                mode = "CONFIRM" if args.confirm else "DRAFT"
                message = "已保存本人确认版本" if args.confirm else "已保存未确认草稿"
                logger.info("%s；版本%s",message,view["id"])
            result = {"symbol":symbol,"mode":mode,"message":message,"result":view}
        emit(dump(result)+"\n" if args.json else render(result,details=args.details),path)
        if result["mode"]=="HISTORY":
            return 0
        view = result["result"]
        return 2 if view.get("status") in ("missing","needs_review") or view.get("missing_fields") else 0
    except BrokenPipeError:
        return 0
    except (ValueError, LookupError, OSError) as exc:
        logger.error("录入未完成：%s",str(exc) if isinstance(exc,(ValueError,LookupError)) else "输入/输出文件不可用，请核对路径与权限")
        return 1
    except (KeyboardInterrupt, EOFError):
        logger.warning("已取消录入")
        return 130
    except Exception as exc:
        logger.error("录入未完成（%s），请核对本地配置与数据库，未请求行情或模型",type(exc).__name__)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
