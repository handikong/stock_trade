import logging
import threading
import fcntl
import time
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from . import analysis
from .config import settings
from .service import refresh_sources
from .store import Job, Session, stamp, uid

SCHEDULED_KINDS = ("scheduled_futu_heat","scheduled_market","scheduled_signals")

logger=logging.getLogger(__name__)


def queue(security_id: str, kind: str, payload: dict):
    if kind in ("discover", "document"):
        raise ValueError("旧研究搜索和URL原件下载已停用，请手工录入研究数据")
    if kind=="analysis":
        raise ValueError(analysis.DISABLED_MESSAGE)
    active_key=f"workspace:{kind}" if kind in ("watchlist_scan","holdings_refresh") else f"{security_id}:{kind}"
    with Session() as session:
        active=session.scalar(select(Job).where(Job.active_key==active_key))
        if active:
            return active.id
        job=Job(id=uid(),security_id=security_id,kind=kind,active_key=active_key,payload=payload)
        session.add(job)
        try:
            session.commit()
            return job.id
        except IntegrityError:
            session.rollback()
            active=session.scalar(select(Job).where(Job.active_key==active_key))
            if active:
                return active.id
            raise


def progress(job_id,percent,message):
    with Session() as session:
        job=session.get(Job,job_id)
        job.progress=percent;job.message=message;job.updated_at=stamp()
        session.commit()


def run(job: Job):
    sid=job.security_id
    if job.kind in SCHEDULED_KINDS:
        from . import post_close
        return post_close.run(job)
    if job.kind=="holdings_refresh":
        from . import market, workspace
        from .providers import failure_detail
        from .service import security_for
        positions=workspace.holdings()["items"]; results=[]; warnings=[]
        for index,item in enumerate(positions,1):
            progress(job.id,int(90*index/max(len(positions),1)),f"正在更新持仓行情：{item['symbol']}")
            logger.info("[%s/%s] 更新持仓行情：%s",index,len(positions),item["symbol"])
            try:
                value=market.refresh_market(security_for(item["symbol"]).id)
                results.append({"symbol":item["symbol"],"result":value})
            except Exception as exc:
                warnings.append({"symbol":item["symbol"],**failure_detail(exc)})
        return {"results":results,"warnings":warnings}
    if job.kind=="watchlist_scan":
        from . import watchlist
        progress(job.id,5,"正在扫描自选股票；不会获取财报或发送通知")
        return watchlist.scan(offline=False,refresh=job.payload.get("refresh",False))
    if job.kind=="refresh":
        result=refresh_sources(sid,job.payload.get("force",False),lambda p,m:progress(job.id,p,m))
        if not result["steps"]:
            raise ValueError("所有数据步骤均未成功，请核对网络、代码与数据源设置")
        return result
    if job.kind=="analysis":
        return {"skipped":analysis.DISABLED_MESSAGE,"status":"disabled"}
    if job.kind=="discover":
        return {"status":"disabled","skipped":"旧搜索任务已停用，不再发起请求"}
    if job.kind=="document":
        return {"status":"disabled","skipped":"旧原件下载任务已停用，不再发起请求"}
    raise ValueError("未知任务类型")


class Worker:
    def __init__(self):
        self.stop=threading.Event()
        self.thread=threading.Thread(target=self.loop,name="research-worker",daemon=True)
        self.leader=None
        self.next_schedule_tick=0.0
        self.last_schedule_message=None

    def start(self):
        settings.storage.mkdir(parents=True,exist_ok=True)
        self.leader=(settings.storage/"background-worker.lock").open("a+b")
        try:
            fcntl.flock(self.leader.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self.leader.close()
            raise ValueError("已有后台Worker运行，请只启动一个Python服务进程") from exc
        try:
            # 普通中断任务维持原策略；盘后任务复用逐股进度及正式投递去重续跑。
            with Session() as session:
                scheduled=SCHEDULED_KINDS
                session.execute(update(Job).where(Job.status=="running",Job.kind.notin_(scheduled)).values(status="failed",active_key=None,
                    message="上次进程中断，已保存的数据保留，请按需重新发起",updated_at=stamp()))
                session.execute(update(Job).where(Job.status=="running",Job.kind.in_(scheduled)).values(status="queued",
                    message="恢复中断的盘后任务，已缓存/已发送进度保留",updated_at=stamp()))
                session.commit()
            self.thread.start()
        except Exception:
            self.leader.close()
            raise

    def loop(self):
        try:
            self._loop()
        finally:
            # 耗时请求结束、线程真正退出后才释放；shutdown超时不会提早交出执行权。
            if self.leader:
                self.leader.close()

    def _schedule_tick(self):
        if time.monotonic()<self.next_schedule_tick:
            return
        self.next_schedule_tick=time.monotonic()+30
        try:
            from . import post_close
            cfg=post_close.policy()
            self.next_schedule_tick=time.monotonic()+cfg.poll_seconds
            result=post_close.enqueue_due()
            label=(result["local_date"],result["message"])
            if label!=self.last_schedule_message:
                logger.info("盘后计划：%s",result["message"])
                self.last_schedule_message=label
        except Exception:
            if self.last_schedule_message!="schedule-error":
                logger.warning("盘后计划检查未完成，请核对计划配置、交易日历及数据库；不发起消息")
                self.last_schedule_message="schedule-error"

    def _loop(self):
        while not self.stop.is_set():
            try:
                self._schedule_tick()
                with Session() as session:
                    job=session.scalar(select(Job).where(Job.status=="queued").order_by(Job.created_at).limit(1))
                    if job:
                        changed=session.execute(update(Job).where(Job.id==job.id,Job.status=="queued")
                            .values(status="running",updated_at=stamp())).rowcount
                        session.commit()
                        if not changed:
                            continue
                if not job:
                    self.stop.wait(1)
                    continue
                try:
                    result=run(job)
                    status="failed" if result.get("status")=="expired" else "completed"
                    message=result.get("message") or (result["skipped"] if result.get("status")=="disabled" else "处理完成，部分资料可能仍需补充" if result.get("warnings") else "处理完成")
                except ValueError as exc:
                    result={};status="failed";message=str(exc)
                except Exception:
                    logger.warning("研究任务未完成：%s",job.kind)
                    result={};status="failed";message="处理未完成，原缓存与已保存材料保留，请核对资料和配置后重试"
                with Session() as session:
                    item=session.get(Job,job.id)
                    if status=="failed" and not result and job.kind in SCHEDULED_KINDS:
                        result={**item.result,"status":"failed","message":message}
                    item.status=status;item.message=message;item.result=result;item.progress=100
                    item.active_key=None;item.updated_at=stamp()
                    session.commit()
                logger.info("后台任务%s · %s：%s",job.kind,status,message)
            except Exception:
                logger.warning("任务队列暂不可用，等待下一次尝试")
                self.stop.wait(3)

    def shutdown(self):
        self.stop.set()
        self.thread.join(timeout=3)
