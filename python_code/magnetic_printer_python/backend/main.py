from __future__ import annotations

import asyncio
import errno
import hashlib
import hmac
import io
import json
import logging
import os
import shutil
import threading
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from secrets import token_hex
from typing import Any, Callable, Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import FastAPI, File, Header, HTTPException, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

try:
    from PIL import Image
except Exception:  # pragma: no cover
    Image = None

try:
    import serial  # type: ignore
except Exception:  # pragma: no cover
    serial = None

if os.name != "nt":
    import fcntl
else:  # pragma: no cover
    fcntl = None  # type: ignore

STATE_IDLE = "idle"
STATE_RUNNING = "running"
STATE_CANCELING = "canceling"
STATE_COMPLETED = "completed"
STATE_FAILED = "failed"
STATE_CANCELED = "canceled"

X_POSITIVE_BITS = "00001111"
X_NEGATIVE_BITS = "11110000"
Y_POSITIVE_BITS = "11000011"
Y_NEGATIVE_BITS = "00111100"
MOVE_DIR_DOWN = "down"
MOVE_DIR_UP = "up"

FLOW_CONFIG_MAGIC = b"MPRF1"
FLOW_CONFIG_VERSION = 1
FLOW_CONFIG_MAX_BYTES = 4 << 20
I2C_SLAVE_IOCTL = 0x0703

DLP_CMD_HANDSHAKE = bytes([0xA6, 0x01, 0x05])
DLP_CMD_ON = bytes([0xA6, 0x02, 0x02, 0x01])
DLP_CMD_OFF = bytes([0xA6, 0x02, 0x02, 0x00])
DLP_CMD_LED_ON = bytes([0xA6, 0x02, 0x03, 0x01])
DLP_CMD_LED_OFF = bytes([0xA6, 0x02, 0x03, 0x00])


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_utc_str() -> str:
    return now_utc().strftime("%Y-%m-%dT%H:%M:%SZ")


def to_jsonable_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def random_hex(n_bytes: int) -> str:
    return token_hex(n_bytes)


def normalize_direction_bits(bits: str) -> str:
    b = (bits or "").strip()
    return b if b else X_POSITIVE_BITS


def validate_direction_bits(bits: str) -> None:
    if len(bits) != 8:
        raise ValueError("directionBits must be 8-bit binary string")
    for ch in bits:
        if ch not in "01":
            raise ValueError("directionBits must contain only 0/1")


def resolve_direction_bits(x: float, y: float) -> str:
    abs_x = abs(x)
    abs_y = abs(y)
    if abs_x < 1e-4 and abs_y < 1e-4:
        return X_POSITIVE_BITS
    if abs_x >= abs_y:
        return X_POSITIVE_BITS if x >= 0 else X_NEGATIVE_BITS
    return Y_POSITIVE_BITS if y >= 0 else Y_NEGATIVE_BITS


def normalize_move_direction(direction: str) -> str:
    d = (direction or "").strip().lower()
    if not d:
        return MOVE_DIR_DOWN
    if d in {MOVE_DIR_DOWN, "movedown", "downward", "d"}:
        return MOVE_DIR_DOWN
    if d in {MOVE_DIR_UP, "moveup", "upward", "u"}:
        return MOVE_DIR_UP
    raise ValueError("moveDirection must be up/down")


def hash_password(password: str) -> tuple[str, str]:
    salt = os.urandom(16)
    out = derive_password_hash(password, salt)
    return salt.hex(), out.hex()


def derive_password_hash(password: str, salt: bytes) -> bytes:
    out = hashlib.sha256(salt + password.encode("utf-8")).digest()
    for _ in range(120000):
        h = hashlib.sha256()
        h.update(salt)
        h.update(out)
        out = h.digest()
    return out


def verify_password(password: str, salt_hex: str, hash_hex: str) -> bool:
    try:
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except ValueError:
        return False
    actual = derive_password_hash(password, salt)
    return hmac.compare_digest(expected, actual)


@dataclass
class MagnetNativeConfig:
    enableGpioPin: int = 27
    i2cBus: int = 1
    tcaAddress: int = 0x70
    tcaChannel: int = 0
    pca9554Address: int = 0x27
    mcp4725Address: int = 0x60
    dacVRef: float = 5.0


@dataclass
class MotionNativeConfig:
    stepPin: int = 13
    dirPin: int = 5
    enablePin: int = 8
    enableLow: bool = False
    frequencyHz: int = 800
    moveFrequencyHz: int = 800
    homeFrequencyHz: int = 1600
    pulseWidthUs: int = 20
    stepsPerRev: int = 3200
    leadMm: float = 4.0
    homeTopPin: int = 21
    homeStopLevel: int = 1
    homeDirection: str = MOVE_DIR_UP
    homeMaxSteps: int = 0
    homeChunkSteps: int = 200
    homeReportEvery: int = 1000


@dataclass
class ExposureNativeConfig:
    serialPort: str = "/dev/ttyUSB0"
    baudRate: int = 115200
    readTimeoutMs: int = 1000
    responseReadBytes: int = 10
    framebufferDevice: str = "/dev/fb0"
    framebufferSettleMs: int = 250


@dataclass
class HardwareConfig:
    useMockHardware: bool = False
    skipWaitInMock: bool = True
    commandTimeoutSeconds: int = 120
    magnet: MagnetNativeConfig = field(default_factory=MagnetNativeConfig)
    motion: MotionNativeConfig = field(default_factory=MotionNativeConfig)
    exposure: ExposureNativeConfig = field(default_factory=ExposureNativeConfig)


@dataclass
class AppConfig:
    listenAddr: str = ":5241"
    dataRoot: str = "runtime_data"
    frontendRoot: str = "../frontend"
    adminUsers: list[str] = field(default_factory=lambda: ["admin"])
    hardware: HardwareConfig = field(default_factory=HardwareConfig)

    @staticmethod
    def default_for_platform() -> "AppConfig":
        cfg = AppConfig()
        cfg.hardware.useMockHardware = os.name == "nt"
        return cfg

    @classmethod
    def load(cls, base_dir: Path) -> "AppConfig":
        cfg = cls.default_for_platform()
        cfg_path = base_dir / "config.json"
        if cfg_path.exists():
            raw = json.loads(cfg_path.read_text(encoding="utf-8"))
            cfg.listenAddr = raw.get("listenAddr", cfg.listenAddr)
            cfg.dataRoot = raw.get("dataRoot", cfg.dataRoot)
            cfg.frontendRoot = raw.get("frontendRoot", cfg.frontendRoot)
            cfg.adminUsers = raw.get("adminUsers", cfg.adminUsers) or ["admin"]
            hw = raw.get("hardware", {})
            cfg.hardware.useMockHardware = bool(hw.get("useMockHardware", cfg.hardware.useMockHardware))
            cfg.hardware.skipWaitInMock = bool(hw.get("skipWaitInMock", cfg.hardware.skipWaitInMock))
            cfg.hardware.commandTimeoutSeconds = int(hw.get("commandTimeoutSeconds", cfg.hardware.commandTimeoutSeconds))
            mg = hw.get("magnet", {})
            mo = hw.get("motion", {})
            ex = hw.get("exposure", {})
            cfg.hardware.magnet = MagnetNativeConfig(**{**cfg.hardware.magnet.__dict__, **mg})
            cfg.hardware.motion = MotionNativeConfig(**{**cfg.hardware.motion.__dict__, **mo})
            cfg.hardware.exposure = ExposureNativeConfig(**{**cfg.hardware.exposure.__dict__, **ex})
        if not cfg.listenAddr.strip():
            cfg.listenAddr = ":5241"
        if not cfg.adminUsers:
            cfg.adminUsers = ["admin"]
        if cfg.hardware.commandTimeoutSeconds <= 0:
            cfg.hardware.commandTimeoutSeconds = 120
        cfg.dataRoot = str(resolve_path(base_dir, cfg.dataRoot))
        cfg.frontendRoot = str(resolve_path(base_dir, cfg.frontendRoot))
        return cfg


def resolve_path(base_dir: Path, p: str) -> Path:
    if not p:
        return base_dir
    path = Path(p)
    if path.is_absolute():
        return path
    return (base_dir / path).resolve()


def is_admin_user(cfg: AppConfig, username: str) -> bool:
    u = (username or "").strip().lower()
    return any((a or "").strip().lower() == u for a in cfg.adminUsers)


def api_result(success: bool, code: str, message: str, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"success": success, "code": code, "message": message}
    payload.update(extra)
    return payload


@dataclass
class SessionInfo:
    token: str
    username: str
    createdAtUtc: datetime


