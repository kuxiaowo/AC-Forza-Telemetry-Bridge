"""ACC shared-memory prefixes and mapping, based on Kunos v1.8.12 docs.

Physics shares AC1's prefix through localVelocity. Static is deliberately
limited to maxFuel; ACC does not implement AC1's later suspension metadata.
"""
import ctypes as C

from .shared_memory import Physics, Graphics, Static, I, F, utf16
from .telemetry import Frame, clamp, finite, gear_byte


class ACCPhysics(Physics):
    pass


class ACCStatic(C.LittleEndianStructure):
    _pack_ = 4
    _fields_ = Static._fields_[:14]  # through maxFuel (420 bytes)


class ACCGraphics(C.LittleEndianStructure):
    _pack_ = 4
    _fields_ = Graphics._fields_[:21] + [  # through normalizedCarPosition
        ("activeCars", I), ("carCoordinates", (F * 3) * 60),
        ("carID", I * 60), ("playerCarID", I),
        ("penaltyTime", F), ("flag", I), ("penalty", I),
        ("idealLineOn", I), ("isInPitLane", I), ("surfaceGrip", F),
        ("mandatoryPitDone", I), ("windSpeed", F), ("windDirection", F),
        ("isSetupMenuVisible", I), ("mainDisplayIndex", I),
        ("secondaryDisplayIndex", I), ("TC", I), ("TCCUT", I),
        ("EngineMap", I), ("ABS", I), ("fuelXLap", F),
        ("rainLights", I), ("flashingLights", I), ("lightsStage", I),
        ("exhaustTemperature", F), ("wiperLV", I),
        ("driverStintTotalTimeLeft", I), ("driverStintTimeLeft", I),
        ("rainTyres", I), ("sessionIndex", I),
    ]


def player_position(g):
    """Return the player's coordinates, never an opponent or arbitrary slot."""
    if not 0 < g.activeCars <= 60:
        return None
    matches = [i for i in range(g.activeCars) if g.carID[i] == g.playerCarID]
    if len(matches) != 1:
        return None
    values = tuple(float(v) for v in g.carCoordinates[matches[0]])
    if any(finite(v, None) is None for v in values):
        return None
    return values


def from_acc(p, g, s, config):
    status = {0: "等待 ACC 进入驾驶", 1: "ACC 回放（暂停发送驾驶数据）",
              2: "已连接 ACC", 3: "ACC 已暂停"}.get(g.status, "未知 ACC 状态")
    if g.status == 2 and g.isSetupMenuVisible:
        status = "ACC 调校菜单（暂停发送驾驶数据）"
    notes = [
        "ACC：功率、扭矩、怠速、驱动形式及归一化悬挂缺少可靠来源，使用兼容占位值",
        "ACC：运动坐标及增压单位未经实机标定，相关 Forza 字段暂为零",
        "ACC：燃油显示共享内存原值；比例按原值/容量计算，单位待实机确认",
        "ACC：离合默认直接映射，方向待实机确认，可使用离合反向选项",
    ]
    max_rpm = float(s.maxRpm)
    if not 100 <= max_rpm <= 100000:
        max_rpm = config["fallback_max_rpm"]
        notes.append("最高转速缺失，使用配置中的仪表量程")
    max_fuel = finite(s.maxFuel)
    if max_fuel <= 0:
        notes.append("油箱容量缺失，燃油比例以 0 占位")
    position = player_position(g)
    if position is None:
        notes.append("玩家车辆坐标缺失或索引无效")
    clutch = finite(p.clutch)
    if config["acc_invert_clutch"]:
        clutch = 1 - clutch
    return Frame(
        game="acc", active=g.status == 2 and not g.isSetupMenuVisible,
        car=utf16(s.carModel), track=utf16(s.track), status=status,
        speed_kmh=clamp(p.speedKmh, 0, 2500), rpm=clamp(p.rpms, 0, 100000),
        max_rpm=max_rpm, gear=gear_byte(p.gear),
        throttle=clamp(p.gas, 0, 1), brake=clamp(p.brake, 0, 1),
        clutch=clamp(clutch, 0, 1),
        steer=clamp(finite(p.steerAngle) * config["steer_scale"] *
                    (-1 if config["invert_steer"] else 1), -1, 1),
        fuel_quantity=max(0., finite(p.fuel)), fuel_unit="原值",
        fuel_fraction=clamp(p.fuel / max_fuel, 0, 1) if max_fuel > 0 else 0.,
        temps_c=tuple(finite(v) for v in p.tyreCoreTemperature),
        suspension=tuple(finite(v) for v in p.suspensionTravel),
        wheel_speed=tuple(finite(v) for v in p.wheelAngularSpeed),
        best_lap=max(0, g.iBestTime) / 1000., last_lap=max(0, g.iLastTime) / 1000.,
        current_lap=max(0, g.iCurrentTime) / 1000., laps=max(0, g.completedLaps),
        race_position=max(0, g.position), distance=max(0., finite(g.distanceTraveled)),
        source_data=dict(player_position=position, acc_g=tuple(finite(v) for v in p.accG),
                         local_velocity=tuple(finite(v) for v in p.localVelocity),
                         local_angular_velocity=tuple(finite(v) for v in p.localAngularVelocity),
                         orientation=tuple(finite(v) for v in (p.heading, p.pitch, p.roll)),
                         turbo_boost=finite(p.turboBoost), fuel_capacity=max_fuel,
                         sm_version=utf16(s.smVersion), game_version=utf16(s.acVersion)),
        notes=notes,
    )
