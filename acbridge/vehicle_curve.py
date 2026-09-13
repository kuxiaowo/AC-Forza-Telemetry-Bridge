"""Read AC vehicle data and calculate a synthetic full-load power curve."""
import bisect
import configparser
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import struct
import uuid
import zlib
from .vendor.acd import get_encryption_key, _decrypt_bytes

def clean_name(name):
    name = name.replace("\\", "/")
    if not name or name.startswith("/") or ":" in name or any(p in ("..", "") for p in name.split("/")):
        raise ValueError("车辆文件包含非法路径")
    return name.lower()


def load_car(car_dir, source="auto"):
    car_dir = Path(car_dir).resolve()
    directory, archive = car_dir / "data", car_dir / "data.acd"
    if source == "auto":
        if directory.is_dir() and archive.is_file():
            raise ValueError("同时存在 data 和 data.acd，请明确选择游戏实际使用的数据来源。")
        source = "data" if directory.is_dir() else "acd"
    files = {}
    if source == "data":
        if not directory.is_dir():
            raise ValueError("未找到车辆 data 目录")
        for p in directory.rglob("*"):
            if p.is_file() and p.suffix.lower() in (".ini", ".lut", ".rto", ".lua"):
                if not p.resolve().is_relative_to(directory.resolve()) or p.stat().st_size > 8*1024*1024:
                    raise ValueError("车辆文件路径或大小异常")
                files[clean_name(p.relative_to(directory).as_posix())] = p.read_bytes()
    elif source == "acd":
        if not archive.is_file() or archive.stat().st_size > 128*1024*1024:
            raise ValueError("未找到有效的 data.acd（最大 128 MB）")
        raw = archive.read_bytes()
        pos = 8 if raw[:4] == struct.pack("<i", -1111) else 0
        key = get_encryption_key(archive)
        while pos < len(raw):
            if pos+4 > len(raw):
                raise ValueError("ACD 文件截断")
            n = struct.unpack_from("<I", raw, pos)[0]; pos += 4
            if not 1 <= n <= 512 or pos+n+4 > len(raw):
                raise ValueError("ACD 条目损坏或格式不支持")
            name = clean_name(raw[pos:pos+n].decode("utf-8")); pos += n
            size = struct.unpack_from("<I", raw, pos)[0]*4; pos += 4
            if size > 32*1024*1024 or pos+size > len(raw) or name in files:
                raise ValueError("ACD 条目长度或名称异常")
            files[name] = bytes(_decrypt_bytes(raw[pos:pos+size], key)); pos += size
    else:
        raise ValueError("数据来源必须是 auto、data 或 acd")
    if "engine.ini" not in files:
        raise ValueError("车辆数据中没有 engine.ini")
    digest = hashlib.sha256()
    for k, v in sorted(files.items()):
        digest.update(k.encode()); digest.update(b"\0"); digest.update(v)
    return files, dict(car_id=car_dir.name, source=source, source_path=str(directory if source == "data" else archive),
                       sha256=digest.hexdigest())


def ini(raw):
    text = raw.decode("utf-8-sig", errors="replace")
    result = configparser.ConfigParser(interpolation=None, strict=False, inline_comment_prefixes=(";", "//"))
    result.read_string(text)
    return result


def number(cfg, section, key, fallback=None):
    value = float(cfg.get(section, key, fallback=str(fallback)))
    if not math.isfinite(value):
        raise ValueError(f"{section}/{key} 非有限数字")
    return value


def lut(raw):
    points = []
    for line in raw.decode("utf-8-sig").splitlines():
        line = line.split(";", 1)[0].split("//", 1)[0].strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split("|")
        if len(fields) != 2:
            raise ValueError("扭矩曲线行格式应为 RPM|Nm")
        r, t = map(float, fields)
        if not math.isfinite(r) or not math.isfinite(t) or r < 0 or t < 0:
            raise ValueError("扭矩曲线包含无效值")
        if points and r <= points[-1][0]:
            raise ValueError("曲线 RPM 必须严格递增，不能重复")
        points.append((r, t))
    if len(points) < 2:
        raise ValueError("曲线至少需要两个点")
    return points