class AuthService:
    def __init__(self, data_root: Path):
        self._lock = threading.RLock()
        self._state_file = data_root / "auth_state.json"
        self._users: dict[str, dict[str, Any]] = {}
        self._lock_owner: str = ""
        self._sessions: dict[str, SessionInfo] = {}
        self._load_state()

    def _load_state(self) -> None:
        with self._lock:
            if not self._state_file.exists():
                return
            raw = json.loads(self._state_file.read_text(encoding="utf-8"))
            self._users = raw.get("users", {}) or {}
            self._lock_owner = raw.get("lockOwner", "") or ""

    def _save_state_locked(self) -> None:
        tmp = self._state_file.with_suffix(".tmp")
        payload = {"users": self._users, "lockOwner": self._lock_owner}
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self._state_file)

    def register(self, username: str, password: str) -> dict[str, Any]:
        username = (username or "").strip()
        if len(username) < 3 or len(username) > 32:
            return api_result(False, "INVALID_USERNAME", "Username must be 3-32 chars.")
        if len(password or "") < 6:
            return api_result(False, "INVALID_PASSWORD", "Password must be at least 6 chars.")
        key = username.lower()
        with self._lock:
            if key in self._users:
                return api_result(False, "USERNAME_EXISTS", "Username already exists.")
            salt, h = hash_password(password)
            self._users[key] = {
                "username": username,
                "passwordHash": h,
                "salt": salt,
                "createdAtUtc": to_jsonable_datetime(now_utc()),
            }
            self._save_state_locked()
        return api_result(True, "OK", "Registered.")

    def login(self, username: str, password: str) -> dict[str, Any]:
        username = (username or "").strip()
        if not username or not password:
            return api_result(False, "INVALID_INPUT", "Username and password are required.")
        with self._lock:
            account = self._users.get(username.lower())
            if not account:
                return api_result(False, "INVALID_CREDENTIALS", "Invalid username or password.")
            if not verify_password(password, account.get("salt", ""), account.get("passwordHash", "")):
                return api_result(False, "INVALID_CREDENTIALS", "Invalid username or password.")
            real_user = account.get("username", username)
            if self._lock_owner and self._lock_owner.lower() != real_user.lower():
                return api_result(False, "DEVICE_LOCKED", f"Device is currently controlled by '{self._lock_owner}'.")
            if not self._lock_owner:
                self._lock_owner = real_user
                self._save_state_locked()
            token = random_hex(32)
            self._sessions[token] = SessionInfo(token=token, username=real_user, createdAtUtc=now_utc())
        return {**api_result(True, "OK", "Logged in."), "token": token, "username": real_user}

    def validate(self, token: str) -> Optional[SessionInfo]:
        with self._lock:
            s = self._sessions.get(token)
            if not s:
                return None
            return SessionInfo(token=s.token, username=s.username, createdAtUtc=s.createdAtUtc)

    def lock_owner(self) -> str:
        with self._lock:
            return self._lock_owner

    def is_lock_owner(self, username: str) -> bool:
        with self._lock:
            return bool(self._lock_owner) and self._lock_owner.lower() == (username or "").strip().lower()

    def logout(self, token: str, device_busy: bool) -> None:
        with self._lock:
            self._sessions.pop(token, None)
            self._release_lock_if_needed_locked(device_busy)

    def release_lock_if_no_session_and_not_busy(self, device_busy: bool) -> None:
        with self._lock:
            self._release_lock_if_needed_locked(device_busy)

    def _release_lock_if_needed_locked(self, device_busy: bool) -> None:
        if device_busy or not self._lock_owner:
            return
        owner = self._lock_owner
        has_owner_session = any(s.username.lower() == owner.lower() for s in self._sessions.values())
        if not has_owner_session:
            self._lock_owner = ""
            self._save_state_locked()


@dataclass
class StoredPackage:
    id: str
    name: str
    uploadedBy: str
    uploadedAtUtc: str
    layerCount: int
    layerThicknessMm: float
    extractedDirectory: str
    manifestPath: str

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "uploadedBy": self.uploadedBy,
            "uploadedAtUtc": self.uploadedAtUtc,
            "layerCount": self.layerCount,
            "layerThicknessMm": self.layerThicknessMm,
        }


class PackageService:
    def __init__(self, data_root: Path):
        self._lock = threading.RLock()
        self._state_file = data_root / "packages_state.json"
        self._packages_dir = data_root / "packages"
        self._packages_dir.mkdir(parents=True, exist_ok=True)
        self._packages: list[StoredPackage] = []
        self._load_state()

    def _load_state(self) -> None:
        with self._lock:
            if not self._state_file.exists():
                return
            raw = json.loads(self._state_file.read_text(encoding="utf-8"))
            items = raw.get("packages", []) or []
            self._packages = [StoredPackage(**x) for x in items]

    def _save_state_locked(self) -> None:
        tmp = self._state_file.with_suffix(".tmp")
        payload = {"packages": [p.__dict__ for p in self._packages]}
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self._state_file)

    def list_packages(self) -> list[dict[str, Any]]:
        with self._lock:
            items = list(self._packages)
        items.sort(key=lambda x: x.uploadedAtUtc, reverse=True)
        return [p.summary() for p in items]

    def get_package(self, package_id: str) -> Optional[StoredPackage]:
        with self._lock:
            for p in self._packages:
                if p.id.lower() == (package_id or "").strip().lower():
                    return StoredPackage(**p.__dict__)
        return None

    def load_manifest(self, pkg: StoredPackage) -> dict[str, Any]:
        path = Path(pkg.manifestPath)
        return json.loads(path.read_text(encoding="utf-8"))

    def upload_package(self, file: UploadFile, uploaded_by: str) -> dict[str, Any]:
        filename = file.filename or ""
        if Path(filename).suffix.lower() != ".zip":
            return api_result(False, "BAD_EXTENSION", "Only .zip package is supported.")
        package_id = random_hex(16)
        package_root = self._packages_dir / package_id
        zip_path = package_root / "source.zip"
        extract_dir = package_root / "extract"
        extract_dir.mkdir(parents=True, exist_ok=True)
        try:
            with zip_path.open("wb") as f:
                shutil.copyfileobj(file.file, f)
            extract_zip_safe(zip_path, extract_dir)
            manifest_path = find_manifest_file(extract_dir)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            layer_count = int(manifest.get("layer_count") or 0)
            records = manifest.get("records", []) or []
            if layer_count <= 0:
                layer_count = len(records)
            stored = StoredPackage(
                id=package_id,
                name=filename,
                uploadedBy=uploaded_by,
                uploadedAtUtc=to_jsonable_datetime(now_utc()) or now_utc_str(),
                layerCount=layer_count,
                layerThicknessMm=float(manifest.get("layer_thickness_mm") or 0),
                extractedDirectory=str(extract_dir),
                manifestPath=str(manifest_path),
            )
            with self._lock:
                self._packages.append(stored)
                self._save_state_locked()
            return {**api_result(True, "OK", "Package uploaded."), "package": stored.summary()}
        except Exception as exc:
            safe_remove_dir(package_root)
            return api_result(False, "MANIFEST_INVALID", str(exc))


def extract_zip_safe(zip_path: Path, dst_root: Path) -> None:
    dst_abs = dst_root.resolve()
    with zipfile.ZipFile(zip_path, "r") as zf:
        for info in zf.infolist():
            clean_name = Path(info.filename)
            if ".." in clean_name.parts:
                raise ValueError(f"zip contains invalid path traversal entry: {info.filename}")
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
    target = "slice_magnetic_manifest.json"
    for path in root.rglob("*"):
        if path.is_file() and path.name.lower() == target.lower():
            return path
    raise FileNotFoundError("slice_magnetic_manifest.json not found in package")


def safe_remove_dir(path: Path) -> None:
    try:
        shutil.rmtree(path, ignore_errors=True)
    except Exception:
        pass


@dataclass
class LayerPlanItem:
    layerIndex: int = 0
    imagePath: str = ""
    layerThicknessMm: float = 0.0
    moveDirection: str = MOVE_DIR_DOWN
    directionBits: str = X_POSITIVE_BITS
    magneticVoltage: float = 0.0
    exposureIntensity: int = 0
    magneticHoldS: float = 0.0
    exposureS: float = 0.0


def manifest_sorted_record_indices(manifest: dict[str, Any]) -> list[int]:
    records = manifest.get("records", []) or []
    indices = list(range(len(records)))
    indices.sort(key=lambda i: (int(records[i].get("layer", 0)), i))
    return indices


def build_layer_plan(manifest: dict[str, Any], pkg: StoredPackage, overrides: dict[str, Any]) -> list[LayerPlanItem]:
    records = manifest.get("records", []) or []
    if not records:
        return []
    thickness = float(manifest.get("layer_thickness_mm") or 0)
    if overrides.get("layerThicknessMm") is not None:
        thickness = float(overrides["layerThicknessMm"])
    if thickness <= 0:
        raise ValueError("layer thickness must be > 0")
    ordered = manifest_sorted_record_indices(manifest)
    plan: list[LayerPlanItem] = []
    for idx in ordered:
        rec = records[idx]
        field = rec.get("field") or {}
        x = float(field.get("x") or 0)
        y = float(field.get("y") or 0)
        direction_bits = resolve_direction_bits(x, y)
        if overrides.get("magneticVoltage") is not None:
            mag_v = float(overrides["magneticVoltage"])
        else:
            mag_v = 0.0
            fs = field.get("strength")
            if fs is not None:
                mag_v = float(fs)
            elif rec.get("strength") is not None:
                mag_v = float(rec.get("strength"))
        intensity = int(rec.get("light_intensity") or 0)
        if overrides.get("exposureIntensity") is not None:
            intensity = int(overrides["exposureIntensity"])
        rel_file = str(rec.get("file") or "").strip()
        image_path = (Path(pkg.extractedDirectory) / Path(rel_file.replace("/", os.sep))).resolve()
        plan.append(
            LayerPlanItem(
                layerIndex=int(rec.get("layer", 0)) + 1,
                imagePath=str(image_path),
                layerThicknessMm=thickness,
                moveDirection=MOVE_DIR_DOWN,
                directionBits=direction_bits,
                magneticVoltage=mag_v,
                exposureIntensity=intensity,
                magneticHoldS=float(overrides.get("magneticHoldSeconds") or 0),
                exposureS=float(overrides.get("exposureSeconds") or 0),
            )
        )
    return plan


@dataclass
class RunContext:
    cancel_event: threading.Event
    deadline_ts: float | None = None

    def is_canceled(self) -> bool:
        if self.cancel_event.is_set():
            return True
        if self.deadline_ts is not None and time.time() > self.deadline_ts:
            self.cancel_event.set()
            return True
        return False


def sleep_ctx(ctx: RunContext, seconds: float) -> None:
    if seconds <= 0:
        return
    step = 0.05
    remaining = seconds
    while remaining > 0:
        if ctx.is_canceled():
            raise RuntimeError("context canceled")
        wait_for = step if remaining > step else remaining
        time.sleep(wait_for)
        remaining -= wait_for


