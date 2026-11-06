"""直接调用 ticker.history；不手动处理 session、cookie 或 crumb。"""

import argparse
import logging
import sys
from pathlib import Path
from time import perf_counter

import yfinance as yf


def main():
    parser = argparse.ArgumentParser(description="直接用 yfinance 请求少量日线")
    parser.add_argument("symbol", nargs="?", default="AAPL", help="股票代码，默认 AAPL")
    args = parser.parse_args()
    symbol = args.symbol.strip().upper()
    print(f"yfinance {yf.__version__}；股票 {symbol}；最近5个交易日日线", flush=True)
    # 仅指定 SDK 缓存目录，不注入凭据；库内部是否自动使用 Cookie 由其实现决定。
    yf.set_tz_cache_location(str(Path(__file__).resolve().parents[1] / "data" / "yf"))
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    started = perf_counter()
    print("直接调用 history，不传入 session、cookie 或 crumb。", flush=True)
    try:
        ticker = yf.Ticker(symbol)
        frame = ticker.history(period="5d", interval="1d", auto_adjust=False,
                               timeout=15, raise_errors=True)
    except Exception as exc:
        print(f"获取失败：{type(exc).__name__}；总耗时 {perf_counter() - started:.2f}秒",
              file=sys.stderr, flush=True)
        return 1
    if frame.empty:
        print("请求结束，但没有返回日线数据。", file=sys.stderr)
        return 1
    print(f"获取成功：{len(frame)}条；总耗时 {perf_counter() - started:.2f}秒", flush=True)
    print(frame[["Open", "High", "Low", "Close", "Volume"]].tail().to_string())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
