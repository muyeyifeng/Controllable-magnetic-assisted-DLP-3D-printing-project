import serial
import time

PORT = "/dev/ttyUSB1"   # 按实际修改
BAUD = 115200
MOTOR_ID = 0x01

CMD_HEAD = 0x3E

CMD_CONNECT     = 0x10
CMD_MOTOR_RUN   = 0x88
CMD_CLEAR_ANGLE = 0x93
CMD_SPEED_CTRL  = 0xA2
CMD_MULTI_POS   = 0xA3
CMD_READ_ANGLE  = 0x92

ANGLE_TOLERANCE = 0.5   # 到位误差 ±0.5°
STABLE_WAIT     = 5.0   # 到位后多等 5 秒

def checksum(data):
    return sum(data) & 0xFF

def send_frame(ser, cmd, data_bytes=b""):
    data_bytes = bytes(data_bytes)
    length = len(data_bytes)

    head = [CMD_HEAD, cmd, MOTOR_ID, length]
    head_sum = checksum(head)

    frame = bytearray()
    frame.extend(head)
    frame.append(head_sum)
    frame.extend(data_bytes)
    data_sum = checksum(data_bytes)
    frame.append(data_sum)

    print("发送:", " ".join(f"{b:02X}" for b in frame))
    ser.write(frame)

def connect_motor(ser):
    send_frame(ser, CMD_CONNECT)
    time.sleep(0.2)

def clear_angle(ser):
    send_frame(ser, CMD_CLEAR_ANGLE)
    time.sleep(0.2)

def motor_run(ser):
    send_frame(ser, CMD_MOTOR_RUN)
    time.sleep(0.2)

def set_max_speed(ser):
    """
    设置速度环为 2880 dps
    来自示例：
    3E A2 01 04 E5 00 65 04 00 69
    数据：00 65 04 00 -> 0x00046500 = 288000 (单位 0.01dps)
    我们直接用同一组数据
    """
    data = bytes([0x00, 0x65, 0x04, 0x00])
    send_frame(ser, CMD_SPEED_CTRL, data)
    time.sleep(0.2)

def pack_multi_pos(angle_deg):
    """
    多圈位置环 A3
    单位：0.01°
    int64 小端
    """
    val = int(angle_deg * 100)  # ★ 关键修正：角度 ×100

    data = val.to_bytes(8, byteorder="little", signed=True)
    # 你给的 90° 示例：28 23 00 00 00 00 00 00
    # 用下面这行可以对比一下：
    # print("调试 A3 数据:", " ".join(f"{b:02X}" for b in data))
    return data

def read_angle_once(ser, timeout=0.5):
    """
    发送 92 读角度，解析一帧返回
    假设返回格式：
    3E 92 ID LEN SUM  [8字节角度]  DATASUM
    角度也是 int64 小端，单位 0.01°
    """
    send_frame(ser, CMD_READ_ANGLE)

    ser.timeout = timeout
    buf = []

    while True:
        b = ser.read(1)
        if not b:
            return None  # 超时

        buf.append(b[0])

        # 找帧头
        if len(buf) >= 1 and buf[0] != CMD_HEAD:
            buf.pop(0)
            continue

        if len(buf) >= 5:
            cmd = buf[1]
            length = buf[3]
            total_len = 5 + length + 1
            if len(buf) >= total_len:
                data = buf[5:5+length]
                # 这里只处理角度反馈
                if cmd == CMD_READ_ANGLE and length >= 8:
                    raw = int.from_bytes(data[:8], byteorder="little", signed=True)
                    angle = raw / 100.0  # ★ 单位 0.01°
                    return angle
                else:
                    # 如果不是我们要的，就清空，重新等
                    buf.clear()

def wait_until_reached(ser, target_angle):
    print(f"等待到达 {target_angle}° (±{ANGLE_TOLERANCE}°) ...")

    start = time.time()
    while True:
        angle = read_angle_once(ser, timeout=0.5)
        if angle is None:
            print("读取角度超时，重试...")
            continue

        print(f"当前角度: {angle:.2f}°")

        if abs(angle - target_angle) <= ANGLE_TOLERANCE:
            print("✅ 已到达目标角度，进入稳定等待 5 秒...")
            time.sleep(STABLE_WAIT)
            break

        # 防止死等，可以加个超时保护（比如 30 秒）
        if time.time() - start > 30:
            print("⚠ 超过 30 秒未到位，退出等待")
            break

        time.sleep(0.1)

if __name__ == "__main__":
    ser = serial.Serial(PORT, BAUD, timeout=0.2)
    print("串口打开成功:", PORT)

    print("1️⃣ 连接电机")
    connect_motor(ser)

    print("2️⃣ 清除多圈角度")
    clear_angle(ser)

    print("3️⃣ 设置全局最大速度（速度环 2880 dps）")
    set_max_speed(ser)

    print("4️⃣ 电机运行使能")
    motor_run(ser)

    # 你可以改这里的几个目标角度测试
    targets = [0.0, 36000.0, 0.0]

    for t in targets:
        print(f"\n===== 发送多圈位置 {t}° =====")
        ser.reset_input_buffer()  # 清掉旧数据，避免读到老的角度
        data = pack_multi_pos(t)
        send_frame(ser, CMD_MULTI_POS, data)
        wait_until_reached(ser, t)

    print("\n✅ 测试结束")
    ser.close()