class SysfsGPIOPin:
    def __init__(self, num: int):
        self.num = num
        self.export_num = num
        self.gpio_path = ""
        self.value_file: io.TextIOWrapper | None = None

    def prepare(self, direction: str) -> None:
        gpio_root = Path("/sys/class/gpio")
        gpio_path = gpio_root / f"gpio{self.export_num}"
        if not gpio_path.exists():
            try:
                (gpio_root / "export").write_text(str(self.export_num), encoding="utf-8")
            except OSError as exc:
                # EBUSY: already exported by another process.
                if exc.errno == errno.EBUSY:
                    pass
                # EINVAL on newer Pi kernels is often caused by using BCM number
                # while sysfs expects global GPIO number. Try auto remap.
                elif exc.errno == errno.EINVAL:
                    mapped = resolve_sysfs_gpio_export_number(gpio_root, self.num)
                    self.export_num = mapped
                    gpio_path = gpio_root / f"gpio{self.export_num}"
                    if not gpio_path.exists():
                        try:
                            (gpio_root / "export").write_text(str(self.export_num), encoding="utf-8")
                        except OSError as exc2:
                            if exc2.errno != errno.EBUSY:
                                raise RuntimeError(
                                    f"export gpio{self.export_num} (mapped from bcm{self.num}) failed: {exc2}"
                                ) from exc2
                else:
                    raise
            deadline = time.time() + 2.0
            while not gpio_path.exists():
                if time.time() > deadline:
                    raise RuntimeError(f"gpio{self.export_num} path did not appear")
                time.sleep(0.01)
        (gpio_path / "direction").write_text(direction, encoding="utf-8")
        self.gpio_path = str(gpio_path)
        self.value_file = open(gpio_path / "value", "r+", encoding="utf-8")

    def write(self, level: int) -> None:
        if not self.value_file:
            raise RuntimeError("gpio not prepared")
        self.value_file.seek(0)
        self.value_file.write("1" if level else "0")
        self.value_file.flush()

    def read(self) -> int:
        if not self.value_file:
            raise RuntimeError("gpio not prepared")
        self.value_file.seek(0)
        c = self.value_file.read(1)
        return 1 if c == "1" else 0

    def close(self) -> None:
        if self.value_file:
            self.value_file.close()
            self.value_file = None


def resolve_sysfs_gpio_export_number(gpio_root: Path, bcm: int) -> int:
    chips = sorted(gpio_root.glob("gpiochip*"))
    if not chips:
        raise RuntimeError("no gpiochip found in /sys/class/gpio")
    candidates: list[tuple[int, int]] = []
    for chip in chips:
        try:
            base = int((chip / "base").read_text(encoding="utf-8").strip())
            ngpio = int((chip / "ngpio").read_text(encoding="utf-8").strip())
        except Exception:
            continue
        if ngpio <= 0:
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
        if serial is None:
            raise RuntimeError("pyserial is required for native exposure")
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
        if Image is None:
            raise RuntimeError("Pillow is required for native exposure")
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
        resized = src.resize((tw, th), Image.NEAREST)
        canvas = Image.new("RGB", (self.width, self.height), "black")
        ox = (self.width - tw) // 2
        oy = (self.height - th) // 2
        canvas.paste(resized, (ox, oy))
        raw = self._to_framebuffer_bytes(canvas)
        self.file.seek(0)
        self.file.write(raw)

    def _to_framebuffer_bytes(self, img: Any) -> bytes:
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


class NativeHardwareBackend:
    def __init__(self, cfg: HardwareConfig, logger: logging.Logger):
        self.cfg = cfg
        self.logger = logger
        self.prepared = False
        self.i2c: I2CNative | None = None
        self.enable_pin: SysfsGPIOPin | None = None
        self.step_pin: SysfsGPIOPin | None = None
        self.dir_pin: SysfsGPIOPin | None = None
        self.move_enable_pin: SysfsGPIOPin | None = None
        self.home_top_pin: SysfsGPIOPin | None = None
        self.serial: SerialNative | None = None
        self.fb: FramebufferNative | None = None

    def prepare(self, ctx: RunContext) -> None:
        if self.prepared:
            return
        if os.name == "nt":
            raise RuntimeError("native hardware backend is linux only")
        self.enable_pin = SysfsGPIOPin(self.cfg.magnet.enableGpioPin)
        self.enable_pin.prepare("out")
        self.enable_pin.write(1)
        self.i2c = I2CNative(self.cfg.magnet.i2cBus)

        self.step_pin = SysfsGPIOPin(self.cfg.motion.stepPin)
        self.dir_pin = SysfsGPIOPin(self.cfg.motion.dirPin)
        self.move_enable_pin = SysfsGPIOPin(self.cfg.motion.enablePin)
        self.step_pin.prepare("out")
        self.dir_pin.prepare("out")
        self.move_enable_pin.prepare("out")
        self._set_motion_enable(False)
        self.step_pin.write(0)
        if self.cfg.motion.homeTopPin > 0:
            self.home_top_pin = SysfsGPIOPin(self.cfg.motion.homeTopPin)
            self.home_top_pin.prepare("in")

        self.serial = SerialNative(
            self.cfg.exposure.serialPort,
            self.cfg.exposure.baudRate,
            self.cfg.exposure.readTimeoutMs,
        )
        self.fb = FramebufferNative(self.cfg.exposure.framebufferDevice)
        self._send_dlp_command(DLP_CMD_HANDSHAKE, "handshake")
        self._send_dlp_command(DLP_CMD_ON, "dlp_on")
        self._send_dlp_command(DLP_CMD_LED_OFF, "led_off")
        self.prepared = True

    def move_layer(self, ctx: RunContext, layer: LayerPlanItem) -> None:
        move_direction = normalize_move_direction(layer.moveDirection)
        steps = self._steps_from_mm(layer.layerThicknessMm)
        if steps <= 0:
            return
        if self.dir_pin is None or self.step_pin is None:
            raise RuntimeError("motion module not prepared")
        self.dir_pin.write(1 if move_direction == MOVE_DIR_UP else 0)
        self._set_motion_enable(True)
        try:
            self._move_exact(ctx, steps, self._move_frequency_hz(), self.cfg.motion.pulseWidthUs)
        finally:
            self._set_motion_enable(False)

    def home(self, ctx: RunContext) -> None:
        if self.home_top_pin is None:
            raise RuntimeError("homeTopPin is not configured")
        stop = self.cfg.motion.homeStopLevel
        if self.home_top_pin.read() == stop:
            return
        direction = normalize_move_direction(self.cfg.motion.homeDirection)
        if self.dir_pin is None:
            raise RuntimeError("motion module not prepared")
        self.dir_pin.write(1 if direction == MOVE_DIR_UP else 0)
        self._set_motion_enable(True)
        total = 0
        chunk = self.cfg.motion.homeChunkSteps if self.cfg.motion.homeChunkSteps > 0 else 200
        report_every = self.cfg.motion.homeReportEvery if self.cfg.motion.homeReportEvery > 0 else 1000
        try:
            while True:
                if self.cfg.motion.homeMaxSteps > 0 and total >= self.cfg.motion.homeMaxSteps:
                    raise RuntimeError(f"home reached max steps: {total}")
                run_count = chunk
                if self.cfg.motion.homeMaxSteps > 0 and total + run_count > self.cfg.motion.homeMaxSteps:
                    run_count = self.cfg.motion.homeMaxSteps - total
                if run_count <= 0:
                    raise RuntimeError(f"home reached max steps: {total}")
                self._move_exact(ctx, run_count, self._home_frequency_hz(), self.cfg.motion.pulseWidthUs)
                total += run_count
                if self.home_top_pin.read() == stop:
                    return
                if total % report_every == 0:
                    self.logger.info("home progress: steps=%d", total)
        finally:
            self._set_motion_enable(False)

    def apply_magnetic_field(self, ctx: RunContext, layer: LayerPlanItem) -> None:
        if not self.i2c or not self.enable_pin:
            raise RuntimeError("magnet module not prepared")
        bits = normalize_direction_bits(layer.directionBits)
        validate_direction_bits(bits)
        out = bits_to_output_byte(bits)
        self.enable_pin.write(0)
        try:
            self._select_tca()
            self._write_pca9554(out)
            self._write_dac(layer.magneticVoltage)
            if layer.magneticHoldS > 0:
                sleep_ctx(ctx, layer.magneticHoldS)
        finally:
            try:
                self._write_dac(0)
            except Exception:
                pass
            try:
                self._disable_tca()
            except Exception:
                pass
            self.enable_pin.write(1)

    def expose_layer(self, ctx: RunContext, layer: LayerPlanItem) -> None:
        if not self.serial or not self.fb:
            raise RuntimeError("exposure module not prepared")
        if not (layer.imagePath or "").strip():
            raise RuntimeError("image path is required")
        self.fb.render_image(layer.imagePath)
        if self.cfg.exposure.framebufferSettleMs > 0:
            sleep_ctx(ctx, self.cfg.exposure.framebufferSettleMs / 1000.0)
        self._set_brightness(layer.exposureIntensity)
        self._send_dlp_command(DLP_CMD_LED_ON, "led_on")
        try:
            if layer.exposureS > 0:
                sleep_ctx(ctx, layer.exposureS)
        finally:
            self._send_dlp_command(DLP_CMD_LED_OFF, "led_off")

    def finish(self, ctx: RunContext) -> None:
        if not self.prepared:
            return
        try:
            self._send_dlp_command(DLP_CMD_LED_OFF, "led_off")
            self._send_dlp_command(DLP_CMD_OFF, "dlp_off")
        except Exception:
            pass
        for pin in [self.step_pin, self.dir_pin, self.move_enable_pin, self.home_top_pin, self.enable_pin]:
            if pin:
                try:
                    pin.close()
                except Exception:
                    pass
        if self.serial:
            try:
                self.serial.close()
            except Exception:
                pass
        if self.fb:
            try:
                self.fb.close()
            except Exception:
                pass
        if self.i2c:
            try:
                self.i2c.close()
            except Exception:
                pass
        self.prepared = False

    def _set_motion_enable(self, enable: bool) -> None:
        if not self.move_enable_pin:
            raise RuntimeError("motion module not prepared")
        level_on = 0 if self.cfg.motion.enableLow else 1
        level_off = 1 - level_on
        self.move_enable_pin.write(level_on if enable else level_off)

    def _move_exact(self, ctx: RunContext, steps: int, freq_hz: int, pulse_width_us: int) -> None:
        if self.step_pin is None:
            raise RuntimeError("motion module not prepared")
        if steps <= 0:
            return
        if freq_hz <= 0:
            raise RuntimeError("frequency must be > 0")
        period = 1.0 / float(freq_hz)
        high_time = pulse_width_us / 1_000_000.0
        if high_time >= period:
            raise RuntimeError("pulseWidthUs must be smaller than pulse period")
        low_time = period - high_time
        for _ in range(steps):
            if ctx.is_canceled():
                raise RuntimeError("context canceled")
            self.step_pin.write(1)
            time.sleep(high_time)
            self.step_pin.write(0)
            time.sleep(low_time)

    def _steps_from_mm(self, mm: float) -> int:
        if mm <= 0 or self.cfg.motion.leadMm <= 0 or self.cfg.motion.stepsPerRev <= 0:
            return 0
        return max(0, int(round((mm * float(self.cfg.motion.stepsPerRev)) / self.cfg.motion.leadMm)))

    def _move_frequency_hz(self) -> int:
        f = self.cfg.motion.moveFrequencyHz or self.cfg.motion.frequencyHz or 800
        return max(1, int(f))

    def _home_frequency_hz(self) -> int:
        f = self.cfg.motion.homeFrequencyHz or self.cfg.motion.frequencyHz or 1600
        return max(1, int(f))

    def _select_tca(self) -> None:
        if not self.i2c:
            raise RuntimeError("i2c not prepared")
        mask = 0
        if 0 <= self.cfg.magnet.tcaChannel <= 7:
            mask = 1 << self.cfg.magnet.tcaChannel
        self.i2c.write(self.cfg.magnet.tcaAddress, bytes([mask]))

    def _disable_tca(self) -> None:
        if not self.i2c:
            return
        self.i2c.write(self.cfg.magnet.tcaAddress, b"\x00")

    def _write_pca9554(self, out: int) -> None:
        if not self.i2c:
            raise RuntimeError("i2c not prepared")
        self.i2c.write(self.cfg.magnet.pca9554Address, bytes([0x03, 0x00]))
        self.i2c.write(self.cfg.magnet.pca9554Address, bytes([0x01, out & 0xFF]))
        readback = self.i2c.read_reg(self.cfg.magnet.pca9554Address, 0x01)
        if readback != (out & 0xFF):
            raise RuntimeError(f"pca9554 readback mismatch: want=0x{out:02X} got=0x{readback:02X}")

    def _write_dac(self, voltage: float) -> None:
        if not self.i2c:
            raise RuntimeError("i2c not prepared")
        vref = float(self.cfg.magnet.dacVRef or 0)
        if vref <= 0:
            raise RuntimeError("invalid dac vref")
        v = min(max(voltage, 0.0), vref)
        code = int(round((v / vref) * 4095.0))
        code = min(max(code, 0), 4095)
        high = (code >> 4) & 0xFF
        low = (code & 0x0F) << 4
        self.i2c.write(self.cfg.magnet.mcp4725Address, bytes([0x40, high, low]))

    def _set_brightness(self, value: int) -> None:
        v = min(max(int(value), 0), 255)
        self._send_dlp_command(bytes([0xA6, 0x02, 0x10, v]), f"brightness_{v}")

    def _send_dlp_command(self, cmd: bytes, desc: str) -> None:
        if not self.serial:
            raise RuntimeError("serial not prepared")
        resp_len = self.cfg.exposure.responseReadBytes if self.cfg.exposure.responseReadBytes > 0 else 10
        resp = self.serial.send(cmd, resp_len)
        if len(resp) == 0:
            raise RuntimeError(f"{desc} timeout")
        if resp[-1] == 0xE0:
            raise RuntimeError(f"{desc} rejected by dlp, response={resp.hex(' ')}")


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


