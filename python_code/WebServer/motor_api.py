#!/usr/bin/env python3
import pigpio
import time
import argparse
import sys
import json
import os
from pathlib import Path

# =========================
# 参数
# =========================
parser = argparse.ArgumentParser()

# 动作
parser.add_argument('--action', type=str, required=True,
                    choices=['status', 'move', 'pos', 'reset-pos'],
                    help='status: 查询状态；move: 运动；pos: 查询位置；reset-pos: 手动清零位置')

# 引脚
parser.add_argument('--pul', type=int, default=13, help='PUL/STEP 引脚 BCM 编号')
parser.add_argument('--dir', type=int, default=5, help='DIR 引脚 BCM 编号')
parser.add_argument('--ena', type=int, default=8, help='ENA 引脚 BCM 编号')
parser.add_argument('--top-limit', type=int, default=20, help='上限位输入引脚 BCM 编号（触发时从1变0）')

# 运动参数
parser.add_argument('--freq', type=int, default=1600, help='步进脉冲频率 Hz')
parser.add_argument('--move', type=str, choices=['up', 'down'], default='up', help='运动方向')
parser.add_argument('--um', type=float, default=None, help='目标位移，单位 um')
parser.add_argument('--steps', type=int, default=None, help='直接指定步数；若指定则优先于 --um')

# 丝杠/细分参数
parser.add_argument('--steps-per-rev', type=int, default=3200, help='每圈脉冲数/step')
parser.add_argument('--lead-mm', type=float, default=4.0, help='丝杠导程，单位 mm/圈')

# 使能极性
parser.add_argument('--ena-active-low', action='store_true', help='使能是否低电平有效')

# STEP 脉冲高电平宽度
parser.add_argument('--pulse-width-us', type=int, default=20, help='STEP 高电平脉宽，单位 us')

# 状态文件
parser.add_argument('--state-file', type=str, default='/tmp/motor_state.json',
                    help='保存当前位置的状态文件')

# 每批发送多少步，便于上移时及时检测上限位
parser.add_argument('--chunk-steps', type=int, default=200,
                    help='每批发送的步数。越小越容易及时停下，但总开销更大')

args = parser.parse_args()


# =========================
# 工具函数
# =========================
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
    lead_um = lead_mm * 1000.0
    return int(round(distance_um * steps_per_rev / lead_um))


def calc_um_from_steps(steps: int, steps_per_rev: int, lead_mm: float) -> float:
    lead_um = lead_mm * 1000.0
    return steps * lead_um / steps_per_rev


def load_state(path: str):
    p = Path(path)
    if not p.exists():
        return {"position_steps": 0}
    try:
        with open(p, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if "position_steps" not in data:
            data["position_steps"] = 0
        return data
    except Exception:
        return {"position_steps": 0}


def save_state(path: str, state: dict):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(p) + ".tmp"
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False)
    os.replace(tmp, p)


def build_step_wave(pi: pigpio.pi, step_pin: int, freq: int, pulse_width_us: int):
    period_us = int(round(1_000_000 / freq))
    if pulse_width_us >= period_us:
        raise ValueError(
            f"pulse-width-us={pulse_width_us} 不能 >= 一个周期 {period_us} us，请降低脉宽或降低频率"
        )

    low_us = period_us - pulse_width_us

    pi.wave_clear()
    pulses = [
        pigpio.pulse(1 << step_pin, 0, pulse_width_us),
        pigpio.pulse(0, 1 << step_pin, low_us),
    ]
    pi.wave_add_generic(pulses)
    wid = pi.wave_create()

    if wid < 0:
        raise RuntimeError("wave_create 失败")

    return wid


def send_steps_chunk(pi: pigpio.pi, wid: int, chunk_steps: int):
    x = chunk_steps & 0xFF
    y = (chunk_steps >> 8) & 0xFF
    chain = [
        255, 0, wid,
        255, 1, x, y
    ]
    pi.wave_chain(chain)


