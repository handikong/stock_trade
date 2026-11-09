"""盘后任务状态入口；不自行启动服务，也不借状态查询触发抓取或群消息。"""
import logging
from datetime import date, timedelta

from .cli import ChineseParser, configure_console_logging, dump
from .console_tables import table

logger=logging.getLogger(__name__)


def main(argv=None):
    parser = ChineseParser(description="查看系统内每日盘后任务；实际执行依赖单进程后台服务常驻。",add_help=False)
    parser.add_argument("-h","--help",action="help",help="查看中文说明")
    parser.add_argument("action",choices=("status","plan","tick","leaders","fetch-leaders","market-brief","holdings-brief","deepseek-check"),nargs="?",default="status",
        help="status查看计划状态；plan查看指定日的飞书清单；leaders查看榜单；fetch-leaders立即取榜；market-brief大盘报告；holdings-brief持仓复盘；deepseek-check检查鉴权；tick只入队")
    parser.add_argument("--json",action="store_true",help="输出完整状态JSON，不含凭据")
    parser.add_argument("--date", metavar="YYYY-MM-DD",
        help="榜单用美东交易日，飞书清单和复盘用北京时间日期；plan 默认明天")
    parser.add_argument("--report",choices=("daily","monday","weekly"),help="仅筛选大盘报告类型，默认展示最新一份")
    parser.add_argument("--preview",action="store_true",help="即时预览；大盘可能更新行情并调用模型，持仓只读缓存")
    parser.add_argument("--card",action="store_true",help="用新版飞书卡片展示大盘简报")
    args = parser.parse_args(argv)
    if args.report and args.action!="market-brief":
        parser.error("--report 只适用于 market-brief")
    if args.card and args.action!="market-brief":
        parser.error("--card 只适用于 market-brief")
    if args.preview and (args.action not in ("market-brief","holdings-brief") or args.report or args.date):
        parser.error("--preview 只适用于即时大盘报告或持仓复盘，请去掉 --date 和 --report")
    if args.date is not None:
        if args.action not in ("plan","leaders","fetch-leaders","market-brief","holdings-brief"):
            parser.error("--date 只适用于飞书清单、榜单、大盘报告或持仓复盘")
        try:
            if date.fromisoformat(args.date).isoformat() != args.date:
                raise ValueError
        except ValueError:
            parser.error("--date 须为有效的 YYYY-MM-DD 日期")
    configure_console_logging()
    try:
        if args.action=="deepseek-check":
            from .market_brief import check_deepseek
            result = check_deepseek()
            if args.json:
                print(dump(result))
            else:
                print(result["message"])
                if result.get("host"):
                    print(f"接口主机 {result['host']}")
                if result.get("key_source"):
                    print(f"密钥读取来源 {result['key_source']}")
            return 0 if result["status"]=="ready" else 1
        if args.action=="holdings-brief":
            from .cli_helpers import storage_ready
            from .holdings_brief import preview, render, saved
            if not storage_ready("research_jobs","manual_positions","portfolio_accounts","portfolio_position_plans","portfolio_position_protection"):
                raise ValueError("持仓存储尚未初始化，请先启动新版后台")
            value = preview() if args.preview else saved(args.date)
            if value is None:
                print("尚无符合条件的持仓复盘，请等待后台生成。")
                return 1
            print(dump(value) if args.json else render(value))
            return 0
        if args.action=="market-brief":
            from .cli_helpers import storage_ready
            from .market_brief import _card, preview, render, saved
            if args.preview:
                from .store import initialize
                initialize()
                value = preview()
            else:
                value = saved(report_type=args.report,local_date=args.date) if storage_ready("research_jobs") else None
            if value is None:
                print("尚无符合条件的大盘报告，请等待后台生成。")
                return 1
            if args.card:
                from .notification_cards import card_text
                card = _card(value)
                header = card["header"]
                print(dump(card) if args.json else "\n\n".join([
                    header["title"]["content"]+"\n"+header["subtitle"]["content"], *card_text(card)]))
            else:
                print(dump(value) if args.json else render(value))
            return 0
        if args.action=="fetch-leaders":
            from .volume_leaders import preview
            report = preview(args.date)
            if args.json:
                print(dump(report))
            else:
                labels = {"added":"将新增或恢复","retained":"已有关注","excluded":"保持移出"}
                print(f"美股成交量前50 · {report['session']}（美东交易日，预览）")
                print(table(["排名","股票","成交股数","预计操作"],
                    [[str(r["rank"]),r["symbol"],f"{r['volume']:,}",labels[r["action"]]]
                     for r in report["rows"]],numeric=(0,2)))
                print(f"预计新增或恢复 {len(report['would_add'])} 只；本次未修改关注列表。")
            return 0
        if args.action=="leaders":
            from .cli_helpers import storage_ready
            from .volume_leaders import latest_report
            source_table = "research_snapshots" if args.date else "watchlist_state"
            report = latest_report(args.date) if storage_ready(source_table) else None
            if args.json:
                print(dump(report))
            elif report is None:
                print(f"{args.date}（美东交易日）没有已保存的全市场榜单。" if args.date else
                      "尚无成功保存的美股成交量榜；请等待下一次盘后任务。")
            else:
                print(f"{'已保存' if args.date else '最近保存'}的美股成交量榜 · {report['session']}（美东交易日）")
                labels = {"added":"新增或恢复","retained":"已有关注","excluded":"保持移出"}
                print(table(["排名","股票","成交股数","首次上榜","累计上榜","关注状态"],
                    [[str(r["rank"]),r["symbol"],f"{r['volume']:,}",r["first_session"],
                      str(r["appearances"]),labels[r["action"]]] for r in report["rows"]]))
            return 1 if args.date and report is None else 0
        from . import post_close
        if args.action=="plan":
            from . import scheduled_delivery as audit
            from .store import now
            current = now().astimezone(audit.BEIJING)
            day = date.fromisoformat(args.date) if args.date else current.date()+timedelta(days=1)
            rows = audit.inspect_plan(audit.plan(day,post_close.policy()),current=current)
            if args.json:
                print(dump({"date":day.isoformat(),"timezone":"Asia/Shanghai","items":[
                    {**row,"start":row["start"].isoformat(),"end":row["end"].isoformat()} for row in rows]}))
            elif rows:
                print(f"计划飞书 · {day}（北京时间）")
                print(table(["开始","截止","任务","子项","状态","原因"], [
                    [row["start"].strftime("%H:%M"),row["end"].strftime("%H:%M"),row["name"],
                     item["item"] or "—",item["status"],item["reason"]] for row in rows for item in row["details"]]))
            else:
                print(f"{day}（北京时间）没有安排定时飞书任务。")
        elif args.action=="tick":
            from .store import initialize
            initialize()
            value=post_close.enqueue_due()
            print(dump(value) if args.json else value["message"]+"；新入队"+str(len(value["queued"]))+"个任务；需要后台Worker执行")
        else:
            value=post_close.status()
            if args.json:
                print(dump(value))
            else:
                cfg=value["policy"]
                print(f"盘后计划 · {'已启用' if cfg['enabled'] else '已停用'} · {cfg['timezone']}")
                print(f"开始日期{cfg['start_date']}；行情{cfg['market_time']}；买点/飞书{cfg['signal_time']}；补跑截止{cfg['catch_up_until']}")
                if cfg["cn_enabled"]:
                    cn = value["cn"]
                    print(f"A 股 · 北京时间交易日 {cfg['cn_market_time']} 更新日线，"
                          f"{cfg['cn_scan_time']} 扫描上个交易日，{cfg['cn_push_time']} 开始推送，"
                          f"{cfg['cn_push_until']} 停止补发")
                    if cn.get("message"):
                        print(cn["message"])
                if cfg["weekly_valuation_enabled"]:
                    print(f"估值：北京时间每周六 {cfg['weekly_valuation_time']} 更新全部关注股，当天可补跑")
                if cfg["market_brief_enabled"]:
                    print(f"大盘报告：北京时间周一至周六 {cfg['market_brief_time']}；补跑至 {cfg['market_brief_until']}")
                if cfg["holdings_brief_enabled"]:
                    push = "发飞书" if cfg["holdings_brief_feishu_enabled"] else "仅归档"
                    print(f"持仓复盘：北京时间周一至周五 {cfg['holdings_brief_time']}；补跑至 {cfg['holdings_brief_until']}；{push}")
                print(value["message"])
                labels={"queued":"排队中","running":"执行中","completed":"已结束","failed":"失败/停止"}
                stages={"scheduled_futu_heat":"富途热榜","scheduled_market":"更新日线","scheduled_signals":"买点与推送",
                        "scheduled_valuation":"每周估值","scheduled_market_brief":"大盘报告","scheduled_holdings_brief":"持仓复盘",
                        "scheduled_cn_heat":"旧 A 股人气榜","scheduled_account_equity":"收盘账户净值"}
                stages.update({kind:"A 股"+post_close.cn_post_close.LABELS[phase]
                               for phase,kind in post_close.cn_post_close.KINDS.items()})
                print(table(["交易日","阶段","状态","进度","说明"],[[j["target_session"],
                    stages.get(j["kind"],j["kind"]),
                    "部分待处理" if j["result_status"]=="partial" else labels.get(j["status"],j["status"]),
                    str(j["progress"])+"%",j["message"]] for j in value["jobs"]]))
                for job in value["jobs"]:
                    for warning in job["warnings"]:
                        print(f"提示 {job['target_session']} {warning.get('symbol','')}：{warning['message']}")
                if not value["storage_initialized"]:
                    print("存储尚未初始化，当前只读取配置；请先启动后台服务。")
                print(value["execution"])
        return 0
    except Exception as exc:
        from .providers import failure_detail
        if args.action=="fetch-leaders":
            logger.error("未取得目标交易日的完整榜单：%s",failure_detail(exc)["message"])
            return 1
        if args.action=="market-brief":
            logger.error("大盘报告未取得：%s",failure_detail(exc)["message"])
            return 1
        if args.action=="holdings-brief":
            logger.error("持仓复盘未取得：%s",failure_detail(exc)["message"])
            return 1
        logger.error("盘后计划状态未取得：%s；新环境请先由你启动后台初始化存储",failure_detail(exc)["message"])
        return 1


if __name__=="__main__":
    raise SystemExit(main())
