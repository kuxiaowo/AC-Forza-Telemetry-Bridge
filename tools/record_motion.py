"""Record raw AC1/ACC fields for controlled axis/sign and unit calibration.

Run from the repository: python -m tools.record_motion --game acc --seconds 60
"""
import argparse
import csv
from datetime import datetime
from pathlib import Path
import signal
import time

from acbridge.shared_memory import ACReader, BusyFrame, utf16
from acbridge.acc import player_position


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--game", choices=("ac", "acc"), default="ac")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--hz", type=float, default=60.0)
    parser.add_argument("--seconds", type=float, default=0, help="0 表示持续记录")
    args = parser.parse_args()
    if not 1 <= args.hz <= 240 or not 0 <= args.seconds < float("inf"):
        parser.error("hz 必须在 1～240；seconds 必须是非负有限数")
    output = args.output or Path("tmp") / f"motion-{args.game}-{datetime.now():%Y%m%d-%H%M%S}.csv"
    output.parent.mkdir(parents=True, exist_ok=True)
    stop = False

    def request_stop(_signum, _frame):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    fields = [
        "elapsed_s", "game", "sm_version", "game_version", "physics_packet", "graphics_packet", "status", "car", "speed_kmh",
        "gas", "brake", "clutch", "steer_angle",
        "fuel_raw", "max_fuel_raw", "turbo_boost_raw",
        "acc_x_g", "acc_y_g", "acc_z_g",
        "local_velocity_x", "local_velocity_y", "local_velocity_z",
        "local_angular_x", "local_angular_y", "local_angular_z",
        "heading", "pitch", "roll", "position_x", "position_y", "position_z",
        "suspension_fl", "suspension_fr", "suspension_rl", "suspension_rr",
    ]
    reader = ACReader(game=args.game)
    started = time.monotonic()
    next_tick = started
    rows = 0
    ready = False
    try:
        with output.open("w", newline="", encoding="utf-8-sig") as stream:
            writer = csv.writer(stream)
            writer.writerow(fields)
            while not stop:
                if args.seconds > 0 and time.monotonic() - started >= args.seconds:
                    break
                try:
                    p, g, s = reader.read()
                except BusyFrame:
                    time.sleep(1 / args.hz)
                    continue
                except OSError:
                    reader.close()
                    time.sleep(0.25)
                    continue
                now = time.monotonic()
                if not ready and g.status == 2:
                    ready = True
                    print(f"READY {output.resolve()}", flush=True)
                position = player_position(g) if args.game == "acc" else tuple(g.carCoordinates)
                writer.writerow([
                    now - started, args.game, utf16(s.smVersion), utf16(s.acVersion),
                    p.packetId, g.packetId, g.status, utf16(s.carModel), p.speedKmh,
                    p.gas, p.brake, p.clutch, p.steerAngle,
                    p.fuel, s.maxFuel, p.turboBoost,
                    *p.accG, *p.localVelocity, *p.localAngularVelocity,
                    p.heading, p.pitch, p.roll, *(position if position is not None else ("",) * 3), *p.suspensionTravel,
                ])
                rows += 1
                if rows % max(1, round(args.hz)) == 0:
                    stream.flush()
                next_tick += 1 / args.hz
                if next_tick < time.monotonic():
                    next_tick = time.monotonic()
                time.sleep(max(0.0, next_tick - time.monotonic()))
    finally:
        reader.close()
        print(f"STOPPED rows={rows} output={output.resolve()}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
