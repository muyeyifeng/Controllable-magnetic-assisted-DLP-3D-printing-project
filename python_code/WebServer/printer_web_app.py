from __future__ import annotations

import mimetypes
import shutil
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, render_template, request, send_from_directory

from hardware_modules import MagnetCommand, MagnetController, MotorController, MotorMoveRequest, UVController, UVOutputRequest
from job_runner import PrinterJobRunner
from printer_state import PersistentState, now_iso


BASE_DIR = Path(__file__).resolve().parent
WEB_DIR = BASE_DIR / "webapp"
DATA_DIR = BASE_DIR / "webapp_data"
UPLOADS_DIR = DATA_DIR / "uploads"
STATE_FILE = DATA_DIR / "printer_state.json"
MOTOR_STATE_FILE = DATA_DIR / "motor_state.json"
MOTOR_PROGRESS_FILE = DATA_DIR / "motor_progress.json"

ALLOWED_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp"}


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
    return f"/uploads/{upload_id}/{relative_path.replace('\\', '/')}"


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


state_store = PersistentState(STATE_FILE, MOTOR_STATE_FILE, MOTOR_PROGRESS_FILE)
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


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/uploads/<upload_id>/<path:relative_path>")
def uploads(upload_id: str, relative_path: str):
    return send_from_directory(UPLOADS_DIR / upload_id, relative_path)


@app.get("/api/state")
def api_state():
    motor.status()
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
    try:
        result = motor.move(
            MotorMoveRequest(
                direction=str(payload.get("direction", "")),
                distance_um=float(payload.get("distance_um", 0)),
                speed_um_s=float(payload.get("speed_um_s", 0)),
            )
        )
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
    duration_s = max(0.05, float(payload.get("duration_s", 0)))
    try:
        uv.set_output(UVOutputRequest(power=power, lamp_on=True))
        try:
            import time

            time.sleep(duration_s)
        finally:
            uv.set_output(UVOutputRequest(power=power, lamp_on=False))
        message = f"手动曝光完成 {duration_s:.2f}s"
        state_store.add_log(message)
        return success(message)
    except Exception as exc:
        return failure(str(exc))


@app.post("/api/manual/magnet")
def api_manual_magnet():
    payload = request.get_json(silent=True) or {}
    enabled = bool(payload.get("enabled", False))
    voltage = float(payload.get("voltage", 0.0))
    try:
        result = magnet.apply(MagnetCommand(voltage=voltage, enabled=enabled, io_enabled=enabled)) if enabled else magnet.off()
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