class HardwareController:
    def __init__(self, cfg: HardwareConfig, logger: logging.Logger):
        self.cfg = cfg
        self.logger = logger
        self.backend: NativeHardwareBackend | None = None
        self.lock = threading.RLock()

    def is_mock(self) -> bool:
        return self.cfg.useMockHardware or os.name == "nt"

    def prepare(self, ctx: RunContext) -> None:
        if self.is_mock():
            self.logger.info("mock hardware prepare")
            return
        backend = self._ensure_backend()
        backend.prepare(ctx)

    def move_layer(self, ctx: RunContext, layer: LayerPlanItem) -> None:
        direction = normalize_move_direction(layer.moveDirection)
        if self.is_mock():
            self.logger.info("mock move layer=%d direction=%s thickness_mm=%.4f", layer.layerIndex, direction, layer.layerThicknessMm)
            return
        layer.moveDirection = direction
        self._ensure_backend().move_layer(ctx, layer)

    def home(self, ctx: RunContext) -> None:
        if self.is_mock():
            self.logger.info("mock home")
            return
        self._ensure_backend().home(ctx)

    def apply_magnetic_field(self, ctx: RunContext, layer: LayerPlanItem) -> None:
        if self.is_mock():
            self.logger.info(
                "mock magnet layer=%d bits=%s voltage=%.3f hold=%.3fs",
                layer.layerIndex,
                layer.directionBits,
                layer.magneticVoltage,
                layer.magneticHoldS,
            )
            if not self.cfg.skipWaitInMock and layer.magneticHoldS > 0:
                sleep_ctx(ctx, layer.magneticHoldS)
            return
        self._ensure_backend().apply_magnetic_field(ctx, layer)

    def expose_layer(self, ctx: RunContext, layer: LayerPlanItem) -> None:
        if self.is_mock():
            self.logger.info(
                "mock exposure layer=%d intensity=%d exposure=%.3fs image=%s",
                layer.layerIndex,
                layer.exposureIntensity,
                layer.exposureS,
                layer.imagePath,
            )
            if not self.cfg.skipWaitInMock and layer.exposureS > 0:
                sleep_ctx(ctx, layer.exposureS)
            return
        self._ensure_backend().expose_layer(ctx, layer)

    def finish(self, ctx: RunContext) -> None:
        if self.is_mock():
            self.logger.info("mock hardware finish")
            return
        self._ensure_backend().finish(ctx)

    def _ensure_backend(self) -> NativeHardwareBackend:
        with self.lock:
            if self.backend is None:
                self.backend = NativeHardwareBackend(self.cfg, self.logger)
            return self.backend


@dataclass
class JobRuntime:
    jobId: str = ""
    owner: str = ""
    packageId: str = ""
    startedAtUtc: str | None = None
    finishedAtUtc: str | None = None
    state: str = STATE_IDLE
    message: str = "idle"
    totalLayers: int = 0
    completedLayers: int = 0
    currentLayer: int = 0
    recentEvents: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


