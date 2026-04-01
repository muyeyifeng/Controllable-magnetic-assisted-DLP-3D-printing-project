#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import glob
import os
import subprocess
import sys
import time
from pathlib import Path

try:
    import serial
except Exception:
    serial = None


CMD_HANDSHAKE = "A6 01 05"
CMD_DLP_ON = "A6 02 02 01"
CMD_DLP_OFF = "A6 02 02 00"
CMD_LED_ON = "A6 02 03 01"
CMD_LED_OFF = "A6 02 03 00"


class DLPController:
    def __init__(self, port: str, baudrate: int, timeout_s: float):
        self.port = port
        self.baudrate = baudrate
        self.timeout_s = timeout_s
        self.ser = None

    def open(self) -> None:
        if serial is None:
            raise RuntimeError("pyserial 未安装，请先安装: pip install pyserial")
        self.ser = serial.Serial(self.port, self.baudrate, timeout=self.timeout_s)
        print(f"[DLP] serial opened: {self.port} @ {self.baudrate}")

    def close(self) -> None:
        if self.ser is not None:
            try:
                self.ser.close()
            except Exception:
                pass
            self.ser = None

    def _send_hex(self, hex_str: str, desc: str) -> bool:
        if self.ser is None:
            raise RuntimeError("串口未打开")
        data = bytes.fromhex(hex_str)
        self.ser.reset_input_buffer()
        self.ser.write(data)
        resp = self.ser.read(10)
        resp_hex = " ".join(f"{b:02X}" for b in resp) if resp else "(timeout)"
        print(f"[DLP] {desc}: send={hex_str} recv={resp_hex}")
        if not resp:
            return False
        return not resp_hex.endswith("E0")

    def handshake(self) -> None:
        if not self._send_hex(CMD_HANDSHAKE, "handshake"):
            raise RuntimeError("DLP 握手失败")

    def dlp_on(self) -> None:
        if not self._send_hex(CMD_DLP_ON, "dlp_on"):
            raise RuntimeError("DLP 开启失败")

    def dlp_off(self) -> None:
        self._send_hex(CMD_DLP_OFF, "dlp_off")

    def led_on(self) -> None:
        if not self._send_hex(CMD_LED_ON, "led_on"):
            raise RuntimeError("LED 开启失败")

    def led_off(self) -> None:
        try:
            self._send_hex(CMD_LED_OFF, "led_off")
        except Exception:
            pass

    def set_brightness(self, value: int) -> None:
        v = max(0, min(255, int(value)))
        cmd = f"A6 02 10 {v:02X}"
        if not self._send_hex(cmd, f"brightness={v}"):
            raise RuntimeError(f"亮度设置失败: {v}")


def check_hdmi_1080() -> tuple[bool, str]:
    status_paths = sorted(glob.glob("/sys/class/drm/card*-HDMI-A-*/status"))
    mode_paths = sorted(glob.glob("/sys/class/drm/card*-HDMI-A-*/modes"))

    if not status_paths:
        return False, "未找到 /sys/class/drm/card*-HDMI-A-*"

    connected = []
    for sp in status_paths:
        try:
            status = Path(sp).read_text(encoding="utf-8", errors="ignore").strip().lower()
        except Exception:
            status = "unknown"
        if status == "connected":
            connected.append(sp)

    if not connected:
        return False, f"HDMI 状态: {[Path(p).read_text(errors='ignore').strip() for p in status_paths]}"

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
        return True, f"connected, modes include 1920x1080 (paths={connected})"
    return False, f"connected but no 1920x1080, modes={sorted(modes)}"


def run_systemctl(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["systemctl", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def suppress_tty_getty(tty: int) -> tuple[str, bool]:
    service = f"getty@tty{tty}.service"
    active = run_systemctl("is-active", service).returncode == 0
    if active:
        p = run_systemctl("stop", service)
        if p.returncode == 0:
            print(f"[TTY] stopped {service} for quiet projection")
            return service, True
        print(f"[TTY] failed to stop {service}: {p.stderr.strip()}")
    return service, False


def restore_tty_getty(service: str, need_restore: bool) -> None:
    if not need_restore:
        return
    p = run_systemctl("start", service)
    if p.returncode == 0:
        print(f"[TTY] restored {service}")
    else:
        print(f"[TTY] failed to restore {service}: {p.stderr.strip()}")


def start_fbi(image_path: Path, tty: int, fbdev: str, settle_s: float) -> subprocess.Popen[str]:
    cmd = [
        "fbi",
        "-T",
        str(tty),
        "-d",
        fbdev,
        "-a",
        "--noverbose",
        str(image_path),
    ]
    print("[HDMI] RUN:", " ".join(cmd))
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True)
    time.sleep(settle_s)
    return proc


def stop_fbi(proc: subprocess.Popen[str] | None) -> None:
    if proc is None:
        return
    if proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


def main() -> None:
    parser = argparse.ArgumentParser(description="HDMI投影 + DLP曝光测试")
    parser.add_argument("--image", required=True, help="要投影的图片路径")
    parser.add_argument("--exposure", type=float, default=5.0, help="曝光秒数，默认5")
    parser.add_argument("--brightness", type=int, default=120, help="亮度0~255，默认120")

    parser.add_argument("--port", default="/dev/ttyUSB0", help="DLP串口，默认/dev/ttyUSB0")
    parser.add_argument("--baud", type=int, default=115200, help="串口波特率")
    parser.add_argument("--timeout", type=float, default=1.0, help="串口超时秒")

    parser.add_argument("--tty", type=int, default=1, help="fbi tty，默认1")
    parser.add_argument("--fb", default="/dev/fb0", help="framebuffer设备，默认/dev/fb0")
    parser.add_argument("--settle", type=float, default=0.5, help="图片切换后等待秒数")
    args = parser.parse_args()

    img = Path(args.image).expanduser().resolve()
    if not img.exists():
        raise RuntimeError(f"图片不存在: {img}")

    ok, msg = check_hdmi_1080()
    print(f"[HDMI] {msg}")
    if not ok:
        raise RuntimeError("HDMI 未就绪(1920x1080)")

    if os.geteuid() != 0:
        print("[WARN] 建议用 sudo 运行此脚本，否则 fbi 或串口可能权限不足")

    fbi_proc = None
    dlp = DLPController(args.port, args.baud, args.timeout)
    tty_service = ""
    tty_restore_needed = False

    try:
        tty_service, tty_restore_needed = suppress_tty_getty(args.tty)

        # 1) 先投影图片
        fbi_proc = start_fbi(img, args.tty, args.fb, args.settle)
        if fbi_proc.poll() is not None:
            rc = fbi_proc.returncode
            if rc != 0:
                raise RuntimeError(f"fbi 启动失败(code={rc})")
            print("[HDMI] fbi exited with code 0 after rendering (accepted)")

        # 2) 再DLP握手/开引擎/设亮度
        dlp.open()
        dlp.handshake()
        dlp.dlp_on()
        dlp.led_off()
        dlp.set_brightness(args.brightness)

        # 3) LED曝光
        print(f"[EXPOSE] LED ON for {args.exposure:.3f}s")
        dlp.led_on()
        time.sleep(max(0.0, args.exposure))
        dlp.led_off()
        print("[EXPOSE] done")

    finally:
        try:
            dlp.led_off()
        except Exception:
            pass
        try:
            dlp.dlp_off()
        except Exception:
            pass
        dlp.close()
        stop_fbi(fbi_proc)
        restore_tty_getty(tty_service, tty_restore_needed)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n用户中断")
        sys.exit(130)
    except Exception as e:
        print(f"\nERROR: {e}")
        sys.exit(1)
