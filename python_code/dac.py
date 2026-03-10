import smbus2
import time

bus = smbus2.SMBus(1)
addr = 0x60

def set_voltage(voltage, vref=5.0):
    # 限幅，防止超范围
    dac = int(voltage / vref * 4096)
    dac = max(0, min(4095, dac))
    
    high = dac >> 4
    low  = (dac & 0x0F) << 4
    
    bus.write_i2c_block_data(addr, 0x40, [high, low])
    print(f"已设置 DAC 输出为 {voltage:.3f}V (DAC={dac})")

try:
    set_voltage(2.361)                  # 输出3.3V
    print("保持30秒中...")
    time.sleep(15)                    # 保持30秒
    
    set_voltage(0.0)                  # 回到0V
    print("电压已恢复为 0V")

except KeyboardInterrupt:
    print("用户终止，关闭输出为0V")
    set_voltage(0.0)

finally:
    bus.close()
