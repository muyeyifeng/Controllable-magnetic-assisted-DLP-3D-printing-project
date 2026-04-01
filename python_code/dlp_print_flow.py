#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
DLP print flow controller.

Reference scripts:
- layer_runner_rewrite.py
- dlp_test.py
- stepper_to_top_pigpio.py
- stepper_pigpio_um.py

Flow summary:
1) Check HDMI has 1920x1080 output.
2) Move platform to top/home, then descend to bottom (max travel, default 221600um).
3) Handshake DLP controller (do not expose yet).
4) Layer loop:
   4.1 handshake
   4.2 project image (single or sequence)
   4.3 fast move to distance-from-bottom N (coarse)
   4.4 slow move to current layer target thickness
   4.5 set brightness (per-layer supported)
   4.6 optional magnet hook (reserved)
   4.7 LED on and hold exposure time
   4.8 switch to next image (if sequence)
   4.9 lift up by N um
   4.10 down by N-layer_thickness um
5) After last layer: slow lift, then return to top.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


CMD_HANDSHAKE = "A6 01 05"
CMD_DLP_ON = "A6 02 02 01"
CMD_DLP_OFF = "A6 02 02 00"
CMD_LED_ON = "A6 02 03 01"
CMD_LED_OFF = "A6 02 03 00"


@dataclass
class LayerParams:
    brightness: int
    exposure_s: float
    lift_um: float
    magnet: dict[str, Any] | None


class FlowError(RuntimeError):
    pass


def run_cmd(cmd: list[str], *, env: dict[str, str] | None = None, cwd: Path | None = None) -> str:
    print("RUN:", " ".join(cmd))
    proc = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if proc.returncode != 0:
        raise FlowError(
            f"Command failed ({proc.returncode}): {' '.join(cmd)}\n"
            f"stdout:\n{proc.stdout}\n"
            f"stderr:\n{proc.stderr}"
        )
    if proc.stdout:
        print(proc.stdout.strip())
    return proc.stdout


