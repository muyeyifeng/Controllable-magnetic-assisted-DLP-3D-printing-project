import RPi.GPIO as GPIO
import time
import sys

# BCM Pin mapping for 8 output bits (MSB → LSB)
pins = [25, 8, 7, 12, 22, 27, 17, 23]

# Mode patterns
patterns = {
    1: "11110000",
    2: "00111100",
    3: "00001111",
    4: "11000011"
}

def setup():
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)
    for pin in pins:
        GPIO.setup(pin, GPIO.OUT)
        GPIO.output(pin, GPIO.LOW)

def set_pattern(mode):
    if mode not in patterns:
        print("❌ 无效模式:", mode)
        return

    pattern = patterns[mode]
    print(f"\n模式 {mode} => 输出: {pattern}")

    for bit_index, bit_value in enumerate(pattern):
        pin = pins[bit_index]
        GPIO.output(pin, GPIO.HIGH if bit_value == '1' else GPIO.LOW)

def main():
    setup()

    # 检查是否传入参数
    if len(sys.argv) < 2:
        print("缺少参数！用法: python3 func.py <模式编号1~4>")
        return

    try:
        mode = int(sys.argv[1])
        if mode == 0:
            print("退出程序")
            return

        set_pattern(mode)
        print(f"已切换模式: {mode}")

    except ValueError:
        print("模式参数必须是数字！")

    finally:
        #GPIO.cleanup()
        print("GPIO 已处理完毕")

if __name__ == "__main__":
    main()