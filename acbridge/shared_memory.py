"""AC1 shared-memory ABI. Read-only OpenFileMapping; never creates game pages.

Layout reference: mdjarv/assettocorsasharedmemory (Physics/Graphics/StaticInfo).
Explicit UTF-16 arrays keep the ABI testable on non-Windows hosts as well.
Only the stable prefixes needed by this bridge are mapped.
"""
import ctypes as C
from ctypes import wintypes as W
import os
import struct
import time

I, F, U = C.c_int32, C.c_float, C.c_uint16


class Physics(C.LittleEndianStructure):
    _pack_ = 4
    _fields_ = [
        ("packetId", I), ("gas", F), ("brake", F), ("fuel", F),
        ("gear", I), ("rpms", I), ("steerAngle", F), ("speedKmh", F),
        ("velocity", F*3), ("accG", F*3), ("wheelSlip", F*4),
        ("wheelLoad", F*4), ("wheelsPressure", F*4), ("wheelAngularSpeed", F*4),
        ("tyreWear", F*4), ("tyreDirtyLevel", F*4), ("tyreCoreTemperature", F*4),
        ("camberRAD", F*4), ("suspensionTravel", F*4),
        ("drs", F), ("tc", F), ("heading", F), ("pitch", F), ("roll", F),
        ("cgHeight", F), ("carDamage", F*5), ("numberOfTyresOut", I),
        ("pitLimiterOn", I), ("abs", F), ("kersCharge", F), ("kersInput", F),
        ("autoShifterOn", I), ("rideHeight", F*2), ("turboBoost", F),
        ("ballast", F), ("airDensity", F), ("airTemp", F), ("roadTemp", F),
        ("localAngularVelocity", F*3), ("finalFF", F), ("performanceMeter", F),
        ("engineBrake", I), ("ersRecoveryLevel", I), ("ersPowerLevel", I),
        ("ersHeatCharging", I), ("ersIsCharging", I), ("kersCurrentKJ", F),
        ("drsAvailable", I), ("drsEnabled", I), ("brakeTemp", F*4),
        ("clutch", F), ("tyreTempI", F*4), ("tyreTempM", F*4), ("tyreTempO", F*4),
        ("isAIControlled", I), ("tyreContactPoint", F*12),
        ("tyreContactNormal", F*12), ("tyreContactHeading", F*12),
        ("brakeBias", F), ("localVelocity", F*3),
    ]


class Graphics(C.LittleEndianStructure):
    _pack_ = 4
    _fields_ = [
        ("packetId", I), ("status", I), ("session", I),
        ("currentTime", U*15), ("lastTime", U*15), ("bestTime", U*15), ("split", U*15),
        ("completedLaps", I), ("position", I), ("iCurrentTime", I),
        ("iLastTime", I), ("iBestTime", I), ("sessionTimeLeft", F),
        ("distanceTraveled", F), ("isInPit", I), ("currentSectorIndex", I),
        ("lastSectorTime", I), ("numberOfLaps", I), ("tyreCompound", U*33),
        ("replayTimeMultiplier", F), ("normalizedCarPosition", F),
        ("carCoordinates", F*3), ("penaltyTime", F), ("flag", I),
        ("idealLineOn", I), ("isInPitLane", I), ("surfaceGrip", F),
        ("mandatoryPitDone", I),
    ]


class Static(C.LittleEndianStructure):
    _pack_ = 4
    _fields_ = [
        ("smVersion", U*15), ("acVersion", U*15),
        ("numberOfSessions", I), ("numCars", I), ("carModel", U*33),
        ("track", U*33), ("playerName", U*33), ("playerSurname", U*33), ("playerNick", U*33),
        ("sectorCount", I), ("maxTorque", F), ("maxPower", F), ("maxRpm", I),
        ("maxFuel", F), ("suspensionMaxTravel", F*4), ("tyreRadius", F*4),
        ("maxTurboBoost", F), ("deprecated1", F), ("deprecated2", F),
        ("penaltiesEnabled", I), ("aidFuelRate", F), ("aidTireRate", F),
        ("aidMechanicalDamage", F), ("aidAllowTyreBlankets", I), ("aidStability", F),
        ("aidAutoClutch", I), ("aidAutoBlip", I), ("hasDRS", I), ("hasERS", I),
        ("hasKERS", I), ("kersMaxJoules", F), ("engineBrakeSettingsCount", I),
        ("ersPowerControllerCount", I), ("trackSplineLength", F), ("trackConfiguration", U*15),
    ]


def utf16(value):
    return bytes(value).decode("utf-16-le", errors="replace").split("\0", 1)[0]


class MissingGame(OSError):
    pass


class BusyFrame(OSError):
    pass


