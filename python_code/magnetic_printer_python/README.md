# Magnetic Printer (Python Rewrite)

这个目录是对 `magnetic_printer` 的 Python 全量重写版本，独立实现后端服务，并复用前端静态页面。

## 目录结构

- `backend/main.py`: Python 后端入口（FastAPI）。
- `backend/requirements.txt`: Python 依赖。
- `backend/config.example.json`: 默认配置样例（Mock 模式）。
- `backend/config.rpi2.sample.json`: 树莓派部署样例。
- `frontend/`: 前端静态页面。

## 已实现功能

- 用户注册 / 登录 / 退出、设备独占锁。
- 切片 ZIP 上传、解压与 `slice_magnetic_manifest.json` 解析。
- 打印任务开始 / 取消 / 状态查询 / SSE 实时状态推送。
- 打印参数覆写：层厚、磁场电压、曝光强度、全局磁场保持时间、全局曝光时间。
- 管理员手动调试接口：磁场 / 曝光 / 移动 / 回零 / 等待。
- 图形化流程编排后端支持（含 `loop`、`magnet_async`、`wait_all_idle`）。
- 流程配置导出 / 导入（`.mpcfg`，AES-GCM 加密）。
- Mock 硬件执行与 Linux 原生硬件执行（GPIO/I2C/串口/Framebuffer）。

## 运行方式

```bash
cd python_code/magnetic_printer_python/backend
python -m venv .venv
# Linux/macOS:
source .venv/bin/activate
# Windows PowerShell:
# .venv\Scripts\Activate.ps1
pip install -r requirements.txt
cp config.example.json config.json
python main.py
```

启动后访问 `http://localhost:5241/index.html`。

## 纯命令行打印脚本（调试推荐）

如果你不想走前后端，可直接用 CLI：

1. 复制配置：

```bash
cd python_code/magnetic_printer_python/cli
cp cli_config.example.json cli_config.json
```

2. 运行：

```bash
cd python_code/magnetic_printer_python/backend
source .venv/bin/activate
python ../cli/print_from_slice.py \
  --zip ../../slice_example.zip \
  --config ../cli/cli_config.json
```

3. 中断：

- `Ctrl+C` 会触发安全停机：停运动 + 清磁场 + 关 DLP。
- 关键参数（`cli_config.json`）：
  - `preHomeEnabled`: 打印前先执行回顶端归零流程
  - `preHomeDropUm`: 回零前先下降一小段，避免已压住上限位
  - `bottomDistanceUm`: 首次下降到底部距离（默认 `221600`）
  - `peelDistanceUm`: 层间抬升/回压距离（默认 `3000`）
  - `finalReturnToTop`: 打印完成后自动回顶端

## 说明

- 默认 Windows 下会自动走 Mock 硬件。
- Linux 若使用真实硬件，请将 `useMockHardware` 设置为 `false`，并确保有权限访问：
  - `/sys/class/gpio`
  - `/dev/i2c-*`
  - DLP 串口（如 `/dev/ttyUSB0`）
  - Framebuffer（如 `/dev/fb0`）
