import lgpio
import time
import argparse
import sys

parser = argparse.ArgumentParser()

# 电机引脚
parser.add_argument('--pul', type=int, default=13)
parser.add_argument('--dir', type=int, default=5)
parser.add_argument('--ena', type=int, default=8)
parser.add_argument('--freq', type=int, default=1600)
parser.add_argument('--ena-active-low', action='store_true')

# 最大步数保护，防止限位失效后一直跑
parser.add_argument('--max-up-steps', type=int, default=40000)
parser.add_argument('--max-down-steps', type=int, default=40000)

# 往复次数
parser.add_argument('--cycles', type=int, default=5)

# 监控引脚
parser.add_argument('--top-pin', type=int, default=20)     # 上升到顶：1 -> 0
parser.add_argument('--bottom-pin', type=int, default=21)  # 下降到底：0 -> 1

# 输入上下拉
parser.add_argument('--top-pull', type=str, choices=['up', 'down', 'none'], default='up')
parser.add_argument('--bottom-pull', type=str, choices=['up', 'down', 'none'], default='down')

# 打印频率
parser.add_argument('--report-every', type=int, default=1000)

args = parser.parse_args()

pull_map = {
    'up': lgpio.SET_PULL_UP,
    'down': lgpio.SET_PULL_DOWN,
    'none': lgpio.SET_PULL_NONE
}

try:
    h = lgpio.gpiochip_open(0)
except Exception as e:
    print(f"打开 gpiochip 失败: {e}")
    sys.exit(1)

try:
    # 输出脚
    for p in [args.pul, args.dir, args.ena]:
        lgpio.gpio_claim_output(h, p)

    # 输入脚
    lgpio.gpio_claim_input(h, args.top_pin, pull_map[args.top_pull])
    lgpio.gpio_claim_input(h, args.bottom_pin, pull_map[args.bottom_pull])

except Exception as e:
    print(f"GPIO 初始化失败: {e}")
    try:
        lgpio.gpiochip_close(h)
    except:
        pass
    sys.exit(1)


def read_pin(pin):
    return lgpio.gpio_read(h, pin)


def motor_enable(enable: bool):
    ena_on = 0 if args.ena_active_low else 1
    ena_off = 1 - ena_on
    lgpio.gpio_write(h, args.ena, ena_on if enable else ena_off)


def set_direction(move: str):
    # up=1, down=0
    lgpio.gpio_write(h, args.dir, 1 if move == 'up' else 0)


def pulse_once(delay):
    lgpio.gpio_write(h, args.pul, 1)
    time.sleep(delay)
    lgpio.gpio_write(h, args.pul, 0)
    time.sleep(delay)


def move_until_edge(move: str, watch_pin: int, edge_from: int, edge_to: int, max_steps: int, report_every: int):
    """
    move: 'up' 或 'down'
    watch_pin: 要监测的引脚
    edge_from -> edge_to: 例如 1->0 或 0->1
    返回:
        steps_taken, triggered(bool), last_level
    """
    delay = 0.5 / args.freq

    set_direction(move)
    motor_enable(True)

    # 读取初始状态
    last_level = read_pin(watch_pin)
    print(f"\n开始 {move}，监控 GPIO{watch_pin}，目标边沿 {edge_from}->{edge_to}，初始电平={last_level}")

    steps_taken = 0
    triggered = False

    try:
        for i in range(max_steps):
            pulse_once(delay)
            steps_taken += 1

            level = read_pin(watch_pin)

            if level != last_level:
                print(f"\n[电平变化] GPIO{watch_pin}: {last_level} -> {level} | step={steps_taken}")

            if last_level == edge_from and level == edge_to:
                print(f"[触发] GPIO{watch_pin} 出现 {edge_from}->{edge_to} | {move} 停止 | step={steps_taken}")
                triggered = True
                last_level = level
                break

            last_level = level

            if steps_taken % report_every == 0:
                sys.stdout.write(f"\r{move} 进度: {steps_taken}/{max_steps}")
                sys.stdout.flush()

        print()

    except KeyboardInterrupt:
        print("\n检测到用户中断。")
        raise

    finally:
        motor_enable(False)

    return steps_taken, triggered, last_level


def main():
    total_error = 0
    results = []

    print("===== 往复误差测试开始 =====")
    print(f"top pin    : GPIO{args.top_pin} (触发条件 1->0)")
    print(f"bottom pin : GPIO{args.bottom_pin} (触发条件 0->1)")
    print(f"cycles     : {args.cycles}")
    print(f"freq       : {args.freq}")
    print()

    # 全局计步变量，按你的要求：
    # 上升到顶后清零；
    # 下降到底后不清零（这里保留下降步数记录）
    current_step_counter = 0

    try:
        for cycle in range(1, args.cycles + 1):
            print(f"\n========== 第 {cycle} 次循环 ==========")

            # ---------------------------
            # 上升：GPIO20 从 1 -> 0
            # 到顶后停机，并计步清零
            # ---------------------------
            up_steps, up_ok, _ = move_until_edge(
                move='up',
                watch_pin=args.top_pin,
                edge_from=1,
                edge_to=0,
                max_steps=args.max_up_steps,
                report_every=args.report_every
            )

            if not up_ok:
                print(f"[警告] 第 {cycle} 次上升未检测到 GPIO{args.top_pin} 的 1->0，已到最大步数停止。")
                break

            print(f"第 {cycle} 次上升实际步数: {up_steps}")
            current_step_counter = 0
            print(f"到顶后计步清零，current_step_counter = {current_step_counter}")

            time.sleep(0.3)

            # ---------------------------
            # 下降：GPIO21 从 0 -> 1
            # 到底后停机，计步不清零
            # ---------------------------
            down_steps, down_ok, _ = move_until_edge(
                move='down',
                watch_pin=args.bottom_pin,
                edge_from=0,
                edge_to=1,
                max_steps=args.max_down_steps,
                report_every=args.report_every
            )

            if not down_ok:
                print(f"[警告] 第 {cycle} 次下降未检测到 GPIO{args.bottom_pin} 的 0->1，已到最大步数停止。")
                break

            current_step_counter += down_steps
            print(f"第 {cycle} 次下降实际步数: {down_steps}")
            print(f"到底后计步不清零，current_step_counter = {current_step_counter}")

            # 误差：上去多少步，下来多少步
            error = up_steps - down_steps
            total_error += error

            results.append({
                'cycle': cycle,
                'up_steps': up_steps,
                'down_steps': down_steps,
                'error': error,
                'counter_after_down': current_step_counter,
            })

            print(f"第 {cycle} 次误差: up - down = {up_steps} - {down_steps} = {error}")

            time.sleep(0.5)

    except KeyboardInterrupt:
        print("\n用户中断测试。")

    finally:
        try:
            motor_enable(False)
        except:
            pass
        try:
            lgpio.gpiochip_close(h)
        except:
            pass

    print("\n===== 测试结果汇总 =====")
    if not results:
        print("没有有效结果。")
        return

    for r in results:
        print(
            f"Cycle {r['cycle']:>2}: "
            f"up={r['up_steps']:>6}, "
            f"down={r['down_steps']:>6}, "
            f"error={r['error']:>6}, "
            f"counter_after_down={r['counter_after_down']:>6}"
        )

    avg_error = total_error / len(results)
    print(f"\n总循环数: {len(results)}")
    print(f"总误差和: {total_error}")
    print(f"平均单次误差: {avg_error:.3f} steps")


if __name__ == "__main__":
    main()