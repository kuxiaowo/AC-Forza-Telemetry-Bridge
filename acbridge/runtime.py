from dataclasses import asdict
import ctypes as C
from ctypes import wintypes as W
import ipaddress
import json
import logging
from logging.handlers import RotatingFileHandler
import math
import os
from pathlib import Path
import socket
import threading
import time

from .vehicles import VehicleStore
from .shared_memory import ACReader, MissingGame, BusyFrame, utf16
from .telemetry import Frame, from_ac, encode, demo_frame

DEFAULTS = dict(host="127.0.0.1", port=8094, hz=60, packet_format="fh4",
                stale_seconds=1.5, invert_clutch=True, invert_steer=False,
                steer_scale=1.0, fallback_max_rpm=9000)


def validate(config):
    result = {**DEFAULTS, **config}
    try:
        ipaddress.IPv4Address(result["host"])
    except (ValueError, TypeError):
        raise ValueError("目标地址必须是 IPv4 地址，例如 127.0.0.1。") from None
    for name, low, high in (("port", 1, 65535), ("hz", 1, 240), ("fallback_max_rpm", 100, 100000)):
        value = result[name]
        if isinstance(value, bool) or int(value) != float(value) or not low <= int(value) <= high:
            raise ValueError(f"{name} 必须是 {low}～{high} 的整数。")
        result[name] = int(value)
    for name, low, high in (("stale_seconds", .5, 10), ("steer_scale", .01, 100)):
        value = float(result[name])
        if not math.isfinite(value) or not low <= value <= high:
            raise ValueError(f"{name} 必须在 {low}～{high} 之间。")
        result[name] = value
    for name in ("invert_clutch", "invert_steer"):
        if not isinstance(result[name], bool):
            raise ValueError(f"{name} 必须为 true 或 false。")
    if result["packet_format"] not in ("fh4", "fh5"):
        raise ValueError("packet_format 必须为 fh4 或 fh5。")
    return result


def load_config(path):
    if not path.exists():
        return dict(DEFAULTS)
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("配置必须是 JSON 对象。")
    return validate(data)