def run_systemctl(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["systemctl", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def check_hdmi_1920x1080() -> tuple[bool, str]:
    # 0) DRM sysfs check (works in headless/SSH on modern Raspberry Pi OS)
    try:
        status_paths = sorted(glob.glob("/sys/class/drm/card*-HDMI-A-*/status"))
        mode_paths = sorted(glob.glob("/sys/class/drm/card*-HDMI-A-*/modes"))
        if status_paths:
            connected = []
            for sp in status_paths:
                try:
                    status = Path(sp).read_text(encoding="utf-8", errors="ignore").strip().lower()
                except Exception:
                    status = "unknown"
                if status == "connected":
                    connected.append(sp)

            if connected:
                modes = set()
                for mp in mode_paths:
                    try:
                        for line in Path(mp).read_text(encoding="utf-8", errors="ignore").splitlines():
                            line = line.strip()
                            if line:
                                modes.add(line)
                    except Exception:
                        pass
                if "1920x1080" in modes:
                    return True, f"DRM connected, modes include 1920x1080 (paths={connected})"
                return False, f"DRM connected but no 1920x1080, modes={sorted(modes)}"
    except Exception:
        pass

    # 1) Prefer xrandr when desktop/X is running.
    try:
        p = subprocess.run(
            ["xrandr", "--query"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=3,
        )
        if p.returncode == 0:
            current_modes = re.findall(r"(\d{3,4}x\d{3,4})\s+\d+\.\d+\*", p.stdout)
            if "1920x1080" in current_modes:
                return True, "xrandr detected active mode 1920x1080"
            return False, f"xrandr active modes: {current_modes or 'none'}"
    except Exception:
        pass

    # 2) Fallback tvservice on Raspberry Pi legacy stack.
    try:
        p = subprocess.run(
            ["tvservice", "-s"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=3,
        )
        if p.returncode == 0:
            # Example: state 0x12000a [HDMI DMT (82) RGB full 16:9], 1920x1080 @ 60.00Hz
            ok = "1920x1080" in p.stdout
            return ok, p.stdout.strip()
    except Exception:
        pass

    return False, "Neither xrandr nor tvservice provided a usable 1920x1080 status"


class DLPController:
    def __init__(self, port: str, baudrate: int, timeout_s: float):
        self.port = port
        self.baudrate = baudrate
        self.timeout_s = timeout_s
        self.ser = None

    def open(self) -> None:
        try:
            import serial  # type: ignore
        except Exception as exc:
            raise FlowError(f"pyserial unavailable: {exc}")

        self.ser = serial.Serial(self.port, self.baudrate, timeout=self.timeout_s)
        print(f"DLP serial opened: {self.port} @ {self.baudrate}")

    def close(self) -> None:
        if self.ser is not None:
            try:
                self.ser.close()
            except Exception:
                pass
            self.ser = None

    def _send_hex(self, hex_str: str, desc: str) -> bool:
        if self.ser is None:
            raise FlowError("DLP serial is not opened")
        try:
            data = bytes.fromhex(hex_str)
            self.ser.reset_input_buffer()
            self.ser.write(data)
            resp = self.ser.read(10)
            resp_hex = " ".join(f"{b:02X}" for b in resp) if resp else "(timeout)"
            print(f"[DLP] {desc}: send={hex_str} recv={resp_hex}")
            if not resp:
                return False
            return not resp_hex.endswith("E0")
        except Exception as exc:
            raise FlowError(f"DLP command failed ({desc}): {exc}")

    def handshake(self) -> None:
        if not self._send_hex(CMD_HANDSHAKE, "handshake"):
            raise FlowError("DLP handshake failed")

    def dlp_on_no_expose(self) -> None:
        # DLP engine on, LED stays off.
        if not self._send_hex(CMD_DLP_ON, "dlp_on"):
            raise FlowError("DLP on failed")
        self.led_off()

    def dlp_off(self) -> None:
        self._send_hex(CMD_DLP_OFF, "dlp_off")

    def set_brightness(self, value: int) -> None:
        v = max(0, min(255, int(value)))
        cmd = f"A6 02 10 {v:02X}"
        if not self._send_hex(cmd, f"brightness={v}"):
            raise FlowError(f"Set brightness failed: {v}")

    def led_on(self) -> None:
        if not self._send_hex(CMD_LED_ON, "led_on"):
            raise FlowError("LED on failed")

    def led_off(self) -> None:
        # Safety action: ignore return value.
        try:
            self._send_hex(CMD_LED_OFF, "led_off")
        except Exception:
            pass


def reconnect_dlp(dlp: DLPController, delay_s: float) -> None:
    dlp.close()
    time.sleep(max(0.0, delay_s))
    dlp.open()
    dlp.handshake()
    dlp.dlp_on_no_expose()


def dlp_op_with_retry(
    dlp: DLPController,
    op_name: str,
    op,
    *,
    retries: int,
    reconnect_delay_s: float,
) -> None:
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            op()
            return
        except Exception as exc:
            last_err = exc
            if attempt >= retries:
                break
            print(
                f"[DLP] {op_name} failed (attempt {attempt + 1}/{retries + 1}): {exc}; reconnecting..."
            )
            try:
                reconnect_dlp(dlp, reconnect_delay_s)
            except Exception as rec_exc:
                last_err = rec_exc
                print(f"[DLP] reconnect failed: {rec_exc}")
    raise FlowError(f"DLP operation failed after retries: {op_name} | {last_err}")


class StepperController:
    def __init__(self, script_dir: Path, settings: dict[str, Any]):
        self.script_dir = script_dir
        self.home_script = script_dir / "stepper_to_top_pigpio.py"
        self.move_script = script_dir / "stepper_pigpio_um.py"
        self.s = settings

    def _common_pins(self) -> list[str]:
        args = [
            "--pul", str(self.s["pul_pin"]),
            "--dir", str(self.s["dir_pin"]),
            "--ena", str(self.s["ena_pin"]),
        ]
        if self.s.get("ena_active_low", True):
            args.append("--ena-active-low")
        return args

    def home_to_top(self) -> None:
        cmd = [
            sys.executable,
            str(self.home_script),
            *self._common_pins(),
            "--freq", str(self.s["home_freq_hz"]),
            "--move", "up",
            "--top-pin", str(self.s["top_sensor_pin"]),
            "--stop-level", str(self.s["top_stop_level"]),
            "--pull", str(self.s["top_pull"]),
            "--steps-per-rev", str(self.s["steps_per_rev"]),
            "--lead-mm", str(self.s["lead_mm"]),
            "--max-steps", str(self.s["home_max_steps"]),
            "--pulse-width-us", str(self.s["pulse_width_us"]),
        ]
        run_cmd(cmd, cwd=self.script_dir)

    def move_relative_um(self, move: str, um: float, freq_hz: int) -> None:
        if um <= 0:
            return
        cmd = [
            sys.executable,
            str(self.move_script),
            *self._common_pins(),
            "--freq", str(int(freq_hz)),
            "--move", move,
            "--um", str(float(um)),
            "--steps-per-rev", str(self.s["steps_per_rev"]),
            "--lead-mm", str(self.s["lead_mm"]),
            "--pulse-width-us", str(self.s["pulse_width_us"]),
        ]
        run_cmd(cmd, cwd=self.script_dir)


class ProjectorController:
    def __init__(self, cfg: dict[str, Any], layer_images: list[Path]):
        self.cfg = cfg
        self.layer_images = layer_images
        self.proc: subprocess.Popen[str] | None = None
        self.current_image: Path | None = None
        self.tty_service = ""
        self.tty_restore_needed = False
        self.tty_suppressed = False

    def _env(self) -> dict[str, str]:
        env = os.environ.copy()
        if self.cfg.get("display"):
            env["DISPLAY"] = str(self.cfg["display"])
        if self.cfg.get("xauthority"):
            env["XAUTHORITY"] = str(self.cfg["xauthority"])
        return env

    def _build_cmd(self, image_path: Path) -> list[str]:
        template = self.cfg.get("viewer_cmd_template") or [
            "feh",
            "--fullscreen",
            "--hide-pointer",
            "--auto-zoom",
            "{image}",
        ]
        cmd = [str(x).replace("{image}", str(image_path)) for x in template]
        if cmd and cmd[0] == "fbi" and "--noverbose" not in cmd:
            cmd.insert(-1, "--noverbose")
        return cmd

    def _infer_tty(self, cmd: list[str]) -> int | None:
        if "-T" in cmd:
            idx = cmd.index("-T")
            if idx + 1 < len(cmd):
                try:
                    return int(cmd[idx + 1])
                except Exception:
                    return None
        return None

    def _suppress_tty_getty_once(self, cmd: list[str]) -> None:
        if self.tty_suppressed:
            return
        if not bool(self.cfg.get("suppress_tty_getty", True)):
            return
        tty = self._infer_tty(cmd)
        if tty is None:
            return
        service = f"getty@tty{tty}.service"
        active = run_systemctl("is-active", service).returncode == 0
        if not active:
            self.tty_suppressed = True
            return
        p = run_systemctl("stop", service)
        if p.returncode == 0:
            self.tty_service = service
            self.tty_restore_needed = True
            self.tty_suppressed = True
            print(f"[TTY] stopped {service} for quiet projection")
            return
        print(f"[TTY] failed to stop {service}: {p.stderr.strip()}")

    def _restore_tty_getty(self) -> None:
        if not self.tty_restore_needed or not self.tty_service:
            return
        p = run_systemctl("start", self.tty_service)
        if p.returncode == 0:
            print(f"[TTY] restored {self.tty_service}")
        else:
            print(f"[TTY] failed to restore {self.tty_service}: {p.stderr.strip()}")
        self.tty_restore_needed = False

    def show(self, layer_idx: int) -> None:
        image = self.layer_images[layer_idx]
        if self.current_image == image and self.proc is not None and self.proc.poll() is None:
            return

        self.stop()
        cmd = self._build_cmd(image)
        self._suppress_tty_getty_once(cmd)
        print(f"[Projector] show layer {layer_idx + 1}: {image}")
        self.proc = subprocess.Popen(
            cmd,
            env=self._env(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
        )
        self.current_image = image
        time.sleep(float(self.cfg.get("switch_settle_s", 0.2)))
        if self.proc.poll() is not None:
            rc = self.proc.returncode
            out, err = self.proc.communicate(timeout=1)
            if rc != 0:
                raise FlowError(
                    f"Projector command failed(code={rc}): {' '.join(cmd)}\n"
                    f"stdout:\n{out}\n"
                    f"stderr:\n{err}"
                )
            print("[Projector] viewer exited with code 0 after rendering (accepted)")

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=2)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
        self.proc = None

    def shutdown(self) -> None:
        self.stop()
        self._restore_tty_getty()


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8-sig") as f:
        return json.load(f)


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    try:
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except Exception:
        # Not all filesystems support fsync on directories.
        pass


def load_progress(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8-sig") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception as exc:
        print(f"[Progress] failed to read {path}: {exc}")
    return None


def resolve_progress_settings(cfg: dict[str, Any], cfg_path: Path) -> tuple[bool, Path, bool]:
    p_cfg = cfg.get("progress", {})
    enabled = bool(p_cfg.get("enabled", True))
    default_name = f"{cfg_path.stem}.progress.json"
    raw_path = str(p_cfg.get("path", default_name))
    progress_path = _resolve_cfg_path(cfg_path.parent, raw_path)
    auto_resume = bool(p_cfg.get("auto_resume", True))
    return enabled, progress_path, auto_resume


def build_progress_payload(
    *,
    cfg_path: Path,
    total_layers: int,
    completed_layers: int,
    completed_thickness_um: float,
    max_travel_um: float,
    status: str,
) -> dict[str, Any]:
    return {
        "version": 1,
        "updated_at_epoch_s": time.time(),
        "status": status,
        "config_path": str(cfg_path),
        "total_layers": int(total_layers),
        "completed_layers": int(completed_layers),
        "completed_thickness_um": float(round(completed_thickness_um, 6)),
        "next_layer": int(completed_layers + 1),
        "max_travel_um": float(max_travel_um),
    }


def _resolve_cfg_path(base_dir: Path, raw_path: str) -> Path:
    p = Path(raw_path).expanduser()
    if not p.is_absolute():
        p = (base_dir / p).resolve()
    else:
        p = p.resolve()
    return p


def resolve_layer_images(cfg: dict[str, Any], base_dir: Path) -> list[Path]:
    img_cfg = cfg["images"]
    mode = img_cfg["mode"]

    if mode == "single":
        p = _resolve_cfg_path(base_dir, str(img_cfg["single_image"]))
        if not p.exists():
            raise FlowError(f"Single image not found: {p}")
        layers = int(cfg["print"].get("total_layers", 0))
        if layers <= 0:
            plan = cfg["print"].get("thickness_plan", [])
            layers = sum(int(x.get("layers", 0)) for x in plan)
        if layers <= 0:
            raise FlowError("For single image mode, print.total_layers must be > 0 (or thickness_plan must define total layers)")
        return [p for _ in range(layers)]

    if mode == "sequence":
        d = _resolve_cfg_path(base_dir, str(img_cfg["sequence_dir"]))
        if not d.exists() or not d.is_dir():
            raise FlowError(f"Sequence dir not found: {d}")
        patterns = tuple(x.lower() for x in img_cfg.get("extensions", [".png", ".bmp", ".jpg", ".jpeg"]))
        files = sorted([p for p in d.iterdir() if p.is_file() and p.suffix.lower() in patterns])
        if not files:
            raise FlowError(f"No images found in sequence dir: {d}")
        requested = int(cfg["print"].get("total_layers", 0))
        if requested <= 0:
            plan = cfg["print"].get("thickness_plan", [])
            requested = sum(int(x.get("layers", 0)) for x in plan)
        if requested <= 0:
            return files
        if len(files) < requested:
            raise FlowError(f"Need {requested} images but only found {len(files)} in {d}")
        return files[:requested]

    raise FlowError(f"Unsupported images.mode: {mode}")


def layer_params_for(idx0: int, cfg: dict[str, Any]) -> LayerParams:
    pr = cfg["print"]
    default = pr["defaults"]

    p = LayerParams(
        brightness=int(default["brightness"]),
        exposure_s=float(default["exposure_s"]),
        lift_um=float(default["lift_um"]),
        magnet=None,
    )

    # Optional plan-level process parameters (brightness/exposure/lift/magnet)
    # can be defined together with thickness_plan segments.
    plan = pr.get("thickness_plan", [])
    if plan:
        idx1 = idx0 + 1
        cursor = 0
        for item in plan:
            n = int(item.get("layers", 0))
            if n <= 0:
                raise FlowError(f"Invalid thickness_plan item layers: {item}")
            start = cursor + 1
            end = cursor + n
            if start <= idx1 <= end:
                if "brightness" in item:
                    p.brightness = int(item["brightness"])
                if "exposure_s" in item:
                    p.exposure_s = float(item["exposure_s"])
                if "lift_um" in item:
                    p.lift_um = float(item["lift_um"])
                if "magnet" in item:
                    p.magnet = item["magnet"]
                break
            cursor = end

    overrides = pr.get("layer_overrides", [])
    idx1 = idx0 + 1
    for ov in overrides:
        if int(ov.get("layer", -1)) != idx1:
            continue
        if "brightness" in ov:
            p.brightness = int(ov["brightness"])
        if "exposure_s" in ov:
            p.exposure_s = float(ov["exposure_s"])
        if "lift_um" in ov:
            p.lift_um = float(ov["lift_um"])
        if "magnet" in ov:
            p.magnet = ov["magnet"]
        break

    return p


def build_thickness_sequence(cfg: dict[str, Any], total_layers: int) -> list[float]:
    plan = cfg["print"].get("thickness_plan", [])
    if not plan:
        t = float(cfg["print"]["layer_thickness_um"])
        return [t for _ in range(total_layers)]

    seq: list[float] = []
    for item in plan:
        n = int(item["layers"])
        t = float(item["thickness_um"])
        if n <= 0 or t <= 0:
            raise FlowError(f"Invalid thickness_plan item: {item}")
        seq.extend([t] * n)

    if len(seq) != total_layers:
        raise FlowError(
            f"thickness_plan layers({len(seq)}) != total image layers({total_layers})"
        )
    return seq


def apply_magnet_reserved(magnet_cfg: dict[str, Any] | None) -> None:
    """Reserved hook: reads per-layer magnet config; optional real call for testing."""
    if not magnet_cfg:
        return

    if not magnet_cfg.get("enabled", False):
        return

    print(f"[Magnet] reserved call with config: {magnet_cfg}")

    # Optional test call: disabled by default because this function sets field then zeros on exit.
    if not magnet_cfg.get("test_call_tca_script", False):
        return

    try:
        from tca_ch0_io_dac import execute_magnet_sequence

        io23 = magnet_cfg.get("io23", [0, 0, 0, 0])
        io27 = magnet_cfg.get("io27", [0, 0, 0, 0])
        dac_voltage = float(magnet_cfg.get("dac_voltage", 0.0))

        ret = execute_magnet_sequence(
            io_23=io23,
            io_27=io27,
            dac_voltage=dac_voltage,
            hold_seconds=float(magnet_cfg.get("hold_s", 0.1)),
        )
        print(f"[Magnet] result: {ret}")
    except Exception as exc:
        raise FlowError(f"Magnet reserved call failed: {exc}")


def move_to_abs_from_top(
    stepper: StepperController,
    current_from_top_um: float,
    target_from_top_um: float,
    freq_hz: int,
) -> float:
    delta = target_from_top_um - current_from_top_um
    if abs(delta) < 0.5:
        return current_from_top_um
    if delta > 0:
        stepper.move_relative_um("down", delta, freq_hz)
    else:
        stepper.move_relative_um("up", -delta, freq_hz)
    return target_from_top_um


def run_flow(cfg: dict[str, Any], cfg_path: Path) -> None:
    ok, detail = check_hdmi_1920x1080()
    print(f"[HDMI] {detail}")
    if not ok:
        raise FlowError("HDMI is not 1920x1080, stop printing")

    base_dir = cfg_path.parent
    layer_images = resolve_layer_images(cfg, base_dir)
    total_layers = len(layer_images)
    thickness_seq = build_thickness_sequence(cfg, total_layers)

    # Core settings.
    max_travel_um = float(cfg["motion"].get("max_travel_um", 221600.0))
    freq_slow = int(cfg["motion"].get("slow_freq_hz", 800))
    # User requested global slow positioning (disable fast coarse descent).
    freq_position = freq_slow

    dlp_retries = int(cfg["dlp"].get("reconnect_retries", 3))
    dlp_reconnect_delay_s = float(cfg["dlp"].get("reconnect_delay_s", 0.5))
    progress_enabled, progress_path, auto_resume = resolve_progress_settings(cfg, cfg_path)

    print(f"[Flow] total_layers={total_layers}, thickness_plan={'enabled' if 'thickness_plan' in cfg['print'] else 'fixed'}")

    stepper = StepperController(base_dir, cfg["stepper"])
    dlp = DLPController(
        port=cfg["dlp"]["port"],
        baudrate=int(cfg["dlp"].get("baudrate", 115200)),
        timeout_s=float(cfg["dlp"].get("timeout_s", 1.0)),
    )
    projector = ProjectorController(cfg["projection"], layer_images)

    current_from_top_um = 0.0
    start_layer_idx = 0
    printed_build_um = 0.0
    completed_layers = 0
    progress_written_for_status = False

    if progress_enabled:
        saved = load_progress(progress_path)
        if saved and auto_resume:
            saved_total = int(saved.get("total_layers", -1))
            saved_done = int(saved.get("completed_layers", 0))
            saved_thickness = float(saved.get("completed_thickness_um", 0.0))
            if (
                saved_total == total_layers
                and 0 < saved_done < total_layers
                and 0.0 <= saved_thickness <= sum(thickness_seq)
            ):
                start_layer_idx = saved_done
                printed_build_um = saved_thickness
                completed_layers = saved_done
                print(
                    f"[Progress] resume enabled: completed_layers={saved_done}/{total_layers}, "
                    f"completed_thickness_um={saved_thickness:.3f}"
                )
            elif saved_total != -1 and saved_total != total_layers:
                print(
                    f"[Progress] ignore saved progress due to layer mismatch: "
                    f"saved={saved_total}, current={total_layers}"
                )

        atomic_write_json(
            progress_path,
            build_progress_payload(
                cfg_path=cfg_path,
                total_layers=total_layers,
                completed_layers=start_layer_idx,
                completed_thickness_um=printed_build_um,
                max_travel_um=max_travel_um,
                status="running",
            ),
        )

    try:
        # Step 2: home first, then down to bottom.
        print("\n[Step 2] Home to top")
        stepper.home_to_top()
        current_from_top_um = 0.0

        if printed_build_um > 0.0:
            target_from_top_um = max(0.0, max_travel_um - printed_build_um)
            print(
                f"[Step 2] Resume positioning from top: down to {target_from_top_um:.3f}um "
                f"(max_travel_um={max_travel_um}, completed_thickness_um={printed_build_um:.3f})"
            )
            stepper.move_relative_um("down", target_from_top_um, freq_position)
            current_from_top_um = target_from_top_um
        else:
            print(f"[Step 2] Down to bottom: {max_travel_um}um")
            stepper.move_relative_um("down", max_travel_um, freq_position)
            current_from_top_um = max_travel_um

        # Step 3: handshake and DLP on, keep LED off.
        print("\n[Step 3] DLP init (no exposure)")
        dlp_op_with_retry(
            dlp,
            "initial_connect",
            lambda: reconnect_dlp(dlp, 0.0),
            retries=dlp_retries,
            reconnect_delay_s=dlp_reconnect_delay_s,
        )

        if printed_build_um > 0.0:
            print(
                f"[Resume] start from saved state: completed_layers={completed_layers}, "
                f"completed_thickness_um={printed_build_um:.3f}, current_from_top_um={current_from_top_um:.3f}"
            )

        # Step 4 loop.
        for i in range(start_layer_idx, total_layers):
            lp = layer_params_for(i, cfg)
            layer_no = i + 1
            current_thickness_um = thickness_seq[i]
            print(f"\n===== Layer {layer_no}/{total_layers} =====")

            # 4.1 handshake
            dlp_op_with_retry(
                dlp,
                "handshake",
                lambda: dlp.handshake(),
                retries=dlp_retries,
                reconnect_delay_s=dlp_reconnect_delay_s,
            )

            # 4.2 project current image
            projector.show(i)

            # Layer target from top: bottom - build_height
            target_build_height_um = printed_build_um + current_thickness_um
            target_from_top_um = max_travel_um - target_build_height_um
            # 4.3 + 4.4 global slow positioning directly to target
            current_from_top_um = move_to_abs_from_top(
                stepper, current_from_top_um, target_from_top_um, freq_position
            )

            # 4.5 set brightness
            dlp_op_with_retry(
                dlp,
                "set_brightness",
                lambda: dlp.set_brightness(lp.brightness),
                retries=dlp_retries,
                reconnect_delay_s=dlp_reconnect_delay_s,
            )

            # 4.6 reserved magnet call
            apply_magnet_reserved(lp.magnet)

            # 4.7 expose
            dlp_op_with_retry(
                dlp,
                "led_on",
                lambda: dlp.led_on(),
                retries=dlp_retries,
                reconnect_delay_s=dlp_reconnect_delay_s,
            )
            time.sleep(lp.exposure_s)
            dlp_op_with_retry(
                dlp,
                "led_off",
                lambda: dlp.led_off(),
                retries=dlp_retries,
                reconnect_delay_s=dlp_reconnect_delay_s,
            )

            if progress_enabled:
                printed_build_um = target_build_height_um
                completed_layers = layer_no
                atomic_write_json(
                    progress_path,
                    build_progress_payload(
                        cfg_path=cfg_path,
                        total_layers=total_layers,
                        completed_layers=completed_layers,
                        completed_thickness_um=printed_build_um,
                        max_travel_um=max_travel_um,
                        status="running",
                    ),
                )
            else:
                printed_build_um = target_build_height_um

            # 4.8 pre-switch next image (sequence mode effect)
            if i + 1 < total_layers:
                projector.show(i + 1)

            # 4.9 + 4.10 peel/recoat for next layer
            if i + 1 < total_layers:
                lift_um = lp.lift_um
                next_thickness_um = thickness_seq[i + 1]
                if lift_um < next_thickness_um:
                    raise FlowError(
                        f"Layer {layer_no}: lift_um({lift_um}) must be >= next layer thickness({next_thickness_um})"
                    )

                stepper.move_relative_um("up", lift_um, freq_slow)
                current_from_top_um -= lift_um

                settle_down_um = lift_um - next_thickness_um
                stepper.move_relative_um("down", settle_down_um, freq_slow)
                current_from_top_um += settle_down_um

        # Step 5: final slow up, then top.
        print("\n[Step 5] Finish: slow lift then home top")
        final_slow_up_um = float(cfg["motion"].get("final_slow_up_um", 500.0))
        stepper.move_relative_um("up", final_slow_up_um, freq_slow)
        current_from_top_um = max(0.0, current_from_top_um - final_slow_up_um)

        stepper.home_to_top()
        current_from_top_um = 0.0

        print("\n[Done] Print flow completed successfully")
        if progress_enabled:
            atomic_write_json(
                progress_path,
                build_progress_payload(
                    cfg_path=cfg_path,
                    total_layers=total_layers,
                    completed_layers=total_layers,
                    completed_thickness_um=sum(thickness_seq),
                    max_travel_um=max_travel_um,
                    status="completed",
                ),
            )
            progress_written_for_status = True

    except BaseException:
        if progress_enabled and not progress_written_for_status:
            atomic_write_json(
                progress_path,
                build_progress_payload(
                    cfg_path=cfg_path,
                    total_layers=total_layers,
                    completed_layers=completed_layers,
                    completed_thickness_um=printed_build_um,
                    max_travel_um=max_travel_um,
                    status="interrupted",
                ),
            )
        raise
    finally:
        # Safety shutdown.
        try:
            dlp.led_off()
        except Exception:
            pass
        try:
            dlp.dlp_off()
        except Exception:
            pass
        dlp.close()
        projector.shutdown()


def main() -> None:
    parser = argparse.ArgumentParser(description="DLP print flow with stepper scripts and reserved magnet hook")
    parser.add_argument(
        "--config",
        type=str,
        default="python_code/dlp_print_config_template.json",
        help="Path to config json",
    )
    args = parser.parse_args()

    cfg_path = Path(args.config).expanduser().resolve()
    if not cfg_path.exists():
        raise FlowError(f"Config not found: {cfg_path}")

    cfg = load_json(cfg_path)
    run_flow(cfg, cfg_path)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted by user")
        sys.exit(130)
    except Exception as e:
        print(f"\nERROR: {e}")
        sys.exit(1)
