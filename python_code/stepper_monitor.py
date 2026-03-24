import RPi.GPIO as GPIO
import time
import argparse
import sys

# --- 参数解析 ---
parser = argparse.ArgumentParser(description='Stepper Motor Control with IO Monitoring')
parser.add_argument('--pul', type=int, default=13, help='Pulse pin (BCM)')
parser.add_argument('--dir', type=int, default=5, help='Direction pin (BCM)')
parser.add_argument('--ena', type=int, default=8, help='Enable pin (BCM)')
parser.add_argument('--freq', type=int, default=1600, help='Frequency in Hz')
parser.add_argument('--steps', type=int, default=32000, help='Total steps to move')
parser.add_argument('--move', type=str, choices=['up', 'down'], default='up', help='Direction')
parser.add_argument('--ena-active-low', action='store_true', help='Enable pin is active low')
args = parser.parse_args()

# --- 全局变量 ---
current_step = 0

# --- GPIO 初始化 ---
GPIO.setmode(GPIO.BCM)
GPIO.setwarnings(False)

# 电机引脚设置
GPIO.setup(args.pul, GPIO.OUT)
GPIO.setup(args.dir, GPIO.OUT)
GPIO.setup(args.ena, GPIO.OUT)

# 监控引脚设置 (开启内部下拉电阻，防止浮空干扰)
MONITOR_PINS = [25]
for pin in MONITOR_PINS:
    GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)

# --- 中断回调函数 ---
def io_callback(channel):
    state = "HIGH" if GPIO.input(channel) else "LOW"
    # 打印格式：时间 - 引脚 - 状态 - 当前步数
    print(f"\n[EVENT] {time.strftime('%H:%M:%S')} | GPIO {channel} -> {state} | At Step: {current_step}")

# 注册中断：监听上升沿和下降沿 (Both Edges)
for pin in MONITOR_PINS:
    GPIO.add_event_detect(pin, GPIO.BOTH, callback=io_callback)

# --- 电机运行逻辑 ---
def run_stepper():
    global current_step
    
    # 设置方向
    GPIO.output(args.dir, GPIO.HIGH if args.move == 'up' else GPIO.LOW)
    
    # 使能电机
    ena_state = GPIO.LOW if args.ena_active_low else GPIO.HIGH
    GPIO.output(args.ena, ena_state)
    
    delay = 1.0 / (args.freq * 2)
    print(f"开始运动: {args.move}, 目标步数: {args.steps}, 频率: {args.freq}Hz")
    print("正在监控 IO 20 和 21... (按 Ctrl+C 停止)")

    try:
        for i in range(args.steps):
            current_step = i + 1
            
            # 发送一个脉冲
            GPIO.output(args.pul, GPIO.HIGH)
            time.sleep(delay)
            GPIO.output(args.pul, GPIO.LOW)
            time.sleep(delay)
            
            # 每 1000 步打印一次进度
            if current_step % 1000 == 0:
                sys.stdout.write(f"\r进度: {current_step}/{args.steps} steps")
                sys.stdout.flush()

        print(f"\n任务完成！总步数: {current_step}")

    except KeyboardInterrupt:
        print("\n用户中止运行")
    finally:
        # 释放使能并清理
        GPIO.output(args.ena, not ena_state)
        GPIO.cleanup()

if __name__ == "__main__":
    run_stepper()
