#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import random
import time
from typing import List

from smbus2 import SMBus

I2C_BUS_DEFAULT = 1
TCA9548A_ADDR_DEFAULT = 0x70
TCA9548A_CH_DEFAULT = 0
PCA9554_ADDR_DEFAULT = 0x27

PCA9554_REG_OUTPUT = 0x01
PCA9554_REG_CONFIG = 0x03


def _validate_levels(levels: List[int]) -> None:
    if len(levels) != 8:
        raise ValueError("IO 列表必须是8位")
    for i, v in enumerate(levels):
        if v not in (0, 1):
            raise ValueError(f"IO[{i}] 只能是0或1，当前为 {v}")


def _tca_select_channel(bus: SMBus, mux_addr: int, ch: int) -> None:
    if not (0 <= ch <= 7):
        raise ValueError("TCA9548A 通道必须是0~7")
    bus.write_byte(mux_addr, 1 << ch)
    time.sleep(0.01)


def _tca_disable_all(bus: SMBus, mux_addr: int) -> None:
    bus.write_byte(mux_addr, 0x00)
    time.sleep(0.01)


def _set_pca9554_io07(bus: SMBus, addr: int, io_levels: List[int]) -> None:
    _validate_levels(io_levels)

    bus.write_byte_data(addr, PCA9554_REG_CONFIG, 0x00)  # IO0~IO7 全输出

    out_new = (
        ((io_levels[0] & 0x01) << 0)
        | ((io_levels[1] & 0x01) << 1)
        | ((io_levels[2] & 0x01) << 2)
        | ((io_levels[3] & 0x01) << 3)
        | ((io_levels[4] & 0x01) << 4)
        | ((io_levels[5] & 0x01) << 5)
        | ((io_levels[6] & 0x01) << 6)
        | ((io_levels[7] & 0x01) << 7)
    )
    bus.write_byte_data(addr, PCA9554_REG_OUTPUT, out_new)


def _build_shift_patterns(width: int = 8, high_count: int = 4) -> List[List[int]]:
    if high_count <= 0 or high_count > width:
        raise ValueError("high_count 必须在 1~width")

    base = [1] * high_count + [0] * (width - high_count)
    patterns: List[List[int]] = []
    for shift in range(width):
        patterns.append(base[-shift:] + base[:-shift] if shift else base[:])
    return patterns


def _fmt_levels(levels: List[int]) -> str:
    return ",".join(str(x) for x in levels)


def run_io_shift_test(
    *,
    bus_id: int,
    tca_addr: int,
    tca_channel: int,
    pca_addr: int,
    min_wait: float,
    max_wait: float,
    cycles: int,
) -> None:
    if min_wait <= 0 or max_wait <= 0:
        raise ValueError("等待时间必须大于0")
    if min_wait > max_wait:
        raise ValueError("min_wait 不能大于 max_wait")
    if cycles < 0:
        raise ValueError("cycles 不能小于0")

    patterns = _build_shift_patterns(width=8, high_count=4)
    step_idx = 0
    total_steps = cycles * len(patterns) if cycles > 0 else None

    print("[INFO] IO 子设备轮转测试启动（仅IO，不写DAC）")
    print(f"[INFO] bus={bus_id}, tca=0x{tca_addr:02X}, ch={tca_channel}, pca=0x{pca_addr:02X}")
    print(f"[INFO] 每步等待: {min_wait:.2f}~{max_wait:.2f}s")
    print("[INFO] 停止: Ctrl+C")

    try:
        with SMBus(bus_id) as bus:
            _tca_select_channel(bus, tca_addr, tca_channel)
            try:
                while True:
                    for levels in patterns:
                        step_idx += 1
                        _set_pca9554_io07(bus, pca_addr, levels)
                        print(f"[STEP {step_idx}] --io27 {_fmt_levels(levels)}")

                        if total_steps is not None and step_idx >= total_steps:
                            return

                        wait_s = random.uniform(min_wait, max_wait)
                        time.sleep(wait_s)
            finally:
                _tca_disable_all(bus, tca_addr)
    except KeyboardInterrupt:
        print("\n[INFO] 用户中断测试")


def main() -> None:
    parser = argparse.ArgumentParser(description="PCA9554 IO轮转测试（仅IO，不输出DAC）")
    parser.add_argument("--bus", type=int, default=I2C_BUS_DEFAULT, help="I2C bus，默认1")
    parser.add_argument("--tca", type=lambda x: int(x, 0), default=TCA9548A_ADDR_DEFAULT, help="TCA地址，默认0x70")
    parser.add_argument("--ch", type=int, default=TCA9548A_CH_DEFAULT, help="TCA通道，默认0")
    parser.add_argument("--pca", type=lambda x: int(x, 0), default=PCA9554_ADDR_DEFAULT, help="PCA9554地址，默认0x27")
    parser.add_argument("--min-wait", type=float, default=1.0, help="每步最小等待秒数，默认1.0")
    parser.add_argument("--max-wait", type=float, default=2.0, help="每步最大等待秒数，默认2.0")
    parser.add_argument("--cycles", type=int, default=0, help="循环轮数，0表示无限循环")

    args = parser.parse_args()

    run_io_shift_test(
        bus_id=args.bus,
        tca_addr=args.tca,
        tca_channel=args.ch,
        pca_addr=args.pca,
        min_wait=args.min_wait,
        max_wait=args.max_wait,
        cycles=args.cycles,
    )


if __name__ == "__main__":
    main()
