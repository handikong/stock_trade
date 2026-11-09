"""由后台生命周期管理的 Go 行情进程及盘中飞书投递。"""
import fcntl
import json
import logging
import os
import subprocess
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import localcontext
from queue import Empty, Full, PriorityQueue
from threading import Event, Thread
from typing import Annotated
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from . import feishu, intraday_rules as rules, market, portfolio_risk as risk
from .config import settings
from .notification_cards import _price
from .notification_probe import _write
from .store import ManualPosition, PositionPlan, Session, digest, now, stamp
from .workspace import day_move

logger = logging.getLogger(__name__)
BEIJING = ZoneInfo("Asia/Shanghai")
NEW_YORK = ZoneInfo("America/New_York")
FOLDER = "intraday-notifications"
BENCHMARKS = rules.BENCHMARKS
D = risk.D
PRIORITY = {"stop":0, "scale":1, "reduce":2, "profit":3, "market":4, "watch":5,
            "add":6, "breakout":7, "reclaim":8}
LABELS = {"stop":"止损复核", "scale":"报价尺度待复核", "reduce":"减仓复核",
          "market":"大盘同步走弱", "watch":"关键位失守预警", "add":"加仓复核",
          "profit":"止盈复核", "breakout":"突破回踩确认", "reclaim":"支撑收回"}
STRUCTURE_NOTES = {"profit":"触及登记止盈价后回落，收线与现价均低于压力区。",
                   "breakout":"突破后回踩守住关键位，现价在 VWAP 上方。",
                   "reclaim":"回踩关键位后收回，已完成 K 线与现价均在缓冲上方。"}


