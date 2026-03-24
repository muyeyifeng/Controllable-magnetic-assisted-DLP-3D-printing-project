import lgpio
import time
import argparse
import sys

# --- 参数解析 ---
parser = argparse.ArgumentParser()
parser.add_argument('--pul', type=int, default=13)
parser.add_argument('--dir', type=int, default=5)
parser.add_argument('--ena', type=int, default=8)
parser.add_argument('--freq', type=int, default=1600)
parser.add_argument('--steps', type=int, default=32000)
parser.add_argument('--move', type=str, choices=['up', 'down'], default='up')
parser.add_argument('--ena-active-low', action='store_true')

# 监控相关参数
parser.add_argument('--monitor-pins', type=int, nargs='*', default=[20, 21],
                    help='要监控的输入引脚列表，例如 --monitor-pins 20 21')
parser.add_argument('--report-every', type=int, default=1000,
                    help='每多少步打印一次当前输入电平')
parser.add_argument('--pull', type=str, choices=['down', 'up', 'none'], default='down',
                    help='输入引脚内部上下拉设置')
args = parser.parse_args()

current_step = 0

# --- 初始化 GPIO ---
try:
    h = lgpio.gpiochip_open(0)
except Exception as e:
    print(f"打开 gpiochip 失败: {e}")
    sys.exit(1)

# --- 设置输出引脚 ---
try:
    for p in [args.pul, args.dir, args.ena]:
        lgpio.gpio_claim_output(h, p)
except Exception as e:
    print(f"声明输出引脚失败: {e}")
    try:
        lgpio.gpiochip_close(h)
    except:
        pass
    sys.exit(1)

# --- 设置输入引脚 ---
pull_map = {
    'down': lgpio.SET_PULL_DOWN,
    'up': lgpio.SET_PULL_UP,
    'none': lgpio.SET_PULL_NONE
}

try:
    for pin in args.monitor_pins:
        lgpio.gpio_claim_input(h, pin, pull_map[args.pull])
except Exception as e:
    print(f"声明输入引脚失败: {e}")
    try:
        lgpio.gpiochip_close(h)
    except:
        pass
    sys.exit(1)

# 记录上一次电平，用于检测变化
last_levels = {}

def read_and_report(force=False):
    """
    读取监控引脚电平：
    - 电平变化时立即打印
    - force=True 时强制打印所有当前电平
    """
    global last_levels, current_step

    for pin in args.monitor_pins:
        try:
            level = lgpio.gpio_read(h, pin)
        except Exception as e:
            print(f"\n[读取失败] GPIO {pin} | step={current_step} | error={e}")
            continue

        if pin not in last_levels:
            last_levels[pin] = level
            print(f"[初始化] GPIO {pin} = {level} | step={current_step}")
        elif level != last_levels[pin]:
            print(f"\n[电平变化] GPIO {pin}: {last_levels[pin]} -> {level} | 当前步数: {current_step}")
            last_levels[pin] = level
        elif force:
            print(f"[状态] GPIO {pin} = {level} | step={current_step}")

def run():
    global current_step

    # 设置方向
    lgpio.gpio_write(h, args.dir, 1 if args.move == 'up' else 0)

    # 电机使能
    ena_on = 0 if args.ena_active_low else 1
    lgpio.gpio_write(h, args.ena, ena_on)

    delay = 0.5 / args.freq

    print(f"电机开始运动: {args.move} | 目标: {args.steps} 步")
    print(f"监控输入引脚: {args.monitor_pins} | pull={args.pull}")
    print("开始前先读取一次输入状态...")

    read_and_report(force=True)

    try:
        for i in range(args.steps):
            current_step = i + 1

            # 发脉冲
            lgpio.gpio_write(h, args.pul, 1)
            time.sleep(delay)
            lgpio.gpio_write(h, args.pul, 0)
            time.sleep(delay)

            # 每一步都检查输入变化
            read_and_report(force=False)

            # 定期打印当前状态
            if current_step % args.report_every == 0:
                sys.stdout.write(f"\r实时进度: {current_step}/{args.steps} steps")
                sys.stdout.flush()
                print()
                read_and_report(force=True)

        print("\n运动完成！")

    except KeyboardInterrupt:
        print("\n检测到用户中断。")

    finally:
        try:
            lgpio.gpio_write(h, args.ena, 1 - ena_on)
        except:
            pass

        try:
            lgpio.gpiochip_close(h)
        except:
            pass

if __name__ == "__main__":
    run()