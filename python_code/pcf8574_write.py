from smbus2 import SMBus

I2C_BUS = 1
PCF_ADDR = 0x25   # 你的 PCF8574 地址

with SMBus(I2C_BUS) as bus:
    value = 0b00001010   # 你要输出的 8-bit
    bus.write_byte(PCF_ADDR, value)
