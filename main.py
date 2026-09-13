import argparse
import json
import os
from pathlib import Path
import sys
import time

from acbridge.runtime import Bridge, load_config, setup_logging


def main():
    parser = argparse.ArgumentParser(description="AC1 → Forza Horizon UDP bridge")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--simulate", action="store_true", help="仅用于显式模拟测试")
    parser.add_argument("--seconds", type=float, default=0, help="无界面模式运行秒数，0 为持续运行")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--snapshot", type=Path, help="无界面模式每秒更新一次诊断 JSON")
    args = parser.parse_args()
    base = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
    setup_logging(base)
    if args.headless:
        bridge = Bridge(load_config(args.config or base / "config.json"), args.simulate, base=base)
        bridge.start()
        started = time.monotonic()
        try:
            while bridge.thread.is_alive():
                time.sleep(.25)
                snap = bridge.get_snapshot()
                if args.snapshot:
                    args.snapshot.parent.mkdir(parents=True, exist_ok=True)
                    temp = args.snapshot.with_suffix(".tmp")
                    temp.write_text(json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")
                    temp.replace(args.snapshot)
                if args.seconds > 0 and time.monotonic()-started >= args.seconds:
                    break
        except KeyboardInterrupt:
            pass
        finally:
            bridge.stop()
            bridge.thread.join(3)
        if sys.stdout:
            print(json.dumps(bridge.get_snapshot(), ensure_ascii=True))
        return 1 if bridge.get_snapshot()["error"] else 0
    import tkinter as tk
    from acbridge.gui import App
    if os.name == "nt":
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (OSError, AttributeError):
            pass
    root = tk.Tk()
    app = App(root, base)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
