import argparse
import asyncio
from dataclasses import fields
import json
import logging
from pathlib import Path
import tomllib
from .backtest import BacktestConfig, backtest
from .book import ResyncRequired
from .demo import demo_events
from .engine import Engine
from .signals import DetectorConfig
from .storage import Journal, read_events
from .security import is_loopback, private_http_logging


def main():
    parser = argparse.ArgumentParser(description="Crypto liquidity intelligence research service")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve")
    serve.add_argument("--mode", choices=["demo", "live", "replay"], default="demo")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8080)
    serve.add_argument(
        "--allow-unauthenticated-demo",
        action="store_true",
        help="Explicitly permit a token-free synthetic demo on loopback only",
    )
    serve.add_argument("--symbols", default="BTCUSDT")
    serve.add_argument("--venues", default="binance,bybit")
    serve.add_argument("--db", default="data/events.sqlite")
    serve.add_argument("--config")
    serve.add_argument(
        "--telegram", action="store_true", help="Explicitly enable external Telegram alert delivery"
    )
    serve.add_argument("--input")
    serve.add_argument("--speed", type=float, default=1)
    demo = commands.add_parser("demo")
    demo.add_argument("--output", default="data/demo.jsonl")
    demo.add_argument("--steps", type=int, default=300)
    replay = commands.add_parser("replay")
    replay.add_argument("input")
    replay.add_argument("--output", default="data/replay-state.json")
    bt = commands.add_parser("backtest")
    bt.add_argument("input")
    bt.add_argument("--output", default="data/backtest.json")
    bt.add_argument("--market", default="binance:BTCUSDT")
    bt.add_argument("--notional", type=float, default=1000)
    bt.add_argument("--latency-ms", type=int, default=250)
    bt.add_argument("--holding-ms", type=int, default=5000)
    bt.add_argument("--fee-bps", type=float, default=4)
    bt.add_argument("--slippage-bps", type=float, default=1)
    bt.add_argument("--imbalance-threshold", type=float, default=0.15)
    bt.add_argument("--allow-without-flow", action="store_true")
    probe = commands.add_parser("probe")
    probe.add_argument("--output", default="data/live-probe.json")
    export = commands.add_parser("export")
    export.add_argument("input")
    export.add_argument("--output", default="data/events.jsonl")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    private_http_logging()

    def save(path, value):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(value, indent=2, allow_nan=False), encoding="utf-8")

    if args.command == "serve":
        from aiohttp import web
        from .server import create_app

        config = DetectorConfig()
        if args.config:
            with open(args.config, "rb") as source:
                data = tomllib.load(source)
            allowed = {f.name for f in fields(DetectorConfig)}
            if set(data.get("signals", {})) - allowed:
                parser.error("unknown signal setting")
            config = DetectorConfig(**data.get("signals", {}))
        venues = tuple(args.venues.split(","))
        if set(venues) - {"binance", "bybit"}:
            parser.error("supported venues: binance,bybit")
        if args.mode == "replay" and not args.input:
            parser.error("--input is required for replay mode")
        if args.input and Path(args.input).resolve() == Path(args.db).resolve():
            parser.error("input and output database must differ")
        if args.allow_unauthenticated_demo and (args.mode != "demo" or not is_loopback(args.host)):
            parser.error("--allow-unauthenticated-demo requires --mode demo and a loopback --host")
        journal = Journal(args.db)
        engine = Engine(journal, config)
        try:
            web.run_app(
                create_app(
                    engine,
                    args.mode,
                    tuple(args.symbols.upper().split(",")),
                    venues,
                    args.telegram,
                    args.input,
                    args.speed,
                    args.allow_unauthenticated_demo,
                ),
                host=args.host,
                port=args.port,
                print=None,
            )
        finally:
            journal.close()
    elif args.command == "demo":
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as target:
            for event in demo_events(args.steps):
                target.write(json.dumps(event.record()) + "\n")
        print(f"Demo recording saved: {args.output}")
    elif args.command == "replay":
        engine, gaps = Engine(), 0
        for event in read_events(args.input):
            try:
                engine.handle(event)
            except ResyncRequired:
                gaps += 1
        state = engine.view()
        state["reconstruction_gaps"] = gaps
        save(args.output, state)
        print(f"Replayed {engine.event_count} events; reconstruction gaps: {gaps}")
    elif args.command == "backtest":
        config = BacktestConfig(
            args.market,
            args.notional,
            args.latency_ms,
            args.holding_ms,
            args.fee_bps,
            args.slippage_bps,
            args.imbalance_threshold,
            not args.allow_without_flow,
        )
        result = backtest(read_events(args.input), config)
        save(args.output, result)
        print(json.dumps({k: v for k, v in result.items() if k != "trades"}, indent=2))
    elif args.command == "probe":
        from .probe import probe_apis

        result = asyncio.run(probe_apis())
        save(args.output, result)
        print(json.dumps(result, indent=2))
    elif args.command == "export":
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as target:
            for event in read_events(args.input):
                target.write(json.dumps(event.record()) + "\n")


if __name__ == "__main__":
    main()
