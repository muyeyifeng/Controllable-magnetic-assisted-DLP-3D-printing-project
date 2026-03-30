#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import glob
import os
import sys
import time

try:
    from smbus2 import SMBus, i2c_msg
except ImportError:
    print("缺少依赖 smbus2。可执行：sudo apt-get install -y python3-smbus 或 pip3 install smbus2")
    sys.exit(1)


DEFAULT_START = 0x03
DEFAULT_END = 0x77


def list_i2c_buses():
    """返回系统存在的 i2c 总线号列表，如 [0, 1]"""
    buses = []
    for path in glob.glob("/dev/i2c-*"):
        try:
            buses.append(int(path.split("-")[-1]))
        except ValueError:
            pass
    return sorted(set(buses))


def probe_addr(bus: SMBus, addr: int, read_probe: bool = False) -> bool:
    """
    探测地址是否存在。
    - 默认用 write_quick（不带数据写）方式探测（快且常用）
    - read_probe=True 时改用 “读取 1 字节” 的方式探测（有些设备对 quick 不响应）
    """
    try:
        if read_probe:
            # 读 1 字节（不少设备会 ACK）
            bus.read_byte(addr)
        else:
            # quick write：不写数据，只测试 ACK
            bus.write_quick(addr)
        return True
    except OSError:
        return False


def scan_bus(bus_id: int, start: int, end: int, read_probe: bool = False):
    found = []
    with SMBus(bus_id) as bus:
        for addr in range(start, end + 1):
            if probe_addr(bus, addr, read_probe=read_probe):
                found.append(addr)
    return found


def tca_select_channel(bus: SMBus, mux_addr: int, ch: int):
    """
    TCA9548A 选择通道：写一个字节，bit 位表示通道
    ch: 0~7
    """
    if not (0 <= ch <= 7):
        raise ValueError("TCA9548A 通道必须是 0~7")
    bus.write_byte(mux_addr, 1 << ch)
    # 给一点时间让总线稳定
    time.sleep(0.01)


def tca_disable_all(bus: SMBus, mux_addr: int):
    """关闭所有通道"""
    bus.write_byte(mux_addr, 0x00)
    time.sleep(0.01)


def scan_via_tca(bus_id: int, mux_addr: int, channels, start: int, end: int, read_probe: bool = False):
    """
    通过 TCA9548A 扫描每个子通道上的设备
    返回 dict: {channel: [addr, ...], ...}
    """
    results = {}
    with SMBus(bus_id) as bus:
        # 先确认 mux 自己是否存在
        if not probe_addr(bus, mux_addr, read_probe=read_probe):
            raise RuntimeError(f"在 I2C-{bus_id} 上未检测到 TCA9548A @0x{mux_addr:02X}")

        for ch in channels:
            tca_select_channel(bus, mux_addr, ch)
            found = []
            for addr in range(start, end + 1):
                # 注意：这里也会“看到”mux_addr 本身吗？通常不会（因为 mux 在主总线上）
                if probe_addr(bus, addr, read_probe=read_probe):
                    found.append(addr)
            results[ch] = found

        tca_disable_all(bus, mux_addr)

    return results


def fmt_addrs(addrs):
    if not addrs:
        return "(none)"
    return " ".join([f"0x{a:02X}" for a in addrs])


def main():
    ap = argparse.ArgumentParser(description="Raspberry Pi I2C 设备扫描（支持 TCA9548A 子通道扫描）")
    ap.add_argument("-b", "--bus", type=int, default=None, help="指定总线号（例如 1）；不指定则扫描所有 /dev/i2c-*")
    ap.add_argument("--start", type=lambda x: int(x, 0), default=DEFAULT_START, help="起始地址，默认 0x03")
    ap.add_argument("--end", type=lambda x: int(x, 0), default=DEFAULT_END, help="结束地址，默认 0x77")
    ap.add_argument("--read-probe", action="store_true", help="用 read_byte 探测（有些设备不响应 write_quick）")

    # TCA9548A 相关
    ap.add_argument("--tca", type=lambda x: int(x, 0), default=0x70, help="TCA9548A 地址（例如 0x70）；指定后扫描子通道")
    ap.add_argument("--ch", default="0-7", help="TCA 通道范围，如 0-7 或 0,1,4")

    args = ap.parse_args()

    if args.start < 0x00 or args.end > 0x7F or args.start > args.end:
        print("地址范围不合法，请确保 0x00~0x7F 且 start<=end")
        sys.exit(2)

    buses = [args.bus] if args.bus is not None else list_i2c_buses()
    if not buses:
        print("未找到 /dev/i2c-*，请确认已启用 I2C（raspi-config）并加载 i2c-dev")
        sys.exit(2)

    def parse_channels(ch_str):
        ch_str = ch_str.strip()
        if "," in ch_str:
            return sorted(set(int(x) for x in ch_str.split(",") if x.strip() != ""))
        if "-" in ch_str:
            a, b = ch_str.split("-", 1)
            a, b = int(a), int(b)
            if a > b:
                a, b = b, a
            return list(range(a, b + 1))
        return [int(ch_str)]

    if args.tca is not None:
        channels = parse_channels(args.ch)
        for bus_id in buses:
            print(f"\n=== Scan via TCA9548A @0x{args.tca:02X} on I2C-{bus_id} (channels: {channels}) ===")
            try:
                results = scan_via_tca(
                    bus_id=bus_id,
                    mux_addr=args.tca,
                    channels=channels,
                    start=args.start,
                    end=args.end,
                    read_probe=args.read_probe,
                )
                for ch in channels:
                    print(f"  CH{ch}: {fmt_addrs(results.get(ch, []))}")
            except Exception as e:
                print(f"  [ERROR] {e}")
        return

    # 普通扫描
    for bus_id in buses:
        print(f"\n=== Scan I2C-{bus_id} addr 0x{args.start:02X}~0x{args.end:02X} ===")
        try:
            found = scan_bus(bus_id, args.start, args.end, read_probe=args.read_probe)
            print(f"  Found ({len(found)}): {fmt_addrs(found)}")
        except FileNotFoundError:
            print(f"  [ERROR] /dev/i2c-{bus_id} 不存在")
        except PermissionError:
            print(f"  [ERROR] 权限不足：请用 sudo 运行，或把用户加入 i2c 组（sudo usermod -aG i2c $USER）")
        except Exception as e:
            print(f"  [ERROR] {e}")


if __name__ == "__main__":
    main()
