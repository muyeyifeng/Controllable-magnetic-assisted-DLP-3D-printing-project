import serial
import time
import binascii

# ================= 配置区域 =================
# 串口端口 (通常是 /dev/ttyUSB0)
PORT = '/dev/ttyUSB0'
# 波特率 (如果不确定，请尝试 115200 或 9600)
BAUDRATE = 115200 
# 超时时间 (秒)
TIMEOUT = 1

# ================= 命令定义 (Hex 字符串) =================
CMD_HANDSHAKE     = "A6 01 05"       # 打开串口/握手
CMD_DLP_ON        = "A6 02 02 01"    # 打开DLP
CMD_DLP_OFF       = "A6 02 02 00"    # 关闭DLP
CMD_FAN_ON        = "A6 02 04 03"    # 开启风扇
CMD_LED_ON        = "A6 02 03 01"    # 开启LED
CMD_LED_OFF       = "A6 02 03 00"    # 关闭LED
CMD_BRIGHTNESS_80 = "A6 02 10 78"    # 亮度80 (0x50)
CMD_SAVE_SETTINGS = "A6 01 7F"       # 保存设置

def send_hex_command(ser, hex_str, description):
    """发送Hex命令并打印响应"""
    try:
        # 1. 将空格分隔的字符串转换为字节数据
        data_to_send = bytes.fromhex(hex_str)
        
        # 2. 清空缓冲区，防止读取到旧数据
        ser.reset_input_buffer()
        
        # 3. 发送数据
        ser.write(data_to_send)
        print(f"\n--- [{description}] ---")
        print(f"发送(Hex): {hex_str.upper()}")
        
        # 4. 读取响应 (根据协议，响应长度通常为4字节，握手可能更多)
        # 读取足够多的字节，直到超时
        response = ser.read(10) 
        
        if not response:
            print("接收(Hex): (超时/无响应) -> 失败 X")
            return False
        
        # 5. 将响应转为大写Hex字符串显示
        response_hex = ' '.join(['{:02X}'.format(b) for b in response])
        print(f"接收(Hex): {response_hex}")
        
        # 6. 简单的成功判断 (检查是否有 'E0' 错误码)
        # 根据您的描述，E0 结尾通常是失败
        if response_hex.endswith("E0"):
             print("结果: 失败 (设备返回错误)")
             return False
        else:
             print("结果: 成功 OK")
             return True
             
    except Exception as e:
        print(f"发生错误: {e}")
        return False

def main():
    try:
        # 初始化串口
        ser = serial.Serial(PORT, BAUDRATE, timeout=TIMEOUT)
        print(f"串口已打开: {PORT} @ {BAUDRATE}bps")
    except Exception as e:
        print(f"无法打开串口 {PORT}: {e}")
        print("请检查USB是否插入，或是否需要 sudo 权限")
        return

    try:
        # 1. 发送握手/打开串口命令
        # 期望: 6A 02 85 DC
        if not send_hex_command(ser, CMD_HANDSHAKE, "步骤1: 握手/连接"):
            print("错误：握手失败，停止后续操作。请检查接线或波特率。")
            return

        # 2. 打开 DLP
        # 期望: 6A 02 82 00
        send_hex_command(ser, CMD_DLP_ON, "步骤2: 打开 DLP 引擎")
        time.sleep(0.5) # 给设备一点反应时间

        # 3. 打开 风扇 (建议在开灯前开风扇)
        # 期望: 6A 02 84 00
        #send_hex_command(ser, CMD_FAN_ON, "步骤3: 开启风扇")
        #time.sleep(0.5)

        # 4. 设置 亮度 (80)
        # 期望: 6A 02 90 00
        send_hex_command(ser, CMD_BRIGHTNESS_80, "步骤4: 设置亮度为 80")
        time.sleep(0.2)

        # 5. 开启 LED (最关键的一步)
        # 期望: 6A 02 83 00
        send_hex_command(ser, CMD_LED_ON, "步骤5: 开启 UV LED")
        
        time.sleep(10)
        send_hex_command(ser, CMD_LED_OFF, "步骤6: 关闭 UV LED")
        
        print("\n====== 测试完成，LED 应该已亮起 ======")
        print("如需关闭，请运行关闭脚本或按 Ctrl+C 并在代码中添加关闭逻辑")
	
    except KeyboardInterrupt:
        print("\n用户强制停止")
    finally:
        send_hex_command(ser, CMD_LED_OFF, "步骤6: 关闭 UV LED")
        ser.close()
        print("串口已关闭")

if __name__ == "__main__":
    main()
