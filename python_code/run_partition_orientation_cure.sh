#!/usr/bin/env bash
set -euo pipefail

# ----------------------------------------
# 分区取向固化批处理脚本
# 用法示例:
#   bash run_partition_orientation_cure.sh
#   bash run_partition_orientation_cure.sh --dry-run
#   bash run_partition_orientation_cure.sh --start-image 10 --first-down-um 3000
# ----------------------------------------

PYTHON_BIN="${PYTHON_BIN:-python}"
CODE_DIR="${CODE_DIR:-/home/mfs/python_code}"
SERIAL_PORT="${SERIAL_PORT:-/dev/ttyUSB0}"

# 是否执行前置复位流程 (#0 #1 #2 #15)
RUN_PREP="true"
# 是否 dry run (只打印不执行)
DRY_RUN="false"
# io 指令是否后台执行 (对应你手打命令里的 &)
IO_BACKGROUND="true"
# 后台下发 io 后等待时间 (秒)
IO_DELAY="2"

# 固化参数
EXPOSURE="10"
BRIGHTNESS="40"
BAUD="115200"
TIMEOUT="1.0"
TTY="1"
FB="/dev/fb0"
SETTLE="0.25"

# 丝杆参数
PUL="13"
DIR="5"
ENA="8"
STEPS_PER_REV="3200"
LEAD_MM="4.0"
PULSE_WIDTH_US="20"

# 前置移动参数
TOP_FREQ_FAST="8000"
TOP_FREQ_SLOW="1600"
DOWN_FAST_UM="200000"
DOWN_SLOW_UM="21400"
UP_UM="3000"
DOWN_FREQ="800"
UP_FREQ="800"
DOWN_UM_FIRST="2950"
DOWN_UM="3000"

# 图像序号从 2 开始，映射为 slice_0002.png ~ slice_0009.png
START_IMAGE="2"

# io27 集合（顺序即打印顺序）
IO_PATTERNS=(
  "1,1,1,1,0,0,0,0"
  "1,1,1,0,0,0,0,1"
  "1,1,0,0,0,0,1,1"
  "1,0,0,0,0,1,1,1"
  "0,0,0,0,1,1,1,1"
  "0,0,0,1,1,1,1,0"
  "0,0,1,1,1,1,0,0"
  "0,1,1,1,1,0,0,0"
)

usage() {
  cat <<'EOF'
Options:
  --no-prep                 跳过前置复位流程
  --dry-run                 只打印命令，不执行
  --start-image N           起始图片编号 (默认 2)
  --first-down-um N         第一层下压距离 (默认 2950)
  --down-um N               其余层下压距离 (默认 3000)
  --up-um N                 每层抬升距离 (默认 3000)
  --io-delay SEC            io 后台启动后等待秒数 (默认 2)
  --io-foreground           io 命令改为前台执行
  -h, --help                查看帮助
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-prep) RUN_PREP="false"; shift ;;
    --dry-run) DRY_RUN="true"; shift ;;
    --start-image) START_IMAGE="$2"; shift 2 ;;
    --first-down-um) DOWN_UM_FIRST="$2"; shift 2 ;;
    --down-um) DOWN_UM="$2"; shift 2 ;;
    --up-um) UP_UM="$2"; shift 2 ;;
    --io-delay) IO_DELAY="$2"; shift 2 ;;
    --io-foreground) IO_BACKGROUND="false"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1"; usage; exit 1 ;;
  esac
done

run_cmd() {
  echo "+ $*"
  if [[ "$DRY_RUN" == "false" ]]; then
    "$@"
  fi
}

run_io() {
  local io_pattern="$1"
  local cmd=(
    "$PYTHON_BIN" "$CODE_DIR/tca_ch0_io_dac.py"
    --io27 "$io_pattern" --dac 1.8 --hold 8.0
  )

  if [[ "$IO_BACKGROUND" == "true" ]]; then
    echo "+ ${cmd[*]} &"
    if [[ "$DRY_RUN" == "false" ]]; then
      "${cmd[@]}" &
    fi
    if [[ "$IO_DELAY" != "0" && "$IO_DELAY" != "0.0" ]]; then
      run_cmd sleep "$IO_DELAY"
    fi
  else
    run_cmd "${cmd[@]}"
  fi
}

run_stepper_um() {
  local freq="$1"
  local move="$2"
  local um="$3"

  run_cmd "$PYTHON_BIN" "$CODE_DIR/stepper_pigpio_um.py" \
    --pul "$PUL" --dir "$DIR" --ena "$ENA" --ena-active-low \
    --freq "$freq" --move "$move" --um "$um" \
    --steps-per-rev "$STEPS_PER_REV" --lead-mm "$LEAD_MM" --pulse-width-us "$PULSE_WIDTH_US"
}

run_exposure() {
  local image_path="$1"

  run_cmd sudo "$PYTHON_BIN" "$CODE_DIR/hdmi_dlp_exposure_test.py" \
    --image "$image_path" \
    --exposure "$EXPOSURE" \
    --brightness "$BRIGHTNESS" \
    --port "$SERIAL_PORT" --baud "$BAUD" --timeout "$TIMEOUT" \
    --tty "$TTY" --fb "$FB" --settle "$SETTLE"
}

if [[ "$RUN_PREP" == "true" ]]; then
  echo "=== 前置复位流程开始 ==="

  run_cmd "$PYTHON_BIN" "$CODE_DIR/stepper_to_top_pigpio.py" \
    --pul "$PUL" --dir "$DIR" --ena "$ENA" --ena-active-low \
    --freq "$TOP_FREQ_FAST" --move up --top-pin 20 --stop-level 0 --pull none \
    --steps-per-rev "$STEPS_PER_REV" --lead-mm "$LEAD_MM" --max-steps 0 --pulse-width-us "$PULSE_WIDTH_US"

  run_cmd "$PYTHON_BIN" "$CODE_DIR/stepper_to_top_pigpio.py" \
    --pul "$PUL" --dir "$DIR" --ena "$ENA" --ena-active-low \
    --freq "$TOP_FREQ_SLOW" --move up --top-pin 20 --stop-level 0 --pull none \
    --steps-per-rev "$STEPS_PER_REV" --lead-mm "$LEAD_MM" --max-steps 0 --pulse-width-us "$PULSE_WIDTH_US"

  run_stepper_um "$TOP_FREQ_FAST" down "$DOWN_FAST_UM"
  run_stepper_um "$DOWN_FREQ" down "$DOWN_SLOW_UM"
  run_stepper_um "$UP_FREQ" up "$UP_UM"

  echo "=== 前置复位流程结束 ==="
fi

echo "=== 分区取向固化开始 ==="
for i in "${!IO_PATTERNS[@]}"; do
  io_pattern="${IO_PATTERNS[$i]}"

  image_num=$((START_IMAGE + i))
  printf -v image_file "slice_%04d.png" "$image_num"
  image_path="$CODE_DIR/slice/$image_file"

  down_um="$DOWN_UM"
  if [[ "$i" -eq 0 ]]; then
    down_um="$DOWN_UM_FIRST"
  fi

  echo "--- 区块 $((i + 1)) / ${#IO_PATTERNS[@]}: io27=$io_pattern image=$image_file ---"

  run_io "$io_pattern"
  run_stepper_um "$DOWN_FREQ" down "$down_um"
  run_cmd sleep 1
  run_exposure "$image_path"
  run_stepper_um "$UP_FREQ" up "$UP_UM"
done

echo "=== 分区取向固化完成 ==="
