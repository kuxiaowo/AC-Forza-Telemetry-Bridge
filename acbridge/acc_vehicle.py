"""Read ACC's local CarData asset without writing game files.

Limited to validated UE4 PAK v11, zlib assets and tagged UE4 properties.
The bump-stop reference span is NOT a certified mechanical travel limit.
No game assets or encryption keys are bundled or persisted.
"""
import ctypes as C
from ctypes import wintypes as W
import hashlib
import math
import os
from pathlib import Path
import re
import struct
import zlib


# Verified from this executable's FSuspBumpStopDef, FSuspHubData and FSuspData
# native constructors. Range defaults to zero, not an inferred measurement.
# Unknown builds may use explicit asset values but cannot fill omitted fields.
ZERO_RANGE_DEFAULT_BUILDS = frozenset({
    "2b2679c269ad710f61ac5c0fe84439799f770c79535378bc98983268c1f937e0",
})


class Reader:
    def __init__(self, data, position=0):
        self.data, self.pos = data, position

    def read(self, size):
        if size < 0 or self.pos + size > len(self.data):
            raise ValueError("ACC 资源截断")
        value = self.data[self.pos:self.pos + size]
        self.pos += size
        return value

    def number(self, fmt):
        return struct.unpack("<" + fmt, self.read(struct.calcsize("<" + fmt)))[0]

    def string(self):
        size = self.number("i")
        if not 0 < abs(size) <= 4096:
            raise ValueError("ACC 资源字符串长度异常")
        raw = self.read(size if size > 0 else -size * 2)
        return raw.decode("utf-8" if size > 0 else "utf-16-le").rstrip("\0")


def aes_decrypt(data, key):
    """AES-256 ECB via Windows CNG; no optional Python crypto dependency."""
    if os.name != "nt" or len(key) != 32 or len(data) % 16:
        raise ValueError("ACC 解包需要 Windows、256 位密钥和完整 AES 块")
    dll = C.WinDLL("bcrypt")
    ptr, ulong, handle = C.c_void_p, W.ULONG, W.HANDLE
    dll.BCryptOpenAlgorithmProvider.argtypes = [C.POINTER(handle), W.LPCWSTR, W.LPCWSTR, ulong]
    dll.BCryptSetProperty.argtypes = [handle, W.LPCWSTR, ptr, ulong, ulong]
    dll.BCryptGenerateSymmetricKey.argtypes = [handle, C.POINTER(handle), ptr, ulong, ptr, ulong, ulong]
    dll.BCryptDecrypt.argtypes = [handle, ptr, ulong, ptr, ptr, ulong, ptr, ulong, C.POINTER(ulong), ulong]
    dll.BCryptDestroyKey.argtypes = [handle]
    dll.BCryptCloseAlgorithmProvider.argtypes = [handle, ulong]
    algorithm, key_handle = handle(), handle()

    def check(status):
        if status:
            raise OSError(f"Windows AES 读取失败：0x{status & 0xffffffff:08x}")

    try:
        check(dll.BCryptOpenAlgorithmProvider(C.byref(algorithm), "AES", None, 0))
        mode = C.create_unicode_buffer("ChainingModeECB")
        check(dll.BCryptSetProperty(algorithm, "ChainingMode", mode, C.sizeof(mode), 0))
        secret = C.create_string_buffer(key)
        check(dll.BCryptGenerateSymmetricKey(algorithm, C.byref(key_handle), None, 0, secret, len(key), 0))
        source, output = C.create_string_buffer(data), C.create_string_buffer(len(data))
        written = ulong()
        check(dll.BCryptDecrypt(key_handle, source, len(data), None, None, 0,
                               output, len(data), C.byref(written), 0))
        return output.raw[:written.value]
    finally:
        if key_handle:
            dll.BCryptDestroyKey(key_handle)
        if algorithm:
            dll.BCryptCloseAlgorithmProvider(algorithm, 0)


