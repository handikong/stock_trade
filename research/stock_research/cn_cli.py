"""A 股人气榜和估值资料核对入口。"""
from . import cn_sources
from .cli import ChineseParser, configure_console_logging, dump
from .cli_helpers import storage_ready
from .providers import failure_detail
from .store import Session


def main(argv=None):
    parser = ChineseParser(description="查看或同步 A 股人气榜，核对公开 EPS 与 PE")
    parser.add_argument("action", choices=("preview","sync","pool","research"), help="操作")
    parser.add_argument("symbol", nargs="?", help="research 使用，例如 600519.SH")
    parser.add_argument("--json", action="store_true", help="输出结构化资料")
    args = parser.parse_args(argv)
    if (args.action == "research") != bool(args.symbol):
        parser.error("仅 research 需要一个沪深股票代码")
    configure_console_logging()
    try:
        if args.action == "research":
            result = cn_sources.research(args.symbol)
        elif args.action == "preview":
            result = cn_sources.fetch_hot_list()
        else:
            if not storage_ready("watchlist_state"):
                raise ValueError("请先完成系统部署，再使用 A 股关注池")
            if args.action == "sync":
                result = cn_sources.sync_hot_list()
            else:
                with Session() as session:
                    result = cn_sources.pool(session)
    except Exception as exc:
        parser.exit(1,"A 股资料处理失败 · " + failure_detail(exc)["message"] + "\n")
    if args.json or args.action == "research":
        print(dump(result))
    elif args.action == "pool":
        members = result["members"]
        print(f"A 股关注 {len(members)} 只 · 最近同步 {result['last_date'] or '暂无'}")
        for row in sorted(members.values(),key=lambda row:row["symbol"]):
            print(f"{row['symbol']}  {row['name']}  最近上榜 {row['last_seen']}")
    else:
        print(f"东方财富候选 {result['candidate_count']} 条 · 合格沪深 A 股 {len(result['stocks'])} 只")
        if "added" in result:
            print(f"新增 {len(result['added'])} 只 · 当前关注 {result['active_count']} 只")
        for row in result["stocks"]:
            print(f"{row['rank']:>3}  {row['symbol']}  {row['name']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
