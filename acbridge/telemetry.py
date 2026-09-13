from dataclasses import dataclass, field
import math
import struct
import zlib
from .shared_memory import utf16


def finite(value, default=0.0):
    value = float(value)
    return value if math.isfinite(value) else default


def clamp(value, low, high):
    return max(low, min(high, finite(value)))


def pedal(value):
    return int(clamp(value, 0, 1) * 255 + 0.5)


def gear_byte(ac_gear):
    # AC: 0 reverse, 1 neutral, 2 first. Forza: 0 reverse, 11 neutral, 1 first.
    if ac_gear == 0:
        return 0
    if ac_gear == 1:
        return 11
    return ac_gear - 1 if 2 <= ac_gear <= 11 else 11


@dataclass
class Frame:
    active: bool = False
    car: str = ""
    track: str = ""
    status: str = "等待游戏"
    speed_kmh: float = 0.0
    rpm: float = 0.0
    power_watts: float = 0.0
    torque_nm: float = 0.0
    max_rpm: float = 9000.0
    gear: int = 11
    throttle: float = 0.0
    brake: float = 0.0
    clutch: float = 0.0
    steer: float = 0.0
    fuel_fraction: float = 0.0
    fuel_litres: float = 0.0
    temps_c: tuple = (0., 0., 0., 0.)
    acc: tuple = (0., 0., 0.)
    velocity: tuple = (0., 0., 0.)
    angular: tuple = (0., 0., 0.)
    orientation: tuple = (0., 0., 0.)
    position: tuple = (0., 0., 0.)
    suspension: tuple = (0., 0., 0., 0.)
    normalized_suspension: tuple = (0., 0., 0., 0.)
    suspension_max_used: tuple = (0., 0., 0., 0.)
    suspension_sources: tuple = ("missing",) * 4
    wheel_speed: tuple = (0., 0., 0., 0.)
    best_lap: float = 0.0
    last_lap: float = 0.0
    current_lap: float = 0.0
    laps: int = 0
    race_position: int = 0
    distance: float = 0.0
    notes: list = field(default_factory=list)


def from_ac(p, g, s, config, vehicles=None):
    status = {0: "等待进入驾驶", 1: "回放（暂停发送驾驶数据）", 2: "已连接 AC", 3: "游戏已暂停"}.get(g.status, "未知游戏状态")
    notes = []
    maxrpm = float(s.maxRpm)
    if not 100 <= maxrpm <= 100000:
        maxrpm = config["fallback_max_rpm"]
        notes.append("最高转速缺失，使用配置中的仪表量程")
    if s.maxFuel <= 0:
        notes.append("油箱容量缺失，燃油比例以 0 占位")
    clutch = 1 - finite(p.clutch) if config["invert_clutch"] else finite(p.clutch)
    steer = finite(p.steerAngle) * config["steer_scale"] * (-1 if config["invert_steer"] else 1)
    fallback = vehicles.ranges(utf16(s.carModel)) if vehicles else (0.,) * 4
    maxima, sources = [], []
    for i in range(4):
        live = finite(s.suspensionMaxTravel[i])
        if 0 < live <= 2:
            maxima.append(live)
            sources.append("shared_memory")
        else:
            maxima.append(fallback[i])
            sources.append("vehicle_config_estimate" if fallback[i] > 0 else "missing")
    norm = tuple(clamp(finite(p.suspensionTravel[i]) / maxima[i], 0, 1)
                 if maxima[i] > 0 else 0. for i in range(4))
    if "vehicle_config_estimate" in sources:
        notes.append("部分悬挂最大行程采用车辆配置推算，尚未标定物理极限")
    if any(value < .1 for value in norm):
        notes.append("原程序红线学习受阻：至少一轮归一化悬挂低于 10%（含最大行程缺失）")
    elif g.status == 2:
        notes.append("红线学习实验：需前进挡稳定、油门≥90%、刹车/离合≤5%，观察断油回落")
    return Frame(
        active=g.status == 2, car=utf16(s.carModel),
        track=(utf16(s.track) + " " + utf16(s.trackConfiguration)).strip(), status=status,
        speed_kmh=clamp(p.speedKmh, 0, 2500), rpm=clamp(p.rpms, 0, 100000), max_rpm=maxrpm,
        gear=gear_byte(p.gear), throttle=clamp(p.gas, 0, 1), brake=clamp(p.brake, 0, 1),
        clutch=clamp(clutch, 0, 1), steer=clamp(steer, -1, 1),
        fuel_fraction=clamp(p.fuel / s.maxFuel, 0, 1) if s.maxFuel > 0 else 0.,
        fuel_litres=max(0., finite(p.fuel)), temps_c=tuple(finite(v) for v in p.tyreCoreTemperature),
        acc=tuple(finite(v) * 9.80665 for v in p.accG),
        velocity=tuple(finite(v) for v in p.localVelocity),
        angular=tuple(finite(v) for v in p.localAngularVelocity),
        orientation=tuple(finite(v) for v in (p.heading, p.pitch, p.roll)),
        position=tuple(finite(v) for v in g.carCoordinates),
        suspension=tuple(finite(v) for v in p.suspensionTravel), normalized_suspension=norm,
        suspension_max_used=tuple(maxima), suspension_sources=tuple(sources),
        wheel_speed=tuple(finite(v) for v in p.wheelAngularSpeed),
        best_lap=max(0, g.iBestTime)/1000., last_lap=max(0, g.iLastTime)/1000.,
        current_lap=max(0, g.iCurrentTime)/1000., laps=max(0, g.completedLaps),
        race_position=max(0, g.position), distance=max(0., finite(g.distanceTraveled)), notes=notes,
    )