def running_games():
    """Identify producers of the shared acpmf_* names, without dependencies."""
    if os.name != "nt":
        raise OSError("游戏进程识别仅支持 Windows。")

    class ProcessEntry(C.Structure):
        _fields_ = [("size", W.DWORD), ("usage", W.DWORD), ("pid", W.DWORD),
                    ("heap", C.c_size_t), ("module", W.DWORD), ("threads", W.DWORD),
                    ("parent", W.DWORD), ("priority", W.LONG), ("flags", W.DWORD),
                    ("exe", W.WCHAR * 260)]

    dll = C.WinDLL("kernel32", use_last_error=True)
    dll.CreateToolhelp32Snapshot.argtypes = [W.DWORD, W.DWORD]
    dll.CreateToolhelp32Snapshot.restype = W.HANDLE
    for name in ("Process32FirstW", "Process32NextW"):
        func = getattr(dll, name)
        func.argtypes = [W.HANDLE, C.POINTER(ProcessEntry)]
        func.restype = W.BOOL
    dll.CloseHandle.argtypes = [W.HANDLE]
    dll.CloseHandle.restype = W.BOOL
    handle = dll.CreateToolhelp32Snapshot(2, 0)
    if handle == C.c_void_p(-1).value:
        raise C.WinError(C.get_last_error())
    games = set()
    try:
        entry = ProcessEntry()
        entry.size = C.sizeof(entry)
        found = dll.Process32FirstW(handle, C.byref(entry))
        while found:
            exe = entry.exe.lower()
            if exe in ("acs.exe", "acs_x86.exe"):
                games.add("ac")
            elif exe == "ac2-win64-shipping.exe":
                games.add("acc")
            found = dll.Process32NextW(handle, C.byref(entry))
        error = C.get_last_error()
        if error != 18:  # ERROR_NO_MORE_FILES
            raise C.WinError(error)
    finally:
        dll.CloseHandle(handle)
    return games


def kernel():
    if os.name != "nt":
        raise OSError("共享内存读取仅支持 Windows。")
    dll = C.WinDLL("kernel32", use_last_error=True)
    dll.OpenFileMappingW.argtypes = [W.DWORD, W.BOOL, W.LPCWSTR]
    dll.OpenFileMappingW.restype = W.HANDLE
    dll.MapViewOfFile.argtypes = [W.HANDLE, W.DWORD, W.DWORD, W.DWORD, C.c_size_t]
    dll.MapViewOfFile.restype = C.c_void_p
    dll.UnmapViewOfFile.argtypes = [C.c_void_p]
    dll.UnmapViewOfFile.restype = W.BOOL
    dll.CloseHandle.argtypes = [W.HANDLE]
    dll.CloseHandle.restype = W.BOOL
    return dll


class ReadPage:
    def __init__(self, name, layout):
        self.dll = kernel()
        self.layout = layout
        self.size = C.sizeof(layout)
        self.handle = self.dll.OpenFileMappingW(4, False, name)  # FILE_MAP_READ
        self.pointer = None
        if not self.handle:
            code = C.get_last_error()
            if code == 2:
                raise MissingGame("等待神力科莎进入赛道")
            raise C.WinError(code)
        self.pointer = self.dll.MapViewOfFile(self.handle, 4, 0, 0, self.size)
        if not self.pointer:
            code = C.get_last_error()
            self.close()
            raise C.WinError(code)

    def read(self, counter=True):
        # Best-effort same-page consistency; AC provides no cross-page transaction.
        for _ in range(5):
            first = C.string_at(self.pointer, 4) if counter else None
            raw = C.string_at(self.pointer, self.size)
            if not counter or first == raw[:4] == C.string_at(self.pointer, 4):
                return self.layout.from_buffer_copy(raw)
        raise BusyFrame("游戏正在更新数据，等待下一帧")

    def close(self):
        if self.pointer:
            self.dll.UnmapViewOfFile(self.pointer)
            self.pointer = None
        if self.handle:
            self.dll.CloseHandle(self.handle)
            self.handle = None


class ACReader:
    def __init__(self, prefix="acpmf_", game="ac"):
        if game not in ("ac", "acc"):
            raise ValueError("游戏数据源必须为 ac 或 acc")
        self.prefix = prefix
        self.game = game
        self.pages = []
        self.next_process_check = 0.

    def read(self):
        # AC1 and ACC publish the same names with different ABIs. Explicit
        # selection alone cannot protect against reading the other game's pages.
        if self.prefix == "acpmf_" and time.monotonic() >= self.next_process_check:
            games = running_games()
            if len(games) > 1:
                raise MissingGame("AC1 与 ACC 同时运行，共享内存冲突，请只保留所选游戏")
            if games != {self.game}:
                raise MissingGame(f"等待所选游戏 {self.game.upper()} 启动并进入赛道")
            # Toolhelp walks the full system process list; keep it out of the
            # 60–240 Hz hot path. Reconnects always perform a fresh check.
            self.next_process_check = time.monotonic() + .25
        if not self.pages:
            try:
                if self.game == "acc":
                    from .acc import ACCPhysics, ACCGraphics, ACCStatic
                    layouts = (ACCPhysics, ACCGraphics, ACCStatic)
                else:
                    layouts = (Physics, Graphics, Static)
                for suffix, layout in zip(("physics", "graphics", "static"), layouts):
                    self.pages.append(ReadPage(self.prefix + suffix, layout))
            except Exception:
                self.close()
                raise
        p = self.pages[0].read()
        g = self.pages[1].read()
        s = self.pages[2].read(False)
        return p, g, s

    def close(self):
        for page in self.pages:
            page.close()
        self.pages = []
        self.next_process_check = 0.
