"""扩充真实扫描池的业务命令；仅筛流动性，不触发策略扫描或飞书投递。"""
import sys
from pathlib import Path

from .cli import ChineseParser, configure_console_logging, dump


def main(argv=None):
    parser = ChineseParser(description="纳指100＋标普500候选去重、近期成交金额筛选")
    parser.add_argument("--apply", action="store_true", help="将合格股票同步到关注列表；默认只预览并保存报告")
    parser.add_argument("--offline", action="store_true", help="只读取已有行情/流动性缓存，缺项不联网")
    parser.add_argument("--refresh-members", action="store_true", help="先更新官方公开名单及SPY代理；失败保留原名单并停止本次同步")
    parser.add_argument("--policy", type=Path, help="指定流动性配置")
    parser.add_argument("--json", action="store_true", help="输出完整JSON")
    args = parser.parse_args(argv)
    configure_console_logging()
    try:
        from .store import initialize
        from . import universe, watchlist
        if args.offline and args.refresh_members:
            raise ValueError("--offline不能与--refresh-members同时使用")
        if args.refresh_members:
            from .universe_sources import refresh
            refresh(universe.policy(args.policy))
        initialize()
        if args.apply:
            watchlist.ensure_seed()
        report = universe.sync(apply=args.apply, offline=args.offline, policy_file=args.policy)
        if args.json:
            print(dump(report))
        else:
            counts = report["counts"]
            print(f"指数候选去重{report['candidate_count']}只；行情截止{report['target_session']}")
            print(f"通过{counts['ELIGIBLE']}只；流动性/价格排除{counts['EXCLUDED']}只；缺数据{counts['UNKNOWN']}只")
            if report["applied"]:
                changes = report["changes"]
                print(f"已加入/恢复{len(changes['added'])}只；移出{len(changes['removed'])}只；当前关注{changes['active_count']}只")
                print(f"保留人工增删决定{len(changes['manual_preserved'])}只；历史和持仓未改动")
                if changes.get("unknown_preserved"):
                    print(f"另有{len(changes['unknown_preserved'])}只原关注暂缺完整数据，保留原状态，未认定通过筛选")
            else:
                print(report.get("apply_error") or "仅预览，未修改关注列表；加--apply写入")
            excluded = [r["symbol"] for r in report["results"] if r["status"] == "EXCLUDED"]
            if excluded:
                print("门槛排除：" + "、".join(excluded))
            print("完整来源、成交金额及逐股原因：" + report["report_file"])
        return 2 if report.get("apply_error") or report["counts"]["UNKNOWN"] else 0
    except KeyboardInterrupt:
        print("已中断；已取得的流动性缓存保留，未完成的批次不应用名单", file=sys.stderr)
        return 130
    except Exception as exc:
        from .providers import failure_detail
        print("指数股票池同步失败：" + failure_detail(exc)["message"], file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
