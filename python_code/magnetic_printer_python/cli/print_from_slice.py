#!/usr/bin/env python3
from __future__ import annotations

import argparse
import atexit
import errno
import fcntl
import json
import os
import subprocess
import shutil
import signal
import tempfile
import threading
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

try:
    from PIL import Image
except Exception as exc:  # pragma: no cover
    raise RuntimeError("Pillow is required. Run: pip install Pillow") from exc

try:
    import serial  # type: ignore
except Exception as exc:  # pragma: no cover
    raise RuntimeError("pyserial is required. Run: pip install pyserial") from exc


I2C_SLAVE_IOCTL = 0x0703
MOVE_DIR_DOWN = "down"
MOVE_DIR_UP = "up"
X_POSITIVE_BITS = "00001111"
X_NEGATIVE_BITS = "11110000"
Y_POSITIVE_BITS = "11000011"
Y_NEGATIVE_BITS = "00111100"

DLP_CMD_HANDSHAKE = bytes([0xA6, 0x01, 0x05])
DLP_CMD_ON = bytes([0xA6, 0x02, 0x02, 0x01])
DLP_CMD_OFF = bytes([0xA6, 0x02, 0x02, 0x00])
DLP_CMD_FAN_ON = bytes([0xA6, 0x02, 0x04, 0x03])
DLP_CMD_LED_ON = bytes([0xA6, 0x02, 0x03, 0x01])
DLP_CMD_LED_OFF = bytes([0xA6, 0x02, 0x03, 0x00])


def log(msg: str) -> None:
    print(time.strftime("[%H:%M:%S]"), msg, flush=True)


def resolve_direction_bits(x: float, y: float) -> str:
    abs_x = abs(x)
    abs_y = abs(y)
    if abs_x < 1e-4 and abs_y < 1e-4:
        return X_POSITIVE_BITS
    if abs_x >= abs_y:
        return X_POSITIVE_BITS if x >= 0 else X_NEGATIVE_BITS
    return Y_POSITIVE_BITS if y >= 0 else Y_NEGATIVE_BITS


def bits_to_output_byte(bits: str) -> int:
    if len(bits) != 8:
        raise ValueError("direction bits must be 8 chars")
    out = 0
    for i, ch in enumerate(bits):
        if ch not in "01":
            raise ValueError("direction bits must contain only 0/1")
        if ch == "1":
            out |= 1 << i
    return out


def sleep_interruptible(stop_event: threading.Event, seconds: float) -> None:
    if seconds <= 0:
        return
    end = time.time() + seconds
    while time.time() < end:
        if stop_event.is_set():
            raise InterruptedError("Interrupted")
        time.sleep(min(0.05, end - time.time()))


@dataclass
class MagnetConfig:
    enableGpioPin: int = 27
    enableActiveLow: Optional[bool] = None
    i2cBus: int = 1
    tcaAddress: int = 0x70
    tcaChannel: int = 0
    pca9554Address: int = 0x27
    mcp4725Address: int = 0x60
    dacVRef: float = 5.0


@dataclass
class MotionConfig:
    stepPin: int = 13
    dirPin: int = 5
    dirHighIsUp: bool = True
    enablePin: int = 8
    enableLow: bool = False
    moveFrequencyHz: int = 100
    homeFrequencyHz: int = 1600
    pulseWidthUs: int = 1000
    stepsPerRev: int = 3200
    leadMm: float = 4.0
    homeTopPin: int = 20
    homeStopLevel: int = 0
    homeChunkSteps: int = 200
    homeMaxSteps: int = 300000


@dataclass
class ExposureConfig:
    serialPort: str = "/dev/ttyUSB0"
    baudRate: int = 115200
    readTimeoutMs: int = 1000
    responseReadBytes: int = 10
    imageLoader: str = "fbi"
    framebufferDevice: str = "/dev/fb0"
    framebufferSettleMs: int = 250
    fbiTty: int = 1
    fbiAutoScale: bool = True
    fbiStopGetty: bool = True
    fbiExtraArgs: list[str] = field(default_factory=list)
    dlpOnSettleMs: int = 500
    sendFanOnCommand: bool = True
    ignoreFanReject: bool = True
    skipBrightnessCommand: bool = False
    ignoreBrightnessReject: bool = False
    minimumIntensity: int = 10
    useSliceIntensity: bool = False
    defaultIntensity: int = 120
    ignoreLedOffReject: bool = True
    repeatDlpOnBeforeEachExposure: bool = False
    logDlpCommands: bool = True


@dataclass
class PrintConfig:
    useMockHardware: bool = False
    moveEnabled: bool = True
    bottomDistanceUm: int = 221600
    preHomeEnabled: bool = True
    preHomeDropUm: int = 3000
    peelDistanceUm: int = 3000
    layerThicknessUmOverride: Optional[int] = None
    finalReturnToTop: bool = True
    magneticEnabled: bool = True
    magneticHoldSeconds: float = 0.0
    exposureEnabled: bool = True
    exposureSeconds: float = 1.0
    minExposureIntensity: int = 1
    maxExposureIntensity: int = 255
    interLayerDelaySeconds: float = 0.0
    logMagneticSteps: bool = True
    interruptReturnToTop: bool = True
    motion: MotionConfig = field(default_factory=MotionConfig)
    magnet: MagnetConfig = field(default_factory=MagnetConfig)
    exposure: ExposureConfig = field(default_factory=ExposureConfig)


