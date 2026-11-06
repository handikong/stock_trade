"""用户主动运行的历史组合回测命令；不启动应用，不调用数据商或模型。"""
import json
import logging
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from .cli import ChineseParser, configure_console_logging
from .config import settings

logger = logging.getLogger("stock_research.backtest_cli")


def parser():
    p = ChineseParser(description="MG7共享现金账户历史回测；仅使用本地行情，不写真实成交。", add_help=False)
    p.add_argument("-h", "--help", action="help", help="查看中文说明")
    p.add_argument("--policy", type=Path, help="账户回测配置，默认config/backtest-policy.json")
    p.add_argument("--cash", help="本次模拟账户总初始资金，覆盖配置；默认10000，不修改真实本金")
    p.add_argument("--currency", help="账户币种；当前仅支持USD")
    p.add_argument("--symbols", nargs="+", help="显式指定股票子集；默认配置中的MG7，缺行情不自动剔除")
    p.add_argument("--history-start", help="预热起点，默认2026-01-01")
    p.add_argument("--start", help="回测起点，默认2026-03-01")
    p.add_argument("--end", help="回测截止，默认各股票共同完整截止")
    p.add_argument("--indicator-policy", type=Path, help="原色带配置")
    p.add_argument("--strategy-policy", type=Path, help="双策略及退出配置")
    p.add_argument("--output", type=Path, help="完整JSON结果路径，不覆盖；默认data/backtests下新建文件")
    p.add_argument("--json", action="store_true", help="控制台输出完整JSON；默认账户绩效、逐股盈亏和买卖明细三张表")
    p.add_argument("--report", type=Path, help="从已有JSON生成表格，不重新模拟交易")
    p.add_argument("--details", action="store_true", help="展开参数、跳过原因和模型限制；默认只看报表")
    p.add_argument("--risk-free-rate", help="夏普/索提诺的无风险年利率小数，例如0.02；默认配置值0")
    p.add_argument("--annualization-sessions", type=int, help="年化交易日，默认252")
    return p


def serialize(report):
    def encode(value):
        if isinstance(value, Decimal):
            return format(value, "f")
        raise TypeError("报告中存在不能保存的类型")
    return json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False, default=encode)


def main(argv=None):
    args = parser().parse_args(argv)
    configure_console_logging()
    try:
        from .backtest import policy, run
        from .backtest_metrics import enrich_report
        from .backtest_tables import render
        overrides = {"initial_cash": args.cash, "currency": args.currency, "symbols": args.symbols,
                     "history_start": args.history_start, "start": args.start, "end": args.end,
                     "risk_free_annual": args.risk_free_rate, "annualization_sessions": args.annualization_sessions}
        if args.report:
            if any(v is not None for v in (args.policy, args.cash, args.currency, args.symbols, args.history_start,
                                           args.start, args.end, args.indicator_policy, args.strategy_policy)):
                raise ValueError("读取已有结果不能同时修改交易参数；请去掉--report重新回测")
            raw = json.loads(args.report.expanduser().read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or raw.get("mode") != "HISTORICAL_SIMULATION":
                raise ValueError("文件不是本项目历史模拟账户报告")
            report = enrich_report(raw, args.risk_free_rate, args.annualization_sessions)
            if args.output:
                output = args.output.expanduser().resolve()
                if output.exists():
                    raise ValueError("输出文件已存在，请使用新文件名")
                report["output_file"] = str(output)
                output.parent.mkdir(parents=True, exist_ok=True)
                with output.open("x", encoding="utf-8") as handle:
                    handle.write(serialize(report) + "\n")
            print(serialize(report) if args.json else render(report, details=args.details))
            return 0
        cfg = policy(args.policy, overrides)
        filename = "backtest-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ".json"
        output = (args.output or settings.storage / "backtests" / filename).expanduser().resolve()
        if output.exists():
            raise ValueError("输出文件已存在，请使用新文件名")
        report = run(cfg, args.indicator_policy, args.strategy_policy)
        report["output_file"] = str(output)
        text = serialize(report)
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("x", encoding="utf-8") as handle:
            handle.write(text + "\n")
        logger.info("回测完成，完整模拟结果保存到%s", output)
        print(text if args.json else render(report, details=args.details))
        return 0
    except KeyboardInterrupt:
        logger.warning("回测已中断；未写入真实账户或成交")
        return 130
    except Exception as exc:
        from .providers import failure_detail
        logger.error("回测失败：%s", failure_detail(exc)["message"])
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
