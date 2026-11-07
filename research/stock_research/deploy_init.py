"""非 Docker 部署初始化：建表，并仅在首次部署写入版本化的美股初始池。"""
import logging

from sqlalchemy import select

from . import watchlist
from .cli import ChineseParser, configure_console_logging
from .config import ROOT
from .store import Session, WatchlistEntry, WatchlistState, initialize

logger = logging.getLogger(__name__)


def _has_watchlist(db):
    return (db.scalar(select(WatchlistState.id).limit(1)) is not None
            or db.scalar(select(WatchlistEntry.symbol).limit(1)) is not None)


def initialize_deployment():
    from .post_close import policy
    seed = None if policy().futu_heat_enabled else watchlist.SeedPolicy.model_validate_json(
        (ROOT / "config" / "deploy-watchlist.json").read_text(encoding="utf-8"))
    initialize()
    # 常规升级不需要争用名单写锁；首次写入时在锁内再次检查。
    with Session() as db:
        if _has_watchlist(db):
            return None
    with watchlist.write_lock(), Session() as db:
        if _has_watchlist(db):
            return None
        if seed is None:
            db.add(WatchlistState(id="default",seed={"version":"futu-heat-v1","symbols":[]}))
            db.commit()
            return 0
        db.add(WatchlistState(id="default", seed=seed.model_dump()))
        db.add(WatchlistState(id=watchlist.UNIVERSE_STATE_ID, seed={
            "managed_symbols":seed.symbols, "manual_overrides":{}, "deployment_seed_version":seed.version,
        }))
        db.add_all([WatchlistEntry(symbol=symbol, active=True) for symbol in seed.symbols])
        db.commit()
        return len(seed.symbols)


def main(argv=None):
    ChineseParser(description="初始化部署数据库和初始关注池；已有关注历史保留").parse_args(argv)
    configure_console_logging()
    try:
        count = initialize_deployment()
    except Exception as exc:
        # 避免输出连接字符串、数据库口令或环境配置。
        logger.error("部署初始化失败（%s）；请检查数据库配置、数据目录权限和关注池写锁", type(exc).__name__)
        return 1
    if count is None:
        logger.info("数据库就绪；已存在关注记录或初始化状态，保留现有名单")
    elif count:
        logger.info("已初始化数据库和美股关注池，共 %s 只", count)
    else:
        logger.info("数据库已初始化；等待首次富途热榜，不导入旧指数股票池")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
