"""信号研究与飞书群通知命令；默认预览，--send显式执行群投递。"""
import logging

from .cli import ChineseParser, configure_console_logging, dump
from .console_tables import table
from .notification_cards import card_text

logger = logging.getLogger("stock_research.notify_cli")
STATUS_LABELS = {"READY": "待处理", "SENDING": "发送中", "SENT": "飞书已接收", "FAILED": "明确失败",
    "UNKNOWN": "送达未知，待核对", "PREVIEW": "预览，未发送", "SKIPPED_SENT": "已发送，跳过",
    "SKIPPED_FAILED": "此前失败，未选择重试", "SKIPPED_UNKNOWN": "此前送达未知，不自动重发",
    "SKIPPED_STALE": "当前有效性不满足", "SKIPPED_STALE_AFTER_RESEARCH": "研究后有效性变化",
    "SKIPPED_SCHEDULE_WINDOW":"计划任务停用或已过时段，未发送",
    "DEFERRED_LIMIT": "本轮达上限，延后"}
KIND_LABELS = {"BUY": "买入信号", "EXIT_CONDITION": "达到退出条件", "ALERT": "预警变化"}


def parser():
    p = ChineseParser(description="股票信号或单股研究快照→飞书群机器人；默认只预览，加--send发送。", add_help=False)
    p._positionals.title = "股票代码或操作"
    p._optionals.title = "可选参数"
    p.add_argument("-h", "--help", action="help", help="查看中文说明")
    p.add_argument("target", nargs="?", default="run", metavar="股票代码或操作",
                   help="例如NVDA只处理该股；run扫描股票池；history查看投递状态；test测试无信号连通性")
    p.add_argument("--send", action="store_true", help="显式发送；默认仅当前有效信号，--research-only发送单股研究快照，test发送连通性消息")
    p.add_argument("--research-only", action="store_true", help="仅对指定股票生成研究快照，无需策略信号；加--send发送一条，不扫描或占正式信号去重")
    p.add_argument("--refresh", action="store_true", help="显式更新所选行情；默认再扫描，--research-only只准备研究，不与--scan-id同时使用")
    p.add_argument("--scan-id", help="使用已有扫描ID；历史回放或非当前有效信号仍不推送")
    p.add_argument("--cached-research", action="store_true", help="发送买点时只读已有研究，不请求财报、预期或DeepSeek")
    p.add_argument("--retry-failed", action="store_true", help="配合--send重试明确失败的当前事件，不包含UNKNOWN")
    p.add_argument("--retry-unknown", action="store_true", help="配合--send重试结果未知的当前事件；须先核对群消息，可能造成重复消息")
    p.add_argument("--limit", type=int, default=50, help="history最多显示条数，默认50")
    p.add_argument("--json", action="store_true", help="输出结构化结果；预览包括完整消息卡片，不包含凭据")
    return p


def render(result):
    if isinstance(result, list):
        return table(["股票", "类型", "信号日", "状态", "尝试", "说明"],
            [[r["symbol"], KIND_LABELS.get(r["kind"], r["kind"]), r["signal_date"], STATUS_LABELS.get(r["status"], r["status"]),
              str(r["attempts"]), r["error"]] for r in result], numeric=(4,))
    if result.get("operation")=="RESEARCH_SNAPSHOT":
        lines = ["单股研究快照 · " + STATUS_LABELS.get(result["status"],result["status"]),
                 "股票："+result["symbol"],"快照编号："+result["request_id"]]
        lines.extend(card_text(result["card"]))
        lines.extend("提示："+w for w in result["warnings"])
        if result.get("error"):
            lines.append("提示："+result["error"])
        if result.get("audit_path"):
            lines.append("独立尝试档案："+result["audit_path"])
        lines.append(result["message"])
        return "\n".join(lines)
    if result.get("operation")=="CONNECTION_TEST":
        lines = ["飞书连通性测试 · " + STATUS_LABELS.get(result["status"],result["status"]),
                 "测试编号："+result["probe_id"], result["card"]["header"]["title"]["content"],
                 result["card"]["elements"][0]["text"]["content"]]
        if result["mode"]=="PREVIEW":
            lines.append("Webhook状态：" + ("已配置，未请求飞书" if result.get("configured") else "未配置或格式无效"))
        if result.get("error"):
            lines.append("提示："+result["error"])
        if result.get("audit_path"):
            lines.append("独立尝试档案："+result["audit_path"])
        lines.extend("提示："+w for w in result["warnings"])
        lines.append(result["message"])
        return "\n".join(lines)
    lines = ["飞书群通知 · " + ("预览（未发送）" if result["mode"] == "PREVIEW" else "发送结果"),
             "处理范围：" + (result["symbol"] if result.get("scope") == "SINGLE" else "整个股票池"),
             "扫描ID：" + result["scan_id"],
             table(["股票", "信号类型", "处理状态", "说明"],
                   [[r["symbol"], KIND_LABELS.get(r["kind"], r["kind"]), STATUS_LABELS.get(r["status"], r["status"]),
                     r.get("error", "")] for r in result["items"]])]
    for item in result["items"]:
        if item.get("card"):
            lines.extend(["", item["card"]["header"]["title"]["content"]])
            lines.extend(card_text(item["card"]))
    if not result["items"]:
        lines.append("没有可处理的当前有效信号；不会把历史信号或旧行情当作新提醒。")
    lines.extend("提示：" + w for w in result["warnings"])
    lines.append(result["message"])
    return "\n".join(lines)


