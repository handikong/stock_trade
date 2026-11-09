import logging
import threading
import fcntl
import time
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from . import account_equity, analysis, cn_post_close, market_caps
from .config import settings
from .service import refresh_sources
from .store import Job, Session, now, stamp, uid

SCHEDULED_KINDS = ("scheduled_futu_heat","scheduled_market","scheduled_signals","scheduled_valuation",
                   "scheduled_market_brief","scheduled_holdings_brief","scheduled_cn_heat",
                   *cn_post_close.KINDS.values(),account_equity.SCHEDULED_KIND)
WORKSPACE_KINDS = ("watchlist_scan","holdings_refresh","cn_holdings_refresh","cn_watchlist_scan",
                   market_caps.JOB_KIND,market_caps.CN_JOB_KIND,*account_equity.MANUAL_KINDS.values())

logger=logging.getLogger(__name__)


def queue(security_id: str, kind: str, payload: dict):
    if kind in ("discover", "document"):
        raise ValueError("旧研究搜索和URL原件下载已停用，请手工录入研究数据")
    if kind=="analysis":
        raise ValueError(analysis.DISABLED_MESSAGE)
    if kind in account_equity.MANUAL_KINDS.values():
        active_key = f"account-equity:{payload['market']}"
    else:
        active_key=f"workspace:{kind}" if kind in WORKSPACE_KINDS else f"{security_id}:{kind}"
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
    if job.kind in account_equity.MANUAL_KINDS.values():
        return account_equity.capture(job,lambda percent,message:progress(job.id,percent,message))
    if job.kind in ("holdings_refresh","cn_holdings_refresh"):
        from . import cn_holdings, market, workspace
        from .providers import failure_detail
        from .service import security_for
        region = "CN" if job.kind=="cn_holdings_refresh" else "US"
        positions=(cn_holdings if region=="CN" else workspace).holdings()["items"]; results=[]; warnings=[]
        for index,item in enumerate(positions,1):
            progress(job.id,int(90*index/max(len(positions),1)),f"正在更新持仓行情：{item['symbol']}")
            logger.info("[%s/%s] 更新持仓行情：%s",index,len(positions),item["symbol"])
            try:
                value=market.refresh_market(security_for(item["symbol"],create=region=="CN").id,region=region)
                results.append({"symbol":item["symbol"],"result":value})
            except Exception as exc:
                warnings.append({"symbol":item["symbol"],**failure_detail(exc)})
        return {"results":results,"warnings":warnings}
    if job.kind in ("watchlist_scan","cn_watchlist_scan"):
        from . import watchlist
        region = "CN" if job.kind=="cn_watchlist_scan" else "US"
        progress(job.id,5,f"正在扫描{'A 股' if region=='CN' else '美股'}；不会获取财报或发送通知")
        return watchlist.scan(offline=False,refresh=job.payload.get("refresh",False),region=region)
    if job.kind in (market_caps.JOB_KIND,market_caps.CN_JOB_KIND):
        region = "CN" if job.kind==market_caps.CN_JOB_KIND else "US"
        return market_caps.refresh(lambda percent,message:progress(job.id,percent,message),region=region)
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
        self.delivery_plan_key=None
        self.delivery_plan=[]
        self.checked_delivery_windows=set()

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
            self._audit_delivery_schedule(cfg)
            result=post_close.enqueue_due()
            label=(result["local_date"],result["message"])
            if label!=self.last_schedule_message:
                logger.info("盘后计划：%s",result["message"])
                self.last_schedule_message=label
        except Exception:
            if self.last_schedule_message!="schedule-error":
                logger.warning("盘后计划检查未完成，请核对计划配置、交易日历及数据库；不发起消息")
                self.last_schedule_message="schedule-error"

    def _audit_delivery_schedule(self,cfg):
        from . import scheduled_delivery as audit
        current=now().astimezone(audit.BEIJING)
        key=(current.date(),cfg.model_dump_json())
        try:
            if key!=self.delivery_plan_key:
                self.delivery_plan=audit.plan(current.date(),cfg)
                self.checked_delivery_windows.clear()
                for row in self.delivery_plan:
                    logger.info("[计划清单] 日期=%s 时间=%s 截止=%s 任务=%s 发送=%s 条件=%s",
                                current.date(),row["start"].strftime("%H:%M"),row["end"].strftime("%H:%M"),row["name"],
                                "条件满足时发送" if row["enabled"] else "已关闭",row["condition"])
                self.delivery_plan_key=key
            expired=[row for row in self.delivery_plan if row["enabled"] and current>=row["end"]
                     and (row["job_id"],row["channel"]) not in self.checked_delivery_windows]
            for row in audit.inspect_plan(expired,current=current):
                item=row["details"][0]
                if item["status"] in ("MISSED","WINDOW_ELAPSED"):
                    logger.warning("[计划推送] 日期=%s 任务=%s 编号=%s 状态=%s 原因=%s",
                                   current.date(),row["name"],row["job_id"],item["status"],item["reason"])
                self.checked_delivery_windows.add((row["job_id"],row["channel"]))
        except Exception as exc:
            logger.warning("[计划清单] 日期=%s 读取失败 · %s",current.date(),type(exc).__name__)

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
                if job.kind in SCHEDULED_KINDS:
                    logger.info("[计划任务] 日期=%s 任务=%s 编号=%s 交易日=%s 状态=STARTED",
                                job.payload.get("local_date"),job.kind,job.id,job.payload.get("target_session"))
                try:
                    result=run(job)
                    status="failed" if result.get("status")=="expired" else "completed"
                    message=result.get("message") or (result["skipped"] if result.get("status")=="disabled" else "处理完成，部分资料可能仍需补充" if result.get("warnings") else "处理完成")
                except ValueError as exc:
                    result={};status="failed";message=str(exc)
                except Exception as exc:
                    logger.warning("研究任务未完成：%s",job.kind)
                    result={};status="failed";message="处理未完成，原缓存与已保存材料保留，请核对资料和配置后重试"
                    if job.kind in SCHEDULED_KINDS:
                        from .providers import failure_detail
                        detail=failure_detail(exc)
                        message=f"{detail['message']}（{detail['error_type']}）"
                with Session() as session:
                    item=session.get(Job,job.id)
                    if status=="failed" and not result and job.kind in SCHEDULED_KINDS:
                        result={**item.result,"status":"failed","message":message}
                    item.status=status;item.message=message;item.result=result;item.progress=100
                    item.active_key=None;item.updated_at=stamp()
                    session.commit()
                if job.kind in SCHEDULED_KINDS:
                    from . import scheduled_delivery as audit
                    audit.log_outcomes(job,result,status,message)
                else:
                    logger.info("后台任务%s · %s：%s",job.kind,status,message)
            except Exception:
                logger.warning("任务队列暂不可用，等待下一次尝试")
                self.stop.wait(3)

    def shutdown(self):
        self.stop.set()
        self.thread.join(timeout=3)
