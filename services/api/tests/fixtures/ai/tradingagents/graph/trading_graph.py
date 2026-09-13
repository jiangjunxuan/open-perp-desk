import os
import signal
import time
from pathlib import Path


class TradingAgentsGraph:
    def __init__(self, selected_analysts, debug, config):
        self.config = config
        assert selected_analysts == ("market", "social", "news")
        assert debug is False
        print("Third-party output must not contaminate the JSON channel.")

    def resolve_instrument_context(self, ticker, asset_type="stock"):
        return f"base context: {ticker}, {asset_type}"

    def propagate(self, ticker, trade_date, asset_type):
        mode = self.config["deep_think_llm"]
        Path(self.config["results_dir"], "pid").write_text(str(os.getpid()))
        if mode == "descendant":
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            child = os.fork()
            if child == 0:
                time.sleep(60)
                os._exit(0)
            Path(self.config["results_dir"], "descendant").write_text(str(child))
            time.sleep(60)
        if mode == "sleep":
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            time.sleep(60)
        if mode == "crash":
            os._exit(31)
        if mode == "error":
            raise RuntimeError("sensitive-provider-token-and-url")
        if mode == "large":
            return {"large": "x" * (1024 * 1024)}, "Hold"
        return {
            "ticker": ticker, "trade_date": trade_date, "asset_type": asset_type,
            "config": self.config, "pid": os.getpid(), "home": os.environ["HOME"],
            "environment": dict(os.environ),
            "context": self.resolve_instrument_context(ticker, asset_type),
        }, "Hold"