def encode(frame, timestamp_ms, packet_format="fh4", race_time=0.0):
    """Little-endian Horizon Dash: 232-byte Sled + 12-byte gap + Dash + padding.

    FH4: 324 bytes. FH5: 331 bytes (same fields, 7 extra reserved bytes).
    Unsupported fields remain zero. No NaNs are used as 'missing' sentinels.
    """
    if packet_format not in ("fh4", "fh5"):
        raise ValueError("仅支持 fh4 / fh5 数据格式")
    data = bytearray(324 if packet_format == "fh4" else 331)
    def put(offset, fmt, *values):
        struct.pack_into("<" + fmt, data, offset, *values)
    def floats(offset, values):
        put(offset, "f" * len(values), *(finite(v) for v in values))
    put(0, "iI", int(frame.active), int(timestamp_ms) & 0xFFFFFFFF)
    floats(8, (frame.max_rpm, 0, frame.rpm))  # Idle RPM unavailable.
    floats(20, frame.acc)
    floats(32, frame.velocity)
    floats(44, frame.angular)
    floats(56, frame.orientation)
    floats(68, frame.normalized_suspension)
    floats(100, frame.wheel_speed)
    floats(196, frame.suspension)
    # Stable synthetic identity avoids mixing different AC cars. Not a Forza car ID.
    ordinal = 1000000000 + zlib.crc32(frame.car.encode("utf-8")) % 1000000000 if frame.car else 0
    put(212, "i", ordinal)
    # Compatibility identity ONLY: Forza rejects vehicle profiles with PI <= 0.
    # This is not an AC performance rating. Keep this experiment's profiles isolated.
    put(220, "i", 1)
    floats(244, frame.position)
    floats(256, (frame.speed_kmh / 3.6, frame.power_watts, frame.torque_nm))
    floats(268, tuple(v * 1.8 + 32 for v in frame.temps_c))
    floats(284, (0, frame.fuel_fraction, frame.distance,
                 frame.best_lap, frame.last_lap, frame.current_lap, race_time))
    put(312, "H", min(65535, max(0, frame.laps)))
    put(314, "BBBBBB", min(255, max(0, frame.race_position)),
        pedal(frame.throttle), pedal(frame.brake), pedal(frame.clutch), 0, frame.gear)
    put(320, "b", round(clamp(frame.steer, -1, 1) * 127))
    return bytes(data)


def demo_frame():
    return Frame(active=True, car="AC_BRIDGE_SIMULATION", track="模拟数据 / 非游戏",
                 status="模拟测试（不是实车数据）", speed_kmh=123.4, rpm=4321, max_rpm=8000,
                 gear=4, throttle=.5, brake=.25, clutch=.25, steer=-.5,
                 fuel_fraction=.5, fuel_litres=25, temps_c=(70., 80., 90., 100.),
                 acc=(9.80665, 0., -4.903325), velocity=(0., 0., 123.4/3.6),
                 best_lap=89.123, last_lap=90.456, current_lap=12.345, laps=3, race_position=2)
