#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import pigpio


parser = argparse.ArgumentParser()
parser.add_argument("--action", type=str, required=True, choices=["status", "move", "pos", "reset-pos"])
parser.add_argument("--pul", type=int, default=13)
parser.add_argument("--dir", type=int, default=5)
parser.add_argument("--ena", type=int, default=8)
parser.add_argument("--top-limit", type=int, default=20)
parser.add_argument("--freq", type=int, default=1600)
parser.add_argument("--move", type=str, choices=["up", "down"], default="up")
parser.add_argument("--um", type=float, default=None)
parser.add_argument("--steps", type=int, default=None)
parser.add_argument("--steps-per-rev", type=int, default=3200)
parser.add_argument("--lead-mm", type=float, default=4.0)
parser.add_argument("--ena-active-low", action="store_true")
parser.add_argument("--pulse-width-us", type=int, default=20)
parser.add_argument("--state-file", type=str, default="/tmp/motor_state.json")
parser.add_argument("--progress-file", type=str, default="/tmp/motor_progress.json")
parser.add_argument("--chunk-steps", type=int, default=200)

args = parser.parse_args()


def atomic_write_json(path: str, payload: dict) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(target) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)
    os.replace(tmp, target)


def json_ok(**kwargs):
    data = {"success": True}
    data.update(kwargs)
    print(json.dumps(data, ensure_ascii=False))
    sys.exit(0)


def json_fail(message, **kwargs):
    data = {"success": False, "message": message}
    data.update(kwargs)
    print(json.dumps(data, ensure_ascii=False))
    sys.exit(1)


def calc_steps_from_um(distance_um: float, steps_per_rev: int, lead_mm: float) -> int:
    return int(round(distance_um * steps_per_rev / (lead_mm * 1000.0)))


def calc_um_from_steps(steps: int, steps_per_rev: int, lead_mm: float) -> float:
    return steps * lead_mm * 1000.0 / steps_per_rev


def load_state(path: str):
    target = Path(path)
    if not target.exists():
        return {"position_steps": 0}
    try:
        with target.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        if "position_steps" not in data:
            data["position_steps"] = 0
        return data
    except Exception:
        return {"position_steps": 0}


def save_state(path: str, state: dict):
    atomic_write_json(path, state)


def write_progress(message: str, position_steps: int, top_limit_triggered: bool, moving: bool, direction: str | None = None):
    atomic_write_json(
        args.progress_file,
        {
            "message": message,
            "position_steps": position_steps,
            "position_um": calc_um_from_steps(position_steps, args.steps_per_rev, args.lead_mm),
            "top_limit_triggered": top_limit_triggered,
            "moving": moving,
            "direction": direction,
            "updated_at": time.time(),
        },
    )


def build_step_wave(pi: pigpio.pi, step_pin: int, freq: int, pulse_width_us: int):
    period_us = int(round(1_000_000 / freq))
    if pulse_width_us >= period_us:
        raise ValueError(f"pulse-width-us={pulse_width_us} 不能 >= 周期 {period_us} us")

    pi.wave_clear()
    pi.wave_add_generic(
        [
            pigpio.pulse(1 << step_pin, 0, pulse_width_us),
            pigpio.pulse(0, 1 << step_pin, period_us - pulse_width_us),
        ]
    )
    wid = pi.wave_create()
    if wid < 0:
        raise RuntimeError("wave_create 失败")
    return wid


def send_steps_chunk(pi: pigpio.pi, wid: int, chunk_steps: int):
    x = chunk_steps & 0xFF
    y = (chunk_steps >> 8) & 0xFF
    pi.wave_chain([255, 0, wid, 255, 1, x, y])