class PrintService:
    def __init__(self, auth: AuthService, pkg: PackageService, cfg: AppConfig, logger: logging.Logger):
        self.lock = threading.RLock()
        self.auth = auth
        self.pkg = pkg
        self.cfg = cfg
        self.logger = logger
        self.running = False
        self.cancel_event: threading.Event | None = None
        self.job = JobRuntime()

    def is_busy(self) -> bool:
        with self.lock:
            return self.running

    def _append_event_locked(self, evt: str) -> None:
        line = f"{now_utc_str()} {evt}"
        self.job.recentEvents.append(line)
        if len(self.job.recentEvents) > 40:
            self.job.recentEvents = self.job.recentEvents[-40:]

    def get_status_for_user(self, username: str) -> dict[str, Any]:
        with self.lock:
            job = self.job.to_dict()
            busy = self.running
        return {
            "isBusy": busy,
            "lockOwner": self.auth.lock_owner(),
            "canControlDevice": self.auth.is_lock_owner(username),
            "requestUser": username,
            "job": job,
        }

    def start(self, user: str, req: dict[str, Any]) -> tuple[dict[str, Any], int]:
        if not self.auth.is_lock_owner(user):
            return api_result(False, "DEVICE_LOCKED", "Only lock owner can start print."), 423
        package_id = (req.get("packageId") or "").strip()
        if not package_id:
            return api_result(False, "BAD_PACKAGE", "PackageId is required."), 400
        overrides = req.get("overrides") or {}
        if float(overrides.get("magneticHoldSeconds") or 0) < 0 or float(overrides.get("exposureSeconds") or 0) < 0:
            return api_result(False, "BAD_OVERRIDE", "Time values must be >= 0."), 400
        if overrides.get("exposureIntensity") is not None:
            iv = int(overrides["exposureIntensity"])
            if iv < 0 or iv > 255:
                return api_result(False, "BAD_OVERRIDE", "Exposure intensity must be 0..255."), 400
        with self.lock:
            if self.running:
                status = self.get_status_for_user(user)
                return {**api_result(False, "BUSY", "Device is busy."), "status": status}, 409
        pkg = self.pkg.get_package(package_id)
        if not pkg:
            return api_result(False, "PACKAGE_NOT_FOUND", "Package not found."), 400
        try:
            manifest = self.pkg.load_manifest(pkg)
            plan = build_layer_plan(manifest, pkg, overrides)
        except Exception as exc:
            return api_result(False, "PLAN_BUILD_FAILED", str(exc)), 400
        if not plan:
            return api_result(False, "EMPTY_PLAN", "No layers found in manifest."), 400

        cancel_event = threading.Event()
        job_id = random_hex(16)
        now = to_jsonable_datetime(now_utc())
        job = JobRuntime(
            jobId=job_id,
            owner=user,
            packageId=pkg.id,
            startedAtUtc=now,
            state=STATE_RUNNING,
            message="print_started",
            totalLayers=len(plan),
            completedLayers=0,
            currentLayer=0,
            recentEvents=[f"{now_utc_str()} print started"],
        )
        with self.lock:
            self.running = True
            self.cancel_event = cancel_event
            self.job = job
        t = threading.Thread(target=self._run_plan, args=(job_id, plan, cancel_event), daemon=True)
        t.start()
        status = self.get_status_for_user(user)
        return {**api_result(True, "OK", "Print started."), "status": status}, 200

    def cancel(self, user: str) -> tuple[dict[str, Any], int]:
        if not self.auth.is_lock_owner(user):
            return api_result(False, "DEVICE_LOCKED", "Only lock owner can cancel print."), 423
        with self.lock:
            if not self.running:
                status = self.get_status_for_user(user)
                return {**api_result(False, "NOT_RUNNING", "No running job."), "status": status}, 409
            self.job.state = STATE_CANCELING
            self.job.message = "cancel_requested"
            self._append_event_locked("cancel requested")
            if self.cancel_event:
                self.cancel_event.set()
        status = self.get_status_for_user(user)
        return {**api_result(True, "OK", "Cancel requested."), "status": status}, 200

    def _run_plan(self, job_id: str, plan: list[LayerPlanItem], cancel_event: threading.Event) -> None:
        hw = HardwareController(self.cfg.hardware, self.logger)
        ctx = RunContext(cancel_event=cancel_event)
        try:
            hw.prepare(ctx)
            for i, layer in enumerate(plan):
                if cancel_event.is_set():
                    self._cancel_job(job_id)
                    return
                self._update_current_layer(job_id, i + 1)
                hw.move_layer(ctx, layer)
                hw.apply_magnetic_field(ctx, layer)
                hw.expose_layer(ctx, layer)
                self._complete_layer(job_id, i + 1)
            with self.lock:
                if self.job.jobId == job_id:
                    self.job.state = STATE_COMPLETED
                    self.job.message = "print_completed"
                    self.job.finishedAtUtc = to_jsonable_datetime(now_utc())
                    self._append_event_locked("print completed")
        except Exception as exc:
            if cancel_event.is_set():
                self._cancel_job(job_id)
            else:
                self._fail_job(job_id, exc)
        finally:
            try:
                hw.finish(RunContext(cancel_event=threading.Event(), deadline_ts=time.time() + 3))
            finally:
                with self.lock:
                    self.running = False
                    self.cancel_event = None
                self.auth.release_lock_if_no_session_and_not_busy(False)

    def _update_current_layer(self, job_id: str, layer: int) -> None:
        with self.lock:
            if self.job.jobId != job_id:
                return
            self.job.currentLayer = layer
            self.job.message = f"running_layer_{layer}"
            self._append_event_locked(f"running layer {layer}/{self.job.totalLayers}")

    def _complete_layer(self, job_id: str, layer: int) -> None:
        with self.lock:
            if self.job.jobId != job_id:
                return
            self.job.completedLayers = layer
            self.job.message = f"layer_done_{layer}"
            self._append_event_locked(f"layer done {layer}/{self.job.totalLayers}")

    def _fail_job(self, job_id: str, exc: Exception) -> None:
        self.logger.error("print job failed: %s", exc)
        with self.lock:
            if self.job.jobId != job_id:
                return
            self.job.state = STATE_FAILED
            self.job.message = f"print_failed: {exc}"
            self.job.finishedAtUtc = to_jsonable_datetime(now_utc())
            self._append_event_locked(f"print failed: {exc}")

    def _cancel_job(self, job_id: str) -> None:
        with self.lock:
            if self.job.jobId != job_id:
                return
            self.job.state = STATE_CANCELED
            self.job.message = "print_canceled"
            self.job.finishedAtUtc = to_jsonable_datetime(now_utc())
            self._append_event_locked("print canceled")


