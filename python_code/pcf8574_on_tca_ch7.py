#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import time
from smbus2 import SMBus

I2C_BUS  = 1
TCA_ADDR = 0x70
TCA_CH   = 7          # 固定 CH7
PCF_ADDR = 0x25

MASK_P1_P4 = 0b00001111  # bit1~bit4


def tca_select(bus: SMBus, ch: int):
    bus.write_byte(TCA_ADDR, 1 << ch)
    time.sleep(0.005)


def tca_disable_all(bus: SMBus):
    bus.write_byte(TCA_ADDR, 0x00)
    time.sleep(0.005)


def write_byte(bus: SMBus, value: int):
    """直接写 8-bit 到 PCF8574"""
    bus.write_byte(PCF_ADDR, value & 0xFF)


def set_p1_p4(bus: SMBus, p1: int, p2: int, p3: int, p4: int):
    """
    只修改 P1~P4，其它位保持不变（读-改-写）
    注意：PCF8574 写 1 是“释放为高/输入”，写 0 才是强拉低输出
    """
    current = bus.read_byte(PCF_ADDR) & 0xFF

    new_bits = ((p1 & 1) << 0) | ((p2 & 1) << 1) | ((p3 & 1) << 2) | ((p4 & 1) << 3)
    new_val = (current & (~MASK_P1_P4 & 0xFF)) | new_bits

    bus.write_byte(PCF_ADDR, new_val)
    return current, new_val


def parse_int_auto(x: str) -> int:
    # 支持 0x1E / 30 / 0b00011110
    return int(x, 0)


def main():
    ap = argparse.ArgumentParser(description="PCF8574(0x25) via TCA9548A(0x70) CH7 write tool")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--byte", type=parse_int_auto, help="直接写一个byte，例如 0x1E 或 0b00011110")
    g.add_argument("--p", nargs=4, type=int, metavar=("P1", "P2", "P3", "P4"),
                   help="只设置 P1~P4 (0/1)，其它位保持不变，例如 --p 1 0 1 1")

    ap.add_argument("--keep", action="store_true", help="执行后不关闭TCA通道（默认会关闭所有通道）")
    args = ap.parse_args()

    with SMBus(I2C_BUS) as bus:
        # 选通 CH7
        tca_select(bus, TCA_CH)

        # 执行写操作
        if args.byte is not None:
            v = args.byte & 0xFF
            write_byte(bus, v)
            print(f"CH{TCA_CH} PCF@0x{PCF_ADDR:02X} write byte: 0x{v:02X} ({v:08b})")
        else:
            p1, p2, p3, p4 = args.p
            oldv, newv = set_p1_p4(bus, p1, p2, p3, p4)
            print(f"CH{TCA_CH} PCF@0x{PCF_ADDR:02X} old: 0x{oldv:02X} ({oldv:08b})")
            print(f"CH{TCA_CH} PCF@0x{PCF_ADDR:02X} new: 0x{newv:02X} ({newv:08b})")

        # 默认关闭所有通道，避免影响其它设备
        if not args.keep:
            tca_disable_all(bus)


if __name__ == "__main__":
    main()