class SysfsGPIOPin:
    def __init__(self, num: int):
        self.num = num
        self.export_num = num
        self.value_file = None

    def prepare(self, direction: str) -> None:
        gpio_root = Path("/sys/class/gpio")
        gpio_path = gpio_root / f"gpio{self.export_num}"
        if not gpio_path.exists():
            try:
                (gpio_root / "export").write_text(str(self.export_num), encoding="utf-8")
            except OSError as exc:
                if exc.errno == errno.EBUSY:
                    pass
                elif exc.errno == errno.EINVAL:
                    self.export_num = resolve_sysfs_gpio_export_number(gpio_root, self.num)
                    gpio_path = gpio_root / f"gpio{self.export_num}"
                    if not gpio_path.exists():
                        (gpio_root / "export").write_text(str(self.export_num), encoding="utf-8")
                else:
                    raise
            deadline = time.time() + 2.0
            while not gpio_path.exists():
                if time.time() > deadline:
                    raise RuntimeError(f"gpio{self.export_num} path did not appear")
                time.sleep(0.01)
        (gpio_path / "direction").write_text(direction, encoding="utf-8")
        self.value_file = open(gpio_path / "value", "r+", encoding="utf-8")

    def write(self, level: int) -> None:
        if self.value_file is None:
            raise RuntimeError("gpio not prepared")
        self.value_file.seek(0)
        self.value_file.write("1" if level else "0")
        self.value_file.flush()

    def close(self) -> None:
        if self.value_file:
            self.value_file.close()
            self.value_file = None

    def read(self) -> int:
        if self.value_file is None:
            raise RuntimeError("gpio not prepared")
        self.value_file.seek(0)
        c = self.value_file.read(1)
        return 1 if c == "1" else 0


def resolve_sysfs_gpio_export_number(gpio_root: Path, bcm: int) -> int:
    chips = sorted(gpio_root.glob("gpiochip*"))
    if not chips:
        raise RuntimeError("no gpiochip found in /sys/class/gpio")
    candidates = []
    for chip in chips:
        try:
            base = int((chip / "base").read_text(encoding="utf-8").strip())
            ngpio = int((chip / "ngpio").read_text(encoding="utf-8").strip())
        except Exception:
            continue
        if bcm < 0 or bcm >= ngpio:
            continue
        label = ""
        try:
            label = (chip / "label").read_text(encoding="utf-8").strip().lower()
        except Exception:
            pass
        score = 0
        if "bcm" in label or "pinctrl" in label:
            score += 10
        if "raspberry" in label:
            score += 5
        candidates.append((base + bcm, score))
    if not candidates:
        raise RuntimeError(f"cannot map bcm gpio {bcm} to global sysfs number")
    candidates.sort(key=lambda x: x[1], reverse=True)
    return candidates[0][0]


class I2CNative:
    def __init__(self, bus: int):
        self.fd = os.open(f"/dev/i2c-{bus}", os.O_RDWR)
        self.lock = threading.Lock()

    def close(self) -> None:
        if self.fd > 0:
            os.close(self.fd)
            self.fd = -1

    def _set_addr(self, addr: int) -> None:
        fcntl.ioctl(self.fd, I2C_SLAVE_IOCTL, addr)

    def write(self, addr: int, data: bytes) -> None:
        with self.lock:
            self._set_addr(addr)
            n = os.write(self.fd, data)
            if n != len(data):
                raise RuntimeError("i2c short write")

    def read_reg(self, addr: int, reg: int) -> int:
        with self.lock:
            self._set_addr(addr)
            os.write(self.fd, bytes([reg]))
            b = os.read(self.fd, 1)
            if len(b) != 1:
                raise RuntimeError("i2c short read")
            return b[0]


class SerialNative:
    def __init__(self, path: str, baud: int, timeout_ms: int):
        self.dev = serial.Serial(path, baudrate=baud, timeout=max(timeout_ms / 1000.0, 0.1))
        self.lock = threading.Lock()

    def close(self) -> None:
        self.dev.close()

    def send(self, cmd: bytes, max_read: int) -> bytes:
        with self.lock:
            self.dev.reset_input_buffer()
            self.dev.write(cmd)
            self.dev.flush()
            return self.dev.read(max_read)


class FramebufferNative:
    def __init__(self, path: str):
        self.path = path or "/dev/fb0"
        self.file = open(self.path, "r+b", buffering=0)
        name = Path(self.path).name
        sys_root = Path("/sys/class/graphics") / name
        w_h = (sys_root / "virtual_size").read_text(encoding="utf-8").strip().split(",")
        self.width = int(w_h[0].strip())
        self.height = int(w_h[1].strip())
        self.bpp = int((sys_root / "bits_per_pixel").read_text(encoding="utf-8").strip())
        stride_file = sys_root / "stride"
        self.stride = int(stride_file.read_text(encoding="utf-8").strip()) if stride_file.exists() else self.width * (self.bpp // 8)

    def close(self) -> None:
        self.file.close()

    def render_image(self, path: str) -> None:
        src = Image.open(path).convert("RGB")
        scale = min(self.width / src.width, self.height / src.height)
        tw = max(1, int(round(src.width * scale)))
        th = max(1, int(round(src.height * scale)))
        resized = src.resize((tw, th), Image.BILINEAR)
        canvas = Image.new("RGB", (self.width, self.height), "black")
        ox = (self.width - tw) // 2
        oy = (self.height - th) // 2
        canvas.paste(resized, (ox, oy))
        raw = self._to_framebuffer_bytes(canvas)
        self.file.seek(0)
        self.file.write(raw)

    def render_solid(self, rgb: tuple[int, int, int]) -> None:
        canvas = Image.new("RGB", (self.width, self.height), rgb)
        raw = self._to_framebuffer_bytes(canvas)
        self.file.seek(0)
        self.file.write(raw)

    def _to_framebuffer_bytes(self, img: Image.Image) -> bytes:
        if self.bpp == 16:
            try:
                packed = img.tobytes("raw", "BGR;16")
                return self._with_stride(packed, self.width * 2)
            except Exception:
                return self._to_framebuffer_bytes_slow(img)
        if self.bpp == 24:
            try:
                packed = img.tobytes("raw", "BGR")
                return self._with_stride(packed, self.width * 3)
            except Exception:
                return self._to_framebuffer_bytes_slow(img)
        if self.bpp == 32:
            try:
                packed = img.tobytes("raw", "BGRX")
                return self._with_stride(packed, self.width * 4)
            except Exception:
                return self._to_framebuffer_bytes_slow(img)
        raise RuntimeError(f"unsupported framebuffer bpp: {self.bpp}")

    def _with_stride(self, packed: bytes, row_bytes: int) -> bytes:
        if self.stride == row_bytes:
            return packed
        out = bytearray(self.stride * self.height)
        for y in range(self.height):
            src_off = y * row_bytes
            dst_off = y * self.stride
            out[dst_off : dst_off + row_bytes] = packed[src_off : src_off + row_bytes]
        return bytes(out)

    def _to_framebuffer_bytes_slow(self, img: Image.Image) -> bytes:
        px = img.load()
        out = bytearray(self.stride * self.height)
        if self.bpp == 16:
            for y in range(self.height):
                base = y * self.stride
                for x in range(self.width):
                    r, g, b = px[x, y]
                    v = ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)
                    out[base + x * 2] = v & 0xFF
                    out[base + x * 2 + 1] = (v >> 8) & 0xFF
            return bytes(out)
        if self.bpp == 24:
            for y in range(self.height):
                base = y * self.stride
                for x in range(self.width):
                    r, g, b = px[x, y]
                    out[base + x * 3] = b
                    out[base + x * 3 + 1] = g
                    out[base + x * 3 + 2] = r
            return bytes(out)
        if self.bpp == 32:
            for y in range(self.height):
                base = y * self.stride
                for x in range(self.width):
                    r, g, b = px[x, y]
                    out[base + x * 4] = b
                    out[base + x * 4 + 1] = g
                    out[base + x * 4 + 2] = r
                    out[base + x * 4 + 3] = 0
            return bytes(out)
        raise RuntimeError(f"unsupported framebuffer bpp: {self.bpp}")


