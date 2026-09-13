"""Adapter-owned vehicle catalog; never reads or writes dashboard profiles."""
import json
import logging
import math
from pathlib import Path

class VehicleStore:
    def __init__(self, directory=None):
        self.records = {}
        if directory is None:
            return
        for path in Path(directory).glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8-sig"))
                car = data["car_id"]
                values = data["suspension"]["fallback_max_travel_m"]
                if data.get("schema_version") != 1 or not isinstance(car, str) or len(values) != 4:
                    raise ValueError("invalid vehicle schema")
                if any(isinstance(v, bool) or not math.isfinite(float(v)) or not 0 < float(v) <= 2 for v in values):
                    raise ValueError("invalid suspension range")
                self.records[car] = tuple(map(float, values))
            except (ValueError, TypeError, KeyError, OSError) as exc:
                logging.getLogger("acbridge").warning("车辆配置忽略 %s: %s", path.name, exc)

    def ranges(self, car):
        return self.records.get(car, (0., 0., 0., 0.))
