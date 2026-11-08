"""A 股收息关注池和估值资料核对入口。"""
from . import cn_income, cn_sources
from .cli import ChineseParser, configure_console_logging, dump
from .cli_helpers import storage_ready
from .providers import failure_detail
from .store import Session


def main(argv=None):
    parser = ChineseParser(description="更新 A 股高息股候选、查看收息关注池或核对公开 EPS 与 PE")
    parser.add_argument("action", choices=("refresh-dividends","pool","research","preview","sync"), help="操作")
    parser.add_argument("symbol", nargs="?", help="research 使用，例如 600519.SH")
    parser.add_argument("--json", action="store_true", help="输出结构化资料")
    args = parser.parse_args(argv)
    if (args.action == "research") != bool(args.symbol):
        parser.error("仅 research 需要一个沪深股票代码")
    configure_console_logging()
    try:
        if args.action == "research":
            result = cn_sources.research(args.symbol)
        elif args.action in ("preview","sync"):
            raise ValueError("东方财富人气榜已停用，请用 refresh-dividends 更新中证红利候选")
        else:
            if not storage_ready("watchlist_state"):
                raise ValueError("请先完成系统部署，再使用 A 股关注池")
            if args.action == "refresh-dividends":
                result = cn_income.refresh_dividends()
            else:
                with Session() as session:
                    result = cn_income.read(session)
    except Exception as exc:
        parser.exit(1,"A 股资料处理失败 · " + failure_detail(exc)["message"] + "\n")
    if args.json or args.action == "research":
        print(dump(result))
    elif args.action == "pool":
        members = result["members"]
        source = result["dividend_source"]
        print(f"A 股收息关注 {len(members)} 只 · 中证红利成分表 {source['as_of'] if source else '待导入'}")
        for row in members:
            group = "大盘宽基 ETF" if row["kind"] == "broad_etf" else "高息股候选"
            print(f"{group}  {row['symbol']}  {row['name']}  {row.get('benchmark') or row['basis']}")
    else:
        print(result["message"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
