"""盘后任务状态入口；不自行启动服务，也不借状态查询触发抓取或群消息。"""
import logging
from datetime import date

from .cli import ChineseParser, configure_console_logging, dump
from .console_tables import table

logger=logging.getLogger(__name__)


def main(argv=None):
    parser = ChineseParser(description="查看系统内每日盘后任务；实际执行依赖单进程后台服务常驻。",add_help=False)
    parser.add_argument("-h","--help",action="help",help="查看中文说明")
    parser.add_argument("action",choices=("status","tick","leaders","fetch-leaders"),nargs="?",default="status",
        help="status查看计划；leaders只读已存榜单；fetch-leaders立即取榜预览；tick只入队")
    parser.add_argument("--json",action="store_true",help="输出完整状态JSON，不含凭据")
    parser.add_argument("--date", metavar="YYYY-MM-DD",
        help="美东交易日；leaders查档案，fetch-leaders仅核对最近完整交易日")
    args = parser.parse_args(argv)
    if args.date is not None:
        if args.action not in ("leaders","fetch-leaders"):
            parser.error("--date 只适用于 leaders 或 fetch-leaders")
        try:
            if date.fromisoformat(args.date).isoformat() != args.date:
                raise ValueError
        except ValueError:
            parser.error("--date 须为有效的 YYYY-MM-DD 美东交易日")
    configure_console_logging()
    try:
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
        if args.action=="tick":
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
                if cfg["weekly_valuation_enabled"]:
                    print(f"估值：北京时间每周六 {cfg['weekly_valuation_time']} 更新全部关注股，当天可补跑")
                print(value["message"])
                labels={"queued":"排队中","running":"执行中","completed":"已结束","failed":"失败/停止"}
                stages={"scheduled_futu_heat":"富途热榜","scheduled_market":"更新日线","scheduled_signals":"买点与推送",
                        "scheduled_valuation":"每周估值"}
                print(table(["交易日","阶段","状态","进度","说明"],[[j["target_session"],
                    stages[j["kind"]],
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
        logger.error("盘后计划状态未取得：%s；新环境请先由你启动后台初始化存储",failure_detail(exc)["message"])
        return 1


if __name__=="__main__":
    raise SystemExit(main())
