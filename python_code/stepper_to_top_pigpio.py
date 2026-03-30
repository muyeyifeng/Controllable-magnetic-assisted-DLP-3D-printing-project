#!/usr/bin/env python3
import argparse
import sys
import time

import pigpio


parser = argparse.ArgumentParser()

# Motor pins
parser.add_argument('--pul', type=int, default=13, help='STEP/PUL pin (BCM)')
parser.add_argument('--dir', type=int, default=5, help='DIR pin (BCM)')
parser.add_argument('--ena', type=int, default=8, help='ENA pin (BCM)')

# Motion
parser.add_argument('--freq', type=int, default=1600, help='step frequency in Hz')
parser.add_argument('--move', type=str, choices=['up', 'down'], default='up', help='move direction')
parser.add_argument('--ena-active-low', action='store_true', help='enable active low')

# Top sensor
parser.add_argument('--top-pin', type=int, default=21, help='top sensor pin (BCM)')
parser.add_argument('--stop-level', type=int, choices=[0, 1], default=1, help='stop when top-pin becomes this level')
parser.add_argument('--pull', type=str, choices=['up', 'down', 'none'], default='none', help='internal pull for top-pin')

# Mechanics
parser.add_argument('--steps-per-rev', type=int, default=3200, help='steps per revolution')
parser.add_argument('--lead-mm', type=float, default=4.0, help='lead screw travel per rev, mm')

# Protection / quality
parser.add_argument('--max-steps', type=int, default=0, help='max steps safety limit, 0 means unlimited')
parser.add_argument('--pulse-width-us', type=int, default=20, help='STEP pulse width in microseconds')
parser.add_argument('--chunk-steps', type=int, default=200, help='steps per wave chunk before checking top sensor')
parser.add_argument('--report-every', type=int, default=1000, help='print progress every N steps')

args = parser.parse_args()


def step_um() -> float:
    return args.lead_mm * 1000.0 / args.steps_per_rev


def motor_enable(pi: pigpio.pi, enable: bool) -> None:
    ena_on = 0 if args.ena_active_low else 1
    ena_off = 1 - ena_on
    pi.write(args.ena, ena_on if enable else ena_off)


def set_direction(pi: pigpio.pi, move: str) -> None:
    pi.write(args.dir, 1 if move == 'up' else 0)


def configure_input(pi: pigpio.pi, pin: int, pull: str) -> None:
    pi.set_mode(pin, pigpio.INPUT)
    if pull == 'up':
        pi.set_pull_up_down(pin, pigpio.PUD_UP)
    elif pull == 'down':
        pi.set_pull_up_down(pin, pigpio.PUD_DOWN)
    else:
        pi.set_pull_up_down(pin, pigpio.PUD_OFF)


def build_step_wave(pi: pigpio.pi, step_pin: int, freq: int, pulse_width_us: int) -> int:
    period_us = int(round(1_000_000 / freq))
    if pulse_width_us >= period_us:
        raise ValueError(
            f"pulse-width-us={pulse_width_us} must be < period {period_us} us at {freq} Hz"
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
        raise RuntimeError('wave_create failed')
    return wid


def send_steps(pi: pigpio.pi, wid: int, total_steps: int) -> None:
    remaining = total_steps
    while remaining > 0:
        chunk = min(remaining, 65535)
        x = chunk & 0xFF
        y = (chunk >> 8) & 0xFF
        chain = [255, 0, wid, 255, 1, x, y]
        pi.wave_chain(chain)
        while pi.wave_tx_busy():
            time.sleep(0.001)
        remaining -= chunk


def main() -> None:
    pi = pigpio.pi()
    if not pi.connected:
        print('pigpio not connected, run: sudo systemctl start pigpiod')
        sys.exit(1)

    wid = None
    try:
        # GPIO init
        pi.set_mode(args.pul, pigpio.OUTPUT)
        pi.set_mode(args.dir, pigpio.OUTPUT)
        pi.set_mode(args.ena, pigpio.OUTPUT)
        configure_input(pi, args.top_pin, args.pull)

        if args.freq <= 0:
            raise ValueError('--freq must be > 0')
        if args.chunk_steps <= 0:
            raise ValueError('--chunk-steps must be > 0')

        su = step_um()

        print('===== Move to top (wave_chain mode) =====')
        print(f'direction            : {args.move}')
        print(f'frequency            : {args.freq} Hz')
        print(f'top sensor pin       : GPIO{args.top_pin}')
        print(f'stop level           : {args.stop_level}')
        print(f'step length          : {su:.6f} um/step')
        print(f'max steps            : {args.max_steps} (0 means unlimited)')
        print(f'chunk steps          : {args.chunk_steps}')
        print()

        initial_level = pi.read(args.top_pin)
        print(f'initial GPIO{args.top_pin} = {initial_level}')
        if initial_level == args.stop_level:
            print('already at top trigger, no movement required')
            print('distance = 0 step = 0 um')
            return

        set_direction(pi, args.move)
        motor_enable(pi, True)

        wid = build_step_wave(pi, args.pul, args.freq, args.pulse_width_us)

        steps = 0
        next_report = max(1, args.report_every)
        t0 = time.time()

        while True:
            if args.max_steps > 0:
                remain = args.max_steps - steps
                if remain <= 0:
                    print()
                    print('[WARN] reached max-steps safety limit, stop')
                    print(f'steps                : {steps} step')
                    print(f'distance             : {steps * su:.3f} um')
                    return
                send_chunk = min(args.chunk_steps, remain)
            else:
                send_chunk = args.chunk_steps

            send_steps(pi, wid, send_chunk)
            steps += send_chunk

            level = pi.read(args.top_pin)
            if level == args.stop_level:
                t1 = time.time()
                distance_um = steps * su
                print()
                print(f'[TRIGGER] GPIO{args.top_pin} = {level}, stop')
                print(f'steps                : {steps} step')
                print(f'distance             : {distance_um:.3f} um')
                print(f'distance             : {distance_um / 1000.0:.6f} mm')
                print(f'elapsed              : {t1 - t0:.4f} s')
                return

            if steps >= next_report:
                sys.stdout.write(f'\rprogress: {steps} step | GPIO{args.top_pin}={level}')
                sys.stdout.flush()
                next_report += args.report_every

    except KeyboardInterrupt:
        print('\nInterrupted by user')

    finally:
        try:
            pi.wave_tx_stop()
        except Exception:
            pass
        try:
            if wid is not None:
                pi.wave_delete(wid)
            pi.wave_clear()
        except Exception:
            pass
        try:
            motor_enable(pi, False)
        except Exception:
            pass
        pi.stop()


if __name__ == '__main__':
    main()
