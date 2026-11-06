"""股票池命令：只处理行情和两套策略，中文日志走stderr，结果走stdout。"""
import argparse
import logging
import sys
from datetime import date
from pathlib import Path

from .cli import ChineseParser, configure_console_logging, decimal_text, dump

logger = logging.getLogger(__name__)


def date_arg(value):
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError
        return value
    except ValueError as exc:
        raise argparse.ArgumentTypeError("日期请使用YYYY-MM-DD格式") from exc


def parser():
    p = ChineseParser(description="MG7自选股池与双策略扫描；首次使用初始化7只股票。", add_help=False)
    p.add_argument("-h", "--help", action="help", help="查看中文使用说明")
    commands = p.add_subparsers(dest="command", required=True, title="操作")

    def command(name, help_text):
        child = commands.add_parser(name, help=help_text, description=help_text, add_help=False)
        child.add_argument("-h", "--help", action="help", help="查看中文使用说明")
        child.add_argument("--json", action="store_true", help="输出完整JSON")
        child.add_argument("--output", type=Path, help="另存输出，不覆盖已有文件")
        return child

    listing = command("list", "查看自选股池")
    listing.add_argument("--all", action="store_true", help="同时显示已移出的股票")
    for name, label in (("add", "添加股票"), ("remove", "移出股票并保留信号历史")):
        command(name, label).add_argument("symbols", nargs="+", help="一个或多个股票代码")
    scanning = command("scan", "批量扫描股票池；已有行情直接复用，缺失时获取yf日线")
    scanning.add_argument("--start", type=date_arg, help="展示及归档起点；默认各股票最后一个可用交易日")
    scanning.add_argument("--history-start", type=date_arg, help="预热起点；默认展示起点所在年份的1月1日")
    scanning.add_argument("--end", type=date_arg, help="历史回放终点；显式设置后全部作为历史参考")
    mode = scanning.add_mutually_exclusive_group()
    mode.add_argument("--offline", action="store_true", help="只用行情缓存，不联网；仍计算并保存指标和信号")
    mode.add_argument("--refresh", action="store_true", help="手动刷新所有股票日线；失败保留缓存，仍遵守限流冷却")
    scanning.add_argument("--policy", type=Path, help="富途色带指标配置文件")
    scanning.add_argument("--strategy-policy", type=Path, help="双策略配置文件")
    listing_history = command("history", "查看曾经发现的信号；不代表当前仍然有效")
    listing_history.add_argument("--symbol", help="只看指定股票")
    listing_history.add_argument("--strategy", choices=("EMA_PULLBACK", "MACD_ICON1"), help="只看指定策略")
    listing_history.add_argument("--kind", choices=("BUY", "EXIT"), help="只看买入BUY或退出EXIT；默认都显示")
    listing_history.add_argument("--start", type=date_arg, help="信号日期起点")
    listing_history.add_argument("--end", type=date_arg, help="信号日期终点")
    listing_history.add_argument("--limit", type=int, default=100, help="最多显示条数，默认100，最大10000")
    command("last", "读取已保存的扫描结果，不重新扫描").add_argument("--scan-id", help="指定扫描ID；默认最近一次")
    command("revisions", "查看一条信号的所有修订版本").add_argument("event_id", help="history --json中的event_id")
    return p


def signal_line(signal, first_discovery=False):
    if signal.get("event_kind") == "EXIT":
        return (f"{signal['as_of']}  {signal.get('symbol', ''):<6}  [退出] {signal['name']}  "
                f"对应买点 {signal['entry_signal_date']}；{signal['status_label']}；"
                f"冻结保护线 {decimal_text(signal['initial_protection_price'])}；"
                f"当日收盘参考 {decimal_text(signal['close_reference'])}（非成交价）；"
                f"{'首次发现' if first_discovery else '已记录'}")
    price = (signal.get("levels") or {}).get("entry_reference")
    return (f"{signal['as_of']}  {signal.get('symbol', ''):<6}  [买入] {signal['name']}  "
            f"参考价 {decimal_text(price)}  {'首次发现' if first_discovery else '已记录'}")


