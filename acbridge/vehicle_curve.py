"""Read runtime vehicle fields directly from AC data/data.acd files."""
import bisect
import configparser
import hashlib
import math
import os
from pathlib import Path
import re
import struct
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
        # Stock AC curves can include negative-RPM starter/reverse-rotation
        # nodes. They are valid curve anchors even though live RPM is clamped.
        if not math.isfinite(r) or not math.isfinite(t) or t < 0:
            raise ValueError("扭矩曲线包含无效值")
        if points and r <= points[-1][0]:
            raise ValueError("曲线 RPM 必须严格递增，不能重复")
        points.append((r, t))
    if len(points) < 2:
        raise ValueError("曲线至少需要两个点")
    return points


def protocol_fields(car_dir, source="auto"):
    """Read runtime fields from the current vehicle's unpacked data."""
    files, _ = load_car(car_dir, source)
    result, notes, engine = {}, [], None
    try:
        engine = ini(files["engine.ini"])
        idle = number(engine, "ENGINE_DATA", "MINIMUM")
        if not 100 < idle <= 100000:
            raise ValueError("超出有效范围")
        result["engine_idle_rpm"] = idle
    except (ValueError, configparser.Error) as exc:
        notes.append(f"怠速转速未识别：{exc}")
    try:
        if "drivetrain.ini" not in files:
            raise ValueError("车辆数据中没有 drivetrain.ini")
        drivetrain = ini(files["drivetrain.ini"])
        traction = drivetrain.get("TRACTION", "TYPE").strip().upper()
        result["drivetrain_type"] = {"FWD": 0, "RWD": 1, "AWD": 2, "AWD2": 2}[traction]
        result["drivetrain_source"] = traction
    except KeyError:
        notes.append(f"驱动形式未识别：不支持 {traction or '空值'}")
    except (ValueError, configparser.Error) as exc:
        notes.append(f"驱动形式未识别：{exc}")
    try:
        if "car.ini" not in files:
            raise ValueError("车辆数据中没有 car.ini")
        car = ini(files["car.ini"])
        # Kunos defines STEER_LOCK as degrees from centre to one side, while
        # shared-memory steerAngle is radians and Forza expects [-1, 1].
        lock_degrees = number(car, "CONTROLS", "STEER_LOCK")
        if not 1 <= lock_degrees <= 1080:
            raise ValueError("转向锁角超出有效范围")
        result["steer_lock_degrees"] = lock_degrees
        result["steer_normalization"] = 1 / math.radians(lock_degrees)
    except (ValueError, configparser.Error) as exc:
        notes.append(f"转向锁角未识别：{exc}")
    try:
        if "suspensions.ini" not in files:
            raise ValueError("车辆数据中没有 suspensions.ini")
        suspension = ini(files["suspensions.ini"])
        ranges = []
        for section in ("FRONT", "REAR"):
            # Kunos SDK: UP/DN are metres from the suspension design zero to
            # each bump stop. Their sum is the full mechanical travel range.
            travel = number(suspension, section, "BUMPSTOP_UP") + number(
                suspension, section, "BUMPSTOP_DN")
            if not 0 < travel <= 2:
                raise ValueError(f"{section} 最大悬挂行程超出有效范围")
            ranges.extend((travel, travel))
        result["suspension_max_travel_m"] = tuple(ranges)
    except (ValueError, configparser.Error) as exc:
        notes.append(f"最大悬挂行程未识别：{exc}")
    try:
        if engine is None:
            raise ValueError("engine.ini 无法解析")
        curve_file = clean_name(engine.get("HEADER", "POWER_CURVE"))
        if curve_file not in files:
            raise ValueError("找不到 engine.ini 指定的动力曲线")
        result["power_curve"] = tuple(lut(files[curve_file]))
        result["power_curve_source"] = curve_file
        if (any(s.startswith("TURBO_") for s in engine.sections()) or
                any(k in files for k in ("ers.ini", "kers.ini", "hybrid.ini"))):
            notes.append("动力值采用基础全负荷曲线估算，未还原涡轮/混动瞬态修正")
    except (ValueError, configparser.Error) as exc:
        notes.append(f"动力曲线未识别：{exc}")
    return result, notes


def curve_torque(points, rpm):
    """Linearly interpolate a power.lut torque curve without extrapolation."""
    if not points:
        return 0.0
    rpm = finite_number(rpm)
    if rpm <= points[0][0]:
        return points[0][1]
    if rpm >= points[-1][0]:
        return points[-1][1]
    xs = [point[0] for point in points]
    i = bisect.bisect_right(xs, rpm) - 1
    a, b = points[i], points[i + 1]
    return a[1] + (b[1] - a[1]) * (rpm - a[0]) / (b[0] - a[0])


def finite_number(value):
    value = float(value)
    return value if math.isfinite(value) else 0.0


def find_car(car, config):
    """Find the exact current AC car directory without storing a local profile."""
    if not car or Path(car).name != car or car in (".", ".."):
        raise ValueError("尚未识别有效 AC 车辆，请先进入赛道并开始适配。")
    roots = []
    if config.get("game_directory"):
        roots.append(Path(config["game_directory"]))
    if os.name == "nt":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
                steam = Path(winreg.QueryValueEx(key, "SteamPath")[0])
            libraries = [steam]
            library_file = steam / "steamapps/libraryfolders.vdf"
            if library_file.exists():
                libraries += [Path(p.replace("\\\\", "\\")) for p in re.findall(
                    r'"path"\s+"([^"]+)"', library_file.read_text(encoding="utf-8"))]
            roots += [p / "steamapps/common/assettocorsa" for p in libraries]
        except OSError:
            pass
    roots += [Path(f"{drive}:/Steam/steamapps/common/assettocorsa") for drive in "CDEFGH"]
    candidates = list(dict.fromkeys(
        (p / "content/cars" / car).resolve()
        for p in roots if (p / "content/cars" / car).is_dir()))
    if (config.get("game_directory") and candidates and
            candidates[0] == (roots[0] / "content/cars" / car).resolve()):
        return candidates[0]
    if len(candidates) != 1:
        raise ValueError("无法唯一匹配游戏安装目录，请在“游戏目录”中选择正在使用的 AC 根目录。")
    return candidates[0]