def analyze(car_dir, source="auto", engine_max=None):
    files, provenance = load_car(car_dir, source)
    engine = ini(files["engine.ini"])
    # A base LUT alone is not a trustworthy full-output curve for these cars.
    if any(s.startswith("TURBO_") for s in engine.sections()) or any(k in files for k in ("ers.ini", "kers.ini", "hybrid.ini")):
        raise ValueError("此车包含涡轮或混动配置，当前版本不导入未经修正的基础曲线；尚未支持完整增压/混动模型。")
    if "script.lua" in files or (Path(car_dir) / "extension" / "ext_config.ini").is_file():
        raise ValueError("发现车辆脚本/扩展配置，无法确认静态曲线等同有效动力模型，当前版本不自动导入。")
    curve_file = clean_name(engine.get("HEADER", "POWER_CURVE"))
    if curve_file not in files:
        raise ValueError("找不到 engine.ini 指定的扭矩曲线")
    curve = lut(files[curve_file])
    limiter = number(engine, "ENGINE_DATA", "LIMITER")
    idle = number(engine, "ENGINE_DATA", "MINIMUM", 900)
    if not 100 < idle < limiter <= 100000:
        raise ValueError("怠速/断油转速无效（暂不支持无断油限制车辆）")
    if curve[0][0] > idle or curve[-1][0] < limiter:
        raise ValueError("曲线未覆盖怠速至断油范围，拒绝外推")
    xs = [p[0] for p in curve]
    def torque(r):
        i = min(len(curve)-2, max(0, bisect.bisect_right(xs, r)-1))
        a, b = curve[i], curve[i+1]
        return a[1] + (b[1]-a[1])*(r-a[0])/(b[0]-a[0])
    # Include source knots plus 50 RPM samples; piecewise linear LUT interpolation.
    rpms = sorted(set([idle, limiter] + [float(r) for r in range(math.ceil(idle/50)*50, math.floor(limiter)+1, 50)]
                      + [r for r, _ in curve if idle <= r <= limiter]))
    points = []
    for r in rpms:
        t = torque(r)
        if t <= 0:
            raise ValueError("驾驶范围内存在零扭矩，不能生成连续正功率档案")
        points.append(dict(TimestampMS=0, Rpm=r, PowerWatts=t*r*math.pi/30, TorqueNm=t,
                           SpeedMetersPerSecond=1.0, Gear=1))
    # Gear/speed are serializer compatibility values, never telemetry or run records.
    peak = max(points, key=lambda p:p["PowerWatts"])
    car = provenance["car_id"]
    ordinal = 1000000000 + zlib.crc32(car.encode()) % 1000000000
    engine_max = limiter if engine_max is None else float(engine_max)
    if not math.isfinite(engine_max) or not 100 < engine_max <= 100000:
        raise ValueError("遥测最高转速量程无效")
    profile = dict(CarId=car, SyntheticOrdinal=ordinal, EngineMaxRpm=engine_max,
                   EngineIdleRpm=idle, RedlineRpm=limiter,
                   MaxPowerWatts=peak["PowerWatts"], MaxPowerRpm=peak["Rpm"],
                   MaxTorqueNm=max(p["TorqueNm"] for p in points), PowerCurvePoints=points)
    provenance.update(curve_file=curve_file, computed_not_measured=True,
        compatibility_fields={"SpeedMetersPerSecond":1.0, "Gear":1, "reason":"原档案校验要求；不是测量值，也不代表轮速模型"},
        model="静态自然吸气全负荷 LUT；无环境、损伤或瞬态修正", step_rpm=50,
        limiter_rpm=limiter, peak_power_rpm=peak["Rpm"], peak_power_kw=peak["PowerWatts"]/1000)
    return profile, provenance