class FlowConfigService:
    def __init__(self, data_root: Path):
        self.lock = threading.RLock()
        self.key_file = data_root / "flow_config.key"
        self.key: bytes | None = None

    def _load_or_create_key_locked(self) -> bytes:
        if self.key and len(self.key) == 32:
            return self.key
        if self.key_file.exists():
            raw = self.key_file.read_bytes()
            if len(raw) != 32:
                raise RuntimeError("invalid flow config key length")
            self.key = raw
            return raw
        key = os.urandom(32)
        tmp = self.key_file.with_suffix(".tmp")
        tmp.write_bytes(key)
        os.chmod(tmp, 0o600)
        tmp.replace(self.key_file)
        self.key = key
        return key

    def encrypt_config(self, req: dict[str, Any]) -> bytes:
        steps = req.get("steps", []) or []
        if not steps:
            raise RuntimeError("program has no steps")
        cfg = {
            "version": FLOW_CONFIG_VERSION,
            "createdAtUtc": to_jsonable_datetime(now_utc()),
            "name": (req.get("name") or "").strip(),
            "steps": steps,
        }
        plain = json.dumps(cfg, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(plain) > FLOW_CONFIG_MAX_BYTES:
            raise RuntimeError("config payload too large")
        with self.lock:
            key = self._load_or_create_key_locked()
        aesgcm = AESGCM(key)
        nonce = os.urandom(12)
        encrypted = aesgcm.encrypt(nonce, plain, FLOW_CONFIG_MAGIC)
        return FLOW_CONFIG_MAGIC + nonce + encrypted

    def decrypt_config(self, payload: bytes) -> dict[str, Any]:
        if len(payload) < len(FLOW_CONFIG_MAGIC):
            raise RuntimeError("invalid config payload")
        if payload[: len(FLOW_CONFIG_MAGIC)] != FLOW_CONFIG_MAGIC:
            raise RuntimeError("invalid config header")
        with self.lock:
            key = self._load_or_create_key_locked()
        aesgcm = AESGCM(key)
        offset = len(FLOW_CONFIG_MAGIC)
        nonce = payload[offset : offset + 12]
        cipher_data = payload[offset + 12 :]
        try:
            plain = aesgcm.decrypt(nonce, cipher_data, FLOW_CONFIG_MAGIC)
        except Exception as exc:
            raise RuntimeError("decrypt config failed") from exc
        if len(plain) > FLOW_CONFIG_MAX_BYTES:
            raise RuntimeError("config payload too large")
        cfg = json.loads(plain.decode("utf-8"))
        if int(cfg.get("version", 0)) != FLOW_CONFIG_VERSION:
            raise RuntimeError("unsupported config version")
        return {
            "name": (cfg.get("name") or "").strip(),
            "steps": cfg.get("steps", []) or [],
        }


@dataclass
class FlowSliceCursor:
    pkg: StoredPackage
    manifest: dict[str, Any]
    ordered_indices: list[int]
    next_idx: int = 0


class FlowRunContext:
    def __init__(self, pkg_svc: PackageService):
        self.pkg_svc = pkg_svc
        self.cursors: dict[str, FlowSliceCursor] = {}

    def get_or_load_cursor(self, package_id: str) -> FlowSliceCursor:
        pid = (package_id or "").strip()
        if not pid:
            raise RuntimeError("slicePackageId is required for slice exposure")
        if pid in self.cursors:
            return self.cursors[pid]
        pkg = self.pkg_svc.get_package(pid)
        if not pkg:
            raise RuntimeError(f"slice package not found: {pid}")
        manifest = self.pkg_svc.load_manifest(pkg)
        ordered = manifest_sorted_record_indices(manifest)
        if not ordered:
            raise RuntimeError(f"slice package has no records: {pid}")
        cursor = FlowSliceCursor(pkg=pkg, manifest=manifest, ordered_indices=ordered, next_idx=0)
        self.cursors[pid] = cursor
        return cursor

    def resolve_slice_record(self, package_id: str, advance: bool) -> tuple[dict[str, Any], str]:
        cursor = self.get_or_load_cursor(package_id)
        if cursor.next_idx >= len(cursor.ordered_indices):
            raise RuntimeError(
                f"slice records exhausted in package {(package_id or '').strip()} (total={len(cursor.ordered_indices)})"
            )
        rec = cursor.manifest.get("records", [])[cursor.ordered_indices[cursor.next_idx]]
        if advance:
            cursor.next_idx += 1
        file_rel = (rec.get("file") or "").strip()
        if not file_rel:
            raise RuntimeError("slice record image file is empty")
        image_path = (Path(cursor.pkg.extractedDirectory) / Path(file_rel.replace("/", os.sep))).resolve()
        return rec, str(image_path)


class AsyncMagnetRunner:
    def __init__(
        self,
        ctx: RunContext,
        hw: HardwareController,
        append_event: Callable[[str], None],
        update_pending: Callable[[int], None],
    ):
        self.ctx = ctx
        self.hw = hw
        self.append_event = append_event
        self.update_pending = update_pending
        self.lock = threading.RLock()
        self.pending = 0
        self.magnet_running = False
        self.first_err: Exception | None = None
        self.idle_event = threading.Event()
        self.idle_event.set()

    def first_error(self) -> Exception | None:
        with self.lock:
            return self.first_err

    def start_magnet(self, layer: LayerPlanItem) -> None:
        with self.lock:
            if self.first_err:
                raise self.first_err
            if self.magnet_running:
                raise RuntimeError("previous async magnet step is still running")
            if self.pending == 0:
                self.idle_event.clear()
            self.pending += 1
            pending = self.pending
            self.magnet_running = True
        self.update_pending(pending)
        self.append_event(f"async magnet started (pending={pending})")

        def run() -> None:
            err: Exception | None = None
            try:
                self.hw.apply_magnetic_field(self.ctx, layer)
            except Exception as exc:  # pragma: no cover
                err = exc
            with self.lock:
                if err and self.first_err is None:
                    self.first_err = err
                self.magnet_running = False
                if self.pending > 0:
                    self.pending -= 1
                pending_now = self.pending
                if pending_now == 0:
                    self.idle_event.set()
            self.update_pending(pending_now)
            if err:
                self.append_event(f"async magnet failed: {err}")
            else:
                self.append_event(f"async magnet completed (pending={pending_now})")

        threading.Thread(target=run, daemon=True).start()

    def wait_all_idle(self, ctx: RunContext) -> None:
        while True:
            with self.lock:
                pending = self.pending
                first_err = self.first_err
            if pending == 0:
                if first_err:
                    raise first_err
                return
            if ctx.is_canceled():
                raise RuntimeError("context canceled")
            self.idle_event.wait(timeout=0.1)


def resolve_slice_strength(rec: dict[str, Any]) -> float:
    field = rec.get("field") or {}
    if field.get("strength") is not None:
        return float(field.get("strength"))
    if rec.get("strength") is not None:
        return float(rec.get("strength"))
    return 0.0


class AdminService:
    def __init__(self, cfg: AppConfig, print_svc: PrintService, pkg_svc: PackageService, logger: logging.Logger):
        self.cfg = cfg
        self.print_svc = print_svc
        self.pkg_svc = pkg_svc
        self.logger = logger
        self.lock = threading.RLock()
        self.running = False
        self.cancel_event: threading.Event | None = None
        self.status: dict[str, Any] = {
            "running": False,
            "lastResult": "idle",
            "recentEvents": [],
            "pendingAsyncOps": 0,
        }

    def _append_event_locked(self, evt: str) -> None:
        line = f"{now_utc_str()} {evt}"
        self.status["recentEvents"] = (self.status.get("recentEvents", []) + [line])[-120:]

    def get_status(self) -> dict[str, Any]:
        with self.lock:
            out = dict(self.status)
            out["recentEvents"] = list(self.status.get("recentEvents", []))
            return out

    def cancel_program(self) -> tuple[dict[str, Any], int]:
        with self.lock:
            if not self.running:
                return {**api_result(False, "NOT_RUNNING", "No admin program is running."), "status": self.get_status()}, 409
            if self.cancel_event:
                self.cancel_event.set()
            self.status["lastResult"] = "cancel_requested"
            self._append_event_locked("cancel requested")
            return {**api_result(True, "OK", "Cancel requested."), "status": self.get_status()}, 200

    def run_program(self, req: dict[str, Any]) -> tuple[dict[str, Any], int]:
        if self.print_svc.is_busy():
            return {**api_result(False, "PRINT_BUSY", "Cannot run admin program while print is running."), "status": self.get_status()}, 409
        steps = req.get("steps", []) or []
        if not steps:
            return {**api_result(False, "EMPTY_PROGRAM", "Program has no steps."), "status": self.get_status()}, 400
        name = (req.get("name") or "").strip() or "flow-program"
        with self.lock:
            if self.running:
                return {
                    **api_result(False, "ADMIN_BUSY", "Another admin program is running."),
                    "status": self.get_status(),
                }, 409
            self.running = True
            self.cancel_event = threading.Event()
            self.status.update(
                {
                    "running": True,
                    "name": name,
                    "startedAtUtc": to_jsonable_datetime(now_utc()),
                    "finishedAtUtc": None,
                    "currentStep": "",
                    "pendingAsyncOps": 0,
                    "lastError": "",
                    "lastResult": "program_started",
                }
            )
            self._append_event_locked(f"program started: {name}")
            cancel_event = self.cancel_event
        threading.Thread(
            target=self._run_program_loop,
            args=(name, steps, cancel_event),
            daemon=True,
        ).start()
        return {**api_result(True, "OK", "Program started."), "status": self.get_status()}, 200

    def _set_done(self, last_result: str, last_error: str) -> None:
        with self.lock:
            self.running = False
            self.cancel_event = None
            self.status["running"] = False
            self.status["finishedAtUtc"] = to_jsonable_datetime(now_utc())
            self.status["currentStep"] = ""
            self.status["pendingAsyncOps"] = 0
            self.status["lastResult"] = last_result
            self.status["lastError"] = last_error
            if last_error:
                self._append_event_locked(f"program failed: {last_error}")
            else:
                self._append_event_locked("program completed")

    def _run_program_loop(self, name: str, steps: list[dict[str, Any]], cancel_event: threading.Event | None) -> None:
        if cancel_event is None:
            return
        ctx = RunContext(cancel_event=cancel_event)
        hw = HardwareController(self.cfg.hardware, self.logger)

        def append_evt(evt: str) -> None:
            with self.lock:
                self._append_event_locked(evt)

        def update_pending(count: int) -> None:
            with self.lock:
                self.status["pendingAsyncOps"] = count

        async_runner = AsyncMagnetRunner(ctx, hw, append_evt, update_pending)
        flow_ctx = FlowRunContext(self.pkg_svc)
        try:
            hw.prepare(ctx)
            self._execute_steps(ctx, hw, async_runner, steps, 0, name, flow_ctx)
            async_runner.wait_all_idle(ctx)
            self._set_done("completed", "")
        except Exception as exc:
            if cancel_event.is_set() or str(exc) == "context canceled":
                self._set_done("canceled", "")
            else:
                self._set_done("failed", str(exc))
        finally:
            try:
                hw.finish(RunContext(cancel_event=threading.Event(), deadline_ts=time.time() + 3))
            except Exception:
                pass

    def _execute_steps(
        self,
        ctx: RunContext,
        hw: HardwareController,
        async_runner: AsyncMagnetRunner,
        steps: list[dict[str, Any]],
        depth: int,
        prefix: str,
        flow_ctx: FlowRunContext,
    ) -> None:
        if depth > 8:
            raise RuntimeError("loop depth exceeds limit")
        for idx, step in enumerate(steps):
            if ctx.is_canceled():
                raise RuntimeError("context canceled")
            async_err = async_runner.first_error()
            if async_err:
                raise RuntimeError(f"async task failed: {async_err}")
            step_type = (step.get("type") or "").strip().lower()
            step_name = f"{prefix}/{idx + 1}:{step_type}"
            with self.lock:
                self.status["currentStep"] = step_name
                self._append_event_locked(f"running {step_name}")
            if step_type == "loop":
                repeat = int(step.get("repeat") or 1)
                if repeat <= 0:
                    repeat = 1
                children = step.get("children", []) or []
                if not children:
                    raise RuntimeError(f"loop step has no children: {step_name}")
                for i in range(repeat):
                    if ctx.is_canceled():
                        raise RuntimeError("context canceled")
                    with self.lock:
                        self._append_event_locked(f"loop {step_name} ({i + 1}/{repeat})")
                    self._execute_steps(ctx, hw, async_runner, children, depth + 1, f"{step_name}.loop{i + 1}", flow_ctx)
                continue
            params = step.get("params") or {}
            self._execute_flow_step(ctx, hw, async_runner, flow_ctx, step_type, params)

    def _execute_flow_step(
        self,
        ctx: RunContext,
        hw: HardwareController,
        async_runner: AsyncMagnetRunner,
        flow_ctx: FlowRunContext,
        step_type: str,
        p: dict[str, Any],
    ) -> None:
        if step_type == "magnet":
            layer = self._build_magnet_layer_for_step(flow_ctx, p)
            hw.apply_magnetic_field(ctx, layer)
            return
        if step_type == "magnet_async":
            layer = self._build_magnet_layer_for_step(flow_ctx, p)
            hold = float(p.get("holdSeconds") or 0)
            if hold < 0:
                raise RuntimeError("holdSeconds must be >= 0")
            async_runner.start_magnet(layer)
            return
        if step_type == "exposure":
            self._execute_flow_exposure(ctx, hw, flow_ctx, p)
            return
        if step_type in {"move", "move_up", "move_down"}:
            layer_th = float(p.get("layerThicknessMm") or 0)
            if layer_th <= 0:
                raise RuntimeError("layerThicknessMm must be > 0")
            if step_type == "move":
                move_dir = normalize_move_direction(str(p.get("moveDirection") or ""))
            elif step_type == "move_up":
                move_dir = MOVE_DIR_UP
            else:
                move_dir = MOVE_DIR_DOWN
            hw.move_layer(ctx, LayerPlanItem(layerThicknessMm=layer_th, moveDirection=move_dir))
            return
        if step_type == "home":
            hw.home(ctx)
            return
        if step_type == "wait_all_idle":
            async_runner.wait_all_idle(ctx)
            return
        if step_type == "wait":
            wait_s = float(p.get("waitSeconds") or 0)
            if wait_s < 0:
                raise RuntimeError("waitSeconds must be >= 0")
            sleep_ctx(ctx, wait_s)
            return
        raise RuntimeError(f"unsupported step type: {step_type}")

    def _build_magnet_layer_for_step(self, flow_ctx: FlowRunContext, p: dict[str, Any]) -> LayerPlanItem:
        source = (p.get("magnetSource") or "").strip().lower()
        use_slice_direction = bool(p.get("useSliceDirection"))
        use_slice_strength = bool(p.get("useSliceStrength"))
        slice_package_id = (p.get("slicePackageId") or "").strip()
        if not source:
            source = "manual"
            if slice_package_id and (use_slice_direction or use_slice_strength):
                source = "slice"
        bits = normalize_direction_bits(str(p.get("directionBits") or ""))
        mag_v = float(p.get("magneticVoltage") or 0)
        if source == "manual":
            validate_direction_bits(bits)
        elif source == "slice":
            advance = bool(p.get("sliceAdvance"))
            rec, _ = flow_ctx.resolve_slice_record(slice_package_id, advance)
            field = rec.get("field") or {}
            if use_slice_direction or not (p.get("directionBits") or "").strip():
                bits = resolve_direction_bits(float(field.get("x") or 0), float(field.get("y") or 0))
            if use_slice_strength:
                if float(p.get("magneticVoltage") or 0) > 0:
                    mag_v = float(p.get("magneticVoltage"))
                else:
                    mag_v = resolve_slice_strength(rec)
            validate_direction_bits(bits)
            if mag_v <= 0:
                raise RuntimeError(
                    "slice magnetic voltage is missing; provide manifest strength or set magneticVoltage override"
                )
        else:
            raise RuntimeError("magnetSource must be manual or slice")
        return LayerPlanItem(
            directionBits=bits,
            magneticVoltage=mag_v,
            magneticHoldS=float(p.get("holdSeconds") or 0),
        )

    def _execute_flow_exposure(self, ctx: RunContext, hw: HardwareController, flow_ctx: FlowRunContext, p: dict[str, Any]) -> None:
        mode = (p.get("imageSource") or "").strip().lower()
        if not mode:
            mode = "slice" if (p.get("slicePackageId") or "").strip() else "manual"
        layer = LayerPlanItem(exposureS=float(p.get("exposureSeconds") or 0))
        intensity = int(p.get("exposureIntensity") or 0)
        if mode == "slice":
            rec, image_path = flow_ctx.resolve_slice_record((p.get("slicePackageId") or "").strip(), True)
            layer.imagePath = image_path
            if bool(p.get("useSliceIntensity")) or intensity <= 0:
                intensity = int(rec.get("light_intensity") or 0)
            if bool(p.get("useSliceMagnet")):
                field = rec.get("field") or {}
                mag_v = resolve_slice_strength(rec)
                if float(p.get("magneticVoltage") or 0) > 0:
                    mag_v = float(p.get("magneticVoltage"))
                if mag_v <= 0:
                    raise RuntimeError(
                        "slice magnetic voltage is missing; provide manifest strength or set magneticVoltage override"
                    )
                mag_layer = LayerPlanItem(
                    directionBits=resolve_direction_bits(float(field.get("x") or 0), float(field.get("y") or 0)),
                    magneticVoltage=mag_v,
                    magneticHoldS=float(p.get("holdSeconds") or 0),
                )
                hw.apply_magnetic_field(ctx, mag_layer)
        elif mode == "manual":
            image_path = (p.get("imagePath") or "").strip()
            if not image_path:
                raise RuntimeError("imagePath is required for exposure")
            layer.imagePath = str(Path(image_path).resolve())
            if intensity <= 0:
                intensity = 80
        else:
            raise RuntimeError("imageSource must be manual or slice")
        if intensity < 0 or intensity > 255:
            raise RuntimeError("exposureIntensity must be 0..255")
        layer.exposureIntensity = intensity
        hw.expose_layer(ctx, layer)

    def run_manual(self, name: str, op: Callable[[RunContext, HardwareController], None]) -> tuple[dict[str, Any], int]:
        if self.print_svc.is_busy():
            return api_result(False, "PRINT_BUSY", "Cannot run manual action while printing."), 409
        with self.lock:
            if self.running:
                return api_result(False, "ADMIN_BUSY", "Admin program is running."), 409
            self.running = True
            self.cancel_event = None
            self.status.update(
                {
                    "running": True,
                    "name": name,
                    "startedAtUtc": to_jsonable_datetime(now_utc()),
                    "finishedAtUtc": None,
                    "currentStep": name,
                    "pendingAsyncOps": 0,
                    "lastError": "",
                    "lastResult": "running",
                }
            )
            self._append_event_locked(f"manual action started: {name}")
        try:
            timeout = self.cfg.hardware.commandTimeoutSeconds if self.cfg.hardware.commandTimeoutSeconds > 0 else 120
            ctx = RunContext(cancel_event=threading.Event(), deadline_ts=time.time() + timeout)
            hw = HardwareController(self.cfg.hardware, self.logger)
            op(ctx, hw)
            with self.lock:
                self.status["lastResult"] = "completed"
                self.status["lastError"] = ""
                self._append_event_locked(f"manual action completed: {name}")
            return api_result(True, "OK", "Manual action completed."), 200
        except Exception as exc:
            with self.lock:
                self.status["lastResult"] = "failed"
                self.status["lastError"] = str(exc)
                self._append_event_locked(f"manual action failed: {exc}")
            return api_result(False, "MANUAL_FAILED", str(exc)), 400
        finally:
            with self.lock:
                self.running = False
                self.status["running"] = False
                self.status["currentStep"] = ""
                self.status["finishedAtUtc"] = to_jsonable_datetime(now_utc())

    def manual_magnet(self, req: dict[str, Any]) -> tuple[dict[str, Any], int]:
        layer = LayerPlanItem(
            layerIndex=0,
            directionBits=normalize_direction_bits(str(req.get("directionBits") or "")),
            magneticVoltage=float(req.get("magneticVoltage") or 0),
            magneticHoldS=float(req.get("holdSeconds") or 0),
        )
        try:
            validate_direction_bits(layer.directionBits)
        except Exception as exc:
            return api_result(False, "BAD_DIRECTION", str(exc)), 400
        if layer.magneticHoldS < 0:
            return api_result(False, "BAD_HOLD", "holdSeconds must be >= 0"), 400

        def op(ctx: RunContext, hw: HardwareController) -> None:
            hw.prepare(ctx)
            hw.apply_magnetic_field(ctx, layer)
            hw.finish(ctx)

        return self.run_manual("manual_magnet", op)

    def manual_exposure(self, req: dict[str, Any]) -> tuple[dict[str, Any], int]:
        image_path = (req.get("imagePath") or "").strip()
        if not image_path:
            return api_result(False, "BAD_IMAGE", "imagePath is required."), 400
        exposure_s = float(req.get("exposureSeconds") or 0)
        if exposure_s < 0:
            return api_result(False, "BAD_EXPOSURE", "exposureSeconds must be >= 0"), 400
        intensity = int(req.get("exposureIntensity") or 0)
        if intensity <= 0:
            intensity = 80
        if intensity > 255:
            return api_result(False, "BAD_INTENSITY", "exposureIntensity must be 0..255"), 400
        layer = LayerPlanItem(
            layerIndex=0,
            imagePath=str(Path(image_path).resolve()),
            exposureIntensity=intensity,
            exposureS=exposure_s,
        )

        def op(ctx: RunContext, hw: HardwareController) -> None:
            hw.prepare(ctx)
            hw.expose_layer(ctx, layer)
            hw.finish(ctx)

        return self.run_manual("manual_exposure", op)

    def manual_move(self, req: dict[str, Any]) -> tuple[dict[str, Any], int]:
        layer_thickness = float(req.get("layerThicknessMm") or 0)
        if layer_thickness <= 0:
            return api_result(False, "BAD_MOVE", "layerThicknessMm must be > 0"), 400
        try:
            move_direction = normalize_move_direction(str(req.get("moveDirection") or ""))
        except Exception as exc:
            return api_result(False, "BAD_MOVE_DIRECTION", str(exc)), 400
        layer = LayerPlanItem(layerIndex=0, layerThicknessMm=layer_thickness, moveDirection=move_direction)

        def op(ctx: RunContext, hw: HardwareController) -> None:
            hw.prepare(ctx)
            hw.move_layer(ctx, layer)
            hw.finish(ctx)

        return self.run_manual("manual_move", op)

    def manual_home(self) -> tuple[dict[str, Any], int]:
        def op(ctx: RunContext, hw: HardwareController) -> None:
            hw.prepare(ctx)
            hw.home(ctx)
            hw.finish(ctx)

        return self.run_manual("manual_home", op)

    def manual_wait(self, req: dict[str, Any]) -> tuple[dict[str, Any], int]:
        wait_s = float(req.get("waitSeconds") or 0)
        if wait_s < 0:
            return api_result(False, "BAD_WAIT", "waitSeconds must be >= 0"), 400

        def op(ctx: RunContext, hw: HardwareController) -> None:
            _ = hw
            sleep_ctx(ctx, wait_s)

        return self.run_manual("manual_wait", op)


def parse_bearer_token(authorization: str | None, request: Request) -> str:
    token = ""
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    if not token:
        token = (request.query_params.get("access_token") or "").strip()
    return token


def parse_json_body(raw: bytes, max_bytes: int = 1 << 20) -> dict[str, Any]:
    if len(raw) > max_bytes:
        raise RuntimeError("request body too large")
    try:
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise RuntimeError("JSON body must be object")
        return data
    except Exception as exc:
        raise RuntimeError(str(exc)) from exc


def create_app() -> FastAPI:
    base_dir = Path.cwd()
    cfg = AppConfig.load(base_dir)
    data_root = Path(cfg.dataRoot)
    data_root.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("magnetic_printer_py")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        h = logging.StreamHandler()
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(h)

    auth_svc = AuthService(data_root)
    pkg_svc = PackageService(data_root)
    print_svc = PrintService(auth_svc, pkg_svc, cfg, logger)
    admin_svc = AdminService(cfg, print_svc, pkg_svc, logger)
    flow_cfg_svc = FlowConfigService(data_root)

    app = FastAPI(title="Magnetic Printer Python Backend")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )

    def require_user(request: Request, authorization: str | None) -> tuple[str, str]:
        token = parse_bearer_token(authorization, request)
        if not token:
            raise HTTPException(status_code=401, detail=api_result(False, "UNAUTHORIZED", "Unauthorized"))
        session = auth_svc.validate(token)
        if not session:
            raise HTTPException(status_code=401, detail=api_result(False, "UNAUTHORIZED", "Unauthorized"))
        return session.username, token

    @app.exception_handler(HTTPException)
    async def http_exception_handler(_: Request, exc: HTTPException) -> JSONResponse:
        if isinstance(exc.detail, dict):
            return JSONResponse(exc.detail, status_code=exc.status_code)
        return JSONResponse(api_result(False, "HTTP_ERROR", str(exc.detail)), status_code=exc.status_code)

    @app.post("/api/auth/register")
    async def auth_register(request: Request) -> JSONResponse:
        try:
            req = parse_json_body(await request.body())
        except Exception as exc:
            return JSONResponse(api_result(False, "BAD_REQUEST", str(exc)), status_code=400)
        result = auth_svc.register(str(req.get("username") or ""), str(req.get("password") or ""))
        return JSONResponse(result, status_code=200 if result.get("success") else 400)

    @app.post("/api/auth/login")
    async def auth_login(request: Request) -> JSONResponse:
        try:
            req = parse_json_body(await request.body())
        except Exception as exc:
            return JSONResponse(api_result(False, "BAD_REQUEST", str(exc)), status_code=400)
        result = auth_svc.login(str(req.get("username") or ""), str(req.get("password") or ""))
        if result.get("success"):
            return JSONResponse(result, status_code=200)
        if result.get("code") == "DEVICE_LOCKED":
            return JSONResponse(result, status_code=423)
        return JSONResponse(result, status_code=400)

    @app.post("/api/auth/logout")
    async def auth_logout(request: Request, authorization: str | None = Header(default=None)) -> JSONResponse:
        _, token = require_user(request, authorization)
        auth_svc.logout(token, print_svc.is_busy())
        return JSONResponse({"message": "logged_out"}, status_code=200)

    @app.get("/api/auth/me")
    async def auth_me(request: Request, authorization: str | None = Header(default=None)) -> JSONResponse:
        user, _ = require_user(request, authorization)
        return JSONResponse(
            {
                "username": user,
                "lockOwner": auth_svc.lock_owner(),
                "canControlDevice": auth_svc.is_lock_owner(user),
                "isBusy": print_svc.is_busy(),
                "isAdmin": is_admin_user(cfg, user),
            },
            status_code=200,
        )

    @app.get("/api/device/status")
    async def device_status(request: Request, authorization: str | None = Header(default=None)) -> JSONResponse:
        user, _ = require_user(request, authorization)
        return JSONResponse(print_svc.get_status_for_user(user), status_code=200)

    @app.get("/api/device/stream")
    async def device_stream(request: Request, authorization: str | None = Header(default=None)) -> StreamingResponse:
        user, _ = require_user(request, authorization)

        async def generator() -> Any:
            while True:
                if await request.is_disconnected():
                    break
                status = print_svc.get_status_for_user(user)
                payload = json.dumps(status, ensure_ascii=False)
                yield f"event: status\ndata: {payload}\n\n"
                await asyncio.sleep(1.0)

        return StreamingResponse(generator(), media_type="text/event-stream")

    @app.get("/api/packages")
    async def list_packages(request: Request, authorization: str | None = Header(default=None)) -> JSONResponse:
        _user, _ = require_user(request, authorization)
        return JSONResponse(pkg_svc.list_packages(), status_code=200)

    @app.post("/api/packages/upload")
    async def upload_package(
        request: Request,
        file: UploadFile = File(...),
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        user, _ = require_user(request, authorization)
        result = pkg_svc.upload_package(file, user)
        return JSONResponse(result, status_code=200 if result.get("success") else 400)

    @app.post("/api/print/start")
    async def print_start(request: Request, authorization: str | None = Header(default=None)) -> JSONResponse:
        user, _ = require_user(request, authorization)
        try:
            req = parse_json_body(await request.body())
        except Exception as exc:
            return JSONResponse(api_result(False, "BAD_REQUEST", str(exc)), status_code=400)
        result, status = print_svc.start(user, req)
        return JSONResponse(result, status_code=status)

    @app.post("/api/print/cancel")
    async def print_cancel(request: Request, authorization: str | None = Header(default=None)) -> JSONResponse:
        user, _ = require_user(request, authorization)
        result, status = print_svc.cancel(user)
        return JSONResponse(result, status_code=status)

    @app.get("/api/direction-map")
    async def direction_map() -> JSONResponse:
        return JSONResponse(
            {
                "xPositive": X_POSITIVE_BITS,
                "xNegative": X_NEGATIVE_BITS,
                "yPositive": Y_POSITIVE_BITS,
                "yNegative": Y_NEGATIVE_BITS,
            },
            status_code=200,
        )

    @app.get("/api/admin/status")
    async def admin_status(request: Request, authorization: str | None = Header(default=None)) -> JSONResponse:
        _user, _ = require_user(request, authorization)
        return JSONResponse(admin_svc.get_status(), status_code=200)

    def admin_guard(user: str) -> Optional[JSONResponse]:
        if is_admin_user(cfg, user):
            return None
        return JSONResponse(api_result(False, "FORBIDDEN", "Admin only"), status_code=403)

    @app.post("/api/admin/manual/magnet")
    async def admin_manual_magnet(request: Request, authorization: str | None = Header(default=None)) -> JSONResponse:
        user, _ = require_user(request, authorization)
        forbidden = admin_guard(user)
        if forbidden:
            return forbidden
        try:
            req = parse_json_body(await request.body())
        except Exception as exc:
            return JSONResponse(api_result(False, "BAD_REQUEST", str(exc)), status_code=400)
        result, status = admin_svc.manual_magnet(req)
        return JSONResponse(result, status_code=status)

    @app.post("/api/admin/manual/exposure")
    async def admin_manual_exposure(request: Request, authorization: str | None = Header(default=None)) -> JSONResponse:
        user, _ = require_user(request, authorization)
        forbidden = admin_guard(user)
        if forbidden:
            return forbidden
        try:
            req = parse_json_body(await request.body())
        except Exception as exc:
            return JSONResponse(api_result(False, "BAD_REQUEST", str(exc)), status_code=400)
        result, status = admin_svc.manual_exposure(req)
        return JSONResponse(result, status_code=status)

    @app.post("/api/admin/manual/move")
    async def admin_manual_move(request: Request, authorization: str | None = Header(default=None)) -> JSONResponse:
        user, _ = require_user(request, authorization)
        forbidden = admin_guard(user)
        if forbidden:
            return forbidden
        try:
            req = parse_json_body(await request.body())
        except Exception as exc:
            return JSONResponse(api_result(False, "BAD_REQUEST", str(exc)), status_code=400)
        result, status = admin_svc.manual_move(req)
        return JSONResponse(result, status_code=status)

    @app.post("/api/admin/manual/home")
    async def admin_manual_home(request: Request, authorization: str | None = Header(default=None)) -> JSONResponse:
        user, _ = require_user(request, authorization)
        forbidden = admin_guard(user)
        if forbidden:
            return forbidden
        result, status = admin_svc.manual_home()
        return JSONResponse(result, status_code=status)

    @app.post("/api/admin/manual/wait")
    async def admin_manual_wait(request: Request, authorization: str | None = Header(default=None)) -> JSONResponse:
        user, _ = require_user(request, authorization)
        forbidden = admin_guard(user)
        if forbidden:
            return forbidden
        try:
            req = parse_json_body(await request.body())
        except Exception as exc:
            return JSONResponse(api_result(False, "BAD_REQUEST", str(exc)), status_code=400)
        result, status = admin_svc.manual_wait(req)
        return JSONResponse(result, status_code=status)

    def ensure_lock_owner(user: str) -> Optional[JSONResponse]:
        if auth_svc.is_lock_owner(user):
            return None
        return JSONResponse(api_result(False, "FORBIDDEN", "only current lock owner can control flow"), status_code=403)

    @app.post("/api/admin/program/run")
    async def admin_program_run(request: Request, authorization: str | None = Header(default=None)) -> JSONResponse:
        user, _ = require_user(request, authorization)
        forbidden = ensure_lock_owner(user)
        if forbidden:
            return forbidden
        try:
            req = parse_json_body(await request.body())
        except Exception as exc:
            return JSONResponse(api_result(False, "BAD_REQUEST", str(exc)), status_code=400)
        result, status = admin_svc.run_program(req)
        return JSONResponse(result, status_code=status)

    @app.post("/api/admin/program/cancel")
    async def admin_program_cancel(request: Request, authorization: str | None = Header(default=None)) -> JSONResponse:
        user, _ = require_user(request, authorization)
        forbidden = ensure_lock_owner(user)
        if forbidden:
            return forbidden
        result, status = admin_svc.cancel_program()
        return JSONResponse(result, status_code=status)

    @app.post("/api/program/config/export")
    async def program_export(request: Request, authorization: str | None = Header(default=None)) -> Response:
        user, _ = require_user(request, authorization)
        forbidden = ensure_lock_owner(user)
        if forbidden:
            return forbidden
        try:
            req = parse_json_body(await request.body())
            enc = flow_cfg_svc.encrypt_config(req)
        except Exception as exc:
            return JSONResponse(api_result(False, "CONFIG_EXPORT_FAILED", str(exc)), status_code=400)
        name = (req.get("name") or "").strip() or "flow-program"
        safe_name = "".join(ch if (ch.isalnum() or ch in "_-") else "_" for ch in name)
        headers = {"Content-Disposition": f'attachment; filename="{safe_name}.mpcfg"'}
        return Response(content=enc, media_type="application/octet-stream", headers=headers, status_code=200)

    @app.post("/api/program/config/import")
    async def program_import(request: Request, authorization: str | None = Header(default=None)) -> JSONResponse:
        user, _ = require_user(request, authorization)
        forbidden = ensure_lock_owner(user)
        if forbidden:
            return forbidden
        raw = await request.body()
        if len(raw) > FLOW_CONFIG_MAX_BYTES:
            return JSONResponse(api_result(False, "CONFIG_IMPORT_FAILED", "config payload too large"), status_code=400)
        try:
            req = flow_cfg_svc.decrypt_config(raw)
        except Exception as exc:
            return JSONResponse(api_result(False, "CONFIG_IMPORT_FAILED", str(exc)), status_code=400)
        return JSONResponse({**api_result(True, "OK", "Config imported."), "config": req}, status_code=200)

    frontend_root = Path(cfg.frontendRoot)
    app.mount("/", StaticFiles(directory=str(frontend_root), html=True), name="frontend")

    logger.info("Python backend listening on http://localhost%s", cfg.listenAddr)
    logger.info("Frontend root: %s", frontend_root)
    logger.info("Data root: %s", data_root)
    logger.info("UseMockHardware: %s", cfg.hardware.useMockHardware or os.name == "nt")
    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    base = Path.cwd()
    cfg = AppConfig.load(base)
    addr = cfg.listenAddr.strip()
    host = "0.0.0.0"
    port = 5241
    if addr.startswith(":"):
        port = int(addr[1:])
    elif ":" in addr:
        h, p = addr.rsplit(":", 1)
        host = h or "0.0.0.0"
        port = int(p)
    uvicorn.run("main:app", host=host, port=port, reload=False)
