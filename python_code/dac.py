import smbus2

bus = smbus2.SMBus(1)
addr = 0x60

def set_voltage(voltage, vref=5.0):
    dac = int(voltage / vref * 4096)
    dac = max(0, min(4095, dac))
    high = dac >> 4
    low  = (dac & 0x0F) << 4
    bus.write_i2c_block_data(addr, 0x40, [high, low])

set_voltage(3.3)
print("已设置 DAC 输出为 3.3V")
