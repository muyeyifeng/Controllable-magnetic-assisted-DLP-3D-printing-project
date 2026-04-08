#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# 满量程36V对应0-5V DAC输出，0.1V步进约等于0.72V磁场变化

import argparse
import time
from typing import Any, Dict, List, Optional

from smbus2 import SMBus

I2C_BUS_DEFAULT = 1
TCA9548A_ADDR_DEFAULT = 0x70
TCA9548A_CH_DEFAULT = 0

GPIO_PIN_DEFAULT = 27

PCA9554_ADDR = 0x27
MCP4725_ADDR = 0x60

PCA9554_REG_OUTPUT = 0x01
PCA9554_REG_CONFIG = 0x03


class GPIOController:
    """优先使用 lgpio，失败则回退到 pigpio。"""

    def __init__(self, pin: int):
        self.pin = pin
        self.backend = None
        self.handle = None
        self.pi = None

    def open(self) -> None:
        try:
            import lgpio  # type: ignore

            h = lgpio.gpiochip_open(0)
            lgpio.gpio_claim_output(h, self.pin, 1)
            self.backend = "lgpio"
            self.handle = h
            return
        except Exception:
            pass

        try:
            import pigpio  # type: ignore

            pi = pigpio.pi()
            if not pi.connected:
                raise RuntimeError("pigpio 未连接，请先启动 pigpiod")
            pi.set_mode(self.pin, pigpio.OUTPUT)
            self.backend = "pigpio"
            self.pi = pi
            return
        except Exception as e:
            raise RuntimeError(f"GPIO 初始化失败(pin={self.pin}): {e}")

    def write(self, level: int) -> None:
        if level not in (0, 1):
            raise ValueError("GPIO 电平必须是 0 或 1")

        if self.backend == "lgpio":
            import lgpio  # type: ignore

            lgpio.gpio_write(self.handle, self.pin, level)
            return

        if self.backend == "pigpio":
            self.pi.write(self.pin, level)
            return

        raise RuntimeError("GPIO 尚未初始化")

    def close(self) -> None:
        try:
            if self.backend == "lgpio" and self.handle is not None:
                import lgpio  # type: ignore

                lgpio.gpiochip_close(self.handle)
            elif self.backend == "pigpio" and self.pi is not None:
                self.pi.stop()
        finally:
            self.backend = None
            self.handle = None
            self.pi = None


def _validate_io_levels(levels: List[int], name: str, width: int) -> None:
    if len(levels) != width:
        raise ValueError(f"{name} 必须是长度为{width}的列表")
    for i, v in enumerate(levels):
        if v not in (0, 1):
            raise ValueError(f"{name}[{i}] 只能是0或1，当前为 {v}")


def _validate_voltage(voltage: float, vref: float) -> None:
    if vref <= 0:
        raise ValueError("vref 必须大于0")
    if not (0 <= voltage <= vref):
        raise ValueError(f"voltage 超出范围：0~{vref}V，当前为 {voltage}V")


def _validate_hold_seconds(hold_seconds: float) -> float:
    s = float(hold_seconds)
    if s < 0:
        raise ValueError("hold_seconds 不能小于0")
    return round(s, 1)


def _tca_select_channel(bus: SMBus, mux_addr: int, ch: int) -> None:
    if not (0 <= ch <= 7):
        raise ValueError("TCA9548A 通道必须是0~7")
    bus.write_byte(mux_addr, 1 << ch)
    time.sleep(0.01)


def _tca_disable_all(bus: SMBus, mux_addr: int) -> None:
    bus.write_byte(mux_addr, 0x00)
    time.sleep(0.01)


def _set_pca9554_io07(bus: SMBus, addr: int, io_levels: List[int]) -> Dict[str, Any]:
    _validate_io_levels(io_levels, f"PCA9554@0x{addr:02X}", 8)

    result: Dict[str, Any] = {
        "device": f"PCA9554@0x{addr:02X}",
        "success": False,
        "config_ok": False,
        "output_ok": False,
        "verify_ok": False,
    }

    try:
        cfg_old = bus.read_byte_data(addr, PCA9554_REG_CONFIG)
        out_old = bus.read_byte_data(addr, PCA9554_REG_OUTPUT)

        # IO0~IO7 全部配置为输出。
        cfg_new = 0x00
        bus.write_byte_data(addr, PCA9554_REG_CONFIG, cfg_new)
        cfg_readback = bus.read_byte_data(addr, PCA9554_REG_CONFIG)
        result["config_ok"] = (cfg_readback == cfg_new)

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
        out_readback = bus.read_byte_data(addr, PCA9554_REG_OUTPUT)
        result["output_ok"] = (out_readback == out_new)

        result["verify_ok"] = result["config_ok"] and result["output_ok"]
        result["success"] = result["verify_ok"]
        result.update(
            {
                "requested_io07": io_levels,
                "config_old": cfg_old,
                "config_new": cfg_new,
                "config_readback": cfg_readback,
                "output_old": out_old,
                "output_new": out_new,
                "output_readback": out_readback,
            }
        )
    except Exception as e:
        result["error"] = str(e)

    return result


