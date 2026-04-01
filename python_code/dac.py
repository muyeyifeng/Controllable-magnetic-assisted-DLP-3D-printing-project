#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import time

import smbus2


def voltage_to_dac_code(voltage: float, vref: float) -> int:
    code = int(voltage / vref * 4096)
    return max(0, min(4095, code))


def set_voltage(bus: smbus2.SMBus, addr: int, voltage: float, vref: float) -> int:
    dac = voltage_to_dac_code(voltage, vref)
    high = dac >> 4
    low = (dac & 0x0F) << 4
    bus.write_i2c_block_data(addr, 0x40, [high, low])
    print(f"DAC output set to {voltage:.3f}V (DAC={dac})")
    return dac


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Set DAC output voltage over I2C")
    parser.add_argument("--voltage", type=float, default=2.361, help="Target voltage in V (default: 2.361)")
    parser.add_argument("--hold", type=float, default=15.0, help="Hold seconds before optional reset to 0V (default: 15)")
    parser.add_argument("--vref", type=float, default=5.0, help="DAC reference voltage in V (default: 5.0)")
    parser.add_argument("--bus", type=int, default=1, help="I2C bus index (default: 1)")
    parser.add_argument("--address", type=lambda x: int(x, 0), default=0x60, help="I2C address, e.g. 0x60 (default: 0x60)")
    parser.add_argument(
        "--restore-zero",
        dest="restore_zero",
        action="store_true",
        default=True,
        help="Reset DAC to 0V after hold time (default: enabled)",
    )
    parser.add_argument(
        "--no-restore-zero",
        dest="restore_zero",
        action="store_false",
        help="Do not reset DAC to 0V after hold time",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.vref <= 0:
        raise ValueError("--vref must be > 0")
    if args.hold < 0:
        raise ValueError("--hold must be >= 0")

    bus = smbus2.SMBus(args.bus)
    try:
        set_voltage(bus, args.address, args.voltage, args.vref)
        if args.hold > 0:
            print(f"Holding for {args.hold:.3f}s...")
            time.sleep(args.hold)
        if args.restore_zero:
            set_voltage(bus, args.address, 0.0, args.vref)
            print("DAC output restored to 0V")
    except KeyboardInterrupt:
        print("Interrupted by user, forcing DAC output to 0V")
        set_voltage(bus, args.address, 0.0, args.vref)
    finally:
        bus.close()


if __name__ == "__main__":
    main()
