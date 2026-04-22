# Magnetic Printer Web Controller

High-stability web controller for magnetic-assisted DLP printing.

## What It Implements

1. Web UI management panel.
2. Upload and parse sliced zip package (requires `slice_magnetic_manifest.json`).
3. Per-layer execution uses manifest values for:
   - layer thickness (`layer_thickness_mm`)
   - magnetic voltage (`field.strength` / `strength`)
   - exposure intensity (`light_intensity`)
4. UI override support:
   - layer thickness
   - magnetic voltage
   - exposure intensity
   - global magnetic hold time
   - global exposure time
5. Frontend/backend split:
   - `frontend/` static UI
   - `backend/` Go API (single binary)
6. Single-device lock:
   - lock owner can login and continue monitoring.
   - other users are blocked while lock is held.
   - login/logout does not stop ongoing job.
7. Direction mapping:
   - `X+ = 00001111`
   - `Y+ = 11000011`
   - `X- = 11110000`
   - `Y- = 00111100`
8. Debug/manual page:
   - `admin.html` (all logged users can open flow programming)
   - manual control for magnet / exposure / move / wait remains admin-only
9. Graph-like flow programming (all logged users):
   - visual step blocks
   - parameter input
   - loop step with nested children
   - run/cancel and status log
   - encrypted config export/import (`.mpcfg`)

## Project Layout

- `backend/`: Go server.
- `frontend/`: static Web UI.

## Run On Windows (Debug With Fake Wait)

1. `cd magnetic_printer/backend`
2. Build:
   - `go build`
3. Run:
   - `./backend.exe`
4. Open login page `http://localhost:<port>/index.html` (port shown in terminal, e.g. `5241`).
5. Login success redirects to `http://localhost:<port>/app.html`.
6. Hardware is mock by default (`UseMockHardware=true`, `SkipWaitInMock=true`).
7. Admin user can open `http://localhost:<port>/admin.html`.

## Deploy On Raspberry Pi

1. Copy `backend/config.example.json` to `backend/config.json`.
   - If you use the same wiring as existing Python scripts, start from `backend/config.rpi2.sample.json`.
2. Set:
   - `hardware.useMockHardware = false`
   - `hardware.skipWaitInMock = false`
3. Configure native hardware modules (pure Go, no Python exec):
   - `hardware.magnet` (TCA9548A + PCA9554 + MCP4725 + GPIO gate)
   - `hardware.motion` (pigpio C API wave chain; exact pulse count control with top limit switch)
   - `hardware.exposure` (DLP serial + framebuffer image render)
4. Install pigpio development library on Pi (for motion module):
   - `sudo apt-get install pigpio libpigpio-dev`
5. Build on Raspberry Pi (recommended for pigpio C API):
   - `cd magnetic_printer/backend`
   - `CGO_ENABLED=1 go build -o magnetic-printer-backend`
6. Optional cross-build from dev machine (CGO disabled):
   - `GOOS=linux GOARCH=arm GOARM=7 go build -o magnetic-printer-backend`
   - note: this build can run, but motion module will reject execution and ask for `CGO_ENABLED=1`.
7. Run on Pi:
   - `./magnetic-printer-backend`

## Notes

- Z axis direction is currently not included in mapping logic.
- Multi-material rotary plate is not implemented yet (reserved for future extension).
- In admin flow programming, `magnet_async` can run magnetic hold asynchronously.
- Use `wait_all_idle` as a synchronization barrier to wait all async sub-device tasks.
