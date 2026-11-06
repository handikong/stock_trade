"""用户直接运行的单股研究命令，不启动网页或后台任务线程。"""
import argparse
import json
import logging
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

logger = logging.getLogger(__name__)


def configure_console_logging():
    # 只配置本项目日志，避免第三方HTTP调试日志带出请求内容或密钥。
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(
        "[%(asctime)s] [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S%z"))
    package_logger = logging.getLogger("stock_research")
    package_logger.handlers.clear()
    package_logger.addHandler(handler)
    package_logger.setLevel(logging.INFO)
    package_logger.propagate = False


class ChineseParser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(2, f"参数错误：{message}\n请使用 --help 查看用法。\n")


def parser() -> argparse.ArgumentParser:
    p = ChineseParser(description="输入美股代码，获取并输出个股研究数据；无需启动网页服务。", add_help=False)
    p._positionals.title = "股票代码"
    p._optionals.title = "可选参数"
    p.add_argument("-h", "--help", action="help", help="查看中文使用说明并退出")
    p.add_argument("symbol", nargs="?", help="例如 AAPL、MSFT、SPY；不填写时交互输入")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--cached", action="store_true", help="仅读取已保存数据，不联网、不调用模型")
    mode.add_argument("--force", action="store_true", help="手动更新行情和技术计算；不恢复财报、电话会议或其他自动研究来源")
    # 仅识别旧参数并在处理来源前解释停用原因，不再展示为可用功能。
    p.add_argument("--analyze", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--document-id", action="append", help=argparse.SUPPRESS)
    p.add_argument("--json", action="store_true", help="输出完整结构化JSON，便于核对或重定向保存")
    p.add_argument("--details", action="store_true", help="展开手工输入/来源/计算结果与技术依据；不读取旧财报和电话会议")
    p.add_argument("--output", type=Path, metavar="文件路径", help="另存本次输出，不覆盖已有文件")
    p.add_argument("--facts-limit", type=int, default=40, metavar="数量", help="兼容旧参数；当前报告仅展示手工财务输入")
    p.add_argument("--materials",type=Path,help=argparse.SUPPRESS)
    p.add_argument("--event",type=Path,help=argparse.SUPPRESS)
    p.add_argument("--freeze-preview",action="store_true",help=argparse.SUPPRESS)
    p.add_argument("--valuation",type=Path,metavar="JSON文件",help="导入手工EPS＋PE JSON或旧v1/v2/v3估值；默认草稿")
    p.add_argument("--confirm-valuation",action="store_true",help="与--valuation同时使用，明确确认本人已核对的手工资料")
    p.add_argument("--guidance",type=Path,help=argparse.SUPPRESS)
    return p


def dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)


def show(value) -> str:
    return "未知/缺失" if value is None or value == "" else str(value)


def decimal_text(value, factor=None) -> str:
    if value is None:
        return "未知/缺失"
    try:
        number = Decimal(str(value))
        if factor is not None:
            number /= Decimal(str(factor))
        return f"{number:,.4f}" if number.is_finite() else "未知/缺失"
    except (InvalidOperation, ValueError, ZeroDivisionError):
        return "未知/缺失"


def render_text(report: dict, facts_limit: int, details=False, display_timezone="Asia/Shanghai") -> str:
    from .research_tables import render_summary
    summary = render_summary(report, display_timezone)
    return summary + ("\n【完整明细与原始证据】\n" + render_details(report, facts_limit) if details else "")