def render_scan(report):
    # 旧批次没有完整配对数据，仍显示原档案，不跨版本拼接或推断历史成交。
    if not any("flows" in row for row in report["results"]):
        return render_scan_events(report)
    from .strategy_flows import render_flows
    labels = {"OK": "行情有效", "STALE": "历史/待复核", "HISTORICAL": "历史回放", "ERROR": "扫描失败"}
    run_labels = {"complete": "完成", "partial": "部分数据需处理", "running": "进行中", "interrupted": "中断"}
    lines = [f"扫描ID：{report['scan_id']}；状态：{run_labels.get(report['status'], report['status'])}",
             f"最近已完成交易日：{report['expected_session']}；本次截止：{report['target_session']}",
             "按股票展示买入—预警—卖出过程。日期为信号日，价格为收盘参考，不是实际成交或持仓。"]
    for row in report["results"]:
        lines.extend(["", f"========== {row['symbol']} · {labels[row['status']]} · 数据截止{row['data_as_of'] or '未知'} =========="])
        if row.get("error"):
            lines.append("原因：" + row["error"]["message"])
            continue
        if row.get("market_cache"):
            lines.append("行情：" + row["market_cache"]["message"])
        for warning in row.get("warnings", []):
            lines.append("数据提示：" + warning["message"])
        if "flows" in row:
            lines.extend(render_flows(row["flows"], symbol=row["symbol"]))
        else:
            lines.append("该股票旧档案没有配对数据，请重新扫描；不能把缺失当作没有信号。")
        lines.append(f"本次首次发现{row.get('first_discovered_count', 0)}个买入/退出事件，新增修订{row.get('new_revision_count', 0)}个；首次发现可能是历史事件。")
    lines.extend(["", "未出现卖出信号的过程列为跟踪中，不代表实际持仓；重叠过程不是加仓。",
                  "指标信号收盘确认后最早下一交易时段使用；保护线日内破位由日线事后识别。未计算实际收益。"])
    return "\n".join(lines)


def render_scan_events(report):
    labels = {"OK": "行情有效", "STALE": "仅作历史参考", "HISTORICAL": "历史回放", "ERROR": "扫描失败"}
    run_labels = {"complete": "完成", "partial": "部分数据需处理", "running": "进行中", "interrupted": "中断"}
    lines = [f"扫描ID：{report['scan_id']}；状态：{run_labels.get(report['status'], report['status'])}",
             f"最近已完成交易日：{report['expected_session']}；本次截止：{report['target_session']}",
             "股票    数据状态        数据截止      策略判断"]
    for row in report["results"]:
        statuses = "；".join(f"{s['name']}：{s['status_label']}" for s in row.get("strategies", []))
        lines.append(f"{row['symbol']:<7} {labels[row['status']]}  {row['data_as_of'] or '未知'}  {statuses}")
        if row.get("error"):
            lines.append("  原因：" + row["error"]["message"])
        if row.get("market_cache"):
            lines.append("  行情：" + row["market_cache"]["message"])
        for warning in row.get("warnings", []):
            lines.append("  提示：" + warning["message"])
        for track in row.get("exit_tracking", []):
            # last仍能展示旧版档案；新版本优先使用保存的分级文案。
            state = track.get("status_label") or {"WATCH": "跌破EMA观察中", "TRACKING": "尚未触发退出", "PROTECTION_UNKNOWN": "保护线未知"}.get(track["status"], track["status"])
            lines.append(f"  技术跟踪：{track['strategy_id']} 买点{track['entry_signal_date']}，{state}；"
                         f"冻结保护线{decimal_text(track['initial_protection_price'])}")
            if track.get("warning"):
                lines.append("    " + track["warning"])
        for alert in row.get("exit_alerts", []):
            label = "最近完整日" if alert in row.get("current_exit_alerts", []) else "历史/待复核"
            lines.append(f"  {label}预警变化：{alert['as_of']} {alert['strategy_id']} 买点{alert['entry_signal_date']}：{alert['status_label']}")
    lines.extend(["", "【最近已完成交易日的信号】"])
    current_count = historical_count = 0
    historical_lines = []
    for row in report["results"]:
        def event_key(s):
            return (s.get("event_kind", "BUY"), s["strategy_id"], s["as_of"], s.get("entry_signal_date"))
        current_keys = {event_key(s) for s in row["current_signals"]}
        # 保存引用与信号逐条对应，重复扫描不会再次标记为首次发现。
        for signal, reference in zip(row["signals"], row.get("signal_records", [])):
            text = signal_line(signal, reference["first_discovery"])
            if event_key(signal) in current_keys:
                current_count += 1
                lines.append(text)
            else:
                historical_count += 1
                historical_lines.append(text)
    if not current_count:
        lines.append("本次没有可列为最新交易日的信号；失败、旧行情及历史回放不代表今日未命中。")
    lines.extend(["", f"【本次范围内历史/待复核信号：{historical_count}条】", *historical_lines])
    lines.append(f"首次发现共{sum(r.get('first_discovered_count', 0) for r in report['results'])}条；"
                 f"新增修订{sum(r.get('new_revision_count', 0) for r in report['results'])}条。")
    lines.append("买入/退出均为技术信号，按买点分别跟踪，不代表实际持仓。参考价默认调整价，不是成交价；保护线日内破位由日线事后识别，收盘指标退出最早下一交易时段使用。尚未接入仓位、成交或收益回测。")
    return "\n".join(lines)


