#!/usr/bin/env python3
import pigpio
import time
import argparse
import sys

parser = argparse.ArgumentParser()

# 电机引脚
parser.add_argument('--pul', type=int, default=13, help='STEP/PUL pin (BCM)')
parser.add_argument('--dir', type=int, default=5, help='DIR pin (BCM)')
parser.add_argument('--ena', type=int, default=8, help='ENA pin (BCM)')

# 方向与速度
parser.add_argument('--freq', type=int, default=1600, help='step frequency in Hz')
parser.add_argument('--move', type=str, choices=['up', 'down'], default='up', help='move direction')
parser.add_argument('--ena-active-low', action='store_true', help='enable active low')

# 顶端检测
parser.add_argument('--top-pin', type=int, default=21, help='top sensor pin (BCM)')
parser.add_argument('--stop-level', type=int, choices=[0, 1], default=1,
                    help='stop when top-pin becomes this level')
parser.add_argument('--pull', type=str, choices=['up', 'down', 'none'], default='none',
                    help='internal pull for top-pin')

# 机械参数
parser.add_argument('--steps-per-rev', type=int, default=3200, help='steps per revolution')
parser.add_argument('--lead-mm', type=float, default=4.0, help='lead screw travel per rev, mm')

# 保护参数
parser.add_argument('--max-steps', type=int, default=50000, help='max steps as safety limit')
parser.add_argument('--pulse-width-us', type=int, default=20, help='STEP pulse width in microseconds')
parser.add_argument('--report-every', type=int, default=1000, help='print progress every N steps')

args = parser.parse_args()


def step_um():
    return args.lead_mm * 1000.0 / args.steps_per_rev


def motor_enable(pi, enable: bool):
    ena_on = 0 if args.ena_active_low else 1
    ena_off = 1 - ena_on
    pi.write(args.ena, ena_on if enable else ena_off)


def set_direction(pi, move: str):
    pi.write(args.dir, 1 if move == 'up' else 0)


def configure_input(pi, pin: int, pull: str):
    pi.set_mode(pin, pigpio.INPUT)
    if pull == 'up':
        pi.set_pull_up_down(pin, pigpio.PUD_UP)
    elif pull == 'down':
        pi.set_pull_up_down(pin, pigpio.PUD_DOWN)
    else:
        pi.set_pull_up_down(pin, pigpio.PUD_OFF)


def pulse_once(pi, step_pin: int, period_us: int, pulse_width_us: int):
    low_us = period_us - pulse_width_us
    if low_us < 1:
        raise ValueError(
            f"pulse-width-us={pulse_width_us} 太大，当前频率 {args.freq} Hz 下一个周期只有 {period_us} us"
        )

    pi.write(step_pin, 1)
    time.sleep(pulse_width_us / 1_000_000.0)
    pi.write(step_pin, 0)
    time.sleep(low_us / 1_000_000.0)


def main():
    pi = pigpio.pi()
    if not pi.connected:
        print("连接 pigpio 失败，请先运行：sudo systemctl start pigpiod")
        sys.exit(1)

    try:
        # GPIO 初始化
        pi.set_mode(args.pul, pigpio.OUTPUT)
        pi.set_mode(args.dir, pigpio.OUTPUT)
        pi.set_mode(args.ena, pigpio.OUTPUT)
        configure_input(pi, args.top_pin, args.pull)

        # 参数
        su = step_um()
        period_us = int(round(1_000_000 / args.freq))

        print("===== 测试：当前位置到顶端距离 =====")
        print(f"方向                : {args.move}")
        print(f"频率                : {args.freq} Hz")
        print(f"顶端检测引脚        : GPIO{args.top_pin}")
        print(f"停止电平            : {args.stop_level}")
        print(f"每步位移            : {su:.6f} um/step")
        print(f"最大保护步数        : {args.max_steps}")
        print()

        initial_level = pi.read(args.top_pin)
        print(f"启动前 GPIO{args.top_pin} = {initial_level}")

        if initial_level == args.stop_level:
            print("当前已经处于触发状态，认为已到顶端。")
            print("距离 = 0 step = 0 um")
            return

        # 启动电机
        set_direction(pi, args.move)
        motor_enable(pi, True)

        steps = 0
        t0 = time.time()

        while True: #steps < args.max_steps:
            pulse_once(pi, args.pul, period_us, args.pulse_width_us)
            steps += 1

            level = pi.read(args.top_pin)

            if level == args.stop_level:
                t1 = time.time()
                distance_um = steps * su
                distance_mm = distance_um / 1000.0

                print()
                print(f"[触发] GPIO{args.top_pin} = {level}，停止")
                print(f"总步数              : {steps} step")
                print(f"对应位移            : {distance_um:.3f} um")
                print(f"对应位移            : {distance_mm:.6f} mm")
                print(f"耗时                : {t1 - t0:.4f} s")
                return

            if steps % args.report_every == 0:
                sys.stdout.write(
                    f"\r进度: {steps} step | GPIO{args.top_pin}={level}"
                )
                sys.stdout.flush()

        print()
        print("[警告] 达到最大保护步数，仍未检测到顶端信号。")
        print(f"已走步数            : {steps} step")
        print(f"对应位移            : {steps * su:.3f} um")

    except KeyboardInterrupt:
        print("\n用户中断。")

    finally:
        try:
            motor_enable(pi, False)
        except:
            pass
        pi.stop()


if __name__ == "__main__":
    main()