def render_details(report: dict, facts_limit: int) -> str:
    lines = []

    def section(title):
        lines.extend(["", f"【{title}】"])

    def detail(title, data, missing):
        section(title)
        lines.append(dump(data) if data else missing)

    profile = report["profile"]
    market = report.get("market") or {}
    cache = report["cache"]
    currency = show(profile.get("currency"))
    lines.append(f"{report['symbol']} · {show(profile.get('name'))}")
    lines.append(f"类型：{show(profile.get('type'))}  交易所：{show(profile.get('exchange'))}  报价币种：{currency}")
    lines.append(f"报告生成时间（UTC）：{report['generated_at']}")
    lines.append(f"最近完整日线收盘：{show(market.get('last_price'))} {currency}  交易日：{show(market.get('last_complete_session'))}")
    lines.append(f"行情来源：{show(market.get('source'))}  获取时间：{show(market.get('fetched_at'))}")
    lines.append(f"缓存：{show(cache.get('state'))} · {show(cache.get('message'))}")
    lines.append(f"行情区间：{show(market.get('actual_start'))} 至 {show(market.get('last_complete_session'))}；应有最近收盘日：{show(cache.get('expected_session'))}")
    if market.get("missing_sessions"):
        lines.append("缺失交易日：" + ", ".join(market["missing_sessions"]))

    section("技术面：支撑、压力与均线")
    technical = market.get("technical") or {}
    if technical.get("stale"):
        lines.append("注意：技术算法或参数已变化，以下为旧结构；普通查询会基于有效行情缓存重算。")
    factor = technical.get("quote_conversion_factor")
    lines.append(f"计算状态：{show(technical.get('status'))}；截至：{show(technical.get('as_of'))}")
    lines.append(f"算法：{show(technical.get('algorithm'))}；参数：{dump(technical.get('parameters', {}))}")
    lines.append(f"技术价格口径：{show(technical.get('price_basis'))}；调整价/报价换算系数：{show(factor)}")
    if factor:
        lines.append(f"ATR（换算至最近收盘报价尺度）：{decimal_text(technical.get('atr'), factor)} {currency}")
    for window, value in technical.get("averages", {}).items():
        basis = "最近收盘报价尺度" if factor else "技术调整价"
        lines.append(f"MA{window}：{decimal_text(value, factor)} {currency}（{basis}）")
    kinds = {"support":"支撑", "resistance":"压力", "inside":"当前价格所在区间"}
    zones = technical.get("zones", [])
    if not zones:
        lines.append("尚无满足确认条件的技术区间；不补造支撑/压力。")
    for zone in zones:
        quoted = zone.get("quote_low") is not None and zone.get("quote_high") is not None
        low, high = (zone["quote_low"], zone["quote_high"]) if quoted else (zone["low"], zone["high"])
        lines.append(f"- {zone.get('strength_label') or kinds.get(zone['kind'], zone['kind'])}：{decimal_text(low)} ～ {decimal_text(high)} {currency}"
            f"（{'最近收盘报价尺度' if quoted else '技术调整价'}）；距收盘{decimal_text(zone.get('distance_pct'))}%"
            f"；独立触及{zone.get('touches')}次；结构评分{zone.get('score')}（非成功概率）")
        lines.append(f"  均线重合：{show(zone.get('moving_average_confluence'))}；枢轴量比：{decimal_text(zone.get('pivot_volume_ratio'))}；最近触及：{show(zone.get('last_touch'))}")
        for reason in zone.get("strength_reasons",[]):
            lines.append("  强弱依据："+reason)
        for reason in zone.get("weakness_reasons",[]):
            lines.append("  限制因素："+reason)
        for point in zone.get("sources", []):
            lines.append(f"  依据：{point.get('kind')} {point.get('date')}，于{point.get('confirmed_at')}确认，技术调整价{decimal_text(point.get('price'))}")
        lines.append(f"  失效复查：{show(zone.get('invalidation'))}")
    lines.append(show(technical.get("message")))

    detail("手工数据、来源、估值计算与缺项", report.get("manual_valuation"), "请录入并确认手工资料。")
    detail("技术与基本面结合完整依据", report.get("fundamental_technical"), "尚无综合判断资料。")
    detail("当前研究模式", report.get("research_scope"), "手工财务与程序技术计算。")
    detail("数据状态", report.get("data_status"), "尚无状态。")
    section("本次处理与缺失说明")
    lines.append(dump(report["command"]))
    lines.append("自动财报/电话会议/外部研究链路已停用，旧档案不回填当前结果。")
    return "\n".join(lines) + "\n"


