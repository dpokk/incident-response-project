"""Run the demo:  python -m demo   →  http://127.0.0.1:8800"""
import os
import sys
import webbrowser
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
# Demo-only defaults, set before Settings is read; any value already set in .env or the shell wins.
os.environ.setdefault("EXECUTION_POLICY_PATH", str(HERE / "execution_policy.json"))
os.environ.setdefault("INVESTIGATE_DELAY_S", "20")


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    import uvicorn

    from investigator.config import Settings

    from .bus import EventBus
    from .engine import Engine
    from .server import create_app

    def log(msg: str) -> None:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

    settings = Settings()
    bus = EventBus()
    engine = Engine(settings, bus, log)
    port = int(os.getenv("DEMO_PORT", "8800"))
    if "--no-browser" not in sys.argv:
        webbrowser.open(f"http://127.0.0.1:{port}")
    uvicorn.run(create_app(engine, bus), host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
