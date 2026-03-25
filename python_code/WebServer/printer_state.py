from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.{time.time_ns()}.tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)

    last_error: Exception | None = None
    for _ in range(5):
        try:
            os.replace(tmp, path)
            return
        except PermissionError as exc:
            last_error = exc
            time.sleep(0.02)

    if tmp.exists():
        tmp.unlink(missing_ok=True)
    if last_error is not None:
        raise last_error


def default_state(motor_state_file: Path, motor_progress_file: Path) -> dict[str, Any]:
    now = now_iso()
    return {
        "machine_state": "idle",
        "status_text": "待机",
        "updated_at": now,
        "devices": {
            "motor": {
                "connected": False,
                "message": "未连接",
                "position_um": 0.0,
                "position_steps": 0,
                "top_limit_triggered": False,
                "is_moving": False,
                "direction": None,
            },
            "uv": {
                "connected": False,
                "message": "未连接",
                "power": 80,
                "lamp_on": False,
            },
            "magnet": {
                "connected": False,
                "message": "未连接",
                "enabled": False,
                "voltage": 0.0,
                "io_level": "low",
                "i2c_addresses": [],
            },
        },
        "images": {
            "upload_id": None,
            "count": 0,
            "directory": None,
            "files": [],
            "preview_index": 1,
        },
        "job": {
            "active": False,
            "paused": False,
            "phase": "idle",
            "current_layer": 0,
            "completed_layers": 0,
            "total_layers": 0,
            "progress_percent": 0.0,
            "current_image_url": None,
            "current_image_name": None,
            "started_at": None,
            "finished_at": None,
            "error": None,
        },
        "settings": {
            "fast_down_distance_um": 1000,
            "fast_down_speed_um_s": 20,
            "fast_up_distance_um": 1000,
            "fast_up_speed_um_s": 20,
            "slow_down_speed_um_s": 5,
            "slow_up_speed_um_s": 5,
            "exposure_time_s": 1.5,
            "exposure_power": 80,
            "inter_layer_delay_s": 0.5,
            "magnet_enabled_for_job": False,
            "magnet_voltage": 2.0,
            "magnet_keep_on_between_layers": True,
            "motor_steps_per_rev": 3200,
            "motor_lead_mm": 4.0,
            "motor_pul_pin": 13,
            "motor_dir_pin": 5,
            "motor_ena_pin": 8,
            "motor_top_limit_pin": 20,
            "motor_ena_active_low": True,
            "motor_pulse_width_us": 20,
            "motor_chunk_steps": 200,
            "motor_state_file": str(motor_state_file),
            "motor_progress_file": str(motor_progress_file),
            "motor_home_max_steps": 200000,
            "motor_home_speed_um_s": 1500,
            "uv_port": "/dev/ttyUSB0",
            "uv_baudrate": 115200,
            "uv_timeout_s": 1.0,
            "magnet_i2c_bus": 1,
            "magnet_i2c_addr_primary": "0x60",
            "magnet_i2c_addr_secondary": "0x61",
            "magnet_io_pin": 21,
            "magnet_io_high_when_enabled": True,
            "auto_pull_up_after_finish": True,
            "auto_pull_up_distance_um": 5000,
            "preview_layer_index": 1,
        },
        "logs": [
            {"time": now, "level": "info", "message": "系统已初始化，等待设备连接"},
        ],
    }


class PersistentState:
    def __init__(self, path: Path, motor_state_file: Path, motor_progress_file: Path):
        self.path = path
        self.lock = threading.RLock()
        self.defaults = default_state(motor_state_file, motor_progress_file)
        self.data = self._load()
        self._normalize_after_restart()
        self.save()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return json.loads(json.dumps(self.defaults))
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                loaded = json.load(handle)
        except Exception:
            return json.loads(json.dumps(self.defaults))

        state = json.loads(json.dumps(self.defaults))
        self._deep_merge(state, loaded)
        return state

    def _normalize_after_restart(self) -> None:
        settings = self.data["settings"]
        defaults = self.defaults["settings"]

        if "magnet_i2c_addr" in settings:
            settings.setdefault("magnet_i2c_addr_primary", settings["magnet_i2c_addr"])
            settings.pop("magnet_i2c_addr", None)

        for key in (
            "motor_home_max_steps",
            "motor_home_speed_um_s",
            "magnet_i2c_addr_primary",
            "magnet_i2c_addr_secondary",
            "magnet_io_pin",
            "magnet_io_high_when_enabled",
        ):
            settings.setdefault(key, defaults[key])

        settings["motor_state_file"] = defaults["motor_state_file"]
        settings["motor_progress_file"] = defaults["motor_progress_file"]

        self.data["devices"]["motor"]["is_moving"] = False
        self.data["devices"]["motor"]["direction"] = None
        self.data["devices"]["uv"]["lamp_on"] = False

        job = self.data["job"]
        if job["active"] or job["paused"]:
            job["active"] = False
            job["paused"] = False
            job["phase"] = "interrupted"
            self.data["machine_state"] = "idle"
            self.data["status_text"] = "服务重启，上次任务未自动恢复"
            self.add_log("检测到服务重启，请确认设备状态后重新开始打印", save=False)

    def _deep_merge(self, base: dict[str, Any], override: dict[str, Any]) -> None:
        for key, value in override.items():
            if isinstance(value, dict) and isinstance(base.get(key), dict):
                self._deep_merge(base[key], value)
            else:
                base[key] = value

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return json.loads(json.dumps(self.data))

    def save(self) -> None:
        with self.lock:
            self.data["updated_at"] = now_iso()
            atomic_write_json(self.path, self.data)

    def mutate(self, fn: Callable[[dict[str, Any]], Any]) -> Any:
        with self.lock:
            result = fn(self.data)
            self.data["updated_at"] = now_iso()
            atomic_write_json(self.path, self.data)
            return result

    def add_log(self, message: str, level: str = "info", save: bool = True) -> None:
        with self.lock:
            self.data["logs"].append({"time": now_iso(), "level": level, "message": message})
            self.data["logs"] = self.data["logs"][-200:]
            self.data["updated_at"] = now_iso()
            if save:
                atomic_write_json(self.path, self.data)