class FBIRenderer:
    def __init__(self, cfg: ExposureConfig, stop_event: threading.Event):
        self.cfg = cfg
        self.stop_event = stop_event
        self.proc: Optional[subprocess.Popen[str]] = None
        self.getty_service = f"getty@tty{int(self.cfg.fbiTty)}.service"
        self.getty_stopped = False
        self.temp_white_image: Optional[Path] = None

    def prepare(self) -> None:
        if shutil.which("fbi") is None:
            raise RuntimeError("fbi command not found. Install with: sudo apt install -y fbi")
        if self.cfg.fbiStopGetty:
            self._suppress_getty()

    def close(self) -> None:
        self._stop_fbi()
        if self.getty_stopped:
            self._run_systemctl("start", self.getty_service, quiet=False)
            self.getty_stopped = False
        if self.temp_white_image and self.temp_white_image.exists():
            try:
                self.temp_white_image.unlink()
            except Exception:
                pass
            self.temp_white_image = None

    def render_image(self, path: str) -> None:
        self.prepare()
        self._start_fbi(Path(path))

    def render_solid(self, rgb: tuple[int, int, int]) -> None:
        self.prepare()
        if self.temp_white_image is None:
            fd, pstr = tempfile.mkstemp(prefix="dlp_fill_", suffix=".png")
            os.close(fd)
            p = Path(pstr)
            # keep default 1080p for projector testing path
            Image.new("RGB", (1920, 1080), rgb).save(p)
            self.temp_white_image = p
        self._start_fbi(self.temp_white_image)

    def _start_fbi(self, image_path: Path) -> None:
        if not image_path.exists():
            raise FileNotFoundError(f"fbi image not found: {image_path}")
        self._stop_fbi()
        cmd = [
            "fbi",
            "-T",
            str(int(self.cfg.fbiTty)),
            "-d",
            self.cfg.framebufferDevice,
            "--noverbose",
        ]
        if self.cfg.fbiAutoScale:
            cmd.append("-a")
        cmd.extend(self.cfg.fbiExtraArgs)
        cmd.append(str(image_path))
        log(f"display via fbi: {' '.join(cmd)}")
        self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True)
        settle_s = max(0.0, self.cfg.framebufferSettleMs / 1000.0)
        if settle_s > 0:
            sleep_interruptible(self.stop_event, settle_s)
        if self.proc.poll() is not None and self.proc.returncode not in (0, None):
            raise RuntimeError(f"fbi exited unexpectedly: rc={self.proc.returncode}")

    def _stop_fbi(self) -> None:
        if self.proc is None:
            return
        if self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=2)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
        self.proc = None

    def _run_systemctl(self, *args: str, quiet: bool = True) -> subprocess.CompletedProcess[str]:
        p = subprocess.run(
            ["systemctl", *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        if not quiet and p.returncode != 0:
            log(f"systemctl {' '.join(args)} failed: {p.stderr.strip()}")
        return p

    def _suppress_getty(self) -> None:
        active = self._run_systemctl("is-active", self.getty_service).returncode == 0
        if not active:
            return
        p = self._run_systemctl("stop", self.getty_service, quiet=False)
        if p.returncode == 0:
            self.getty_stopped = True
            log(f"tty quiet mode enabled: stopped {self.getty_service}")


class PrinterRuntime:
    def __init__(self, cfg: PrintConfig, stop_event: threading.Event):
        self.cfg = cfg
        self.stop_event = stop_event
        self.mock = cfg.useMockHardware or os.name == "nt"
        self.step_pin = None
        self.dir_pin = None
        self.en_pin = None
        self.mag_en_pin = None
        self.home_pin = None
        self.i2c = None
        self.serial = None
        self.fb = None
        self.mag_active_low = None
        self.z_um = 0
        self._emergency_lock = threading.Lock()
        self._emergency_done = False

    def prepare_motion(self) -> None:
        if self.mock or self.step_pin is not None:
            return
        self.step_pin = SysfsGPIOPin(self.cfg.motion.stepPin)
        self.dir_pin = SysfsGPIOPin(self.cfg.motion.dirPin)
        self.en_pin = SysfsGPIOPin(self.cfg.motion.enablePin)
        self.step_pin.prepare("out")
        self.dir_pin.prepare("out")
        self.en_pin.prepare("out")
        if self.cfg.motion.homeTopPin > 0:
            self.home_pin = SysfsGPIOPin(self.cfg.motion.homeTopPin)
            self.home_pin.prepare("in")
        self.step_pin.write(0)
        self._set_motion_enable(False)

    def prepare_magnet(self) -> None:
        if self.mock or self.i2c is not None:
            return
        self.mag_en_pin = SysfsGPIOPin(self.cfg.magnet.enableGpioPin)
        self.mag_en_pin.prepare("out")
        self.i2c = I2CNative(self.cfg.magnet.i2cBus)
        self.mag_active_low = self._resolve_magnet_active_low()
        self._set_magnet_gate(False)

    def prepare_exposure(self) -> None:
        if self.mock or self.serial is not None:
            return
        self.serial = SerialNative(
            self.cfg.exposure.serialPort,
            self.cfg.exposure.baudRate,
            self.cfg.exposure.readTimeoutMs,
        )
        loader = (self.cfg.exposure.imageLoader or "fbi").strip().lower()
        if loader == "fbi":
            self.fb = FBIRenderer(self.cfg.exposure, self.stop_event)
            self.fb.prepare()
        elif loader == "framebuffer":
            self.fb = FramebufferNative(self.cfg.exposure.framebufferDevice)
        else:
            raise RuntimeError(f"unsupported imageLoader: {self.cfg.exposure.imageLoader}")
        self._ensure_dlp_ready("startup")

    def move_um(self, distance_um: int, direction: str, reason: str = "") -> None:
        if distance_um <= 0:
            return
        mm = distance_um / 1000.0
        direction = direction.strip().lower()
        if direction not in {MOVE_DIR_DOWN, MOVE_DIR_UP}:
            raise ValueError("direction must be up/down")
        if self.mock:
            log(f"[MOCK] move {direction} {distance_um}um ({mm:.4f}mm) {reason}".strip())
            sleep_interruptible(self.stop_event, 0.05)
            if direction == MOVE_DIR_DOWN:
                self.z_um += distance_um
            else:
                self.z_um = max(0, self.z_um - distance_um)
            return
        self.prepare_motion()
        steps = int(round((mm * self.cfg.motion.stepsPerRev) / self.cfg.motion.leadMm))
        if steps <= 0:
            log(f"move {direction} {distance_um}um ({mm:.4f}mm) -> 0 step, skipped")
            return
        dir_level = self._direction_to_level(direction)
        self.dir_pin.write(dir_level)
        self._set_motion_enable(True)
        try:
            freq = max(1, int(self.cfg.motion.moveFrequencyHz))
            pulse_us = max(10, int(self.cfg.motion.pulseWidthUs))
            period = 1.0 / freq
            high = pulse_us / 1_000_000.0
            if high >= period:
                raise RuntimeError("pulseWidthUs must be smaller than step period")
            low = period - high
            log(f"move {direction} {distance_um}um ({mm:.4f}mm) -> {steps} steps @ {freq}Hz {reason}".strip())
            for _ in range(steps):
                if self.stop_event.is_set():
                    raise InterruptedError("Interrupted")
                self.step_pin.write(1)
                time.sleep(high)
                self.step_pin.write(0)
                time.sleep(low)
            if direction == MOVE_DIR_DOWN:
                self.z_um += distance_um
            else:
                self.z_um = max(0, self.z_um - distance_um)
        finally:
            self._set_motion_enable(False)

    def home_to_top(self, pre_drop_um: int) -> None:
        if self.mock:
            log(f"[MOCK] pre-home drop {pre_drop_um}um then home to top")
            if pre_drop_um > 0:
                self.move_um(pre_drop_um, MOVE_DIR_DOWN, reason="pre-home drop")
            self.z_um = 0
            return
        self.prepare_motion()
        if self.home_pin is None:
            raise RuntimeError("homeTopPin is not configured; cannot home to top")
        stop = 1 if int(self.cfg.motion.homeStopLevel) else 0
        # Only pre-drop when already on the top limit.
        if pre_drop_um > 0 and self.home_pin.read() == stop:
            self.move_um(pre_drop_um, MOVE_DIR_DOWN, reason="pre-home drop")
        chunk = max(1, int(self.cfg.motion.homeChunkSteps))
        max_steps = max(1, int(self.cfg.motion.homeMaxSteps))
        freq = max(1, int(self.cfg.motion.homeFrequencyHz))
        pulse_us = max(10, int(self.cfg.motion.pulseWidthUs))
        period = 1.0 / freq
        high = pulse_us / 1_000_000.0
        if high >= period:
            raise RuntimeError("pulseWidthUs must be smaller than step period (home)")
        low = period - high

        dir_level = self._direction_to_level(MOVE_DIR_UP)
        self.dir_pin.write(dir_level)
        self._set_motion_enable(True)
        total = 0
        try:
            while total < max_steps:
                if self.stop_event.is_set():
                    raise InterruptedError("Interrupted")
                if self.home_pin.read() == stop:
                    log(f"home reached top limit, steps={total}")
                    self.z_um = 0
                    return
                run = min(chunk, max_steps - total)
                for _ in range(run):
                    if self.stop_event.is_set():
                        raise InterruptedError("Interrupted")
                    self.step_pin.write(1)
                    time.sleep(high)
                    self.step_pin.write(0)
                    time.sleep(low)
                    total += 1
                    if self.home_pin.read() == stop:
                        log(f"home reached top limit, steps={total}")
                        self.z_um = 0
                        return
            raise RuntimeError(f"home failed: reached max steps ({max_steps})")
        finally:
            self._set_motion_enable(False)

    def apply_magnetic(self, direction_bits: str, voltage: float, hold_s: float) -> None:
        if self.mock:
            log(f"[MOCK] magnet bits={direction_bits} v={voltage:.3f} hold={hold_s:.3f}s")
            sleep_interruptible(self.stop_event, hold_s)
            return
        self.prepare_magnet()
        out = bits_to_output_byte(direction_bits)
        hold_safe = max(0.0, float(hold_s))
        if self.cfg.logMagneticSteps:
            log(
                f"magnet on: bits={direction_bits} out=0x{out:02x} "
                f"v={float(voltage):.3f} hold={hold_safe:.3f}s"
            )
        if hold_safe <= 0 and self.cfg.logMagneticSteps:
            log("magnet hold is 0s: pulse may be too short to observe during debug")
        self._set_magnet_gate(True)
        try:
            self._select_tca()
            self._write_pca9554(out)
            self._write_dac(voltage)
            sleep_interruptible(self.stop_event, hold_safe)
        finally:
            try:
                self._write_dac(0.0)
            except Exception:
                pass
            try:
                self._disable_tca()
            except Exception:
                pass
            self._set_magnet_gate(False)
            if self.cfg.logMagneticSteps:
                log("magnet off")

    def magnet_self_test(self, bits: str, voltage: float, hold_s: float, repeat: int, interval_s: float) -> None:
        rep = max(1, int(repeat))
        gap = max(0.0, float(interval_s))
        for idx in range(rep):
            if self.stop_event.is_set():
                raise InterruptedError("Interrupted")
            log(f"magnet-test {idx + 1}/{rep}")
            self.apply_magnetic(bits, voltage, hold_s)
            if idx + 1 < rep and gap > 0:
                sleep_interruptible(self.stop_event, gap)

    def expose(self, image_path: str, intensity: int, seconds: float) -> None:
        if self.mock:
            log(f"[MOCK] exposure intensity={intensity} seconds={seconds:.3f} image={image_path}")
            sleep_interruptible(self.stop_event, seconds)
            return
        self.prepare_exposure()
        log(f"exposure start: intensity={intensity} sec={seconds:.3f} image={image_path}")
        self.fb.render_image(image_path)
        self._set_brightness(intensity)
        self._send_dlp(DLP_CMD_LED_ON, "led_on")
        try:
            sleep_interruptible(self.stop_event, seconds)
        finally:
            self._send_dlp(
                DLP_CMD_LED_OFF,
                "led_off",
                allow_reject=self.cfg.exposure.ignoreLedOffReject,
                silent_reject=self.cfg.exposure.ignoreLedOffReject,
            )
            log("exposure done")

    def dlp_self_test(
        self,
        image_path: Optional[str],
        intensity: int,
        seconds: float,
        repeat: int,
        interval_s: float,
    ) -> None:
        if self.mock:
            log(f"[MOCK] dlp-test intensity={intensity} sec={seconds:.3f} repeat={repeat}")
            return
        self.prepare_exposure()
        rep = max(1, int(repeat))
        gap = max(0.0, float(interval_s))
        for idx in range(rep):
            if self.stop_event.is_set():
                raise InterruptedError("Interrupted")
            if image_path:
                self.fb.render_image(image_path)
            else:
                self.fb.render_solid((255, 255, 255))
            log(f"dlp-test exposure {idx + 1}/{rep}: intensity={intensity}, seconds={seconds:.3f}")
            self._set_brightness(intensity)
            self._send_dlp(DLP_CMD_LED_ON, "led_on")
            try:
                sleep_interruptible(self.stop_event, seconds)
            finally:
                self._send_dlp(
                    DLP_CMD_LED_OFF,
                    "led_off",
                    allow_reject=self.cfg.exposure.ignoreLedOffReject,
                    silent_reject=self.cfg.exposure.ignoreLedOffReject,
                )
            if idx + 1 < rep and gap > 0:
                sleep_interruptible(self.stop_event, gap)

    def emergency_stop(self, return_to_top: bool = False) -> None:
        log("emergency stop: stopping motion + magnetic + dlp")
        if self.step_pin:
            self._safe_call("step_pin low", lambda: self.step_pin.write(0))
        self._safe_call("motion disable", lambda: self._set_motion_enable(False))
        if self.i2c:
            self._safe_call("dac zero", lambda: self._write_dac(0.0))
            self._safe_call("tca disable", self._disable_tca)
        self._safe_call("magnet gate off", lambda: self._set_magnet_gate(False))
        if self.serial:
            self._safe_call(
                "dlp led_off",
                lambda: self._send_dlp(DLP_CMD_LED_OFF, "led_off", allow_reject=True, silent_reject=True),
            )
            self._safe_call("dlp off", lambda: self._send_dlp(DLP_CMD_OFF, "dlp_off", allow_reject=True))
        if return_to_top and self.cfg.interruptReturnToTop:
            self._safe_call("return to top after interrupt", self._safe_return_to_top_after_interrupt)

    def request_emergency_stop(self, reason: str, return_to_top: bool = False) -> None:
        self.stop_event.set()
        with self._emergency_lock:
            if self._emergency_done:
                return
            self._emergency_done = True
        log(f"safety stop requested: {reason}")
        self.emergency_stop(return_to_top=return_to_top)

    def close(self) -> None:
        self.request_emergency_stop("runtime.close")
        for pin in [self.step_pin, self.dir_pin, self.en_pin, self.mag_en_pin, self.home_pin]:
            try:
                if pin:
                    pin.close()
            except Exception:
                pass
        try:
            if self.i2c:
                self.i2c.close()
        except Exception:
            pass
        try:
            if self.serial:
                self.serial.close()
        except Exception:
            pass
        try:
            if self.fb:
                self.fb.close()
        except Exception:
            pass

    def _resolve_magnet_active_low(self) -> bool:
        if self.cfg.magnet.enableActiveLow is not None:
            return bool(self.cfg.magnet.enableActiveLow)
        # auto detect by testing if PCA9554 can be read
        for active_low in (True, False):
            try:
                self.mag_active_low = active_low
                self._set_magnet_gate(True)
                self._select_tca()
                _ = self.i2c.read_reg(self.cfg.magnet.pca9554Address, 0x01)
                log(f"magnet gate auto-detected: enableActiveLow={active_low}")
                return active_low
            except Exception:
                pass
            finally:
                try:
                    self._disable_tca()
                except Exception:
                    pass
                try:
                    self._set_magnet_gate(False)
                except Exception:
                    pass
        raise RuntimeError("cannot access PCA9554 under any gate polarity")

    def _set_motion_enable(self, enable: bool) -> None:
        if self.en_pin is None:
            return
        level_on = 0 if self.cfg.motion.enableLow else 1
        self.en_pin.write(level_on if enable else 1 - level_on)

    def _direction_to_level(self, direction: str) -> int:
        up_level = 1 if self.cfg.motion.dirHighIsUp else 0
        down_level = 1 - up_level
        return up_level if direction == MOVE_DIR_UP else down_level

    def _set_magnet_gate(self, on: bool) -> None:
        if self.mag_en_pin is None:
            return
        active_low = True if self.mag_active_low is None else self.mag_active_low
        level_on = 0 if active_low else 1
        self.mag_en_pin.write(level_on if on else 1 - level_on)

    def _select_tca(self) -> None:
        mask = 1 << self.cfg.magnet.tcaChannel if 0 <= self.cfg.magnet.tcaChannel <= 7 else 0
        self.i2c.write(self.cfg.magnet.tcaAddress, bytes([mask]))

    def _disable_tca(self) -> None:
        self.i2c.write(self.cfg.magnet.tcaAddress, b"\x00")

    def _write_pca9554(self, out: int) -> None:
        self.i2c.write(self.cfg.magnet.pca9554Address, bytes([0x03, 0x00]))
        self.i2c.write(self.cfg.magnet.pca9554Address, bytes([0x01, out & 0xFF]))
        readback = self.i2c.read_reg(self.cfg.magnet.pca9554Address, 0x01)
        if readback != (out & 0xFF):
            raise RuntimeError(f"pca9554 readback mismatch: want=0x{out:02x} got=0x{readback:02x}")

    def _write_dac(self, voltage: float) -> None:
        vref = max(0.1, self.cfg.magnet.dacVRef)
        v = min(max(voltage, 0.0), vref)
        code = int(round((v / vref) * 4095.0))
        high = (code >> 4) & 0xFF
        low = (code & 0x0F) << 4
        self.i2c.write(self.cfg.magnet.mcp4725Address, bytes([0x40, high, low]))

    def _ensure_dlp_ready(self, stage: str) -> None:
        log(f"dlp init ({stage})")
        self._send_dlp(DLP_CMD_HANDSHAKE, "handshake")
        self._send_dlp(DLP_CMD_ON, "dlp_on")
        settle_s = max(0.0, self.cfg.exposure.dlpOnSettleMs / 1000.0)
        if settle_s > 0:
            sleep_interruptible(self.stop_event, settle_s)
        if self.cfg.exposure.sendFanOnCommand:
            self._send_dlp(
                DLP_CMD_FAN_ON,
                "fan_on",
                allow_reject=self.cfg.exposure.ignoreFanReject,
                silent_reject=self.cfg.exposure.ignoreFanReject,
            )
        self._send_dlp(
            DLP_CMD_LED_OFF,
            "led_off",
            allow_reject=self.cfg.exposure.ignoreLedOffReject,
            silent_reject=self.cfg.exposure.ignoreLedOffReject,
        )

    def _send_dlp(self, cmd: bytes, desc: str, allow_reject: bool = False, silent_reject: bool = False) -> None:
        if self.cfg.exposure.logDlpCommands:
            log(f"dlp tx {desc}: {cmd.hex(' ')}")
        resp = self.serial.send(cmd, max(1, self.cfg.exposure.responseReadBytes))
        if self.cfg.exposure.logDlpCommands:
            log(f"dlp rx {desc}: {resp.hex(' ') if resp else '<empty>'}")
        if len(resp) == 0:
            raise RuntimeError(f"{desc} timeout")
        if resp[-1] == 0xE0:
            if allow_reject:
                if not silent_reject:
                    log(f"{desc} rejected but ignored: {resp.hex(' ')}")
                return
            raise RuntimeError(f"{desc} rejected by dlp, response={resp.hex(' ')}")

    def _safe_call(self, label: str, fn) -> None:  # noqa: ANN001
        try:
            fn()
        except Exception as exc:
            log(f"safety step failed ({label}): {exc}")

    def _safe_return_to_top_after_interrupt(self) -> None:
        if self.mock or not self.cfg.moveEnabled:
            return
        self.prepare_motion()
        freq = max(1, int(self.cfg.motion.homeFrequencyHz))
        pulse_us = max(10, int(self.cfg.motion.pulseWidthUs))
        period = 1.0 / freq
        high = pulse_us / 1_000_000.0
        if high >= period:
            raise RuntimeError("pulseWidthUs must be smaller than step period (interrupt return)")
        low = period - high

        self.dir_pin.write(self._direction_to_level(MOVE_DIR_UP))
        self._set_motion_enable(True)
        steps_done = 0
        try:
            stop = 1 if int(self.cfg.motion.homeStopLevel) else 0
            # Prefer hardware top limit when available.
            if self.home_pin is not None:
                max_steps = max(1, int(self.cfg.motion.homeMaxSteps))
                if self.home_pin.read() == stop:
                    self.z_um = 0
                    log("interrupt return: already at top limit")
                    return
                log(f"interrupt return: moving up to top limit @ {freq}Hz")
                while steps_done < max_steps:
                    self.step_pin.write(1)
                    time.sleep(high)
                    self.step_pin.write(0)
                    time.sleep(low)
                    steps_done += 1
                    if self.home_pin.read() == stop:
                        self.z_um = 0
                        log(f"interrupt return: reached top limit, steps={steps_done}")
                        return
                log(f"interrupt return: top limit not reached within {max_steps} steps")
                return

            # Fallback without top limit: use tracked z height.
            mm = max(0.0, self.z_um / 1000.0)
            steps = int(round((mm * self.cfg.motion.stepsPerRev) / self.cfg.motion.leadMm))
            if steps <= 0:
                log("interrupt return: no home pin and z estimate is zero, skipped")
                return
            log(f"interrupt return: no home pin, moving up estimated {steps} steps @ {freq}Hz")
            for _ in range(steps):
                self.step_pin.write(1)
                time.sleep(high)
                self.step_pin.write(0)
                time.sleep(low)
                steps_done += 1
            self.z_um = 0
            log(f"interrupt return: estimated top move completed, steps={steps_done}")
        finally:
            self._set_motion_enable(False)

    def _set_brightness(self, intensity: int) -> None:
        if self.cfg.exposure.skipBrightnessCommand:
            return
        val = max(0, min(255, int(intensity)))
        floor = max(0, min(255, int(self.cfg.exposure.minimumIntensity)))
        if 0 < val < floor:
            val = floor
        self._send_dlp(
            bytes([0xA6, 0x02, 0x10, val]),
            f"brightness_{val}",
            allow_reject=bool(self.cfg.exposure.ignoreBrightnessReject),
        )


def extract_zip_safe(zip_path: Path, dst_root: Path) -> None:
    dst_abs = dst_root.resolve()
    with zipfile.ZipFile(zip_path, "r") as zf:
        for info in zf.infolist():
            clean_name = Path(info.filename)
            if ".." in clean_name.parts:
                raise ValueError(f"zip path traversal: {info.filename}")
            target = (dst_root / clean_name).resolve()
            if target != dst_abs and dst_abs not in target.parents:
                raise ValueError(f"zip entry escaped destination: {info.filename}")
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info, "r") as src, target.open("wb") as out:
                shutil.copyfileobj(src, out)


def find_manifest_file(root: Path) -> Path:
    for p in root.rglob("slice_magnetic_manifest.json"):
        if p.is_file():
            return p
    raise FileNotFoundError("slice_magnetic_manifest.json not found in zip")


def load_config(path: Path) -> PrintConfig:
    cfg = PrintConfig()
    if not path.exists():
        return cfg
    raw = json.loads(path.read_text(encoding="utf-8"))
    for k in [
        "useMockHardware",
        "moveEnabled",
        "bottomDistanceUm",
        "preHomeEnabled",
        "preHomeDropUm",
        "peelDistanceUm",
        "layerThicknessUmOverride",
        "finalReturnToTop",
        "magneticEnabled",
        "magneticHoldSeconds",
        "exposureEnabled",
        "exposureSeconds",
        "minExposureIntensity",
        "maxExposureIntensity",
        "interLayerDelaySeconds",
    ]:
        if k in raw:
            setattr(cfg, k, raw[k])
    if "motion" in raw:
        cfg.motion = MotionConfig(**{**cfg.motion.__dict__, **raw["motion"]})
    if "magnet" in raw:
        cfg.magnet = MagnetConfig(**{**cfg.magnet.__dict__, **raw["magnet"]})
    if "exposure" in raw:
        cfg.exposure = ExposureConfig(**{**cfg.exposure.__dict__, **raw["exposure"]})
    return cfg


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run magnetic-assisted DLP print directly from sliced zip")
    p.add_argument("--zip", default="", help="Path to slice zip, e.g. slice_example.zip")
    p.add_argument("--config", default="cli_config.json", help="Path to config json")
    p.add_argument("--start-layer", type=int, default=0, help="Start layer index (0-based)")
    p.add_argument("--end-layer", type=int, default=-1, help="End layer index inclusive (-1 means all)")
    p.add_argument("--skip-positioning", action="store_true", help="Skip pre-home and bottom move (for resume)")
    p.add_argument("--dlp-test", action="store_true", help="Run DLP-only exposure test and exit")
    p.add_argument("--dlp-image", default="", help="Image path for DLP test (default: full white frame)")
    p.add_argument("--dlp-intensity", type=int, default=-1, help="DLP test intensity (0-255, -1 uses config)")
    p.add_argument("--dlp-seconds", type=float, default=2.0, help="DLP test exposure seconds")
    p.add_argument("--dlp-repeat", type=int, default=1, help="DLP test repeat count")
    p.add_argument("--dlp-interval", type=float, default=0.5, help="DLP test interval seconds")
    p.add_argument("--magnet-test", action="store_true", help="Run magnetic-only test and exit")
    p.add_argument("--magnet-bits", default=X_POSITIVE_BITS, help="8-bit direction mask, e.g. 00001111")
    p.add_argument("--magnet-voltage", type=float, default=1.0, help="Magnetic DAC voltage for test")
    p.add_argument("--magnet-hold", type=float, default=1.0, help="Magnetic hold seconds for test")
    p.add_argument("--magnet-repeat", type=int, default=1, help="Magnetic test repeat count")
    p.add_argument("--magnet-interval", type=float, default=0.5, help="Magnetic test interval seconds")
    p.add_argument("--keep-extracted", action="store_true", help="Keep extracted temp files")
    p.add_argument("--mock", action="store_true", help="Force mock hardware mode")
    return p.parse_args()


def resolve_strength(record: dict) -> float:
    field = record.get("field") or {}
    if field.get("strength") is not None:
        return float(field["strength"])
    if record.get("strength") is not None:
        return float(record["strength"])
    return 0.0


def main() -> int:
    if os.name == "nt":
        log("warning: non-linux platform, script will run in mock mode")

    args = parse_args()
    zip_path = Path(args.zip).resolve() if args.zip else None
    config_path = Path(args.config).resolve()

    cfg = load_config(config_path)
    if args.mock:
        cfg.useMockHardware = True
    if os.name == "nt":
        cfg.useMockHardware = True

    stop_event = threading.Event()
    tmp_dir = Path(tempfile.mkdtemp(prefix="slice_job_"))
    runtime = PrinterRuntime(cfg, stop_event)
    signal_count = {"n": 0}

    def on_signal(signum, frame):  # noqa: ANN001
        _ = frame
        signal_count["n"] += 1
        runtime.request_emergency_stop(f"signal {signum}", return_to_top=True)
        if signal_count["n"] >= 2:
            log("second interrupt received, forcing exit now")
            os._exit(130)

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)
    atexit.register(lambda: runtime.request_emergency_stop("atexit"))

    try:
        if args.dlp_test:
            image_path = args.dlp_image.strip()
            if image_path:
                image_abs = Path(image_path).resolve()
                if not image_abs.exists():
                    raise FileNotFoundError(f"dlp-test image not found: {image_abs}")
                image_use = str(image_abs)
            else:
                image_use = None
            intensity = int(cfg.exposure.defaultIntensity if args.dlp_intensity < 0 else args.dlp_intensity)
            intensity = max(cfg.minExposureIntensity, min(cfg.maxExposureIntensity, intensity))
            runtime.dlp_self_test(
                image_path=image_use,
                intensity=intensity,
                seconds=max(0.01, float(args.dlp_seconds)),
                repeat=max(1, int(args.dlp_repeat)),
                interval_s=max(0.0, float(args.dlp_interval)),
            )
            log("dlp-test completed")
            return 0
        if args.magnet_test:
            bits = str(args.magnet_bits).strip()
            if len(bits) != 8 or any(ch not in "01" for ch in bits):
                raise ValueError(f"invalid --magnet-bits: {bits}")
            runtime.magnet_self_test(
                bits=bits,
                voltage=float(args.magnet_voltage),
                hold_s=max(0.0, float(args.magnet_hold)),
                repeat=max(1, int(args.magnet_repeat)),
                interval_s=max(0.0, float(args.magnet_interval)),
            )
            log("magnet-test completed")
            return 0

        if zip_path is None or not zip_path.exists():
            raise FileNotFoundError(f"zip not found: {zip_path}")
        if cfg.exposureEnabled:
            log("dlp preflight check: startup handshake/communication")
            runtime.prepare_exposure()
        log(f"extracting zip: {zip_path}")
        extract_zip_safe(zip_path, tmp_dir)
        manifest_path = find_manifest_file(tmp_dir)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        records = manifest.get("records", []) or []
        records.sort(key=lambda r: (int(r.get("layer", 0)), str(r.get("file", ""))))

        if not records:
            raise RuntimeError("manifest has no records")

        manifest_layer_thickness_um = int(round(float(manifest.get("layer_thickness_mm") or 0.05) * 1000.0))
        layer_thickness_um = cfg.layerThicknessUmOverride if cfg.layerThicknessUmOverride is not None else manifest_layer_thickness_um
        if layer_thickness_um <= 0:
            raise RuntimeError("layer thickness must be > 0")

        end_layer = args.end_layer if args.end_layer >= 0 else max(int(r.get("layer", 0)) for r in records)
        selected = [r for r in records if args.start_layer <= int(r.get("layer", 0)) <= end_layer]
        log(f"records total={len(records)} selected={len(selected)} layers={args.start_layer}..{end_layer}")

        # group records by layer to support multi-exposure in the same layer
        layer_groups: dict[int, list[dict]] = {}
        for rec in selected:
            layer = int(rec.get("layer", -1))
            layer_groups.setdefault(layer, []).append(rec)
        ordered_layers = sorted(layer_groups.keys())

        if args.skip_positioning:
            log("skip-positioning enabled: pre-home and bottom move skipped")
        else:
            if cfg.moveEnabled and cfg.preHomeEnabled:
                runtime.home_to_top(max(0, int(cfg.preHomeDropUm)))
            if cfg.moveEnabled and cfg.bottomDistanceUm > 0:
                runtime.move_um(cfg.bottomDistanceUm, MOVE_DIR_DOWN, reason="to bottom")

        peel_um = max(0, int(cfg.peelDistanceUm))
        for layer_idx, layer_no in enumerate(ordered_layers, start=1):
            if stop_event.is_set():
                raise InterruptedError("Interrupted")
            group = layer_groups[layer_no]
            log(f"layer {layer_no} ({layer_idx}/{len(ordered_layers)}), records={len(group)}")

            # Step 2 / 6: up peel distance before entering this layer process
            if cfg.moveEnabled and peel_um > 0:
                runtime.move_um(peel_um, MOVE_DIR_UP, reason="pre-layer lift")

            # Step 3/4 + Step 5 within same layer:
            # first record: magnet -> down(peel-layer_th) -> expose
            # additional same-layer records: up(peel) -> magnet -> down(peel) -> expose
            for rec_idx, rec in enumerate(group):
                if stop_event.is_set():
                    raise InterruptedError("Interrupted")
                field = rec.get("field") or {}
                x = float(field.get("x") or 0.0)
                y = float(field.get("y") or 0.0)
                bits = resolve_direction_bits(x, y)
                strength = resolve_strength(rec)
                if cfg.exposure.useSliceIntensity:
                    intensity = int(rec.get("light_intensity") or cfg.exposure.defaultIntensity)
                else:
                    intensity = int(cfg.exposure.defaultIntensity)
                intensity = max(cfg.minExposureIntensity, min(cfg.maxExposureIntensity, intensity))
                rel_file = str(rec.get("file") or "").strip()
                if not rel_file:
                    raise RuntimeError(f"layer {layer_no}: image file path empty")
                img = (manifest_path.parent / Path(rel_file.replace("/", os.sep))).resolve()
                if not img.exists():
                    raise FileNotFoundError(f"layer {layer_no}: image not found: {img}")

                if rec_idx > 0 and cfg.moveEnabled and peel_um > 0:
                    runtime.move_um(peel_um, MOVE_DIR_UP, reason=f"same-layer({layer_no}) re-lift #{rec_idx}")

                if cfg.magneticEnabled:
                    runtime.apply_magnetic(bits, strength, cfg.magneticHoldSeconds)
                elif cfg.logMagneticSteps:
                    log("magnet skipped: magneticEnabled=false")

                if cfg.moveEnabled:
                    if rec_idx == 0:
                        down_um = max(0, peel_um - layer_thickness_um)
                    else:
                        down_um = peel_um
                    if down_um > 0:
                        runtime.move_um(down_um, MOVE_DIR_DOWN, reason=f"press for layer {layer_no} rec#{rec_idx}")

                if cfg.exposureEnabled:
                    runtime.expose(str(img), intensity, cfg.exposureSeconds)

            if cfg.interLayerDelaySeconds > 0:
                sleep_interruptible(stop_event, cfg.interLayerDelaySeconds)

        if cfg.moveEnabled and cfg.finalReturnToTop and runtime.z_um > 0:
            runtime.move_um(runtime.z_um, MOVE_DIR_UP, reason="return to top")

        log("print completed")
        return 0
    except InterruptedError:
        log("print interrupted by user")
        return 130
    except KeyboardInterrupt:
        runtime.request_emergency_stop("KeyboardInterrupt", return_to_top=True)
        log("print interrupted by keyboard")
        return 130
    finally:
        runtime.close()
        if args.keep_extracted:
            log(f"extracted files kept: {tmp_dir}")
        else:
            shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