def find_install(config):
    roots = []
    if config.get("acc_game_directory"):
        selected = Path(config["acc_game_directory"]).resolve()
        if not (selected / "AC2/Content/Paks/AC2-WindowsNoEditor.pak").is_file():
            raise ValueError("acc_game_directory 中没有 ACC 资源包")
        return selected
    if os.name == "nt":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
                steam = Path(winreg.QueryValueEx(key, "SteamPath")[0])
            libraries = [steam]
            catalog = steam / "steamapps/libraryfolders.vdf"
            if catalog.is_file():
                libraries += [Path(p.replace("\\\\", "\\")) for p in re.findall(
                    r'"path"\s+"([^"]+)"', catalog.read_text(encoding="utf-8"))]
            roots += [p / "steamapps/common/Assetto Corsa Competizione" for p in libraries]
        except OSError:
            pass
    roots = list(dict.fromkeys(p.resolve() for p in roots if
                 (p / "AC2/Content/Paks/AC2-WindowsNoEditor.pak").is_file()))
    if len(roots) != 1:
        raise ValueError("无法唯一找到 ACC；请设置 acc_game_directory 为当前游戏根目录")
    return roots[0]


class Pak:
    """Bounded, read-only reader for the subset required by CarData."""
    def __init__(self, root):
        self.path = root / "AC2/Content/Paks/AC2-WindowsNoEditor.pak"
        self.exe = root / "AC2/Binaries/Win64/AC2-Win64-Shipping.exe"
        size = self.path.stat().st_size
        with self.path.open("rb") as stream:
            stream.seek(max(0, size - 512))
            tail = stream.read()
            marker = tail.rfind(struct.pack("<I", 0x5a6f12e1))
            if marker < 17 or len(tail) - marker < 204:
                raise ValueError("ACC PAK 文件头不支持")
            version, offset, length = struct.unpack_from("<iqq", tail, marker + 4)
            self.footer = tail[marker - 17:marker + 204]
            if version != 11 or not 0 < length <= 64 * 1024 * 1024 or offset + length > size:
                raise ValueError("ACC PAK 版本或索引长度不支持")
            self.compression = tail[marker + 44:marker + 76].rstrip(b"\0")
            digest = tail[marker + 24:marker + 44]
            stream.seek(offset)
            encrypted = stream.read(length)
        self.key = None
        self.zero_range_defaults_verified = False
        if tail[marker - 1]:
            # Local executable's eight consecutive uint32 stores. Accept only
            # a candidate which decrypts the current index to its recorded SHA1.
            if self.exe.stat().st_size > 256 * 1024 * 1024:
                raise ValueError("ACC 程序大小异常")
            executable = self.exe.read_bytes()
            self.zero_range_defaults_verified = hashlib.sha256(executable).hexdigest() in ZERO_RANGE_DEFAULT_BUILDS
            pattern = rb'\xc7[\x00-\x07].{4}(?:\xc7[\x40-\x47].{5}){7}'
            for match in re.finditer(pattern, executable, re.DOTALL):
                code = match.group()
                candidate = code[2:6] + b"".join(code[9 + 7*j:13 + 7*j] for j in range(7))
                # Check the tiny header before decrypting the complete index.
                header = aes_decrypt(encrypted[:16], candidate)
                if struct.unpack_from("<i", header)[0] != 10 or header[4:14] != b"../../../\0":
                    continue
                plain = aes_decrypt(encrypted, candidate)
                if hashlib.sha1(plain).digest() == digest:
                    self.key = candidate
                    break
            else:
                raise ValueError("无法验证当前 ACC 资源索引；不使用未知版本车辆参数")
        else:
            plain = encrypted
            if hashlib.sha1(plain).digest() != digest:
                raise ValueError("ACC 索引校验失败")
        self.index_digest = digest.hex()
        index = Reader(plain)
        if index.string() != "../../../":
            raise ValueError("ACC 资源挂载点不支持")
        count = index.number("I")
        if count > 1000000:
            raise ValueError("ACC 资源数量异常")
        index.number("Q")  # path hash seed
        if index.number("I"):
            index.read(36)  # optional path hash index; not needed for exact lookup
        if not index.number("I"):
            raise ValueError("ACC 缺少完整资源目录")
        directory_offset, directory_size = index.number("Q"), index.number("Q")
        directory_hash = index.read(20)
        directory = Reader(self.block(directory_offset, directory_size, directory_hash))
        self.entries = {}
        directories = directory.number("I")
        if directories > 1000000:
            raise ValueError("ACC 目录数量异常")
        for _ in range(directories):
            parent, files = directory.string(), directory.number("I")
            if files > 1000000:
                raise ValueError("ACC 目录资源数量异常")
            for _ in range(files):
                filename, entry = directory.string(), directory.number("i")
                path = parent.lstrip("/") + filename
                # Never extract backups or guessed similar car names.
                if path.endswith(("/data/CarData.uasset", "/data/CarData.uexp")):
                    self.entries[path] = entry
        self.encoded = index.read(index.number("I"))

    def block(self, offset, size, digest=None):
        if not 0 <= offset or not 0 < size <= 64 * 1024 * 1024 or offset + size > self.path.stat().st_size:
            raise ValueError("ACC 资源读取范围异常")
        with self.path.open("rb") as stream:
            stream.seek(offset)
            data = stream.read(size)
        if self.key:
            data = aes_decrypt(data, self.key)
        if digest and hashlib.sha1(data).digest() != digest:
            raise ValueError("ACC 资源目录校验失败")
        return data

    def get(self, path):
        if path not in self.entries or self.entries[path] < 0:
            raise ValueError("当前 ACC 车型没有唯一、受支持的 CarData 资源")
        entry = Reader(self.encoded, self.entries[path])
        bits = entry.number("I")
        if bits & 63 == 63:
            entry.number("I")
        offset = entry.number("I" if bits & (1 << 31) else "Q")
        with self.path.open("rb") as stream:
            stream.seek(offset)
            header = Reader(stream.read(4096))
            header.number("Q")
            packed, size, method = header.number("Q"), header.number("Q"), header.number("I")
            digest = header.read(20)
            if not 0 < size <= 2 * 1024 * 1024 or not 0 < packed <= 2 * 1024 * 1024:
                raise ValueError("ACC 车辆资源大小异常")
            if method != 1 or self.compression != b"Zlib":
                raise ValueError("ACC 车辆资源压缩方式不支持")
            count = header.number("I")
            if not 1 <= count <= 128:
                raise ValueError("ACC 车辆资源压缩块数量异常")
            blocks = [(header.number("Q"), header.number("Q")) for _ in range(count)]
            encrypted = bool(header.number("B") & 1)
            header.number("I")
            chunks, hashed = [], bytearray()
            for begin, end in blocks:
                length = end - begin
                if not 0 < length <= 2 * 1024 * 1024 or begin < header.pos:
                    raise ValueError("ACC 压缩块范围异常")
                stream.seek(offset + begin)
                raw = stream.read((length + 15) & ~15 if encrypted else length)
                hashed.extend(raw)  # PAK hash covers encrypted AES padding too
                compressed = aes_decrypt(raw, self.key)[:length] if encrypted else raw
                decoder = zlib.decompressobj()
                chunk = decoder.decompress(compressed, size + 1)
                if len(chunk) > size or not decoder.eof:
                    raise ValueError("ACC 资源解压长度异常")
                chunks.append(chunk)
            data = b"".join(chunks)
            if len(data) != size or hashlib.sha1(hashed).digest() != digest:
                raise ValueError("ACC 车辆资源完整性校验失败")
            return data

    def car(self, car):
        if not re.fullmatch(r"[a-z0-9_]+", car):
            raise ValueError("ACC 车型标识无效")
        matches = [p for p in self.entries if p.endswith(f"/{car}/data/CarData.uasset")]
        if len(matches) != 1:
            raise ValueError("无法唯一匹配 ACC 车型物理资源")
        path = matches[0]
        asset, export = self.get(path), self.get(path[:-7] + ".uexp")
        values = asset_properties(asset, export)
        spans = []
        defaulted = []
        for axle in ("hubFront", "hubRear"):
            base = f"Suspensions.{axle}.bumpStop."
            up, down = values.get(base + "up.Range"), values.get(base + "down.Range")
            if self.zero_range_defaults_verified:
                if up is None:
                    up = 0.
                    defaulted.append(base + "up.Range")
                if down is None:
                    down = 0.
                    defaulted.append(base + "down.Range")
            if not isinstance(up, float) or not isinstance(down, float) or not (
                    math.isfinite(up) and math.isfinite(down) and .001 < abs(up - down) <= 1):
                spans.extend((0., 0.))
            else:
                spans.extend((abs(up - down),) * 2)
        return dict(suspension_reference_m=tuple(spans),
                    engine_idle_rpm=values.get("EngineData.Minimum", 0),
                    engine_limiter_rpm=values.get("EngineData.Limiter", 0),
                    verified_default_fields=defaulted,
                    source_path=path, index_sha1=self.index_digest,
                    asset_sha256=hashlib.sha256(asset + export).hexdigest())


