"""立即查看或同步富途美股热榜；不启动策略、持仓或行情更新。"""
from . import futu_heat
from .cli import ChineseParser, configure_console_logging, dump
from .cli_helpers import storage_ready
from .providers import failure_detail
from .store import initialize


def main(argv=None):
    parser = ChineseParser(description="富途美股热榜（取前100条，只保留股票）")
    parser.add_argument("action",choices=("preview","sync","status"),nargs="?",default="preview")
    parser.add_argument("--send",action="store_true",help="同步成功后发一条新增名单飞书消息")
    parser.add_argument("--json",action="store_true",help="输出榜单详情")
    args = parser.parse_args(argv)
    if args.send and args.action != "sync":
        parser.error("--send 只能用于 sync")
    configure_console_logging()
    try:
        if args.action == "preview":
            report = futu_heat.fetch()
        elif args.action == "status":
            report = futu_heat.saved() if storage_ready("watchlist_state") else None
        else:
            initialize()
            day = futu_heat.local_day()
            report = futu_heat.sync(day,send=args.send,allowed=lambda:futu_heat.local_day()==day)
    except Exception as exc:
        parser.exit(1,"富途热榜处理失败：" + failure_detail(exc)["message"] + "\n")
    if args.json:
        print(dump(report))
    elif report is None:
        print("尚无富途热榜入池记录。")
    else:
        print(f"候选 {report['candidate_count']} 条，股票 {len(report['stocks'])} 只，排除 {len(report['excluded'])} 条")
        print("股票：" + "、".join(row["symbol"] for row in report["stocks"]))
        if "added" in report:
            print(f"{report['date']} {'首次重建' if report['initial'] else '日常同步'}："
                  f"较同步前新增 {len(report['added'])} 只，移出 {len(report['removed'])} 只，"
                  f"当前关注 {report['active_count']} 只")
            print("新增：" + ("、".join(row["symbol"] for row in report["added"]) or "无"))
            print("飞书：" + report["delivery"]["status"] + " " + report["delivery"].get("message",""))
    return 0 if not args.send or report["delivery"]["status"] in ("SENT","NO_ADDITIONS") else 2


if __name__ == "__main__":
    raise SystemExit(main())
