"""独立支撑压力比对命令；不跑策略扫描、不抓财报、不调用AI、不推送。"""
import logging
from pathlib import Path

from .cli import ChineseParser, configure_console_logging, dump
from .cli_helpers import emit, normalize_symbol, output_path, storage_ready

logger = logging.getLogger("stock_research.levels_cli")


def parser():
    p = ChineseParser(description="单独核对K线技术支撑压力、强弱及基本面配合。", add_help=False)
    p.add_argument("-h", "--help", action="help", help="查看中文说明")
    p.add_argument("symbol", help="例如GOOG、NVDA")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--cached", action="store_true", help="只读已有缓存，不请求行情、不写库")
    mode.add_argument("--refresh", action="store_true", help="显式更新日线，不获取PE/财报或调用AI")
    p.add_argument("--details", action="store_true", help="附全部技术区间、结构依据与基本面结合计算详情")
    p.add_argument("--json", action="store_true", help="输出完整技术和综合结果，便于保存比对")
    p.add_argument("--output", type=Path, help="另存输出，拒绝覆盖已有文件")
    return p


def main(argv=None):
    p = parser(); args = p.parse_args(argv); configure_console_logging()
    if args.json and args.details:
        p.error("--json已包含完整数据，不与--details同时使用")
    try:
        symbol = normalize_symbol(args.symbol); path = output_path(args.output)
        from .service import build_report, security_for
        from .store import initialize, stamp
        from .market import refresh_market
        from .financial_reports import run_steps
        command = {"mode":"cached" if args.cached else "refresh" if args.refresh else "cache_first", "steps":{},"warnings":[]}
        if args.cached:
            if not storage_ready("securities","research_snapshots"):
                raise LookupError("尚无行情缓存，请先执行stock-levels 股票代码获取日线")
            security = security_for(symbol)
            logger.info("只读既有技术与手工估值，不请求行情或写库")
        else:
            initialize()
            security = security_for(symbol,create=True)
            logger.info("正在处理%s行情和技术区间；EPS及PE仅读手工输入",symbol)
            command.update(run_steps(security.id,args.refresh,[(10,"正在计算技术支撑压力","market",refresh_market)]))
        report = build_report(security)
        # 导出完整技术结构与结合层，不输出K线数组、原件或整份手工提示正文。
        result = {"symbol":symbol,"profile":report["profile"],"generated_at":stamp(),"cache":report["cache"],
                  "market":report["market"],"fundamental_technical":report["fundamental_technical"],
                  "manual_valuation":report["manual_valuation"],
                  "manual_valuation_id":report["manual_valuation"].get("id"),"command":command}
        from .research_tables import render_levels
        from .config import settings
        content = dump(result)+"\n" if args.json else render_levels(result,display_timezone=settings.research_display_timezone,details=args.details)
        emit(content,path)
        technical = (result["market"] or {}).get("technical") or {}
        if not result["market"]:
            logger.warning("没有行情；研究结果中已列明缺项")
            return 1
        if command["warnings"] or result["cache"].get("state")!="fresh" or technical.get("stale") or technical.get("status")!="ready" or result["fundamental_technical"]["status"]!="available":
            logger.warning("比对输出完成，部分技术/基本面条件未齐；退出码2不表示命令崩溃")
            return 2
        return 0
    except BrokenPipeError:
        return 0
    except (ValueError, LookupError, OSError) as exc:
        logger.error("支撑压力查询未完成：%s",str(exc) if isinstance(exc,(ValueError,LookupError)) else "文件不可用，请核对路径与权限")
        return 1
    except KeyboardInterrupt:
        logger.warning("已取消支撑压力查询")
        return 130
    except Exception as exc:
        logger.error("支撑压力查询未完成（%s），请核对本地配置/行情来源；不输出凭据",type(exc).__name__)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
