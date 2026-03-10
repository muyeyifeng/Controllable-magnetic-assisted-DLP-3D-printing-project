#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from smbus2 import SMBus
import time

I2C_BUS = 1          # 树莓派默认 I2C-1
TCA_ADDR = 0x70     # TCA9548A 固定地址
ADDR_START = 0x03
ADDR_END = 0x77


def select_channel(bus, ch):
    """
    选择 TCA9548A 子通道
    ch: 0~7
    """
    bus.write_byte(TCA_ADDR, 1 << ch)
    time.sleep(0.01)


def disable_all(bus):
    """关闭所有通道"""
    bus.write_byte(TCA_ADDR, 0x00)
    time.sleep(0.01)


def probe(bus, addr):
    """
    探测 I2C 地址是否存在
    用 read_byte 比 write_quick 兼容性更好
    """
    try:
        bus.read_byte(addr)
        return True
    except:
        return False


def main():
    print("=== TCA9548A Sub-bus Scanner ===")
    print(f"TCA9548A address: 0x{TCA_ADDR:02X}")
    print(f"I2C bus: {I2C_BUS}\n")

    with SMBus(I2C_BUS) as bus:
        # 先确认 TCA 存在
        if not probe(bus, TCA_ADDR):
            print("ERROR: 没有检测到 TCA9548A @0x70")
            return

        for ch in range(8):
            print(f"---- Select CH{ch} ----")
            select_channel(bus, ch)

            found = []
            for addr in range(ADDR_START, ADDR_END + 1):
                if probe(bus, addr):
                    found.append(addr)

            if found:
                print("Found:", " ".join([f"0x{a:02X}" for a in found]))
            else:
                print("Found: none")
            print()

        disable_all(bus)
        print("All channels disabled.")


if __name__ == "__main__":
    main()
