from __future__ import annotations

import json
import mimetypes
import os
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, render_template, request, send_from_directory


BASE_DIR = Path(__file__).resolve().parent
WEB_DIR = BASE_DIR / "webapp"
DATA_DIR = BASE_DIR / "webapp_data"
UPLOADS_DIR = DATA_DIR / "uploads"
STATE_FILE = DATA_DIR / "printer_state.json"
MOTOR_STATE_FILE = DATA_DIR / "motor_state.json"

ALLOWED_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp"}


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.{time.time_ns()}.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    last_error = None
    for _ in range(5):
        try:
            os.replace(tmp, path)
            return
        except PermissionError as exc:
            last_error = exc
            time.sleep(0.02)
    if tmp.exists():
        tmp.unlink(missing_ok=True)
    if last_error:
        raise last_error


def natural_sort_key(value: str) -> list[Any]:
    result: list[Any] = []
    chunk = ""
    is_digit = None
    for char in value:
        char_is_digit = char.isdigit()
        if is_digit is None or char_is_digit == is_digit:
            chunk += char
            is_digit = char_is_digit
            continue
        result.append((0, int(chunk)) if is_digit else (1, chunk.lower()))
        chunk = char
        is_digit = char_is_digit
    if chunk:
        result.append((0, int(chunk)) if is_digit else (1, chunk.lower()))
    return result


def relative_upload_url(upload_id: str, relative_path: str) -> str:
    return f"/uploads/{upload_id}/{relative_path.replace(os.sep, '/')}"


def default_state() -> dict[str, Any]:
    return {
        "machine_state": "idle",
        "status_text": "待机",
        "updated_at": now_iso(),
        "devices": {
            "motor": {"connected": False, "message": "未连接", "position_um": 0.0},
            "uv": {"connected": False, "message": "未连接", "power": 80, "lamp_on": False},
            "magnet": {"connected": False, "message": "未连接", "enabled": False, "voltage": 0.0},
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
            "motor_state_file": str(MOTOR_STATE_FILE),
            "uv_port": "/dev/ttyUSB0",
            "uv_baudrate": 115200,
            "uv_timeout_s": 1.0,
            "magnet_i2c_bus": 1,
            "magnet_i2c_addr": "0x60",
            "auto_pull_up_after_finish": True,
            "auto_pull_up_distance_um": 5000,
            "preview_layer_index": 1,
        },
        "logs": [
            {"time": now_iso(), "level": "info", "message": "系统已初始化，等待设备连接"},
        ],
    }


class PersistentState:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.RLock()
        self.data = self._load()
        self._normalize_after_restart()
        self.save()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return default_state()
        try:
            with self.path.open("r", encoding="utf-8") as f:
                loaded = json.load(f)
        except Exception:
            return default_state()

        state = default_state()
        self._deep_merge(state, loaded)
        return state

    def _normalize_after_restart(self) -> None:
        job = self.data["job"]
        if job["active"] or job["paused"]:
            job["active"] = False
            job["paused"] = False
            job["phase"] = "interrupted"
            self.data["machine_state"] = "idle"
            self.data["status_text"] = "服务已重启，打印任务未自动恢复"
            self.add_log("检测到服务重启，上一次任务未自动恢复，请确认设备状态后重新开始。", save=False)

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

    def mutate(self, fn) -> Any:
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