def save_config(path, config):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(validate(config), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def setup_logging(base):
    directory = base / "logs"
    directory.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("acbridge")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = RotatingFileHandler(directory / "bridge.log", maxBytes=1024*1024, backupCount=2, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    return logger


class PortGuard:
    """Prevent two bridge processes from interleaving packets at one destination."""
    def __init__(self, host, port):
        self.handle = None
        if os.name == "nt":
            self.dll = C.WinDLL("kernel32", use_last_error=True)
            self.dll.CreateMutexW.argtypes = [C.c_void_p, W.BOOL, W.LPCWSTR]
            self.dll.CreateMutexW.restype = W.HANDLE
            self.dll.CloseHandle.argtypes = [W.HANDLE]
            self.dll.CloseHandle.restype = W.BOOL
            self.handle = self.dll.CreateMutexW(None, False, f"Local\\ACForzaBridge_{host}_{port}")
            if not self.handle:
                raise C.WinError(C.get_last_error())
            if C.get_last_error() == 183:
                self.close()
                raise OSError("另一个适配器正在向此地址和端口发送数据，请先停止它。")

    def close(self):
        if self.handle:
            self.dll.CloseHandle(self.handle)
            self.handle = None


class Bridge:
    def __init__(self, config, simulated=False, reader=None, base=None):
        self.base = Path(base) if base is not None else None
        self.learning_requested = threading.Event()
        self.pending_learning = None
        self.learning_check_at = 0.
        self.vehicles = VehicleStore(Path(base) / "vehicles" if base is not None else None)
        self.config = validate(config)
        self.simulated = simulated
        self.reader = reader if reader is not None else ACReader()
        self.stop_event = threading.Event()
        self.thread = None
        self.lock = threading.Lock()
        self.snapshot = dict(status="未启动", running=False, sent=0, actual_hz=0., frame=asdict(Frame()), error="")
        self.logger = logging.getLogger("acbridge")

    def get_snapshot(self):
        with self.lock:
            return dict(self.snapshot)

    def publish(self, **kwargs):
        with self.lock:
            self.snapshot.update(kwargs)

    def start(self):
        if self.thread and self.thread.is_alive():
            raise RuntimeError("适配器已在运行")
        self.stop_event.clear()
        self.thread = threading.Thread(target=self.run, name="ac-telemetry", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()

    def learn(self):
        snap=self.get_snapshot()
        if self.simulated or not snap['running'] or not snap['frame']['car'] or snap.get('learning'):
            raise ValueError('请先开始适配并连接真实 AC 车辆；不能在模拟模式或学习期间再次启动。')
        self.publish(learning=True,learning_result='准备读取当前车辆曲线…')
        self.learning_requested.set()

    def run(self):
        sock = guard = None
        cfg = self.config
        target = (cfg["host"], cfg["port"])
        sent = 0
        start = tick = rate_start = time.monotonic()
        rate_count = 0
        actual_hz = 0.
        last_ids = None
        last_change = start
        reopen_at = 0.
        last_status = None
        identity = None
        active_elapsed = 0.
        last_tick = start
        was_active = False
        frame = Frame()
        self.publish(running=True, error="")
        try:
            guard = PortGuard(*target)
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(.2)
            while not self.stop_event.is_set():
                now = time.monotonic()
                if self.learning_requested.is_set():
                    self.learning_requested.clear()
                    try:
                        from .learning import run_learning
                        result=run_learning(self,sock,target,frame)
                    except Exception as exc:
                        result=f'学习未完成：{exc}'
                        self.logger.exception('动力学习未完成')
                    self.publish(learning=False,learning_result=result)
                    last_ids=None
                    last_change=tick=last_tick=time.monotonic()
                    was_active=False
                    continue
                if self.simulated:
                    frame = demo_frame()
                elif now < reopen_at:
                    frame = Frame(status="等待游戏重新连接")
                else:
                    try:
                        p, g, s = self.reader.read()
                        version = utf16(s.acVersion)
                        if not version.startswith("1.") or not utf16(s.carModel):
                            raise MissingGame("等待原版 AC 的有效车辆数据（仅支持 AC1）")
                        ids = (p.packetId, g.packetId)
                        if ids != last_ids:
                            last_ids, last_change = ids, now
                        if now - last_change > cfg["stale_seconds"]:
                            raise MissingGame("游戏数据已停止更新")
                        frame = from_ac(p, g, s, cfg, self.vehicles)
                        new_identity = (frame.car, frame.track, g.session)
                        if new_identity != identity:
                            # Separate datasets when changing cars / tracks, even during a live session.
                            if identity is not None:
                                sock.sendto(encode(Frame(), int(now*1000), cfg["packet_format"]), target)
                            identity, active_elapsed = new_identity, 0.
                    except BusyFrame:
                        self.stop_event.wait(1/cfg["hz"])
                        continue
                    except MissingGame as exc:
                        frame = Frame(status=str(exc))
                        self.reader.close()
                        reopen_at = now + .5
                    except OSError as exc:
                        frame = Frame(status="共享内存读取失败")
                        self.publish(error=str(exc))
                        self.reader.close()
                        reopen_at = now + 1.
                        self.logger.warning("读取失败: %s", exc)
                dt = min(.2, max(0., now-last_tick))
                if frame.active and was_active:
                    active_elapsed += dt
                was_active, last_tick = frame.active, now
                # Inactive packets zero driving values, while UI retains connection metadata.
                outgoing = frame if frame.active else Frame(car=frame.car, max_rpm=frame.max_rpm)
                # Stable across bridge restarts/mode switches: the receiver treats a
                # decreasing timestamp as a game restart and stops listening.
                sock.sendto(encode(outgoing, int(now*1000), cfg["packet_format"], active_elapsed), target)
                sent += 1
                rate_count += 1
                if now - rate_start >= 1:
                    actual_hz = rate_count / (now-rate_start)
                    rate_start, rate_count = now, 0
                if frame.status != last_status:
                    self.logger.info("%s; target=%s:%d; format=%s", frame.status, *target, cfg["packet_format"])
                    last_status = frame.status
                self.publish(status=frame.status, frame=asdict(frame), sent=sent, actual_hz=actual_hz,
                             error="" if frame.active else self.get_snapshot()["error"])
                tick += 1/cfg["hz"]
                if tick < time.monotonic():
                    tick = time.monotonic()
                self.stop_event.wait(max(0., tick-time.monotonic()))
        except Exception as exc:
            self.logger.exception("发送线程停止")
            self.publish(status="运行失败", error=str(exc))
        finally:
            if sock:
                try:
                    for _ in range(3):
                        sock.sendto(encode(Frame(), int(time.monotonic()*1000), cfg["packet_format"]), target)
                except OSError:
                    pass
                sock.close()
            self.reader.close()
            if guard:
                guard.close()
            self.publish(running=False, actual_hz=0.)
            if self.stop_event.is_set():
                self.publish(status="已停止", frame=asdict(Frame()))