def render(command, result):
    if command in ("scan", "last"):
        return render_scan(result)
    if command == "list":
        return "股票池：\n" + ("\n".join(f"{r['symbol']}  {'关注中' if r['active'] else '已移出'}" for r in result) or "暂无股票")
    if command in ("add", "remove"):
        return "\n".join(f"{r['symbol']}：{r['result']}" for r in result)
    if command == "history":
        lines = [f"曾经发现的信号：本次显示{len(result)}条（受筛选及条数限制），不代表当前有效。"]
        for row in result:
            lines.append(signal_line(row["signal"]))
            lines.append(f"  首次存档（UTC）：{row['first_seen_at']}；版本数：{row['revision_count']}；事件ID：{row['event_id']}")
        return "\n".join(lines)
    return dump(result)


def main(argv=None):
    args = parser().parse_args(argv)
    configure_console_logging()
    try:
        from . import watchlist
        from .store import initialize

        if args.output and args.output.exists():
            raise ValueError("输出文件已经存在，请指定新文件名")
        if args.command in ("add", "remove"):
            args.symbols = [watchlist.normalize_symbol(s) for s in args.symbols]
        initialize()
        watchlist.ensure_seed()
        if args.command == "list":
            result = watchlist.entries(args.all)
        elif args.command in ("add", "remove"):
            result = watchlist.change_symbols(args.symbols, args.command == "add")
        elif args.command == "scan":
            result = watchlist.scan(start=args.start, history_start=args.history_start, end=args.end,
                offline=args.offline, refresh=args.refresh, policy_file=args.policy,
                strategy_policy=args.strategy_policy)
        elif args.command == "history":
            result = watchlist.history(symbol=args.symbol, strategy_id=args.strategy,
                                      start=args.start, end=args.end, limit=args.limit, event_kind=args.kind)
        elif args.command == "last":
            result = watchlist.scan_record(args.scan_id)
        else:
            result = watchlist.revisions(args.event_id)
        output = dump(result) if args.json else render(args.command, result)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open("x", encoding="utf-8") as handle:
                handle.write(output + "\n")
            logger.info("结果已保存到%s", args.output)
        print(output)
        return 2 if args.command == "scan" and result["status"] != "complete" else 0
    except KeyboardInterrupt:
        logger.warning("操作已中断；已提交的股票结果保留，可用last查看")
        return 130
    except Exception as exc:
        from .providers import failure_detail
        logger.error("股票池操作失败：%s", failure_detail(exc)["message"])
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