def asset_properties(asset, export):
    """Read named scalar fields from tagged UE4.26 cooked assets, fail closed."""
    header = Reader(asset)
    if header.number("I") != 0x9e2a83c1 or header.number("i") != -7:
        raise ValueError("ACC CarData 资源版本不支持")
    header.read(12)
    if header.number("i") != 0:  # custom versions: only validated unversioned header
        raise ValueError("ACC CarData 自定义版本不支持")
    if header.number("i") != len(asset):
        raise ValueError("ACC CarData 资源头长度异常")
    header.string()
    header.number("I")
    count, offset = header.number("i"), header.number("i")
    if not 0 < count <= 10000 or not header.pos <= offset < len(asset):
        raise ValueError("ACC CarData 名称表异常")
    table = Reader(asset, offset)
    names = []
    for _ in range(count):
        names.append(table.string())
        table.read(4)  # name hashes
    data = Reader(export)
    result = {}

    def name():
        index, number = data.number("i"), data.number("i")
        if not 0 <= index < len(names) or number:
            raise ValueError("ACC CarData 属性名称不支持")
        return names[index]

    def properties(end, prefix="", depth=0):
        if depth > 16:
            raise ValueError("ACC CarData 属性嵌套异常")
        while data.pos < end:
            field = name()
            if field == "None":
                return
            kind = name()
            size, index = data.number("i"), data.number("i")
            struct_type = None
            if kind == "StructProperty":
                struct_type = name()
                data.read(16)
            elif kind == "BoolProperty":
                data.read(1)
            elif kind in ("ByteProperty", "EnumProperty", "ArrayProperty"):
                name()
            elif kind in ("MapProperty", "SetProperty"):
                raise ValueError("ACC CarData 集合属性不支持")
            if data.number("B"):
                data.read(16)
            start = data.pos
            if size < 0 or start + size > end or index != 0:
                raise ValueError("ACC CarData 属性边界异常")
            path = prefix + field
            if kind in ("FloatProperty", "IntProperty") and size == 4:
                result[path] = data.number("f" if kind == "FloatProperty" else "i")
            elif struct_type and struct_type not in ("Vector", "Rotator", "Guid", "LinearColor", "Vector2D", "Quat"):
                properties(start + size, path + ".", depth + 1)
            data.pos = start + size
        raise ValueError("ACC CarData 属性缺少结束标记")

    properties(len(export))
    return result


class ACCVehicleData:
    def __init__(self, config):
        self.config, self.pak = config, None
        self.signature = None

    def read(self, car):
        root = find_install(self.config)
        paths = (root / "AC2/Content/Paks/AC2-WindowsNoEditor.pak",
                 root / "AC2/Binaries/Win64/AC2-Win64-Shipping.exe")
        signature = tuple((str(p), p.stat().st_size, p.stat().st_mtime_ns) for p in paths)
        if self.pak is None or self.signature != signature:
            self.pak = Pak(root)
            self.signature = signature
        return self.pak.car(car)
