"""非 Docker 部署初始化：建表，并仅在首次部署写入版本化的美股初始池。"""
import logging
import os
import secrets

from dotenv import set_key
from sqlalchemy import select

from . import watchlist
from .cli import ChineseParser, configure_console_logging
from .config import ROOT, Settings, settings
from .store import Session, WatchlistEntry, WatchlistState, initialize

logger = logging.getLogger(__name__)


def _ensure_network_token(host):
    if host == "127.0.0.1" or settings.app_token.get_secret_value().strip():
        return
    # 环境变量比 .env 优先；空值覆盖时明确失败，不启动没有认证的对外服务。
    if any(key.upper() == "APP_TOKEN" for key in os.environ):
        raise ValueError("对外监听需要访问口令；请移除或填写空的 APP_TOKEN 环境变量后重试")
    set_key(str(ROOT / ".env"), "APP_TOKEN", secrets.token_urlsafe(32), quote_mode="never")
    if not Settings().app_token.get_secret_value().strip():
        raise ValueError("对外访问口令未生效，请检查服务器 .env 后重试")
    logger.info("已在服务器 .env 中生成访问口令 APP_TOKEN；日志不显示口令")


def _has_watchlist(db):
    return (db.scalar(select(WatchlistState.id).limit(1)) is not None
            or db.scalar(select(WatchlistEntry.symbol).limit(1)) is not None)


def initialize_deployment():
    seed = watchlist.SeedPolicy.model_validate_json(
        (ROOT / "config" / "deploy-watchlist.json").read_text(encoding="utf-8"))
    initialize()
    # 常规升级不需要争用名单写锁；首次写入时在锁内再次检查。
    with Session() as db:
        if _has_watchlist(db):
            return 0
    with watchlist.write_lock(), Session() as db:
        if _has_watchlist(db):
            return 0
        db.add(WatchlistState(id="default", seed=seed.model_dump()))
        db.add(WatchlistState(id=watchlist.UNIVERSE_STATE_ID, seed={
            "managed_symbols":seed.symbols, "manual_overrides":{}, "deployment_seed_version":seed.version,
        }))
        db.add_all([WatchlistEntry(symbol=symbol, active=True) for symbol in seed.symbols])
        db.commit()
        return len(seed.symbols)


def main(argv=None):
    parser = ChineseParser(description="初始化部署数据库和初始关注池；已有关注历史保留")
    parser.add_argument("--listen-host", choices=("127.0.0.1", "0.0.0.0"), default="127.0.0.1",
        help="对外监听时确保已设置访问口令")
    args = parser.parse_args(argv)
    configure_console_logging()
    try:
        _ensure_network_token(args.listen_host)
    except ValueError as exc:
        logger.error("%s", exc)
        return 1
    except OSError:
        logger.error("无法在服务器 .env 中设置访问口令，请检查文件及目录权限")
        return 1
    try:
        count = initialize_deployment()
    except Exception as exc:
        # 避免输出连接字符串、数据库口令或环境配置。
        logger.error("部署初始化失败（%s）；请检查数据库配置、数据目录权限和关注池写锁", type(exc).__name__)
        return 1
    if count:
        logger.info("已初始化数据库和美股关注池，共 %s 只", count)
    else:
        logger.info("数据库就绪；已存在关注记录或初始化状态，保留现有名单")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
