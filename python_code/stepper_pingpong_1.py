#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import time
import argparse
import pigpio

def pulse_steps(pi, pin_pul, steps, freq_hz, duty=0.5):
    """
    用 pigpio wave 发送指定步数的脉冲（更稳）。
    steps: 脉冲个数
    freq_hz: 脉冲频率（Hz）= steps/s
    duty: 占空比
    """
    if steps <= 0:
        return
    if freq_hz <= 0:
        raise ValueError("freq_hz must be > 0")

    period_us = int(1_000_000 / freq_hz)
    high_us = max(1, int(period_us * duty))
    low_us = max(1, period_us - high_us)

    # 一步=一个上升沿（绝大多数驱动器），这里用一个完整高低脉冲表示1步
    pulses = [
        pigpio.pulse(1 << pin_pul, 0, high_us),
        pigpio.pulse(0, 1 << pin_pul, low_us),
    ]

    pi.wave_clear()
    pi.wave_add_generic(pulses)
    wid = pi.wave_create()
    if wid < 0:
        raise RuntimeError("wave_create failed")

    # 重复 steps 次
    pi.wave_send_repeat(wid)
    time.sleep(steps * period_us / 1_000_000.0 + 0.01)
    pi.wave_tx_stop()
    pi.wave_delete(wid)

def set_enable(pi, pin_ena, enable, ena_active_low):
    """
    enable=True 表示“使能电机”
    ena_active_low=True 表示 ENA 低电平有效（你的情况）
    """
    if pin_ena is None:
        return
    if ena_active_low:
        level = 0 if enable else 1
    else:
        level = 1 if enable else 0
    pi.write(pin_ena, level)

def main():
    ap = argparse.ArgumentParser(description="Raspberry Pi stepper control (PUL/DIR/ENA)")

    ap.add_argument("--pul", type=int, default=13, help="BCM GPIO for PUL+ (default: 13)")
    ap.add_argument("--dir", type=int, default=5,  help="BCM GPIO for DIR+ (default: 5)")
    ap.add_argument("--ena", type=int, default=8,  help="BCM GPIO for ENA+ (default: 8). Use -1 to disable ENA control")

    ap.add_argument("--ena-active-low", action="store_true", help="ENA is active-low (common on many drivers)")
    ap.add_argument("--dir-invert", action="store_true", help="invert direction polarity")

    ap.add_argument("--freq", type=float, default=800, help="step frequency in Hz (steps/s). default 800")
    ap.add_argument("--steps", type=int, default=1600, help="steps to move (default 1600)")
    ap.add_argument("--pause", type=float, default=0.3, help="pause seconds at each end (default 0.3)")
    ap.add_argument("--loops", type=int, default=0, help="loops for pingpong. 0 = infinite (default 0)")
    ap.add_argument("--accel", type=int, default=0, help="simple accel steps for ramp (0=off). e.g. 200")

    # 新增：运动方向与模式
    ap.add_argument("--move", choices=["down", "up"], default="down",
                    help="direction for one-way/continuous move. default down")
    ap.add_argument("--mode", choices=["one", "pingpong", "continuous"], default="one",
                    help="one=move once, pingpong=back&forth, continuous=run forever. default one")

    args = ap.parse_args()

    pin_pul = args.pul
    pin_dir = args.dir
    pin_ena = None if args.ena < 0 else args.ena

    pi = pigpio.pi()
    if not pi.connected:
        raise RuntimeError("pigpio daemon not running. Run: sudo systemctl start pigpiod")

    # set modes
    pi.set_mode(pin_pul, pigpio.OUTPUT)
    pi.set_mode(pin_dir, pigpio.OUTPUT)
    if pin_ena is not None:
        pi.set_mode(pin_ena, pigpio.OUTPUT)

    # idle levels
    pi.write(pin_pul, 0)

    # enable motor
    set_enable(pi, pin_ena, True, args.ena_active_low)
    time.sleep(0.1)

    def write_dir(forward: bool):
        """
        forward=True -> DIR=1（默认）
        你想要的“向下/向上”由 --move 控制，这里 forward 只是内部抽象
        """
        level = 1 if forward else 0
        if args.dir_invert:
            level ^= 1
        pi.write(pin_dir, level)
        time.sleep(0.001)  # DIR setup time

    def move_with_optional_ramp(steps, base_freq):
        if args.accel <= 0 or steps <= 2 * args.accel:
            pulse_steps(pi, pin_pul, steps, base_freq)
            return

        ramp = args.accel
        f_start = max(50.0, base_freq * 0.2)

        # ramp-up
        for i in range(ramp):
            f = f_start + (base_freq - f_start) * (i + 1) / ramp
            pulse_steps(pi, pin_pul, 1, f)

        # cruise
        pulse_steps(pi, pin_pul, steps - 2 * ramp, base_freq)

        # ramp-down
        for i in range(ramp):
            f = base_freq - (base_freq - f_start) * (i + 1) / ramp
            pulse_steps(pi, pin_pul, 1, f)

    # 约定：move=down 对应 forward=False（你要“向下移动”就走这个）
    # 如果你实际方向相反，用 --dir-invert 一键翻转
    one_way_forward = (args.move == "up")

    try:
        if args.mode == "one":
            # 单向移动一次（你现在要的）
            write_dir(one_way_forward)
            move_with_optional_ramp(args.steps, args.freq)

        elif args.mode == "continuous":
            # 单向持续旋转（按 Ctrl+C 停）
            write_dir(one_way_forward)
            while True:
                pulse_steps(pi, pin_pul, 200, args.freq)  # 分段发脉冲，便于中断

        else:  # pingpong
            loop = 0
            while True:
                write_dir(True)
                move_with_optional_ramp(args.steps, args.freq)
                time.sleep(args.pause)

                write_dir(False)
                move_with_optional_ramp(args.steps, args.freq)
                time.sleep(args.pause)

                loop += 1
                if args.loops > 0 and loop >= args.loops:
                    break

    except KeyboardInterrupt:
        pass
    finally:
        set_enable(pi, pin_ena, False, args.ena_active_low)
        pi.wave_clear()
        pi.stop()

if __name__ == "__main__":
    main()