class MotorController:
    def __init__(self, base_dir: Path, state: PersistentState):
        self.base_dir = base_dir
        self.state = state
        self.script = base_dir / "motor_api.py"

    def _settings(self) -> dict[str, Any]:
        return self.state.snapshot()["settings"]

    def _calc_freq_from_speed(self, speed_um_s: float, steps_per_rev: int, lead_mm: float) -> int:
        lead_um = lead_mm * 1000.0
        freq = speed_um_s * steps_per_rev / lead_um
        return max(1, int(round(freq)))

    def _run_script(self, args: list[str]) -> dict[str, Any]:
        if not self.script.exists():
            raise RuntimeError("motor_api.py 不存在")

        proc = subprocess.run(
            [sys.executable, str(self.script), *args],
            cwd=str(self.base_dir),
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        payload = None
        for line in reversed((proc.stdout or "").splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
                break
            except json.JSONDecodeError:
                continue
        if payload is None:
            raise RuntimeError((proc.stderr or proc.stdout or "电机接口无返回").strip())
        if not payload.get("success", False):
            raise RuntimeError(payload.get("message", "电机操作失败"))
        return payload

    def _update_position(self, position_um: float, connected: bool = True, message: str = "已连接") -> None:
        def apply(data: dict[str, Any]) -> None:
            data["devices"]["motor"]["connected"] = connected
            data["devices"]["motor"]["message"] = message
            data["devices"]["motor"]["position_um"] = round(position_um, 3)

        self.state.mutate(apply)

    def connect(self) -> dict[str, Any]:
        settings = self._settings()
        if self.script.exists():
            try:
                payload = self._run_script(
                    [
                        "--action",
                        "status",
                        "--state-file",
                        settings["motor_state_file"],
                        "--steps-per-rev",
                        str(settings["motor_steps_per_rev"]),
                        "--lead-mm",
                        str(settings["motor_lead_mm"]),
                        "--pul",
                        str(settings["motor_pul_pin"]),
                        "--dir",
                        str(settings["motor_dir_pin"]),
                        "--ena",
                        str(settings["motor_ena_pin"]),
                        "--top-limit",
                        str(settings["motor_top_limit_pin"]),
                    ]
                    + (["--ena-active-low"] if settings["motor_ena_active_low"] else [])
                )
                self._update_position(payload.get("position_um", 0.0))
                return payload
            except Exception as exc:
                self.state.add_log(f"电机真实连接失败，切换为模拟状态: {exc}", level="warning")

        position = self.state.snapshot()["devices"]["motor"].get("position_um", 0.0)
        self._update_position(position)
        return {"success": True, "message": "电机模拟接口已连接", "position_um": position}

    def status(self) -> dict[str, Any]:
        return self.state.snapshot()["devices"]["motor"]

    def move(self, direction: str, distance_um: float, speed_um_s: float) -> dict[str, Any]:
        settings = self._settings()
        if distance_um <= 0:
            raise RuntimeError("移动距离必须大于 0")
        if speed_um_s <= 0:
            raise RuntimeError("移动速度必须大于 0")

        if self.script.exists():
            try:
                freq = self._calc_freq_from_speed(
                    speed_um_s,
                    settings["motor_steps_per_rev"],
                    settings["motor_lead_mm"],
                )
                payload = self._run_script(
                    [
                        "--action",
                        "move",
                        "--move",
                        direction,
                        "--um",
                        str(distance_um),
                        "--freq",
                        str(freq),
                        "--state-file",
                        settings["motor_state_file"],
                        "--steps-per-rev",
                        str(settings["motor_steps_per_rev"]),
                        "--lead-mm",
                        str(settings["motor_lead_mm"]),
                        "--pul",
                        str(settings["motor_pul_pin"]),
                        "--dir",
                        str(settings["motor_dir_pin"]),
                        "--ena",
                        str(settings["motor_ena_pin"]),
                        "--top-limit",
                        str(settings["motor_top_limit_pin"]),
                        "--pulse-width-us",
                        str(settings["motor_pulse_width_us"]),
                        "--chunk-steps",
                        str(settings["motor_chunk_steps"]),
                    ]
                    + (["--ena-active-low"] if settings["motor_ena_active_low"] else [])
                )
                self._update_position(payload.get("position_um", 0.0))
                return payload
            except Exception as exc:
                self.state.add_log(f"电机真实移动失败，改用模拟位移: {exc}", level="warning")

        current = float(self.state.snapshot()["devices"]["motor"].get("position_um", 0.0))
        position_um = max(0.0, current - distance_um) if direction == "up" else current + distance_um
        self._update_position(position_um)
        return {
            "success": True,
            "message": f"模拟移动完成: {direction} {distance_um}um",
            "position_um": position_um,
        }

    def home(self) -> dict[str, Any]:
        settings = self._settings()
        if self.script.exists():
            try:
                payload = self._run_script(
                    [
                        "--action",
                        "move",
                        "--move",
                        "up",
                        "--steps",
                        "200000",
                        "--freq",
                        "1200",
                        "--state-file",
                        settings["motor_state_file"],
                        "--steps-per-rev",
                        str(settings["motor_steps_per_rev"]),
                        "--lead-mm",
                        str(settings["motor_lead_mm"]),
                        "--pul",
                        str(settings["motor_pul_pin"]),
                        "--dir",
                        str(settings["motor_dir_pin"]),
                        "--ena",
                        str(settings["motor_ena_pin"]),
                        "--top-limit",
                        str(settings["motor_top_limit_pin"]),
                        "--pulse-width-us",
                        str(settings["motor_pulse_width_us"]),
                        "--chunk-steps",
                        str(settings["motor_chunk_steps"]),
                    ]
                    + (["--ena-active-low"] if settings["motor_ena_active_low"] else [])
                )
                self._update_position(payload.get("position_um", 0.0))
                return payload
            except Exception as exc:
                self.state.add_log(f"机械归零失败，改用模拟归零: {exc}", level="warning")

        self._update_position(0.0)
        return {"success": True, "message": "模拟机械归零完成", "position_um": 0.0}


class UVController:
    CMD_HANDSHAKE = "A6 01 05"
    CMD_DLP_ON = "A6 02 02 01"
    CMD_DLP_OFF = "A6 02 02 00"
    CMD_LED_ON = "A6 02 03 01"
    CMD_LED_OFF = "A6 02 03 00"

    def __init__(self, state: PersistentState):
        self.state = state

    def _settings(self) -> dict[str, Any]:
        return self.state.snapshot()["settings"]

    @contextmanager
    def _serial(self):
        try:
            import serial  # type: ignore
        except Exception as exc:
            raise RuntimeError(f"pyserial 不可用: {exc}") from exc

        settings = self._settings()
        ser = serial.Serial(
            settings["uv_port"],
            int(settings["uv_baudrate"]),
            timeout=float(settings["uv_timeout_s"]),
        )
        try:
            yield ser
        finally:
            ser.close()

    def _send_hex(self, ser, hex_str: str) -> str:
        ser.reset_input_buffer()
        ser.write(bytes.fromhex(hex_str))
        response = ser.read(10)
        return " ".join(f"{b:02X}" for b in response)

    def _set_state(self, connected: bool, message: str, lamp_on: bool | None = None, power: int | None = None) -> None:
        def apply(data: dict[str, Any]) -> None:
            data["devices"]["uv"]["connected"] = connected
            data["devices"]["uv"]["message"] = message
            if lamp_on is not None:
                data["devices"]["uv"]["lamp_on"] = lamp_on
            if power is not None:
                data["devices"]["uv"]["power"] = power

        self.state.mutate(apply)

    def connect(self) -> dict[str, Any]:
        try:
            with self._serial() as ser:
                self._send_hex(ser, self.CMD_HANDSHAKE)
                self._send_hex(ser, self.CMD_DLP_ON)
            self._set_state(True, "已连接")
            return {"success": True, "message": "UV 光机已连接"}
        except Exception as exc:
            self._set_state(True, f"模拟连接: {exc}")
            self.state.add_log(f"UV 光机未进入真实串口模式，已启用模拟接口: {exc}", level="warning")
            return {"success": True, "message": "UV 光机模拟接口已连接"}

    def set_power(self, power: int) -> dict[str, Any]:
        power = max(0, min(255, int(power)))
        brightness_cmd = f"A6 02 10 {power:02X}"
        try:
            with self._serial() as ser:
                self._send_hex(ser, self.CMD_HANDSHAKE)
                self._send_hex(ser, self.CMD_DLP_ON)
                self._send_hex(ser, brightness_cmd)
            self._set_state(True, "亮度已更新", power=power)
            return {"success": True, "message": f"曝光强度已设置为 {power}"}
        except Exception as exc:
            self._set_state(True, f"模拟亮度更新: {exc}", power=power)
            return {"success": True, "message": f"模拟曝光强度已设置为 {power}"}

    def lamp(self, enabled: bool) -> dict[str, Any]:
        cmd = self.CMD_LED_ON if enabled else self.CMD_LED_OFF
        message = "UV 灯已开启" if enabled else "UV 灯已关闭"
        try:
            with self._serial() as ser:
                self._send_hex(ser, self.CMD_HANDSHAKE)
                self._send_hex(ser, self.CMD_DLP_ON)
                self._send_hex(ser, cmd)
            self._set_state(True, message, lamp_on=enabled)
            return {"success": True, "message": message}
        except Exception as exc:
            self._set_state(True, f"模拟灯控: {exc}", lamp_on=enabled)
            return {"success": True, "message": f"模拟{message}"}

    def show_image(self, image_name: str) -> dict[str, Any]:
        self.state.add_log(f"已切换到图层图像: {image_name}")
        return {"success": True, "message": f"已切换图像 {image_name}"}

    def expose(self, power: int, duration_s: float) -> dict[str, Any]:
        duration_s = max(0.05, float(duration_s))
        power = max(0, min(255, int(power)))
        self.set_power(power)
        self.lamp(True)
        time.sleep(duration_s)
        self.lamp(False)
        self._set_state(True, f"曝光完成 {duration_s:.2f}s", lamp_on=False, power=power)
        return {"success": True, "message": f"曝光完成 {duration_s:.2f}s", "duration_s": duration_s, "power": power}


class MagnetController:
    def __init__(self, state: PersistentState):
        self.state = state

    def _settings(self) -> dict[str, Any]:
        return self.state.snapshot()["settings"]

    def _write_voltage(self, voltage: float) -> None:
        settings = self._settings()
        try:
            import smbus2  # type: ignore
        except Exception as exc:
            raise RuntimeError(f"smbus2 不可用: {exc}") from exc

        bus_id = int(settings["magnet_i2c_bus"])
        addr = int(str(settings["magnet_i2c_addr"]), 16)
        dac = int(max(0.0, min(5.0, voltage)) / 5.0 * 4095)
        high = dac >> 4
        low = (dac & 0x0F) << 4
        bus = smbus2.SMBus(bus_id)
        try:
            bus.write_i2c_block_data(addr, 0x40, [high, low])
        finally:
            bus.close()

    def _set_state(self, connected: bool, message: str, enabled: bool | None = None, voltage: float | None = None) -> None:
        def apply(data: dict[str, Any]) -> None:
            data["devices"]["magnet"]["connected"] = connected
            data["devices"]["magnet"]["message"] = message
            if enabled is not None:
                data["devices"]["magnet"]["enabled"] = enabled
            if voltage is not None:
                data["devices"]["magnet"]["voltage"] = round(voltage, 3)

        self.state.mutate(apply)

    def connect(self) -> dict[str, Any]:
        voltage = float(self.state.snapshot()["devices"]["magnet"].get("voltage", 0.0))
        self._set_state(True, "已连接", enabled=voltage > 0.0, voltage=voltage)
        return {"success": True, "message": "磁场控制已连接"}

    def set_voltage(self, voltage: float, enabled: bool = True) -> dict[str, Any]:
        voltage = max(0.0, min(5.0, float(voltage)))
        try:
            self._write_voltage(voltage if enabled else 0.0)
            self._set_state(True, "磁场已更新", enabled=enabled and voltage > 0.0, voltage=voltage if enabled else 0.0)
            return {"success": True, "message": f"磁场电压已设置为 {voltage:.3f}V"}
        except Exception as exc:
            applied = voltage if enabled else 0.0
            self._set_state(True, f"模拟磁场更新: {exc}", enabled=enabled and voltage > 0.0, voltage=applied)
            return {"success": True, "message": f"模拟磁场电压已设置为 {applied:.3f}V"}

    def off(self) -> dict[str, Any]:
        return self.set_voltage(0.0, enabled=False)


class PrinterJobRunner:
    def __init__(self, state: PersistentState, motor: MotorController, uv: UVController, magnet: MagnetController):
        self.state = state
        self.motor = motor
        self.uv = uv
        self.magnet = magnet
        self.thread: threading.Thread | None = None
        self.pause_event = threading.Event()
        self.stop_event = threading.Event()
        self.control_lock = threading.Lock()

    def _set_job_state(self, **updates: Any) -> None:
        def apply(data: dict[str, Any]) -> None:
            data["job"].update(updates)

        self.state.mutate(apply)

    def _set_machine_state(self, machine_state: str, status_text: str) -> None:
        def apply(data: dict[str, Any]) -> None:
            data["machine_state"] = machine_state
            data["status_text"] = status_text

        self.state.mutate(apply)

    def _wait_if_paused(self) -> None:
        while self.pause_event.is_set() and not self.stop_event.is_set():
            time.sleep(0.2)

    def _interruptible_sleep(self, seconds: float) -> None:
        end = time.time() + max(0.0, seconds)
        while time.time() < end:
            if self.stop_event.is_set():
                break
            self._wait_if_paused()
            time.sleep(0.1)

    def start(self) -> dict[str, Any]:
        with self.control_lock:
            state = self.state.snapshot()
            if self.thread and self.thread.is_alive():
                raise RuntimeError("已有打印任务在运行")
            if state["images"]["count"] <= 0:
                raise RuntimeError("请先上传切片图像 ZIP")

            self.pause_event.clear()
            self.stop_event.clear()
            total_layers = state["images"]["count"]

            def apply(data: dict[str, Any]) -> None:
                data["machine_state"] = "running"
                data["status_text"] = "自动打印进行中"
                data["job"] = {
                    "active": True,
                    "paused": False,
                    "phase": "starting",
                    "current_layer": 0,
                    "completed_layers": 0,
                    "total_layers": total_layers,
                    "progress_percent": 0.0,
                    "current_image_url": None,
                    "current_image_name": None,
                    "started_at": now_iso(),
                    "finished_at": None,
                    "error": None,
                }

            self.state.mutate(apply)
            self.state.add_log(f"自动打印已启动，共 {total_layers} 层")
            self.thread = threading.Thread(target=self._run_job, name="printer-job", daemon=True)
            self.thread.start()
            return {"success": True, "message": "自动打印已启动"}

    def pause(self) -> dict[str, Any]:
        if not self.thread or not self.thread.is_alive():
            raise RuntimeError("当前没有运行中的打印任务")
        self.pause_event.set()
        self._set_machine_state("paused", "打印已暂停")
        self._set_job_state(paused=True, phase="paused")
        self.state.add_log("打印任务已暂停")
        return {"success": True, "message": "打印已暂停"}

    def resume(self) -> dict[str, Any]:
        if not self.thread or not self.thread.is_alive():
            raise RuntimeError("当前没有可继续的打印任务")
        self.pause_event.clear()
        self._set_machine_state("running", "自动打印进行中")
        self._set_job_state(paused=False, phase="resuming")
        self.state.add_log("打印任务已继续")
        return {"success": True, "message": "打印已继续"}

    def stop(self) -> dict[str, Any]:
        if not self.thread or not self.thread.is_alive():
            raise RuntimeError("当前没有运行中的打印任务")
        self.stop_event.set()
        self.pause_event.clear()
        self.state.add_log("正在请求停止打印任务", level="warning")
        return {"success": True, "message": "正在停止打印任务"}

    def _run_job(self) -> None:
        try:
            state = self.state.snapshot()
            settings = state["settings"]
            images = state["images"]["files"]
            keep_magnet = bool(settings["magnet_keep_on_between_layers"])
            enable_magnet = bool(settings["magnet_enabled_for_job"])
            magnet_voltage = float(settings["magnet_voltage"])

            if enable_magnet:
                self.magnet.set_voltage(magnet_voltage, enabled=True)
                self.state.add_log(f"打印前已设置磁场电压 {magnet_voltage:.3f}V")

            for idx, image in enumerate(images, start=1):
                if self.stop_event.is_set():
                    raise RuntimeError("打印任务已被停止")

                self._wait_if_paused()
                if self.stop_event.is_set():
                    raise RuntimeError("打印任务已被停止")

                self._set_job_state(
                    phase="switching-image",
                    current_layer=idx,
                    current_image_url=image["url"],
                    current_image_name=image["name"],
                )
                self._set_machine_state("running", f"正在打印第 {idx}/{len(images)} 层")
                self.state.add_log(f"第 {idx}/{len(images)} 层: 切换到 {image['name']}")
                self.uv.show_image(image["name"])

                if enable_magnet and not keep_magnet:
                    self.magnet.set_voltage(magnet_voltage, enabled=True)

                up_distance = float(settings["fast_up_distance_um"])
                up_speed = float(settings["fast_up_speed_um_s"])
                if up_distance > 0:
                    self._set_job_state(phase="moving-up")
                    self.motor.move("up", up_distance, up_speed)
                    self.state.add_log(f"第 {idx} 层: 电机上移 {up_distance:.1f}um")

                self._interruptible_sleep(float(settings["inter_layer_delay_s"]))
                if self.stop_event.is_set():
                    raise RuntimeError("打印任务已被停止")

                down_distance = float(settings["fast_down_distance_um"])
                down_speed = float(settings["fast_down_speed_um_s"])
                if down_distance > 0:
                    self._set_job_state(phase="moving-down")
                    self.motor.move("down", down_distance, down_speed)
                    self.state.add_log(f"第 {idx} 层: 电机下移 {down_distance:.1f}um")

                self._interruptible_sleep(float(settings["inter_layer_delay_s"]))
                if self.stop_event.is_set():
                    raise RuntimeError("打印任务已被停止")

                self._set_job_state(phase="exposing")
                self.uv.expose(int(settings["exposure_power"]), float(settings["exposure_time_s"]))
                self.state.add_log(
                    f"第 {idx} 层: 完成曝光 {float(settings['exposure_time_s']):.2f}s / 功率 {int(settings['exposure_power'])}"
                )

                if enable_magnet and not keep_magnet:
                    self.magnet.off()

                progress = round(idx / len(images) * 100, 2)
                self._set_job_state(
                    phase="layer-finished",
                    completed_layers=idx,
                    progress_percent=progress,
                )

            if enable_magnet and keep_magnet:
                self.magnet.off()

            if bool(settings["auto_pull_up_after_finish"]) and float(settings["auto_pull_up_distance_um"]) > 0:
                distance = float(settings["auto_pull_up_distance_um"])
                self.motor.move("up", distance, float(settings["fast_up_speed_um_s"]))
                self.state.add_log(f"打印完成后自动提起 {distance:.1f}um")

            def finish(data: dict[str, Any]) -> None:
                data["machine_state"] = "idle"
                data["status_text"] = "打印完成"
                data["job"]["active"] = False
                data["job"]["paused"] = False
                data["job"]["phase"] = "completed"
                data["job"]["finished_at"] = now_iso()
                data["job"]["progress_percent"] = 100.0
                data["job"]["completed_layers"] = data["job"]["total_layers"]
                data["job"]["error"] = None

            self.state.mutate(finish)
            self.state.add_log("自动打印任务已完成")
        except Exception as exc:
            self.uv.lamp(False)
            self.magnet.off()

            message = str(exc)
            is_stopped = "已被停止" in message

            def fail(data: dict[str, Any]) -> None:
                data["machine_state"] = "idle"
                data["status_text"] = "打印已停止" if is_stopped else "打印失败"
                data["job"]["active"] = False
                data["job"]["paused"] = False
                data["job"]["phase"] = "stopped" if is_stopped else "error"
                data["job"]["finished_at"] = now_iso()
                data["job"]["error"] = None if is_stopped else message

            self.state.mutate(fail)
            self.state.add_log(message, level="warning" if is_stopped else "error")
        finally:
            self.pause_event.clear()
            self.stop_event.clear()


state_store = PersistentState(STATE_FILE)
motor = MotorController(BASE_DIR, state_store)
uv = UVController(state_store)
magnet = MagnetController(state_store)
job_runner = PrinterJobRunner(state_store, motor, uv, magnet)

app = Flask(
    __name__,
    template_folder=str(WEB_DIR / "templates"),
    static_folder=str(WEB_DIR / "static"),
)


def success(message: str, **extra: Any):
    payload = {"success": True, "message": message}
    payload.update(extra)
    return jsonify(payload)


def failure(message: str, status_code: int = 400, **extra: Any):
    payload = {"success": False, "message": message}
    payload.update(extra)
    return jsonify(payload), status_code


def list_images(directory: Path, upload_id: str) -> list[dict[str, str]]:
    files: list[dict[str, str]] = []
    for path in directory.rglob("*"):
        if path.is_file() and path.suffix.lower() in ALLOWED_IMAGE_SUFFIXES:
            rel = path.relative_to(directory).as_posix()
            files.append(
                {
                    "name": path.name,
                    "relative_path": rel,
                    "url": relative_upload_url(upload_id, rel),
                    "mime": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                }
            )
    files.sort(key=lambda item: natural_sort_key(item["relative_path"]))
    return files


def safe_extract_zip(zip_path: Path, target_dir: Path) -> None:
    with zipfile.ZipFile(zip_path, "r") as zf:
        for member in zf.infolist():
            target_path = (target_dir / member.filename).resolve()
            if not str(target_path).startswith(str(target_dir.resolve())):
                raise RuntimeError("ZIP 文件包含非法路径")
        zf.extractall(target_dir)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/uploads/<upload_id>/<path:relative_path>")
def uploads(upload_id: str, relative_path: str):
    base = UPLOADS_DIR / upload_id
    return send_from_directory(base, relative_path)


@app.get("/api/state")
def api_state():
    return jsonify(state_store.snapshot())


@app.post("/api/settings")
def api_settings():
    payload = request.get_json(silent=True) or {}
    incoming = payload.get("settings")
    if not isinstance(incoming, dict):
        return failure("缺少 settings 数据")

    def apply(data: dict[str, Any]) -> None:
        for key, value in incoming.items():
            if key in data["settings"]:
                data["settings"][key] = value
        preview_index = int(max(1, data["settings"]["preview_layer_index"]))
        data["images"]["preview_index"] = preview_index

    state_store.mutate(apply)
    return success("参数已保存", settings=state_store.snapshot()["settings"])


@app.post("/api/devices/<device>/reconnect")
def api_device_reconnect(device: str):
    try:
        if device == "motor":
            result = motor.connect()
        elif device == "uv":
            result = uv.connect()
        elif device == "magnet":
            result = magnet.connect()
        else:
            return failure("未知设备", 404)
        state_store.add_log(result["message"])
        return success(result["message"], device=device, state=state_store.snapshot()["devices"][device])
    except Exception as exc:
        state_store.add_log(f"{device} 重连失败: {exc}", level="error")
        return failure(str(exc), 500)


@app.post("/api/upload")
def api_upload():
    uploaded = request.files.get("zipFile")
    if not uploaded or not uploaded.filename:
        return failure("请选择 ZIP 文件")
    if not uploaded.filename.lower().endswith(".zip"):
        return failure("仅支持 ZIP 文件")

    upload_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    target_dir = UPLOADS_DIR / upload_id
    tmp_zip = target_dir.with_suffix(".zip")
    target_dir.mkdir(parents=True, exist_ok=True)
    uploaded.save(tmp_zip)

    try:
        safe_extract_zip(tmp_zip, target_dir)
        files = list_images(target_dir, upload_id)
        if not files:
            raise RuntimeError("ZIP 中未找到图像文件")

        def apply(data: dict[str, Any]) -> None:
            data["images"]["upload_id"] = upload_id
            data["images"]["count"] = len(files)
            data["images"]["directory"] = str(target_dir)
            data["images"]["files"] = files
            data["images"]["preview_index"] = 1
            data["settings"]["preview_layer_index"] = 1
            data["job"]["total_layers"] = len(files)
            data["job"]["current_image_url"] = files[0]["url"]
            data["job"]["current_image_name"] = files[0]["name"]

        state_store.mutate(apply)
        state_store.add_log(f"切片包已上传，共 {len(files)} 张图像")
        return success(
            "上传成功",
            imageCount=len(files),
            previewUrl=files[0]["url"],
            uploadId=upload_id,
            uploadTime=now_iso(),
        )
    except Exception as exc:
        shutil.rmtree(target_dir, ignore_errors=True)
        return failure(str(exc))
    finally:
        if tmp_zip.exists():
            tmp_zip.unlink(missing_ok=True)


@app.post("/api/preview/select")
def api_preview_select():
    payload = request.get_json(silent=True) or {}
    index = int(payload.get("index", 1))
    snapshot = state_store.snapshot()
    total = snapshot["images"]["count"]
    if total <= 0:
        return failure("当前没有可预览的切片")
    index = max(1, min(total, index))

    def apply(data: dict[str, Any]) -> None:
        data["images"]["preview_index"] = index
        data["settings"]["preview_layer_index"] = index

    state_store.mutate(apply)
    return success("预览层已切换", index=index)


@app.post("/api/manual/move")
def api_manual_move():
    payload = request.get_json(silent=True) or {}
    direction = payload.get("direction")
    distance_um = float(payload.get("distance_um", 0))
    speed_um_s = float(payload.get("speed_um_s", 0))
    if direction not in {"up", "down"}:
        return failure("direction 必须是 up 或 down")
    try:
        result = motor.move(direction, distance_um, speed_um_s)
        state_store.add_log(result["message"])
        return success(result["message"], motor=state_store.snapshot()["devices"]["motor"])
    except Exception as exc:
        return failure(str(exc))


@app.post("/api/manual/home")
def api_manual_home():
    try:
        result = motor.home()
        state_store.add_log(result["message"])
        return success(result["message"], motor=state_store.snapshot()["devices"]["motor"])
    except Exception as exc:
        return failure(str(exc))


@app.post("/api/manual/expose")
def api_manual_expose():
    payload = request.get_json(silent=True) or {}
    power = int(payload.get("power", 0))
    duration_s = float(payload.get("duration_s", 0))
    try:
        result = uv.expose(power, duration_s)
        state_store.add_log(result["message"])
        return success(result["message"])
    except Exception as exc:
        return failure(str(exc))


@app.post("/api/manual/magnet")
def api_manual_magnet():
    payload = request.get_json(silent=True) or {}
    enabled = bool(payload.get("enabled", False))
    voltage = float(payload.get("voltage", 0.0))
    try:
        result = magnet.set_voltage(voltage, enabled=enabled) if enabled else magnet.off()
        state_store.add_log(result["message"])
        return success(result["message"], magnet=state_store.snapshot()["devices"]["magnet"])
    except Exception as exc:
        return failure(str(exc))


@app.post("/api/print/start")
def api_print_start():
    try:
        result = job_runner.start()
        return success(result["message"])
    except Exception as exc:
        return failure(str(exc))


@app.post("/api/print/pause")
def api_print_pause():
    try:
        result = job_runner.pause()
        return success(result["message"])
    except Exception as exc:
        return failure(str(exc))


@app.post("/api/print/resume")
def api_print_resume():
    try:
        result = job_runner.resume()
        return success(result["message"])
    except Exception as exc:
        return failure(str(exc))


@app.post("/api/print/stop")
def api_print_stop():
    try:
        result = job_runner.stop()
        return success(result["message"])
    except Exception as exc:
        return failure(str(exc))


if __name__ == "__main__":
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    app.run(host="0.0.0.0", port=5000, debug=False)