def main():
    state = load_state(args.state_file)
    position_steps = int(state.get("position_steps", 0))

    pi = pigpio.pi()
    if not pi.connected:
        json_fail("连接 pigpio 失败，请先启动 pigpiod")

    callback = None
    top_limit_falling = False

    def top_limit_callback(gpio, level, tick):
        nonlocal top_limit_falling
        if level == 0:
            top_limit_falling = True

    try:
        pi.set_mode(args.pul, pigpio.OUTPUT)
        pi.set_mode(args.dir, pigpio.OUTPUT)
        pi.set_mode(args.ena, pigpio.OUTPUT)
        pi.set_mode(args.top_limit, pigpio.INPUT)
        pi.set_pull_up_down(args.top_limit, pigpio.PUD_UP)
        callback = pi.callback(args.top_limit, pigpio.FALLING_EDGE, top_limit_callback)

        top_limit_level = pi.read(args.top_limit)
        top_limit_triggered = top_limit_level == 0
        if top_limit_triggered and position_steps != 0:
            position_steps = 0
            state["position_steps"] = 0
            save_state(args.state_file, state)

        if args.action == "status":
            write_progress("电机状态正常", position_steps, top_limit_triggered, False)
            json_ok(
                message="电机状态正常",
                pigpio_connected=True,
                top_limit_triggered=top_limit_triggered,
                top_limit_level=top_limit_level,
                position_steps=position_steps,
                position_um=calc_um_from_steps(position_steps, args.steps_per_rev, args.lead_mm),
            )

        if args.action == "pos":
            write_progress("当前位置", position_steps, top_limit_triggered, False)
            json_ok(
                message="当前位置",
                position_steps=position_steps,
                position_um=calc_um_from_steps(position_steps, args.steps_per_rev, args.lead_mm),
                top_limit_triggered=top_limit_triggered,
            )

        if args.action == "reset-pos":
            state["position_steps"] = 0
            save_state(args.state_file, state)
            write_progress("位置已清零", 0, top_limit_triggered, False)
            json_ok(message="位置已清零", position_steps=0, position_um=0.0)

        if args.action == "move":
            if args.steps is None and args.um is None:
                json_fail("move 必须指定 --steps 或 --um")

            if args.steps is not None:
                target_steps = int(args.steps)
                target_um = calc_um_from_steps(target_steps, args.steps_per_rev, args.lead_mm)
            else:
                target_um = float(args.um)
                target_steps = calc_steps_from_um(target_um, args.steps_per_rev, args.lead_mm)

            if target_steps <= 0:
                json_fail(f"计算得到的步数非法: {target_steps}")

            if args.move == "up" and top_limit_triggered:
                state["position_steps"] = 0
                save_state(args.state_file, state)
                write_progress("已在上限位", 0, True, False)
                json_ok(
                    message="已在上限位，无需继续上移",
                    stopped_by_top_limit=True,
                    position_steps=0,
                    position_um=0.0,
                    moved_steps=0,
                    top_limit_triggered=True,
                )

            pi.write(args.dir, 1 if args.move == "up" else 0)
            ena_on = 0 if args.ena_active_low else 1
            ena_off = 1 - ena_on
            pi.write(args.ena, ena_on)

            wid = None
            moved_steps = 0
            t0 = time.time()
            stopped_by_top_limit = False

            try:
                wid = build_step_wave(pi, args.pul, args.freq, args.pulse_width_us)
                remaining = target_steps
                write_progress(f"电机运动中: {args.move}", position_steps, top_limit_triggered, True, args.move)

                while remaining > 0:
                    chunk = min(remaining, args.chunk_steps)
                    send_steps_chunk(pi, wid, chunk)

                    while pi.wave_tx_busy():
                        if args.move == "up" and (top_limit_falling or pi.read(args.top_limit) == 0):
                            pi.wave_tx_stop()
                            stopped_by_top_limit = True
                            break
                        time.sleep(0.001)

                    if stopped_by_top_limit:
                        position_steps = 0
                        top_limit_triggered = True
                        write_progress("触发上限位，已停止", position_steps, True, False, args.move)
                        break

                    moved_steps += chunk
                    remaining -= chunk

                    if args.move == "up":
                        position_steps = max(0, position_steps - chunk)
                        top_limit_triggered = position_steps == 0 and pi.read(args.top_limit) == 0
                    else:
                        position_steps += chunk
                        top_limit_triggered = False

                    write_progress(f"电机运动中: {args.move}", position_steps, top_limit_triggered, True, args.move)

                state["position_steps"] = position_steps
                save_state(args.state_file, state)
                write_progress(
                    "运动完成" if not stopped_by_top_limit else "触发上限位，已停止并清零",
                    position_steps,
                    top_limit_triggered,
                    False,
                    args.move,
                )

                json_ok(
                    message="运动完成" if not stopped_by_top_limit else "触发上限位，已停止并清零",
                    move=args.move,
                    target_steps=target_steps,
                    target_um=target_um,
                    moved_steps=moved_steps,
                    moved_um=calc_um_from_steps(moved_steps, args.steps_per_rev, args.lead_mm),
                    stopped_by_top_limit=stopped_by_top_limit,
                    top_limit_triggered=top_limit_triggered,
                    position_steps=position_steps,
                    position_um=calc_um_from_steps(position_steps, args.steps_per_rev, args.lead_mm),
                    elapsed_s=round(time.time() - t0, 4),
                )
            finally:
                try:
                    if wid is not None and wid >= 0:
                        pi.wave_delete(wid)
                except Exception:
                    pass
                try:
                    pi.wave_clear()
                except Exception:
                    pass
                try:
                    pi.write(args.ena, ena_off)
                except Exception:
                    pass

        json_fail("未知 action")
    except KeyboardInterrupt:
        json_fail("用户中断")
    except Exception as exc:
        json_fail(f"运行失败: {exc}")
    finally:
        try:
            if callback is not None:
                callback.cancel()
        except Exception:
            pass
        pi.stop()


if __name__ == "__main__":
    main()