def _set_mcp4725_voltage(bus: SMBus, addr: int, voltage: float, vref: float) -> Dict[str, Any]:
    _validate_voltage(voltage, vref)

    result: Dict[str, Any] = {
        "device": f"MCP4725@0x{addr:02X}",
        "success": False,
    }

    try:
        dac = int(voltage / vref * 4095)
        dac = max(0, min(4095, dac))

        high = (dac >> 4) & 0xFF
        low = (dac & 0x0F) << 4

        bus.write_i2c_block_data(addr, 0x40, [high, low])

        result.update(
            {
                "success": True,
                "voltage_requested": voltage,
                "vref": vref,
                "dac_code": dac,
                "tx_bytes": [0x40, high, low],
            }
        )
    except Exception as e:
        result["error"] = str(e)

    return result


def _hold_with_interrupt(hold_seconds: float) -> None:
    left = hold_seconds
    while left > 0:
        sl = min(0.05, left)
        time.sleep(sl)
        left -= sl


def _best_effort_zero_dac(
    i2c_bus: int,
    tca_addr: int,
    tca_channel: int,
    vref: float,
) -> Dict[str, Any]:
    try:
        with SMBus(i2c_bus) as bus:
            _tca_select_channel(bus, tca_addr, tca_channel)
            try:
                return _set_mcp4725_voltage(bus, MCP4725_ADDR, 0.0, vref)
            finally:
                try:
                    _tca_disable_all(bus, tca_addr)
                except Exception:
                    pass
    except Exception as e:
        return {
            "device": f"MCP4725@0x{MCP4725_ADDR:02X}",
            "success": False,
            "error": str(e),
        }


def execute_magnet_sequence(
    io_23: Optional[List[int]],
    io_27: List[int],
    dac_voltage: float,
    hold_seconds: float,
    *,
    gpio_pin: int = GPIO_PIN_DEFAULT,
    i2c_bus: int = I2C_BUS_DEFAULT,
    tca_addr: int = TCA9548A_ADDR_DEFAULT,
    tca_channel: int = TCA9548A_CH_DEFAULT,
    vref: float = 5.0,
) -> Dict[str, Any]:
    """
    执行时序:
    1) GPIO27 拉低
    2) 选通 TCA9548A 指定通道
    3) 设置 0x27 的 IO0~IO7
    4) 设置 0x60 DAC 到目标电压
    5) 保持 N 秒(0.1s 精度)
    6) 结束时 DAC 归零
    7) GPIO27 拉高

    安全策略:
    - 若中断/异常，仍会尝试 DAC=0V 且 GPIO27=高。
    """
    resolved_io07 = resolve_io07_levels(io_23, io_27)
    _validate_voltage(dac_voltage, vref)
    hold_s = _validate_hold_seconds(hold_seconds)

    result: Dict[str, Any] = {
        "i2c_bus": i2c_bus,
        "tca_addr": f"0x{tca_addr:02X}",
        "tca_channel": tca_channel,
        "gpio_pin": gpio_pin,
        "hold_seconds": hold_s,
        "interrupted": False,
        "overall_success": False,
        "gpio_low_before_start": {"success": False},
        "pca9554_0x27_io07": {},
        "requested_io07": resolved_io07,
        "mcp4725_0x60_set": {},
        "mcp4725_0x60_zero_on_exit": {"success": False},
        "gpio_high_on_exit": {"success": False},
        "errors": [],
    }

    gpio = GPIOController(gpio_pin)

    try:
        gpio.open()
        gpio.write(0)
        result["gpio_low_before_start"] = {"success": True, "level": 0}

        with SMBus(i2c_bus) as bus:
            tca_selected = False
            try:
                _tca_select_channel(bus, tca_addr, tca_channel)
                tca_selected = True

                result["pca9554_0x27_io07"] = _set_pca9554_io07(bus, PCA9554_ADDR, resolved_io07)
                result["mcp4725_0x60_set"] = _set_mcp4725_voltage(bus, MCP4725_ADDR, dac_voltage, vref)

                if (
                    result["pca9554_0x27_io07"].get("success", False)
                    and result["mcp4725_0x60_set"].get("success", False)
                ):
                    _hold_with_interrupt(hold_s)
            finally:
                if tca_selected:
                    zero_ret = _set_mcp4725_voltage(bus, MCP4725_ADDR, 0.0, vref)
                    result["mcp4725_0x60_zero_on_exit"] = zero_ret
                    if not zero_ret.get("success", False):
                        result["errors"].append(f"退出归零DAC失败: {zero_ret.get('error', 'unknown')}")

                    try:
                        _tca_disable_all(bus, tca_addr)
                    except Exception as e:
                        result["errors"].append(f"关闭TCA通道失败: {e}")

    except KeyboardInterrupt:
        result["interrupted"] = True
        result["errors"].append("收到 KeyboardInterrupt")
    except Exception as e:
        result["errors"].append(str(e))
    finally:
        if not result["mcp4725_0x60_zero_on_exit"].get("success", False):
            zero_ret = _best_effort_zero_dac(i2c_bus, tca_addr, tca_channel, vref)
            result["mcp4725_0x60_zero_on_exit"] = zero_ret
            if not zero_ret.get("success", False):
                result["errors"].append(f"兜底归零DAC失败: {zero_ret.get('error', 'unknown')}")

        try:
            gpio.write(1)
            result["gpio_high_on_exit"] = {"success": True, "level": 1}
        except Exception as e:
            result["gpio_high_on_exit"] = {"success": False, "error": str(e)}
            result["errors"].append(f"退出拉高GPIO失败: {e}")

        try:
            gpio.close()
        except Exception:
            pass

    result["overall_success"] = all(
        [
            result["gpio_low_before_start"].get("success", False),
            result["pca9554_0x27_io07"].get("success", False),
            result["mcp4725_0x60_set"].get("success", False),
            result["mcp4725_0x60_zero_on_exit"].get("success", False),
            result["gpio_high_on_exit"].get("success", False),
            not result["interrupted"],
        ]
    )

    return result


