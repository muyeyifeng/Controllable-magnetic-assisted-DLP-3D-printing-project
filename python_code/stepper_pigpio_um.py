#!/usr/bin/env python3
import pigpio
import time
import argparse
import sys
import math

parser = argparse.ArgumentParser()

# 引脚
parser.add_argument('--pul', type=int, default=13, help='PUL/STEP 引脚 BCM 编号')
parser.add_argument('--dir', type=int, default=5, help='DIR 引脚 BCM 编号')
parser.add_argument('--ena', type=int, default=8, help='ENA 引脚 BCM 编号')

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

args = parser.parse_args()


def calc_steps_from_um(distance_um: float, steps_per_rev: int, lead_mm: float) -> int:
    lead_um = lead_mm * 1000.0
    return int(round(distance_um * steps_per_rev / lead_um))


def calc_um_from_steps(steps: int, steps_per_rev: int, lead_mm: float) -> float:
    lead_um = lead_mm * 1000.0
    return steps * lead_um / steps_per_rev


def build_step_wave(pi: pigpio.pi, step_pin: int, freq: int, pulse_width_us: int):
    """
    创建一个 step 周期波形：
    - 高电平 pulse_width_us
    - 低电平补足整个周期
    """
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


def send_steps(pi: pigpio.pi, wid: int, total_steps: int):
    """
    用 wave_chain 发送指定步数。
    pigpio 的 loop 次数是 16 位，因此超过 65535 要分块。
    """
    remaining = total_steps

    while remaining > 0:
        chunk = min(remaining, 65535)

        x = chunk & 0xFF
        y = (chunk >> 8) & 0xFF

        chain = [
            255, 0, wid,       # 发送 wid
            255, 1, x, y       # 重复 chunk 次
        ]

        pi.wave_chain(chain)

        while pi.wave_tx_busy():
            time.sleep(0.001)

        remaining -= chunk


def main():
    if args.steps is None and args.um is None:
        print("错误：必须指定 --steps 或 --um 其中一个")
        sys.exit(1)

    if args.steps is not None:
        steps = args.steps
        distance_um = calc_um_from_steps(steps, args.steps_per_rev, args.lead_mm)
    else:
        distance_um = args.um
        steps = calc_steps_from_um(distance_um, args.steps_per_rev, args.lead_mm)

    if steps <= 0:
        print(f"错误：计算得到步数为 {steps}，请检查输入")
        sys.exit(1)

    # 显示换算关系
    step_um = args.lead_mm * 1000.0 / args.steps_per_rev

    print("===== 参数信息 =====")
    print(f"方向: {args.move}")
    print(f"频率: {args.freq} Hz")
    print(f"每步位移: {step_um:.6f} um/step")
    print(f"目标位移: {distance_um:.3f} um")
    print(f"目标步数: {steps} steps")
    print(f"脉宽: {args.pulse_width_us} us")
    print()

    pi = pigpio.pi()
    if not pi.connected:
        print("连接 pigpio 失败。请先启动：sudo systemctl start pigpiod")
        sys.exit(1)

    try:
        # 设置引脚模式
        pi.set_mode(args.pul, pigpio.OUTPUT)
        pi.set_mode(args.dir, pigpio.OUTPUT)
        pi.set_mode(args.ena, pigpio.OUTPUT)

        # 方向
        pi.write(args.dir, 1 if args.move == 'up' else 0)

        # 使能
        ena_on = 0 if args.ena_active_low else 1
        ena_off = 1 - ena_on
        pi.write(args.ena, ena_on)

        # 构建波形
        wid = build_step_wave(pi, args.pul, args.freq, args.pulse_width_us)

        t0 = time.time()
        send_steps(pi, wid, steps)
        t1 = time.time()

        # 清理波形
        pi.wave_delete(wid)

        # 关闭使能
        pi.write(args.ena, ena_off)

        print("运动完成")
        print(f"理论位移: {steps * step_um:.3f} um")
        print(f"耗时: {t1 - t0:.4f} s")

    except KeyboardInterrupt:
        print("\n用户中断")
        try:
            pi.wave_tx_stop()
        except:
            pass
        try:
            pi.write(args.ena, 1 - (0 if args.ena_active_low else 1))
        except:
            pass

    except Exception as e:
        print(f"运行失败: {e}")
        try:
            pi.wave_tx_stop()
        except:
            pass
        try:
            pi.write(args.ena, 1 - (0 if args.ena_active_low else 1))
        except:
            pass
        sys.exit(1)

    finally:
        try:
            pi.wave_clear()
        except:
            pass
        pi.stop()


if __name__ == "__main__":
    main()