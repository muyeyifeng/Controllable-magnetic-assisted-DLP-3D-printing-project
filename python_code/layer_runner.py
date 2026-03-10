#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import time
import subprocess
import argparse
import sys


def run_cmd(cmd: list[str]):
    """运行命令，失败则直接退出。"""
    print("RUN:", " ".join(cmd))
    r = subprocess.run(cmd)
    if r.returncode != 0:
        raise RuntimeError(f"Command failed (code={r.returncode}): {' '.join(cmd)}")


def main():
    ap = argparse.ArgumentParser(description="Run layered motion + DLP script by calling other scripts")
    ap.add_argument("--layers", type=int, default=100, help="number of layers (default 100)")
    ap.add_argument("--steps-up", type=int, default=8000, help="up steps per layer (default 8000)")
    ap.add_argument("--steps-down", type=int, default=7960, help="down steps per layer (default 7960)")
    ap.add_argument("--freq", type=int, default=500, help="step frequency (default 500)")
    ap.add_argument("--wait", type=float, default=0.5, help="sleep seconds between steps (default 0.5)")

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

    dlp_cmd = ["python3", args.dlp_script]

    try:
        for layer in range(1, args.layers + 1):
            print(f"\n=== Layer {layer}/{args.layers} ===")

            # UP
            run_cmd(stepper_base + ["--steps", str(args.steps_up), "--move", "up"])
            time.sleep(args.wait)

            # DOWN
            run_cmd(stepper_base + ["--steps", str(args.steps_down), "--move", "down"])
            time.sleep(args.wait)

            # DLP
            run_cmd(dlp_cmd)
            time.sleep(args.wait)

        # UP
        run_cmd(stepper_base + ["--steps", "32000", "--move", "up"])

        print("\n✅ All layers done.")

    except KeyboardInterrupt:
        print("\n⛔ Interrupted by user (Ctrl+C).")
        sys.exit(130)
    except Exception as e:
        print("\n❌ ERROR:", e)
        sys.exit(1)


if __name__ == "__main__":
    main()