def _parse_io_levels(text: str) -> List[int]:
    vals = [int(x.strip()) for x in text.split(",")]
    if len(vals) not in (4, 8):
        raise ValueError("IO 参数必须是4位或8位逗号列表")
    _validate_io_levels(vals, "IO 参数", len(vals))
    return vals


def resolve_io07_levels(io_23: Optional[List[int]], io_27: List[int]) -> List[int]:
    # 新模式：--io27 直接给 8 位。
    if len(io_27) == 8:
        _validate_io_levels(io_27, "io_27", 8)
        return io_27

    # 兼容旧模式：--io23 与 --io27 各给 4 位，拼成 IO0~IO7。
    _validate_io_levels(io_27, "io_27", 4)
    if io_23 is None:
        raise ValueError("当 --io27 为4位时，必须同时提供 --io23 的4位参数")
    _validate_io_levels(io_23, "io_23", 4)
    return list(io_23) + list(io_27)


def main() -> None:
    parser = argparse.ArgumentParser(description="GPIO27 + CH0下PCA9554(0x27, IO0~IO7) + MCP4725 顺序控制")
    parser.add_argument(
        "--io27",
        type=str,
        required=True,
        help="PCA9554@0x27 IO设置。推荐8位：1,0,1,0,0,1,0,1；兼容4位(需配合--io23)",
    )
    parser.add_argument(
        "--io23",
        type=str,
        required=False,
        help="兼容旧参数：当--io27为4位时，提供旧的前4位(映射到IO0~IO3)",
    )
    parser.add_argument("--dac", type=float, required=True, help="MCP4725 电压值，例如 2.5")
    parser.add_argument("--hold", type=float, required=True, help="保持秒数，支持0.1s精度，例如 1.2")

    parser.add_argument("--gpio", type=int, default=GPIO_PIN_DEFAULT, help="GPIO 引脚号，默认 27")
    parser.add_argument("--bus", type=int, default=I2C_BUS_DEFAULT, help="I2C bus，默认 1")
    parser.add_argument("--tca", type=lambda x: int(x, 0), default=TCA9548A_ADDR_DEFAULT, help="TCA 地址，默认 0x70")
    parser.add_argument("--ch", type=int, default=TCA9548A_CH_DEFAULT, help="TCA 通道，默认 CH0")
    parser.add_argument("--vref", type=float, default=5.0, help="DAC 参考电压，默认 5.0V")

    args = parser.parse_args()

    ret = execute_magnet_sequence(
        io_23=_parse_io_levels(args.io23) if args.io23 else None,
        io_27=_parse_io_levels(args.io27),
        dac_voltage=args.dac,
        hold_seconds=args.hold,
        gpio_pin=args.gpio,
        i2c_bus=args.bus,
        tca_addr=args.tca,
        tca_channel=args.ch,
        vref=args.vref,
    )

    print(ret)


if __name__ == "__main__":
    main()
