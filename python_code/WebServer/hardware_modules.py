from __future__ import annotations

import json
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from printer_state import PersistentState


@dataclass(slots=True)
class MotorMoveRequest:
    direction: str
    distance_um: float
    speed_um_s: float


@dataclass(slots=True)
class UVOutputRequest:
    power: int
    lamp_on: bool


@dataclass(slots=True)
class MagnetCommand:
    voltage: float
    enabled: bool
    io_enabled: bool | None = None


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

    def _read_json_file(self, path: Path) -> dict[str, Any] | None:
        if not path.exists():
            return None
        try:
            with path.open("r", encoding="utf-8") as handle:
                return json.load(handle)
        except Exception:
            return None

    def _update_state(
        self,
        *,
        connected: bool,
        message: str,
        position_um: float | None = None,
        position_steps: int | None = None,
        top_limit_triggered: bool | None = None,
        is_moving: bool | None = None,
        direction: str | None = None,
    ) -> None:
        def apply(data: dict[str, Any]) -> None:
            motor = data["devices"]["motor"]
            motor["connected"] = connected
            motor["message"] = message
            if position_um is not None:
                motor["position_um"] = round(float(position_um), 3)
            if position_steps is not None:
                motor["position_steps"] = int(position_steps)
            if top_limit_triggered is not None:
                motor["top_limit_triggered"] = bool(top_limit_triggered)
            if is_moving is not None:
                motor["is_moving"] = bool(is_moving)
            if direction is not None or is_moving is False:
                motor["direction"] = direction if is_moving else None

        self.state.mutate(apply)

    def _parse_payload(self, stdout: str, stderr: str) -> dict[str, Any]:
        for line in reversed((stdout or "").splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not payload.get("success", False):
                raise RuntimeError(payload.get("message", "电机操作失败"))
            return payload
        raise RuntimeError((stderr or stdout or "电机接口没有返回有效 JSON").strip())

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
        return self._parse_payload(proc.stdout, proc.stderr)

    def _base_script_args(self, settings: dict[str, Any]) -> list[str]:
        args = [
            "--state-file",
            str(settings["motor_state_file"]),
            "--progress-file",
            str(settings["motor_progress_file"]),
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
        if settings["motor_ena_active_low"]:
            args.append("--ena-active-low")
        return args

    def connect(self) -> dict[str, Any]:
        settings = self._settings()
        if self.script.exists():
            try:
                payload = self._run_script(["--action", "status", *self._base_script_args(settings)])
                self._update_state(
                    connected=True,
                    message=payload.get("message", "电机已连接"),
                    position_um=payload.get("position_um", 0.0),
                    position_steps=payload.get("position_steps", 0),
                    top_limit_triggered=payload.get("top_limit_triggered", False),
                    is_moving=False,
                )
                return payload
            except Exception as exc:
                self.state.add_log(f"电机真实连接失败，已切换为模拟模式: {exc}", level="warning")

        snapshot = self.state.snapshot()["devices"]["motor"]
        self._update_state(
            connected=True,
            message="电机模拟接口已连接",
            position_um=snapshot.get("position_um", 0.0),
            position_steps=snapshot.get("position_steps", 0),
            top_limit_triggered=snapshot.get("top_limit_triggered", False),
            is_moving=False,
        )
        return {"success": True, "message": "电机模拟接口已连接"}

    def status(self) -> dict[str, Any]:
        settings = self._settings()
        if self.script.exists():
            try:
                payload = self._run_script(["--action", "status", *self._base_script_args(settings)])
                self._update_state(
                    connected=True,
                    message=payload.get("message", "电机状态正常"),
                    position_um=payload.get("position_um", 0.0),
                    position_steps=payload.get("position_steps", 0),
                    top_limit_triggered=payload.get("top_limit_triggered", False),
                    is_moving=False,
                )
            except Exception:
                pass
        return self.state.snapshot()["devices"]["motor"]

    def _simulate_move(self, request: MotorMoveRequest) -> dict[str, Any]:
        settings = self._settings()
        steps_per_rev = int(settings["motor_steps_per_rev"])
        lead_mm = float(settings["motor_lead_mm"])
        distance_um = float(request.distance_um)
        speed_um_s = float(request.speed_um_s)
        step_um = lead_mm * 1000.0 / steps_per_rev
        target_steps = max(1, int(round(distance_um / step_um)))
        delay_s = max(0.01, step_um / max(speed_um_s, 1.0))
        snapshot = self.state.snapshot()["devices"]["motor"]
        current_steps = int(snapshot.get("position_steps", 0))
        top_limit_triggered = bool(snapshot.get("top_limit_triggered", False))

        self._update_state(
            connected=True,
            message=f"模拟运动中: {request.direction}",
            is_moving=True,
            direction=request.direction,
        )

        moved_steps = 0
        for _ in range(target_steps):
            if request.direction == "up":
                if current_steps <= 0:
                    current_steps = 0
                    top_limit_triggered = True
                    break
                current_steps -= 1
                top_limit_triggered = current_steps == 0
            else:
                current_steps += 1
                top_limit_triggered = False
            moved_steps += 1
            self._update_state(
                connected=True,
                message=f"模拟运动中: {request.direction}",
                position_steps=current_steps,
                position_um=current_steps * step_um,
                top_limit_triggered=top_limit_triggered,
                is_moving=True,
                direction=request.direction,
            )
            time.sleep(delay_s)

        self._update_state(
            connected=True,
            message="模拟运动完成",
            position_steps=current_steps,
            position_um=current_steps * step_um,
            top_limit_triggered=top_limit_triggered,
            is_moving=False,
        )
        return {
            "success": True,
            "message": "模拟运动完成",
            "position_steps": current_steps,
            "position_um": round(current_steps * step_um, 3),
            "moved_steps": moved_steps,
            "stopped_by_top_limit": top_limit_triggered and request.direction == "up",
        }

    def move(self, request: MotorMoveRequest, should_stop: Callable[[], bool] | None = None) -> dict[str, Any]:
        if request.direction not in {"up", "down"}:
            raise RuntimeError("direction 必须为 up 或 down")
        if request.distance_um <= 0:
            raise RuntimeError("移动距离必须大于 0")
        if request.speed_um_s <= 0:
            raise RuntimeError("移动速度必须大于 0")

        settings = self._settings()
        progress_file = Path(str(settings["motor_progress_file"]))
        progress_file.unlink(missing_ok=True)

        if self.script.exists():
            try:
                freq = self._calc_freq_from_speed(
                    float(request.speed_um_s),
                    int(settings["motor_steps_per_rev"]),
                    float(settings["motor_lead_mm"]),
                )
                command = [
                    sys.executable,
                    str(self.script),
                    "--action",
                    "move",
                    "--move",
                    request.direction,
                    "--um",
                    str(request.distance_um),
                    "--freq",
                    str(freq),
                    *self._base_script_args(settings),
                ]
                proc = subprocess.Popen(
                    command,
                    cwd=str(self.base_dir),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                )
                self._update_state(
                    connected=True,
                    message=f"电机运动中: {request.direction}",
                    is_moving=True,
                    direction=request.direction,
                )

                while proc.poll() is None:
                    progress = self._read_json_file(progress_file)
                    if progress:
                        self._update_state(
                            connected=True,
                            message=progress.get("message", f"电机运动中: {request.direction}"),
                            position_steps=progress.get("position_steps"),
                            position_um=progress.get("position_um"),
                            top_limit_triggered=progress.get("top_limit_triggered"),
                            is_moving=True,
                            direction=request.direction,
                        )
                    if should_stop and should_stop():
                        proc.terminate()
                        raise RuntimeError("打印任务已被停止")
                    time.sleep(0.05)

                stdout, stderr = proc.communicate()
                payload = self._parse_payload(stdout, stderr)
                self._update_state(
                    connected=True,
                    message=payload.get("message", "电机运动完成"),
                    position_steps=payload.get("position_steps", 0),
                    position_um=payload.get("position_um", 0.0),
                    top_limit_triggered=payload.get("top_limit_triggered", False),
                    is_moving=False,
                )
                return payload
            except Exception as exc:
                self.state.add_log(f"电机真实移动失败，已切换为模拟模式: {exc}", level="warning")

        return self._simulate_move(request)

    def home(self) -> dict[str, Any]:
        settings = self._settings()
        home_distance_um = (
            int(settings["motor_home_max_steps"]) * float(settings["motor_lead_mm"]) * 1000.0 / int(settings["motor_steps_per_rev"])
        )
        result = self.move(
            MotorMoveRequest(
                direction="up",
                distance_um=home_distance_um,
                speed_um_s=float(settings["motor_home_speed_um_s"]),
            )
        )
        return {**result, "message": "电机回零完成"}


class UVController:
    CMD_HANDSHAKE = "A6 01 05"
    CMD_DLP_ON = "A6 02 02 01"
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

    def _send_hex(self, ser: Any, hex_str: str) -> None:
        ser.reset_input_buffer()
        ser.write(bytes.fromhex(hex_str))
        ser.read(10)

    def _set_state(self, connected: bool, message: str, *, lamp_on: bool | None = None, power: int | None = None) -> None:
        def apply(data: dict[str, Any]) -> None:
            uv = data["devices"]["uv"]
            uv["connected"] = connected
            uv["message"] = message
            if lamp_on is not None:
                uv["lamp_on"] = bool(lamp_on)
            if power is not None:
                uv["power"] = int(power)

        self.state.mutate(apply)

    def connect(self) -> dict[str, Any]:
        try:
            with self._serial() as ser:
                self._send_hex(ser, self.CMD_HANDSHAKE)
                self._send_hex(ser, self.CMD_DLP_ON)
            self._set_state(True, "UV 光机已连接")
            return {"success": True, "message": "UV 光机已连接"}
        except Exception as exc:
            self._set_state(True, f"UV 模拟连接: {exc}")
            self.state.add_log(f"UV 光机未进入真实串口模式，已启用模拟接口: {exc}", level="warning")
            return {"success": True, "message": "UV 光机模拟接口已连接"}

    def show_image(self, image_name: str) -> dict[str, Any]:
        self.state.add_log(f"已切换图层图像: {image_name}")
        return {"success": True, "message": f"已切换图像 {image_name}"}

    def set_output(self, request: UVOutputRequest) -> dict[str, Any]:
        power = max(0, min(255, int(request.power)))
        brightness_cmd = f"A6 02 10 {power:02X}"
        switch_cmd = self.CMD_LED_ON if request.lamp_on else self.CMD_LED_OFF
        status_text = "UV 输出已开启" if request.lamp_on else "UV 输出已关闭"
        try:
            with self._serial() as ser:
                self._send_hex(ser, self.CMD_HANDSHAKE)
                self._send_hex(ser, self.CMD_DLP_ON)
                self._send_hex(ser, brightness_cmd)
                self._send_hex(ser, switch_cmd)
            self._set_state(True, status_text, lamp_on=request.lamp_on, power=power)
            return {"success": True, "message": status_text, "power": power, "lamp_on": request.lamp_on}
        except Exception as exc:
            self._set_state(True, f"UV 模拟输出: {exc}", lamp_on=request.lamp_on, power=power)
            return {
                "success": True,
                "message": f"UV 模拟输出{'开启' if request.lamp_on else '关闭'}",
                "power": power,
                "lamp_on": request.lamp_on,
            }


class MagnetController:
    def __init__(self, state: PersistentState):
        self.state = state

    def _settings(self) -> dict[str, Any]:
        return self.state.snapshot()["settings"]

    def _i2c_addresses(self) -> list[int]:
        settings = self._settings()
        addresses: list[int] = []
        for key in ("magnet_i2c_addr_primary", "magnet_i2c_addr_secondary"):
            raw = str(settings.get(key, "")).strip()
            if raw:
                addresses.append(int(raw, 16))
        return addresses

    def _write_i2c_voltage(self, voltage: float) -> None:
        try:
            import smbus2  # type: ignore
        except Exception as exc:
            raise RuntimeError(f"smbus2 不可用: {exc}") from exc

        settings = self._settings()
        bus_id = int(settings["magnet_i2c_bus"])
        dac = int(max(0.0, min(5.0, voltage)) / 5.0 * 4095)
        high = dac >> 4
        low = (dac & 0x0F) << 4
        bus = smbus2.SMBus(bus_id)
        try:
            for addr in self._i2c_addresses():
                bus.write_i2c_block_data(addr, 0x40, [high, low])
        finally:
            bus.close()

    def _write_io_level(self, enabled: bool) -> str:
        settings = self._settings()
        pin = int(settings["magnet_io_pin"])
        active_high = bool(settings["magnet_io_high_when_enabled"])
        level = 1 if enabled == active_high else 0
        level_text = "high" if level else "low"

        try:
            import pigpio  # type: ignore
        except Exception as exc:
            raise RuntimeError(f"pigpio 不可用: {exc}") from exc

        pi = pigpio.pi()
        if not pi.connected:
            raise RuntimeError("连接 pigpio 失败，请先启动 pigpiod")
        try:
            pi.set_mode(pin, pigpio.OUTPUT)
            pi.write(pin, level)
        finally:
            pi.stop()
        return level_text

    def _set_state(
        self,
        connected: bool,
        message: str,
        *,
        enabled: bool | None = None,
        voltage: float | None = None,
        io_level: str | None = None,
    ) -> None:
        addresses = [hex(addr) for addr in self._i2c_addresses()]

        def apply(data: dict[str, Any]) -> None:
            magnet = data["devices"]["magnet"]
            magnet["connected"] = connected
            magnet["message"] = message
            magnet["i2c_addresses"] = addresses
            if enabled is not None:
                magnet["enabled"] = bool(enabled)
            if voltage is not None:
                magnet["voltage"] = round(float(voltage), 3)
            if io_level is not None:
                magnet["io_level"] = io_level

        self.state.mutate(apply)

    def connect(self) -> dict[str, Any]:
        snapshot = self.state.snapshot()["devices"]["magnet"]
        self._set_state(
            True,
            "磁场控制已连接",
            enabled=snapshot.get("enabled", False),
            voltage=snapshot.get("voltage", 0.0),
            io_level=snapshot.get("io_level", "low"),
        )
        return {"success": True, "message": "磁场控制已连接"}

    def apply(self, command: MagnetCommand) -> dict[str, Any]:
        applied_voltage = max(0.0, min(5.0, float(command.voltage))) if command.enabled else 0.0
        io_enabled = command.enabled if command.io_enabled is None else bool(command.io_enabled)
        try:
            self._write_i2c_voltage(applied_voltage)
            io_level = self._write_io_level(io_enabled)
            self._set_state(
                True,
                "磁场输出已更新",
                enabled=command.enabled and applied_voltage > 0.0,
                voltage=applied_voltage,
                io_level=io_level,
            )
            return {"success": True, "message": f"磁场已设置为 {applied_voltage:.3f}V", "voltage": applied_voltage}
        except Exception as exc:
            self._set_state(
                True,
                f"磁场模拟输出: {exc}",
                enabled=command.enabled and applied_voltage > 0.0,
                voltage=applied_voltage,
                io_level="high" if io_enabled else "low",
            )
            return {"success": True, "message": f"磁场模拟设置为 {applied_voltage:.3f}V", "voltage": applied_voltage}

    def off(self) -> dict[str, Any]:
        return self.apply(MagnetCommand(voltage=0.0, enabled=False, io_enabled=False))
