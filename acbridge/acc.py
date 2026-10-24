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


def from_acc(p, g, s, config, vehicle_fields=None):
    vehicle_fields = vehicle_fields or {}
    status = {0: "等待 ACC 进入驾驶", 1: "ACC 回放（暂停发送驾驶数据）",
              2: "已连接 ACC", 3: "ACC 已暂停"}.get(g.status, "未知 ACC 状态")
    if g.status == 2 and g.isSetupMenuVisible:
        status = "ACC 调校菜单（暂停发送驾驶数据）"
    notes = [
        "ACC：功率、扭矩、驱动形式缺少可靠来源，使用兼容占位值",
        "ACC：运动坐标及增压单位未经实机标定，相关 Forza 字段暂为零",
        "ACC：燃油显示共享内存原值；比例按原值/容量计算，单位待实机确认",
        "ACC：离合默认直接映射，发动机未启动时的离合值不用于判断踏板方向",
    ]
    references = vehicle_fields.get("suspension_reference_m", (0.,) * 4)
    normalized = tuple(clamp(finite(p.suspensionTravel[i]) / references[i], 0, 1)
                       if references[i] > 0 else 0. for i in range(4))
    sources = tuple("acc_vehicle_bumpstop_reference" if v > 0 else "missing" for v in references)
    if all(v > 0 for v in references):
        notes.append("ACC：悬挂按车辆限位参考区间归一化，非已验证的全机械行程；可用于接收端红线学习筛选")
        if any(v < .1 for v in normalized):
            notes.append("红线学习暂停：至少一轮悬挂低于参考行程的 10%")
        elif g.status == 2:
            notes.append("红线学习：启用接收端自动监测并解锁；稳定前进挡、油门≥90%、刹车/离合≤5%，观察断油回落")
    else:
        notes.append("ACC 车辆悬挂参考数据缺失，归一化悬挂为零，接收端红线学习受阻")
    max_rpm = float(s.maxRpm)
    if not 100 <= max_rpm <= 100000:
        limiter = finite(vehicle_fields.get("engine_limiter_rpm", 0))
        if 100 <= limiter <= 100000:
            max_rpm = limiter
            notes.append("最高转速共享内存缺失，使用当前车辆文件的断油转速")
        else:
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
        idle_rpm=clamp(vehicle_fields.get("engine_idle_rpm", 0), 0, 100000),
        throttle=clamp(p.gas, 0, 1), brake=clamp(p.brake, 0, 1),
        clutch=clamp(clutch, 0, 1),
        steer=clamp(finite(p.steerAngle) * config["steer_scale"] *
                    (-1 if config["invert_steer"] else 1), -1, 1),
        fuel_quantity=max(0., finite(p.fuel)), fuel_unit="原值",
        fuel_fraction=clamp(p.fuel / max_fuel, 0, 1) if max_fuel > 0 else 0.,
        temps_c=tuple(finite(v) for v in p.tyreCoreTemperature),
        suspension=tuple(finite(v) for v in p.suspensionTravel),
        normalized_suspension=normalized, suspension_max_used=tuple(references),
        suspension_sources=sources,
        wheel_speed=tuple(finite(v) for v in p.wheelAngularSpeed),
        best_lap=max(0, g.iBestTime) / 1000., last_lap=max(0, g.iLastTime) / 1000.,
        current_lap=max(0, g.iCurrentTime) / 1000., laps=max(0, g.completedLaps),
        race_position=max(0, g.position), distance=max(0., finite(g.distanceTraveled)),
        source_data=dict(player_position=position, acc_g=tuple(finite(v) for v in p.accG),
                         local_velocity=tuple(finite(v) for v in p.localVelocity),
                         local_angular_velocity=tuple(finite(v) for v in p.localAngularVelocity),
                         orientation=tuple(finite(v) for v in (p.heading, p.pitch, p.roll)),
                         turbo_boost=finite(p.turboBoost), fuel_capacity=max_fuel,
                         vehicle_data=vehicle_fields,
                         sm_version=utf16(s.smVersion), game_version=utf16(s.acVersion)),
        notes=notes,
    )
