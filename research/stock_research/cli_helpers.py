"""命令行只读检查、输入解析和防覆盖输出；不触发行情/研究请求。"""
import json
from pathlib import Path
import re
import sys


def normalize_symbol(value):
    symbol = value.strip().upper().removeprefix("$")
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9.\-]{0,14}", symbol):
        raise ValueError("请输入有效美股代码，例如GOOG、NVDA或BRK-B")
    return symbol


def storage_ready(*tables):
    from sqlalchemy import inspect
    from .store import engine
    # 预览/查询不能为不存在的SQLite库创建空文件，更不能顺便迁移表。
    if engine.dialect.name == "sqlite":
        database = engine.url.database
        if database and database != ":memory:" and not Path(database).is_file():
            return False
    inspector = inspect(engine)
    return all(inspector.has_table(name) for name in tables)


def parse_input(file=None):
    """可粘贴JSON或读取文件/管道；只移除一层完整Markdown围栏，不猜正文中的JSON。"""
    limit = 1_000_000
    if file and str(file) != "-":
        with Path(file).expanduser().open("rb") as stream:
            raw = stream.read(limit + 1)
    elif sys.stdin.isatty():
        print("请粘贴GPT返回的JSON，另起一行输入END结束（也可Ctrl+D）：", file=sys.stderr, flush=True)
        chunks = []; size = 0
        while True:
            line = sys.stdin.readline()
            if not line or line.strip() == "END":
                break
            encoded = line.encode("utf-8")
            size += len(encoded)
            if size > limit:
                raise ValueError("输入JSON不能超过1MB")
            chunks.append(encoded)
        raw = b"".join(chunks)
    else:
        raw = sys.stdin.buffer.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("输入JSON不能超过1MB")
    try:
        content = raw.decode("utf-8-sig").strip()
        if content.startswith("```"):
            lines = content.splitlines()
            if len(lines) < 3 or lines[0].strip().lower() not in ("```", "```json") or lines[-1].strip() != "```":
                raise ValueError
            content = "\n".join(lines[1:-1])
        def pairs(values):
            result = {}
            for key, value in values:
                if key in result:
                    raise ValueError
                result[key] = value
            return result
        data = json.loads(content, object_pairs_hook=pairs)
        if not isinstance(data, dict):
            raise ValueError
        return data
    except (ValueError, UnicodeError):
        raise ValueError("输入需要一个有效UTF-8 JSON对象，不能含重复字段或额外说明；可保留外层```json围栏") from None


def output_path(value):
    if value is None:
        return None
    path = Path(value).expanduser().resolve()
    if path.exists():
        raise ValueError("输出文件已存在，请更换路径；不会覆盖已有文件")
    return path


def emit(content, path=None):
    if path:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8") as stream:
            stream.write(content)
    sys.stdout.write(content)
    sys.stdout.flush()