def main(argv=None):
    p = parser(); args = p.parse_args(argv)
    configure_console_logging()
    if args.target == "history" and any((args.send, args.refresh, args.scan_id, args.cached_research,args.research_only,
                                         args.retry_failed, args.retry_unknown)):
        p.error("history只支持--limit和--json，不刷新或发送")
    if args.target == "test" and any((args.refresh,args.scan_id,args.cached_research,args.retry_failed,args.retry_unknown,args.research_only)):
        p.error("test只支持--send和--json，不扫描股票、刷新或重试正式信号")
    if args.research_only:
        if args.target in ("run","history","test"):
            p.error("--research-only需要指定单只股票，例如GOOG")
        if any((args.scan_id,args.cached_research,args.retry_failed,args.retry_unknown)):
            p.error("--research-only只支持股票、--refresh、--send和--json，不接受扫描记录或正式信号重试参数")
    try:
        if args.target=="test":
            # 独立诊断不初始化数据库/股票池，不创建虚假信号或正式SignalDelivery。
            from .notification_probe import run
            result = run(send=args.send)
            print(dump(result) if args.json else render(result))
            if result.get("interrupted"):
                return 130
            return 2 if result["status"] in ("FAILED","UNKNOWN") or result["warnings"] else 0
        from .store import initialize
        from . import notifications, watchlist
        symbol = None if args.target in ("run", "history") else watchlist.normalize_symbol(args.target)
        initialize()
        if args.research_only:
            from .research_notification import run
            result = run(symbol,refresh=args.refresh,send=args.send)
            print(dump(result) if args.json else render(result))
            if result.get("interrupted"):
                return 130
            return 2 if result["status"] in ("FAILED","UNKNOWN") or result["warnings"] else 0
        if args.target == "history":
            result = notifications.history(args.limit)
        else:
            if symbol is None and not args.scan_id:
                watchlist.ensure_seed()
            if args.retry_unknown:
                logger.warning("已选择重发UNKNOWN事件，原消息可能已到群，请先核对以免重复")
            result = notifications.run(symbol=symbol, scan_id=args.scan_id, refresh=args.refresh, send=args.send,
                cached_research=args.cached_research, retry_failed=args.retry_failed, retry_unknown=args.retry_unknown)
        print(dump(result) if args.json else render(result))
        if isinstance(result, dict) and (result["warnings"] or any(i["status"] in
            ("FAILED", "UNKNOWN", "SKIPPED_UNKNOWN", "SKIPPED_FAILED", "SKIPPED_STALE", "SKIPPED_STALE_AFTER_RESEARCH", "DEFERRED_LIMIT") for i in result["items"])):
            return 2
        return 0
    except KeyboardInterrupt:
        logger.warning("研究快照中断；请先核对群消息及research-notifications中的SENDING/UNKNOWN档案，不自动重试" if args.research_only else
                       "连通性测试中断；请先核对群消息，SENDING/UNKNOWN档案均按送达未知处理，不自动重试" if args.target=="test" else
                       "通知流程中断；发送中状态下次会标为UNKNOWN，不自动重复投递")
        return 130
    except Exception as exc:
        from .providers import failure_detail
        logger.error("通知流程未完成：%s", failure_detail(exc)["message"])
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
