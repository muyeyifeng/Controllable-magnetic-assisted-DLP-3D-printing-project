#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import time
import subprocess
import argparse
import sys
import serial
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
CMD_BRIGHTNESS_80 = "A6 02 10 A0"    # 亮度160 (0xA0)
CMD_BRIGHTNESS_0  = "A6 02 10 0A"    # 亮度0 (0x00)
CMD_SAVE_SETTINGS = "A6 01 7F"       # 保存设置

# ================= 全局变量 =================
steps_total_offset = 0  # 用于记录总的步数偏移，方便在最后回到初始位置
exposure_time_per_layer = 10  # 每层曝光时间（秒），可以根据需要调整
stepper_frequency = 800  # 步进电机频率（Hz），可以根据需要调整
stepper_up_steps_per_layer = 8000  # 每层向上移动的步数
stepper_down_steps_per_layer = 7970  # 每层向下移动的步数   
stepper_wait_between_moves = 1  # 每层移动之间的等待时间（秒）
stepper_remove_sample_address = 64000 # 在最后移除样品的步数（相对于初始位置），根据实际情况调整

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

def inital_dlp():
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
        
        print("\n====== 测试完成 ======")
        print("如需关闭，请运行关闭脚本或按 Ctrl+C 并在代码中添加关闭逻辑")
	
    except KeyboardInterrupt:
        print("\n用户强制停止")
    finally:
        send_hex_command(ser, CMD_LED_OFF, "步骤6: 关闭 UV LED")
        ser.close()
        print("串口已关闭")

def run_cmd(cmd: list[str]):
    """运行命令，失败则直接退出。"""
    print("RUN:", " ".join(cmd))
    r = subprocess.run(cmd)
    if r.returncode != 0:
        raise RuntimeError(f"Command failed (code={r.returncode}): {' '.join(cmd)}")


def main():
    global steps_total_offset

    ap = argparse.ArgumentParser(description="Run layered motion + DLP script by calling other scripts")
    ap.add_argument("--layers", type=int, default=100, help="number of layers (default 100)")
    ap.add_argument("--steps-up", type=int, default=stepper_up_steps_per_layer, help="up steps per layer (default 8000)")
    ap.add_argument("--steps-down", type=int, default=stepper_down_steps_per_layer, help="down steps per layer (default 7960)")
    ap.add_argument("--freq", type=int, default=stepper_frequency, help="step frequency (default 500)")
    ap.add_argument("--wait", type=float, default=stepper_wait_between_moves, help="sleep seconds between steps (default 1)")

    ap.add_argument("--stepper-script", default="stepper_pingpong_1.py",
                    help="stepper script filename (default stepper_pingpong_1.py)")
    ap.add_argument("--dlp-script", default="dlp_test.py",
                    help="dlp script filename (default dlp_test.py)")

    # 这些一般固定，但也给你可调
    ap.add_argument("--pul", type=int, default=13, help="BCM PUL pin passed to stepper script (default 13)")
    ap.add_argument("--dir", type=int, default=5, help="BCM DIR pin passed to stepper script (default 5)")
    ap.add_argument("--ena", type=int, default=8, help="BCM ENA pin passed to stepper script (default 8)")
    ap.add_argument("--ena-active-low", action="store_true", default=True,
                    help="pass --ena-active-low to stepper script (default True)")
    ap.add_argument("--dir-invert", action="store_true", help="pass --dir-invert to stepper script")

    args = ap.parse_args()

    # 组装 stepper 基础命令
    stepper_base = ["python3", args.stepper_script]
    stepper_base += ["--pul", str(args.pul), "--dir", str(args.dir), "--ena", str(args.ena)]
    stepper_base += ["--freq", str(args.freq), "--loops", "1", "--mode", "one"]

    if args.ena_active_low:
        stepper_base += ["--ena-active-low"]
    if args.dir_invert:
        stepper_base += ["--dir-invert"]

    #dlp_cmd = ["python3", args.dlp_script]

    try:
        try:
            # 初始化串口
            ser = serial.Serial(PORT, BAUDRATE, timeout=TIMEOUT)
            print(f"串口已打开: {PORT} @ {BAUDRATE}bps")
        except Exception as e:
            print(f"无法打开串口 {PORT}: {e}")
            print("请检查USB是否插入，或是否需要 sudo 权限")
            return

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
        
        for layer in range(1, args.layers + 1):
            print(f"\n=== Layer {layer}/{args.layers} ===")

            # UP
            run_cmd(stepper_base + ["--steps", str(args.steps_up), "--move", "up"])
            steps_total_offset += args.steps_up
            time.sleep(args.wait)

            # DOWN
            run_cmd(stepper_base + ["--steps", str(args.steps_down), "--move", "down"])
            steps_total_offset -= args.steps_down
            time.sleep(args.wait)

            # DLP
            #run_cmd(dlp_cmd)
            
            # 5. 开启 LED (最关键的一步)
            # 期望: 6A 02 83 00
            send_hex_command(ser, CMD_LED_ON, "步骤5: 开启 UV LED")
            
            # 曝光时间（这里直接用 sleep 模拟，实际可以根据需要调整）
            time.sleep(exposure_time_per_layer)
            send_hex_command(ser, CMD_LED_OFF, "步骤6: 关闭 UV LED")

            # 每层结束后等待一会儿，给设备反应时间
            time.sleep(args.wait)

        # UP
        move_to_remove_sample = stepper_remove_sample_address - steps_total_offset
        print(f"\n准备移除样品，向上移动 {move_to_remove_sample} 步")
        run_cmd(stepper_base + ["--steps", str(move_to_remove_sample), "--move", "up"])
        steps_total_offset += move_to_remove_sample

        print("\n✅ All layers done.")
    except KeyboardInterrupt:
        print("\n⛔ Interrupted by user (Ctrl+C).")

        sys.exit(130)
    except Exception as e:
        print("\n❌ ERROR:", e)
        sys.exit(1)
    finally:
        send_hex_command(ser, CMD_LED_OFF, "步骤6: 关闭 UV LED")
        ser.close()
        print("串口已关闭")
        print(f"\n总步数偏移: {steps_total_offset} (正数表示向上，负数表示向下)")
        print("\n如果需要回到初始位置，请运行：")
        print(f"\npython3 {args.stepper_script} --freq {args.freq} --pul {args.pul} --dir {args.dir} --ena {args.ena} --steps {abs(steps_total_offset)} --move {'down' if steps_total_offset > 0 else 'up'}")



if __name__ == "__main__":
    inital_dlp()
    main()