class Policy(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    beijing_push_cutoff: time = time(2)
    opening_minutes: int = Field(15, ge=5, le=30)
    confirmation_minutes: int = Field(5, ge=1, le=15)
    quote_max_age_seconds: int = Field(25, ge=10, le=90)
    buffer_daily_atr: D = Field(D("0.10"), gt=0, le=1)
    scale_review_fraction: D = Field(D("0.35"), gt=0, lt=1)
    take_profit_targets: dict[str, Annotated[D, Field(gt=0)]] = Field(default_factory=dict)


@dataclass(frozen=True)
class TradingSession:
    day: date
    previous: str
    opened: datetime
    closed: datetime
    cutoff: datetime
    historical_opens: tuple[datetime, ...]


def _trading_session(day, policy):
    calendar, _ = market.calendar_context()
    if not calendar.is_session(day.isoformat()):
        return None
    key = calendar.date_to_session(day.isoformat())
    opened = calendar.session_open(key).to_pydatetime()
    closed = calendar.session_close(key).to_pydatetime()
    previous = calendar.previous_session(key)
    local = opened.astimezone(BEIJING)
    cutoff = datetime.combine(local.date(), policy.beijing_push_cutoff, BEIJING)
    if cutoff <= opened:
        cutoff += timedelta(days=1)
    history = []
    prior = previous
    while len(history) < 5:
        start, end = calendar.session_open(prior), calendar.session_close(prior)
        if end-start == timedelta(minutes=390):
            history.append(start.to_pydatetime())
        prior = calendar.previous_session(prior)
    return TradingSession(day, previous.date().isoformat(), opened, closed, min(cutoff, closed), tuple(history))


def _event(kind, symbol, evidence, identity, **extra):
    return {"kind":kind, "symbol":symbol, "evidence":evidence, "identity":identity, **extra}


def _card(event, current, stage=None):
    kind, symbol = event["kind"], event["symbol"]
    fields = [event["evidence"]]
    if event.get("price") is not None:
        observed = datetime.fromisoformat(event["observed_at"]) if event.get("observed_at") else current
        fields.append(f"观测价 {_price(event['price'])} USD · {observed.astimezone(BEIJING):%H:%M:%S} 北京时间")
    if stage:
        fields.append(f"{stage['label']} · {_price(stage['min_price'])}–{_price(stage['max_price'])} USD")
        fields.append(f"计划剩余最多 {stage['remaining']} 股 · 保护位 {_price(stage['stop'])} USD")
    fields.append("IEX 行情 · 请在券商端核对价格和仓位，手动决定是否执行")
    return {"header":{"template":"red" if kind in ("stop","reduce","market") else "blue",
                      "title":{"tag":"plain_text","content":f"{symbol} · {LABELS[kind]}"}},
            "elements":[{"tag":"div","text":{"tag":"plain_text","content":"\n".join(fields)}}]}


def _risk_text(row, code, refs, prices, mode):
    parts = [f"大盘 {mode}"]
    changes = [rules.relative_return(symbol, refs, prices) for symbol in (code, *BENCHMARKS)]
    if all(change is not None for change in changes):
        parts.append(f"相对 QQQ {(changes[0]-changes[1])*100:+.2f} 个百分点、SPY {(changes[0]-changes[2])*100:+.2f} 个百分点")
    if row.get("relative_volume"):
        parts.append(f"IEX 同时段量比 {row['relative_volume']:.2f} 倍")
    return " · ".join(parts)


def _signal_key(event):
    return event["kind"], event["symbol"], event["identity"]


class Monitor:
    def __init__(self):
        self._stop = Event()
        self._process = None
        self._reader = None
        self._raw = None
        self._delivery_state = None
        self._queue = PriorityQueue(40)
        self._dedup = {}
        self._sequence = 0
        self._supervisor = self._sender = None
        self._view = {"state":"盘中监控未启用","items":{}}

    def start(self):
        if not settings.intraday_enabled:
            return
        if not (settings.apca_api_key_id.get_secret_value().strip() and settings.apca_api_secret_key.get_secret_value().strip()):
            self._view = {"state":"请在服务器配置 Alpaca Key ID 和 Secret","items":{}}
            logger.warning(self._view["state"])
            return
        if not os.access(settings.intraday_binary, os.X_OK):
            self._view = {"state":"Go 行情程序尚未构建，请执行部署命令","items":{}}
            logger.warning(self._view["state"])
            return
        self._stop.clear()
        self._supervisor = Thread(target=self._supervise, name="intraday-supervisor", daemon=True)
        self._supervisor.start()

    def shutdown(self):
        self._stop.set()
        if self._supervisor:
            self._supervisor.join(timeout=8)

    def read(self):
        view = self._view
        if view.get("at") and now()-datetime.fromisoformat(view["at"]) > timedelta(seconds=10):
            return {**view, "state":"盘中行情快照待更新", "items":{}, "total":None, "benchmarks":{}}
        return view

    def _launch(self, trading, symbols, policy):
        env = {**os.environ,"APCA_API_KEY_ID":settings.apca_api_key_id.get_secret_value(),
               "APCA_API_SECRET_KEY":settings.apca_api_secret_key.get_secret_value()}
        args = [str(settings.intraday_binary.resolve()),"--open",trading.opened.isoformat(),"--close",trading.closed.isoformat(),
                "--symbols",",".join(symbols),"--confirmation",str(policy.confirmation_minutes),
                "--opening",str(policy.opening_minutes),
                "--history",",".join(value.isoformat() for value in trading.historical_opens)]
        process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, encoding="utf-8", env=env)
        self._process = process

        def read():
            for line in process.stdout:
                try:
                    self._raw = process, json.loads(line)
                except ValueError:
                    logger.warning("Go 行情快照无法解析")

        self._reader = Thread(target=read, name="intraday-stream", daemon=True)
        self._reader.start()
        logger.info("启动 Alpaca IEX 盘中行情，订阅 %s 个代码", len(symbols))

    def _disconnect(self):
        self._delivery_state = None
        process = self._process
        self._process = None
        if process is None:
            return
        process.stdin.close()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        self._reader.join(timeout=1)
        process.stdout.close()
        self._raw = None

    def _supervise(self):
        settings.storage.mkdir(parents=True, exist_ok=True)
        with (settings.storage / "intraday.lock").open("a+b") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                self._view = {"state":"已有盘中监控进程","items":{}}
                return
            try:
                policy = Policy.model_validate_json(settings.intraday_policy_file.read_text(encoding="utf-8"))
                if policy.beijing_push_cutoff.tzinfo is not None:
                    raise ValueError("截止时间应为北京时间时分")
            except (OSError, ValueError):
                self._view = {"state":"盘中监控配置无效","items":{}}
                logger.error("请检查 INTRADAY_POLICY_FILE 的盘中监控配置")
                return
            self._sender = Thread(target=self._deliver, name="intraday-feishu", daemon=True)
            self._sender.start()
            try:
                self._loop(policy)
            finally:
                self._disconnect()

    def _inputs(self, trading, cached):
        with Session() as session, localcontext() as precision:
            precision.prec = 48
            held = session.scalars(select(ManualPosition).where(ManualPosition.active.is_(True),
                                   ManualPosition.currency == "USD")).all()
            ordered = sorted(held, key=lambda p: (-D(p.quantity)*D(p.average_cost), p.symbol))
            positions = {p.symbol:p for p in ordered}
            symbols = tuple(dict.fromkeys((*BENCHMARKS, *positions)))[:30]
            cfg = risk.policy()
            refs = {}
            for code in symbols:
                position = positions.get(code)
                version = position.version if position else None
                cached_ref = cached.get(code)
                if cached_ref and cached_ref[0] == version and cached_ref[1]["previous_close"] is not None:
                    refs[code] = cached_ref
                else:
                    item = risk.asset(session, code, trading.previous, cfg)
                    refs[code] = version, rules.references(session, position, item, trading.previous, trading.day.isoformat())
            active_plans = select(PositionPlan).where(PositionPlan.symbol.in_(symbols), PositionPlan.status=="active",
                                                     PositionPlan.expires_at>stamp())
            plans = {row.symbol:row for row in session.scalars(active_plans) if not row.payload.get("override")}
        return positions, symbols, refs, plans

    def _loop(self, policy):
        day = trading = None
        symbols = subscribed = ()
        refs, positions, plans = {}, {}, {}
        reload_at, restart_at, processed = now(), now(), None
        while not self._stop.is_set():
            current = now()
            session_status = {}
            try:
                eastern_day = current.astimezone(NEW_YORK).date()
                if eastern_day != day:
                    trading = _trading_session(eastern_day, policy)
                    day, refs = eastern_day, {}
                    self._dedup.clear()
                    reload_at = current
                if (trading and trading.opened-timedelta(minutes=2) <= current < trading.closed and current >= reload_at):
                    positions, symbols, refs, plans = self._inputs(trading, refs)
                    reload_at = current + timedelta(seconds=20)
                if not trading or not trading.opened <= current < trading.closed:
                    if self._process:
                        self._disconnect()
                    self._view = {"state":"等待美股常规开盘","items":{}}
                else:
                    session_status = {"session":trading.day.isoformat(), "items":{},
                                      "push_active":settings.intraday_feishu_enabled and current < trading.cutoff}
                    if self._process and (self._process.poll() is not None or subscribed != symbols):
                        self._disconnect()
                        restart_at = current + timedelta(seconds=3)
                        self._view = {**session_status, "state":"正在重新订阅 Alpaca 行情"}
                    if not self._process and current >= restart_at:
                        self._launch(trading, symbols, policy)
                        subscribed, processed = symbols, None
                        self._view = {**session_status, "state":"正在连接 Alpaca"}
                    raw = self._raw
                    if raw and raw[0] is self._process and raw[1]["at"] != processed:
                        processed = raw[1]["at"]
                        self._handle(trading, positions, {code:ref for code, (_,ref) in refs.items()}, plans, raw[1], policy)
            except Exception:
                # 行情线程异常不能影响盘后后台，也不能继续使用旧快照发送。
                logger.exception("盘中监控处理失败")
                self._disconnect()
                self._view = {"items":{}, **session_status, "state":"盘中监控暂不可用，等待重连"}
                reload_at = restart_at = now() + timedelta(seconds=20)
                if self._stop.wait(5):
                    break
            self._stop.wait(1)

    def _handle(self, trading, positions, refs, plans, frame, policy):
        current = now()
        prices, scale = rules.usable_prices(frame, refs, current, policy, trading.opened, trading.closed)
        stable_prices = {symbol:value for symbol,value in prices.items() if symbol not in scale}
        live = frame["symbols"]
        mode = rules.market_mode(frame, refs, stable_prices) if frame["backfill"] else "待确认"
        items = {}
        values, day_changes = {}, []
        total_cost = D(0)
        for code, position in positions.items():
            if code not in prices:
                continue
            price = prices[code]
            last, tape = live[code]["last"], live[code]
            quantity = D(position.quantity)
            value = quantity * price
            basis = quantity * D(position.average_cost)
            change = value - basis
            if code not in scale:
                values[code] = value
                total_cost += basis
            item = {"version":position.version,"price":str(price),"at":last["at"],
                           "market_value":risk.cash(value) if code not in scale else None,
                           "pnl":risk.cash(change) if code not in scale else None,
                           "unrealized_return":str(change / basis) if code not in scale else None,
                           "vwap":tape.get("vwap"),"volume":tape["volume"],"relative_volume":tape.get("relative_volume"),
                           "ema20":str(refs[code]["levels"]["EMA20"]) if "EMA20" in refs[code]["levels"] else None,
                           "support":list(map(str,refs[code]["support"])) if refs[code]["support"] else None,
                           "resistance":list(map(str,refs[code]["resistance"])) if refs[code]["resistance"] else None,
                           "trend":refs[code]["trend"],"scale_review":code in scale}
            previous = refs[code]["previous_close"]
            if code not in scale and previous is not None:
                day_pnl, day_return = day_move(price, previous, quantity)
                day_changes.append(day_pnl)
                item.update(day_pnl=str(day_pnl),day_return=str(day_return))
            items[code] = item
        total = None
        if positions and len(values) == len(positions):
            market_value = sum(values.values())
            pnl = market_value - total_cost
            total = {"market_value":risk.cash(market_value), "pnl":risk.cash(pnl),
                     "unrealized_return":str(pnl / total_cost)}
            if len(day_changes) == len(positions):
                total["day_pnl"] = str(sum(day_changes))
            for code, value in values.items():
                items[code]["weight"] = str(value / market_value)
        view = {"at":frame["at"],"state":"IEX 行情已连接" if frame["ready"] else frame["state"],"items":items,
                      "session":trading.day.isoformat(),"cutoff_at":trading.cutoff.isoformat(),
                      "push_active":settings.intraday_feishu_enabled and current<trading.cutoff,
                      "excluded":[code for code in positions if code not in live],"total":total,"market":mode,
                      "benchmarks":{code:{"price":str(prices[code]),"at":live[code]["last"]["at"]}
                                    for code in BENCHMARKS if code in prices}}
        ordinary = frame["backfill"] and current >= trading.opened + timedelta(minutes=policy.opening_minutes)
        events = []
        if ordinary and mode == "同步走弱":
            events.append(_event("market", "QQQ / SPY", "两只基准均收在各自 VWAP 和关键支撑下方。暂停普通加仓提示。","joint"))
        for code, price in prices.items():
            position = positions.get(code)
            if position is None:
                continue
            row, ref = live[code], refs[code]
            meta = {"version":position.version, "price":price, "observed_at":row["last"]["at"]}
            if code in scale:
                events.append(_event("scale", code, "盘中成交与前收盘差异较大，请核对是否跳空、拆股或报价口径异常。",
                                     "scale", **meta))
                continue
            updated = datetime.fromisoformat(position.updated_at)
            observations = [sample for sample in (row["last"], row.get("recent_low"))
                            if rules.fresh(sample, current, policy.quote_max_age_seconds, trading.opened, trading.closed)
                            and datetime.fromisoformat(sample["at"]) >= updated]
            observed = min(observations,key=lambda sample: sample["price"],default=None)
            touched = [level for level in ref["stops"] if observed and D(str(observed["price"])) <= level]
            if touched:
                stop = touched[-1]
                events.append(_event("stop", code, f"有效成交触及冻结保护位 {_price(stop)} USD。"
                                     "请核对券商成交和剩余仓位。", str(stop),
                                     **{**meta,"price":D(str(observed["price"])),"observed_at":observed["at"]}))
                continue
            if not ordinary:
                continue
            broken = rules.break_state(row, ref, price, policy)
            if broken:
                confirmed, levels, (name, level) = broken
                kind = "reduce" if confirmed else "watch"
                note = ("连续两根完整 K 线收在破位缓冲下方" if confirmed else "盘中价已越过缓冲，等待完整 K 线确认")
                anchors = " / ".join(f"{label} {_price(value)}" for label, value in levels)
                events.append(_event(kind, code, f"{anchors} USD · {note}\n"+_risk_text(row, code, refs, prices, mode),
                                     f"{name}:{level}", **meta))
                continue
            profit_target = policy.take_profit_targets.get(code) if price > D(position.average_cost) else None
            structure = rules.structure_state(row, ref, price, policy, profit_target)
            if structure and datetime.fromisoformat(row["latest"]["t"]) >= updated:
                kind, levels, (name, level) = structure
                anchors = " / ".join(f"{label} {_price(value)}" for label, value in levels)
                events.append(_event(kind, code, f"{anchors} USD · {STRUCTURE_NOTES[kind]}\n"+
                                     _risk_text(row, code, refs, prices, mode), f"{name}:{level}", **meta))
                if kind == "profit":
                    continue
            plan = plans.get(code)
            if plan:
                for stage in plan.payload["stages"]:
                    if stage["id"] in ("left","right") and stage["remaining"]>0 and rules.add_stage(row,ref,price,stage,mode,policy):
                        note = "左侧回踩后收回触发位" if stage["id"]=="left" else "右侧突破后回踩守住触发位"
                        events.append(_event("add", code, note+"\n"+_risk_text(row,code,refs,prices,mode),
                                             f"{plan.id}:{stage['id']}", plan_id=plan.id, plan_version=plan.version,
                                             stage=stage["id"], **meta))
        for event in events:
            if event["symbol"] in items:
                label = "加仓形态满足，待风控复核" if event["kind"] == "add" else LABELS[event["kind"]]
                items[event["symbol"]]["signal"] = label
        self._view = view
        self._delivery_state = trading, frame, stable_prices, mode, {_signal_key(event) for event in events}
        if not view["push_active"] or not frame["ready"]:
            return
        for event in events:
            self._enqueue(trading, frame, event)

    def _enqueue(self, trading, frame, event):
        event_id = digest([trading.day.isoformat(), event["kind"],event["symbol"],event["identity"]])[:32]
        bucket = datetime.fromisoformat(frame["at"]).timestamp() // (300 if event["kind"]=="add" else 30)
        if self._dedup.get(event_id) == bucket or (settings.storage / FOLDER / f"{event_id}.json").exists():
            return
        self._sequence += 1
        try:
            self._queue.put_nowait((PRIORITY[event["kind"]],self._sequence,(trading,event_id,frame["at"],event)))
        except Full:
            return
        self._dedup[event_id] = bucket

    def _deliver(self):
        while not self._stop.is_set():
            try:
                _, _, request = self._queue.get(timeout=1)
            except Empty:
                continue
            try:
                self._send(*request)
            except Exception:
                logger.exception("盘中提醒归档或风险复核失败")
            self._stop.wait(1)

    def _send(self, trading, event_id, at, event):
        reviewed_at = None

        def timely():
            current = now()
            live = self._delivery_state
            return (not self._stop.is_set() and live and live[0] == trading and trading.opened <= current < trading.cutoff
                    and current-datetime.fromisoformat(at) <= timedelta(seconds=20) and live[1]["ready"]
                    and current-datetime.fromisoformat(live[1]["at"]) <= timedelta(seconds=10)
                    and (reviewed_at is None or live[1]["at"] == reviewed_at)
                    and (event["kind"] == "stop" or _signal_key(event) in live[4]))

        if not timely():
            return
        folder = settings.storage / FOLDER
        path = folder / f"{event_id}.json"
        if path.exists():
            return
        kind, symbol = event["kind"], event["symbol"]
        if kind != "market":
            with Session() as session:
                position = session.scalar(select(ManualPosition).where(ManualPosition.symbol==symbol,
                                                                      ManualPosition.active.is_(True)))
                if position is None or position.version != event["version"]:
                    return
        stage = None
        live = self._delivery_state
        if not live or live[0] != trading:
            return
        if kind == "add":
            if live[3] in ("待确认","同步走弱") or symbol not in live[2]:
                return
            with localcontext() as precision:
                precision.prec = 48
                stage = rules.review_add(event, live[2])
            price = live[2][symbol]
            if not stage or not D(stage["min_price"]) <= price <= D(stage["max_price"]):
                return
            reviewed_at = live[1]["at"]
            event = {**event, "price":price,"observed_at":live[1]["symbols"][symbol]["last"]["at"]}
        try:
            if not feishu.destination(required=False):
                return
        except ValueError:
            logger.warning("盘中飞书提醒的机器人配置无效")
            return
        if not timely():
            return
        folder.mkdir(parents=True, exist_ok=True)
        record = {"created_at":stamp(),"status":"SENDING","card":_card(event, now(), stage)}
        _write(path, record)
        try:
            # 归档落盘和风控计算也可能跨过凌晨 2 点，真正发送前再检查一次。
            if not timely():
                record.update(status="SKIPPED")
                return
            accepted = feishu.send(record["card"],allowed=timely)
            record.update(status="SENT" if accepted else "SKIPPED")
        except feishu.DeliveryFailure as exc:
            record.update(status=exc.status,error=str(exc))
        except ValueError as exc:
            record.update(status="FAILED",error=str(exc))
        except Exception:
            record.update(status="UNKNOWN",error="发送结果无法确认，请核对飞书群消息")
        finally:
            if record["status"] == "SKIPPED":
                path.unlink(missing_ok=True)
            else:
                _write(path, {**record,"finished_at":stamp()})


monitor = Monitor()