# =========================
# 主逻辑
# =========================
def main():
    step_um = args.lead_mm * 1000.0 / args.steps_per_rev
    state = load_state(args.state_file)
    position_steps = int(state.get("position_steps", 0))

    pi = pigpio.pi()
    if not pi.connected:
        json_fail("连接 pigpio 失败，请先启动 pigpiod")

    cb = None
    top_limit_falling = False

    def top_limit_callback(gpio, level, tick):
        nonlocal top_limit_falling
        # pigpio.FALLING_EDGE -> level == 0
        if level == 0:
            top_limit_falling = True

    try:
        # 基本引脚设置
        pi.set_mode(args.pul, pigpio.OUTPUT)
        pi.set_mode(args.dir, pigpio.OUTPUT)
        pi.set_mode(args.ena, pigpio.OUTPUT)

        pi.set_mode(args.top_limit, pigpio.INPUT)
        pi.set_pull_up_down(args.top_limit, pigpio.PUD_UP)

        # 注册上限位下降沿回调
        cb = pi.callback(args.top_limit, pigpio.FALLING_EDGE, top_limit_callback)

        # 实时读取当前上限位状态
        top_limit_level = pi.read(args.top_limit)  # 1=未触发, 0=触发
        top_limit_triggered = (top_limit_level == 0)

        # 如果当前已经在上限位，则当前位置强制清零
        if top_limit_triggered and position_steps != 0:
            position_steps = 0
            state["position_steps"] = 0
            save_state(args.state_file, state)

        # -------------------------
        # action = status
        # -------------------------
        if args.action == 'status':
            json_ok(
                message="状态正常",
                pigpio_connected=True,
                top_limit_triggered=top_limit_triggered,
                top_limit_level=top_limit_level,
                position_steps=position_steps,
                position_um=calc_um_from_steps(position_steps, args.steps_per_rev, args.lead_mm)
            )

        # -------------------------
        # action = pos
        # -------------------------
        if args.action == 'pos':
            json_ok(
                message="当前位置",
                position_steps=position_steps,
                position_um=calc_um_from_steps(position_steps, args.steps_per_rev, args.lead_mm),
                top_limit_triggered=top_limit_triggered
            )

        # -------------------------
        # action = reset-pos
        # -------------------------
        if args.action == 'reset-pos':
            state["position_steps"] = 0
            save_state(args.state_file, state)
            json_ok(
                message="位置已清零",
                position_steps=0,
                position_um=0.0
            )

        # -------------------------
        # action = move
        # -------------------------
        if args.action == 'move':
            if args.steps is None and args.um is None:
                json_fail("move 动作必须指定 --steps 或 --um")

            if args.steps is not None:
                target_steps = int(args.steps)
                target_um = calc_um_from_steps(target_steps, args.steps_per_rev, args.lead_mm)
            else:
                target_um = float(args.um)
                target_steps = calc_steps_from_um(target_um, args.steps_per_rev, args.lead_mm)

            if target_steps <= 0:
                json_fail(f"计算得到步数为 {target_steps}，请检查输入")

            # 若当前已在上限位且还要求继续向上，直接返回
            if args.move == 'up' and top_limit_triggered:
                state["position_steps"] = 0
                save_state(args.state_file, state)
                json_ok(
                    message="已在上限位，无需继续上移",
                    stopped_by_top_limit=True,
                    position_steps=0,
                    position_um=0.0,
                    moved_steps=0
                )

            # 设置方向
            pi.write(args.dir, 1 if args.move == 'up' else 0)

            # 使能
            ena_on = 0 if args.ena_active_low else 1
            ena_off = 1 - ena_on
            pi.write(args.ena, ena_on)

            wid = None
            moved_steps = 0
            t0 = time.time()

            try:
                wid = build_step_wave(pi, args.pul, args.freq, args.pulse_width_us)

                remaining = target_steps
                stopped_by_top_limit = False

                while remaining > 0:
                    chunk = min(remaining, args.chunk_steps)

                    send_steps_chunk(pi, wid, chunk)

                    # 等当前 chunk 发完；如果上移时碰到上限位，则立即停止
                    while pi.wave_tx_busy():
                        if args.move == 'up':
                            # 既检测回调，也检测当前电平
                            if top_limit_falling or pi.read(args.top_limit) == 0:
                                pi.wave_tx_stop()
                                stopped_by_top_limit = True
                                break
                        time.sleep(0.001)

                    if stopped_by_top_limit:
                        # 上移触发上限位，位置直接清零
                        position_steps = 0
                        moved_steps = 0  # 这里不再强调本次实际走了多少，因为已经以顶端为零点
                        break

                    # 本 chunk 完整执行成功，更新位置
                    moved_steps += chunk
                    remaining -= chunk

                    if args.move == 'up':
                        position_steps = max(0, position_steps - chunk)
                    else:
                        position_steps = position_steps + chunk

                # 保存位置
                state["position_steps"] = position_steps
                save_state(args.state_file, state)

                t1 = time.time()

                json_ok(
                    message="运动完成" if not stopped_by_top_limit else "触发上限位，已停止并清零",
                    move=args.move,
                    target_steps=target_steps,
                    target_um=target_um,
                    moved_steps=moved_steps,
                    moved_um=calc_um_from_steps(moved_steps, args.steps_per_rev, args.lead_mm),
                    stopped_by_top_limit=stopped_by_top_limit,
                    top_limit_triggered=(pi.read(args.top_limit) == 0),
                    position_steps=position_steps,
                    position_um=calc_um_from_steps(position_steps, args.steps_per_rev, args.lead_mm),
                    elapsed_s=round(t1 - t0, 4)
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
    except Exception as e:
        json_fail(f"运行失败: {e}")
    finally:
        try:
            if cb is not None:
                cb.cancel()
        except Exception:
            pass
        pi.stop()


if __name__ == "__main__":
    main()