def main() -> int:
    p = parser()
    args = p.parse_args()
    configure_console_logging()
    if args.facts_limit < 0:
        p.error("--facts-limit 不能为负数")
    if args.json and args.details:
        p.error("--json已包含完整数据，不与--details同时使用")
    if args.confirm_valuation and not args.valuation:
        p.error("--confirm-valuation必须与--valuation文件同时使用")
    if args.analyze or args.document_id:
        p.error("原自动研究链路已停用，请用--valuation录入本人核对的手工数据")
    if any((args.materials,args.event,args.freeze_preview,args.guidance)):
        p.error("旧原件/事件/指引导入与冻结流程已停用，请通过统一手工研究输入录入估值数据")
    if args.cached and any((args.materials,args.event,args.freeze_preview,args.valuation,args.guidance)):
        p.error("--cached只读缓存，不能同时导入资料、记录事件、冻结或新增估值")
    try:
        symbol = args.symbol
        if not symbol:
            if not sys.stdin.isatty():
                p.error("请在命令后填写股票代码，例如 stock-research AAPL")
            print("请输入美股代码：", end="", file=sys.stderr, flush=True)
            symbol = input().strip()
        output_path = args.output.expanduser().resolve() if args.output else None
        if output_path and output_path.exists():
            raise FileExistsError
        # 延迟导入：--help不连接数据库、不初始化供应商，也无需服务在运行。
        logger.info("正在初始化研究配置与本地存储")
        from .store import initialize, stamp
        from .config import settings
        from .service import build_report, refresh_sources, security_for
        initialize()
        settings.averages
        security = security_for(symbol, create=not args.cached)
        logger.info("开始研究：%s；模式：%s", security.symbol,
            "只读缓存" if args.cached else "强制刷新" if args.force else "优先复用有效缓存")
        command = {"mode":"cached" if args.cached else "force" if args.force else "refresh",
            "steps":{}, "warnings":[], "analysis_requested":False,"analysis_policy":"manual-input-no-llm-v1",
            "valuation_policy":"manual-eps-pe-v4"}
        from .inputs import import_valuations
        def input_step(label,fn):
            logger.info("正在处理%s",label)
            try:
                command["steps"][label]=fn()
            except Exception as exc:
                from .providers import failure_detail
                warning={**failure_detail(exc),"stage":label}
                command["warnings"].append(warning)
                logger.warning("[%s] %s",label,warning["message"])
        if not args.cached:
            refreshed=refresh_sources(security.id,args.force,lambda percent,message:logger.info("[%s%%] %s",percent,message),
                analyze_reports=False)
            command["steps"].update(refreshed["steps"]);command["warnings"].extend(refreshed["warnings"])
        else:
            logger.info("仅读取已保存研究数据，不请求外部来源或模型")
        if args.valuation:
            input_step("估值",lambda:import_valuations(security.id,args.valuation,confirm=args.confirm_valuation))
            # 导入在行情之后进行；重新列手工状态，避免保留导入之前的缺失警告。
            from .financial_reports import run_steps
            from .manual_valuation import check
            checked=run_steps(security.id,False,[(95,"正在核对导入后的手工估值","valuation",check)])
            command["warnings"]=[w for w in command["warnings"] if w.get("stage")!="valuation"] + checked["warnings"]
            command["steps"].update(checked["steps"])
        logger.info("正在汇总研究报告")
        report = build_report(security)
        report.pop("jobs", None)
        report.update({"generated_at":stamp(), "command":command})
        content = dump(report) + "\n" if args.json else render_text(report, args.facts_limit, args.details, settings.research_display_timezone)
        sys.stdout.write(content)
        sys.stdout.flush()
        if output_path:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            # 研究导出不能意外覆盖原始证据、配置或用户上一次的核对报告。
            with output_path.open("x", encoding="utf-8") as target:
                target.write(content)
            logger.info("报告已保存：%s", output_path)
        if not report.get("market") and report["manual_valuation"]["status"] == "missing":
            logger.error("未取得可用行情，报告中的缺失项请先核对网络与股票代码")
            return 1
        logger.info("研究输出完成：%s；行情缓存状态：%s；本次步骤警告：%s条",
            security.symbol, report["cache"]["state"], len(command["warnings"]))
        return 2 if command["warnings"] else 0
    except (ValueError, LookupError) as exc:
        logger.error("无法完成查询：%s", exc)
        return 1
    except FileExistsError:
        logger.error("报告文件已存在，请更换 --output 文件名；本次未覆盖旧文件")
        return 1
    except (KeyboardInterrupt, EOFError):
        logger.warning("已取消查询，已保存数据保留")
        return 130
    except BrokenPipeError:
        return 0
    except Exception as exc:
        # 不打印可能带有数据库口令、请求头或API密钥的底层异常正文。
        logger.error("查询未完成（%s），请核对依赖、.env、网络与数据目录权限", type(exc).__name__)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
