"""Record raw AC1 motion fields for axis/sign calibration."""
import argparse
import csv
from datetime import datetime
from pathlib import Path
import signal
import time

from acbridge.shared_memory import ACReader, BusyFrame, MissingGame, utf16


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    parser.add_argument("--hz", type=float, default=60.0)
    args = parser.parse_args()
    output = args.output or Path("tmp") / f"motion-{datetime.now():%Y%m%d-%H%M%S}.csv"
    output.parent.mkdir(parents=True, exist_ok=True)
    stop = False

    def request_stop(_signum, _frame):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    fields = [
        "elapsed_s", "physics_packet", "graphics_packet", "status", "car", "speed_kmh",
        "gas", "brake", "clutch", "steer_angle",
        "acc_x_g", "acc_y_g", "acc_z_g",
        "local_velocity_x", "local_velocity_y", "local_velocity_z",
        "local_angular_x", "local_angular_y", "local_angular_z",
        "heading", "pitch", "roll", "position_x", "position_y", "position_z",
        "suspension_fl", "suspension_fr", "suspension_rl", "suspension_rr",
    ]
    reader = ACReader()
    started = time.monotonic()
    next_tick = started
    rows = 0
    ready = False
    try:
        with output.open("w", newline="", encoding="utf-8-sig") as stream:
            writer = csv.writer(stream)
            writer.writerow(fields)
            while not stop:
                try:
                    p, g, s = reader.read()
                except BusyFrame:
                    continue
                except MissingGame:
                    reader.close()
                    time.sleep(0.25)
                    continue
                now = time.monotonic()
                if not ready and g.status == 2:
                    ready = True
                    print(f"READY {output.resolve()}", flush=True)
                writer.writerow([
                    now - started, p.packetId, g.packetId, g.status, utf16(s.carModel), p.speedKmh,
                    p.gas, p.brake, p.clutch, p.steerAngle,
                    *p.accG, *p.localVelocity, *p.localAngularVelocity,
                    p.heading, p.pitch, p.roll, *g.carCoordinates, *p.suspensionTravel,
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
