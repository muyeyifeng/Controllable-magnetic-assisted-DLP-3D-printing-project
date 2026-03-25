from __future__ import annotations

import threading
import time
from typing import Any

from hardware_modules import MagnetCommand, MagnetController, MotorController, MotorMoveRequest, UVController, UVOutputRequest
from printer_state import PersistentState, now_iso


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
                raise RuntimeError("打印任务已被停止")
            self._wait_if_paused()
            time.sleep(0.05)

    def _run_uv_exposure(self, power: int, duration_s: float) -> None:
        self.uv.set_output(UVOutputRequest(power=power, lamp_on=True))
        try:
            self._interruptible_sleep(duration_s)
        finally:
            self.uv.set_output(UVOutputRequest(power=power, lamp_on=False))

    def start(self) -> dict[str, Any]:
        with self.control_lock:
            snapshot = self.state.snapshot()
            if self.thread and self.thread.is_alive():
                raise RuntimeError("已有打印任务在运行")
            if snapshot["images"]["count"] <= 0:
                raise RuntimeError("请先上传切片图像 ZIP")

            self.pause_event.clear()
            self.stop_event.clear()
            total_layers = snapshot["images"]["count"]

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
            snapshot = self.state.snapshot()
            settings = snapshot["settings"]
            images = snapshot["images"]["files"]
            keep_magnet = bool(settings["magnet_keep_on_between_layers"])
            enable_magnet = bool(settings["magnet_enabled_for_job"])
            magnet_voltage = float(settings["magnet_voltage"])

            if enable_magnet:
                self.magnet.apply(MagnetCommand(voltage=magnet_voltage, enabled=True, io_enabled=True))
                self.state.add_log(f"打印开始前已设置磁场 {magnet_voltage:.3f}V")

            for idx, image in enumerate(images, start=1):
                if self.stop_event.is_set():
                    raise RuntimeError("打印任务已被停止")
                self._wait_if_paused()

                self._set_job_state(
                    phase="switching-image",
                    current_layer=idx,
                    current_image_url=image["url"],
                    current_image_name=image["name"],
                )
                self._set_machine_state("running", f"正在打印第 {idx}/{len(images)} 层")
                self.state.add_log(f"第 {idx}/{len(images)} 层，切换到 {image['name']}")
                self.uv.show_image(image["name"])

                if enable_magnet and not keep_magnet:
                    self.magnet.apply(MagnetCommand(voltage=magnet_voltage, enabled=True, io_enabled=True))

                up_distance = float(settings["fast_up_distance_um"])
                up_speed = float(settings["fast_up_speed_um_s"])
                if up_distance > 0:
                    self._set_job_state(phase="moving-up")
                    self.motor.move(
                        MotorMoveRequest(direction="up", distance_um=up_distance, speed_um_s=up_speed),
                        should_stop=self.stop_event.is_set,
                    )
                    self.state.add_log(f"第 {idx} 层，上移 {up_distance:.1f} um")

                self._interruptible_sleep(float(settings["inter_layer_delay_s"]))

                down_distance = float(settings["fast_down_distance_um"])
                down_speed = float(settings["fast_down_speed_um_s"])
                if down_distance > 0:
                    self._set_job_state(phase="moving-down")
                    self.motor.move(
                        MotorMoveRequest(direction="down", distance_um=down_distance, speed_um_s=down_speed),
                        should_stop=self.stop_event.is_set,
                    )
                    self.state.add_log(f"第 {idx} 层，下移 {down_distance:.1f} um")

                self._interruptible_sleep(float(settings["inter_layer_delay_s"]))

                self._set_job_state(phase="exposing")
                self._run_uv_exposure(
                    power=int(settings["exposure_power"]),
                    duration_s=float(settings["exposure_time_s"]),
                )
                self.state.add_log(
                    f"第 {idx} 层，曝光 {float(settings['exposure_time_s']):.2f}s，功率 {int(settings['exposure_power'])}"
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
                self.motor.move(
                    MotorMoveRequest(
                        direction="up",
                        distance_um=distance,
                        speed_um_s=float(settings["fast_up_speed_um_s"]),
                    ),
                    should_stop=self.stop_event.is_set,
                )
                self.state.add_log(f"打印完成后自动提起 {distance:.1f} um")

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
            self.uv.set_output(UVOutputRequest(power=0, lamp_on=False))
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
