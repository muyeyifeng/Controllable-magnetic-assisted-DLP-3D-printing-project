//go:build linux

package main

import (
	"context"
	"errors"
	"fmt"
	"image"
	_ "image/gif"
	_ "image/jpeg"
	_ "image/png"
	"io"
	"log"
	"math"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"

	"golang.org/x/sys/unix"
)

const (
	i2cSlaveIoctl = 0x0703
)

var (
	dlpCmdHandshake = []byte{0xA6, 0x01, 0x05}
	dlpCmdOn        = []byte{0xA6, 0x02, 0x02, 0x01}
	dlpCmdOff       = []byte{0xA6, 0x02, 0x02, 0x00}
	dlpCmdLEDOn     = []byte{0xA6, 0x02, 0x03, 0x01}
	dlpCmdLEDOff    = []byte{0xA6, 0x02, 0x03, 0x00}
)

type linuxHardwareBackend struct {
	logger   *log.Logger
	magnet   *magnetNative
	motion   *motionNative
	exposure *exposureNative
}

func newNativeHardwareBackend(cfg HardwareConfig, logger *log.Logger) (hardwareBackend, error) {
	b := &linuxHardwareBackend{
		logger:   logger,
		magnet:   newMagnetNative(cfg.Magnet, logger),
		motion:   newMotionNative(cfg.Motion, logger),
		exposure: newExposureNative(cfg.Exposure, logger),
	}
	return b, nil
}

func (b *linuxHardwareBackend) Prepare(ctx context.Context) error {
	if err := b.magnet.Prepare(ctx); err != nil {
		return err
	}
	if err := b.motion.Prepare(ctx); err != nil {
		return err
	}
	if err := b.exposure.Prepare(ctx); err != nil {
		return err
	}
	return nil
}

func (b *linuxHardwareBackend) MoveLayer(ctx context.Context, layer LayerPlanItem) error {
	return b.motion.MoveLayer(ctx, layer)
}

func (b *linuxHardwareBackend) Home(ctx context.Context) error {
	return b.motion.Home(ctx)
}

func (b *linuxHardwareBackend) ApplyMagneticField(ctx context.Context, layer LayerPlanItem) error {
	return b.magnet.ApplyMagneticField(ctx, layer)
}

func (b *linuxHardwareBackend) ExposeLayer(ctx context.Context, layer LayerPlanItem) error {
	return b.exposure.ExposeLayer(ctx, layer)
}

func (b *linuxHardwareBackend) Finish(ctx context.Context) error {
	err1 := b.exposure.Finish(ctx)
	err2 := b.motion.Finish(ctx)
	err3 := b.magnet.Finish(ctx)
	if err1 != nil {
		return err1
	}
	if err2 != nil {
		return err2
	}
	return err3
}

type sysfsGPIOPin struct {
	num       int
	exportNum int
	gpioPath  string
	valueFile *os.File
}

func newSysfsGPIOPin(num int) *sysfsGPIOPin {
	return &sysfsGPIOPin{num: num}
}

func (p *sysfsGPIOPin) Prepare(direction string) error {
	if p.num < 0 {
		return errors.New("invalid gpio pin")
	}
	gpioRoot := "/sys/class/gpio"
	exportNum := p.num
	gpioPath := filepath.Join(gpioRoot, fmt.Sprintf("gpio%d", exportNum))
	if _, err := os.Stat(gpioPath); err != nil {
		if !errors.Is(err, os.ErrNotExist) {
			return err
		}
		if err := os.WriteFile(filepath.Join(gpioRoot, "export"), []byte(strconv.Itoa(exportNum)), 0o644); err != nil {
			if isGPIOBusyErr(err) {
				// Already exported by another process.
			} else if isGPIOInvalidArgErr(err) {
				mapped, mapErr := resolveSysfsGPIOExportNumber(gpioRoot, p.num)
				if mapErr != nil {
					return fmt.Errorf("export gpio%d failed: %w (auto-remap failed: %v)", p.num, err, mapErr)
				}
				exportNum = mapped
				gpioPath = filepath.Join(gpioRoot, fmt.Sprintf("gpio%d", exportNum))
				if _, stErr := os.Stat(gpioPath); stErr != nil {
					if !errors.Is(stErr, os.ErrNotExist) {
						return stErr
					}
					if err2 := os.WriteFile(filepath.Join(gpioRoot, "export"), []byte(strconv.Itoa(exportNum)), 0o644); err2 != nil && !isGPIOBusyErr(err2) {
						return fmt.Errorf("export gpio%d (mapped from bcm%d) failed: %w", exportNum, p.num, err2)
					}
				}
			} else {
				return fmt.Errorf("export gpio%d failed: %w", p.num, err)
			}
		}
	}

	deadline := time.Now().Add(2 * time.Second)
	for {
		if _, err := os.Stat(gpioPath); err == nil {
			break
		}
		if time.Now().After(deadline) {
			return fmt.Errorf("gpio%d path did not appear", p.num)
		}
		time.Sleep(10 * time.Millisecond)
	}

	directionPath := filepath.Join(gpioPath, "direction")
	if err := os.WriteFile(directionPath, []byte(direction), 0o644); err != nil {
		return fmt.Errorf("set gpio%d direction failed: %w", p.num, err)
	}

	f, err := os.OpenFile(filepath.Join(gpioPath, "value"), os.O_RDWR, 0)
	if err != nil {
		return fmt.Errorf("open gpio%d value failed: %w", exportNum, err)
	}
	p.exportNum = exportNum
	p.gpioPath = gpioPath
	p.valueFile = f
	return nil
}

func (p *sysfsGPIOPin) Write(level int) error {
	if p.valueFile == nil {
		return errors.New("gpio not prepared")
	}
	b := byte('0')
	if level != 0 {
		b = byte('1')
	}
	_, err := p.valueFile.WriteAt([]byte{b}, 0)
	return err
}

func (p *sysfsGPIOPin) Read() (int, error) {
	if p.valueFile == nil {
		return 0, errors.New("gpio not prepared")
	}
	buf := []byte{'0'}
	_, err := p.valueFile.ReadAt(buf, 0)
	if err != nil && !errors.Is(err, io.EOF) {
		return 0, err
	}
	if buf[0] == '1' {
		return 1, nil
	}
	return 0, nil
}

func (p *sysfsGPIOPin) Close() error {
	if p.valueFile != nil {
		err := p.valueFile.Close()
		p.valueFile = nil
		return err
	}
	return nil
}

func isGPIOBusyErr(err error) bool {
	return strings.Contains(strings.ToLower(err.Error()), "busy")
}

func isGPIOInvalidArgErr(err error) bool {
	return strings.Contains(strings.ToLower(err.Error()), "invalid argument")
}

func resolveSysfsGPIOExportNumber(gpioRoot string, bcm int) (int, error) {
	chips, err := filepath.Glob(filepath.Join(gpioRoot, "gpiochip*"))
	if err != nil {
		return 0, err
	}
	if len(chips) == 0 {
		return 0, errors.New("no gpiochip found in /sys/class/gpio")
	}
	type cand struct {
		exportNum int
		score     int
	}
	candidates := make([]cand, 0, len(chips))
	for _, chip := range chips {
		base, err1 := readIntFromFile(filepath.Join(chip, "base"))
		ngpio, err2 := readIntFromFile(filepath.Join(chip, "ngpio"))
		if err1 != nil || err2 != nil || ngpio <= 0 {
			continue
		}
		if bcm < 0 || bcm >= ngpio {
			continue
		}
		labelBytes, _ := os.ReadFile(filepath.Join(chip, "label"))
		label := strings.ToLower(strings.TrimSpace(string(labelBytes)))
		score := 0
		if strings.Contains(label, "bcm") || strings.Contains(label, "pinctrl") {
			score += 10
		}
		if strings.Contains(label, "raspberry") {
			score += 5
		}
		candidates = append(candidates, cand{
			exportNum: base + bcm,
			score:     score,
		})
	}
	if len(candidates) == 0 {
		return 0, fmt.Errorf("cannot map bcm gpio %d to global sysfs number", bcm)
	}
	best := candidates[0]
	for _, c := range candidates[1:] {
		if c.score > best.score {
			best = c
		}
	}
	return best.exportNum, nil
}

func readIntFromFile(path string) (int, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		return 0, err
	}
	return strconv.Atoi(strings.TrimSpace(string(b)))
}

type i2cNative struct {
	fd int
	mu sync.Mutex
}

func openI2CNative(bus int) (*i2cNative, error) {
	if bus < 0 {
		bus = 1
	}
	path := fmt.Sprintf("/dev/i2c-%d", bus)
	fd, err := unix.Open(path, unix.O_RDWR, 0)
	if err != nil {
		return nil, fmt.Errorf("open i2c bus failed: %w", err)
	}
	return &i2cNative{fd: fd}, nil
}

func (b *i2cNative) close() error {
	if b == nil || b.fd <= 0 {
		return nil
	}
	fd := b.fd
	b.fd = -1
	return unix.Close(fd)
}

func (b *i2cNative) write(addr int, data []byte) error {
	b.mu.Lock()
	defer b.mu.Unlock()
	if err := b.setAddr(addr); err != nil {
		return err
	}
	n, err := unix.Write(b.fd, data)
	if err != nil {
		return err
	}
	if n != len(data) {
		return io.ErrShortWrite
	}
	return nil
}

func (b *i2cNative) readReg(addr int, reg byte) (byte, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	if err := b.setAddr(addr); err != nil {
		return 0, err
	}
	if n, err := unix.Write(b.fd, []byte{reg}); err != nil || n != 1 {
		if err != nil {
			return 0, err
		}
		return 0, io.ErrShortWrite
	}
	buf := []byte{0}
	n, err := unix.Read(b.fd, buf)
	if err != nil {
		return 0, err
	}
	if n != 1 {
		return 0, io.ErrUnexpectedEOF
	}
	return buf[0], nil
}

func (b *i2cNative) setAddr(addr int) error {
	_, _, errno := unix.Syscall(unix.SYS_IOCTL, uintptr(b.fd), uintptr(i2cSlaveIoctl), uintptr(addr))
	if errno != 0 {
		return errno
	}
	return nil
}

type magnetNative struct {
	cfg    MagnetNativeConfig
	logger *log.Logger

	mu       sync.Mutex
	prepared bool
	enable   *sysfsGPIOPin
	i2c      *i2cNative
}

func newMagnetNative(cfg MagnetNativeConfig, logger *log.Logger) *magnetNative {
	return &magnetNative{cfg: cfg, logger: logger}
}

func (m *magnetNative) Prepare(ctx context.Context) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.prepared {
		return nil
	}
	enable := newSysfsGPIOPin(m.cfg.EnableGPIOPin)
	if err := enable.Prepare("out"); err != nil {
		return err
	}
	if err := enable.Write(1); err != nil {
		return err
	}
	bus, err := openI2CNative(m.cfg.I2CBus)
	if err != nil {
		return err
	}
	m.enable = enable
	m.i2c = bus
	m.prepared = true
	return nil
}

func (m *magnetNative) ApplyMagneticField(ctx context.Context, layer LayerPlanItem) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	if !m.prepared || m.i2c == nil || m.enable == nil {
		return errors.New("magnet module not prepared")
	}
	bits := normalizeDirectionBits(layer.DirectionBits)
	if err := validateDirectionBits(bits); err != nil {
		return err
	}
	out, err := bitsToOutputByte(bits)
	if err != nil {
		return err
	}

	if err := m.enable.Write(0); err != nil {
		return err
	}

	defer func() {
		_ = m.writeDAC(0)
		_ = m.disableTCA()
		_ = m.enable.Write(1)
	}()

	if err := m.selectTCA(); err != nil {
		return err
	}
	if err := m.writePCA9554(out); err != nil {
		return err
	}
	if err := m.writeDAC(layer.MagneticVoltage); err != nil {
		return err
	}
	if layer.MagneticHoldS > 0 {
		if err := sleepCtx(ctx, time.Duration(layer.MagneticHoldS*float64(time.Second))); err != nil {
			return err
		}
	}
	return nil
}

func (m *magnetNative) Finish(ctx context.Context) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	if !m.prepared {
		return nil
	}
	_ = m.writeDAC(0)
	_ = m.disableTCA()
	_ = m.enable.Write(1)
	_ = m.enable.Close()
	err := m.i2c.close()
	m.i2c = nil
	m.enable = nil
	m.prepared = false
	return err
}

func (m *magnetNative) selectTCA() error {
	mask := byte(0)
	if m.cfg.TCAChannel >= 0 && m.cfg.TCAChannel <= 7 {
		mask = byte(1 << m.cfg.TCAChannel)
	}
	return m.i2c.write(m.cfg.TCAAddress, []byte{mask})
}

func (m *magnetNative) disableTCA() error {
	return m.i2c.write(m.cfg.TCAAddress, []byte{0x00})
}

func (m *magnetNative) writePCA9554(out byte) error {
	const (
		regOutput = 0x01
		regConfig = 0x03
	)
	if err := m.i2c.write(m.cfg.PCA9554Address, []byte{regConfig, 0x00}); err != nil {
		return err
	}
	if err := m.i2c.write(m.cfg.PCA9554Address, []byte{regOutput, out}); err != nil {
		return err
	}
	readback, err := m.i2c.readReg(m.cfg.PCA9554Address, regOutput)
	if err != nil {
		return err
	}
	if readback != out {
		return fmt.Errorf("pca9554 readback mismatch: want=0x%02X got=0x%02X", out, readback)
	}
	return nil
}

func (m *magnetNative) writeDAC(voltage float64) error {
	v := voltage
	if v < 0 {
		v = 0
	}
	if m.cfg.DACVRef <= 0 {
		return errors.New("invalid dac vref")
	}
	if v > m.cfg.DACVRef {
		v = m.cfg.DACVRef
	}
	code := int(math.Round((v / m.cfg.DACVRef) * 4095.0))
	if code < 0 {
		code = 0
	}
	if code > 4095 {
		code = 4095
	}
	high := byte((code >> 4) & 0xFF)
	low := byte((code & 0x0F) << 4)
	return m.i2c.write(m.cfg.MCP4725Address, []byte{0x40, high, low})
}

func bitsToOutputByte(bits string) (byte, error) {
	if len(bits) != 8 {
		return 0, errors.New("direction bits must be 8 chars")
	}
	var out byte
	for i := 0; i < len(bits); i++ {
		ch := bits[i]
		if ch != '0' && ch != '1' {
			return 0, errors.New("direction bits must contain only 0 or 1")
		}
		if ch == '1' {
			out |= byte(1 << i)
		}
	}
	return out, nil
}

type motionNative struct {
	cfg    MotionNativeConfig
	logger *log.Logger

	mu       sync.Mutex
	prepared bool
	driver   motionDriver
}

type motionDriver interface {
	Prepare() error
	SetDirection(moveDirection string) error
	Enable(enable bool) error
	ReadTop() (int, error)
	MoveExact(ctx context.Context, steps int, freqHz int, pulseWidthUS int) error
	Close() error
}

func newMotionNative(cfg MotionNativeConfig, logger *log.Logger) *motionNative {
	return &motionNative{cfg: cfg, logger: logger}
}

func (m *motionNative) Prepare(ctx context.Context) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.prepared {
		return nil
	}
	driver, err := newMotionDriver(m.cfg, m.logger)
	if err != nil {
		return err
	}
	if err := driver.Prepare(); err != nil {
		return err
	}
	m.driver = driver
	m.prepared = true
	return nil
}

func (m *motionNative) MoveLayer(ctx context.Context, layer LayerPlanItem) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	if !m.prepared {
		return errors.New("motion module not prepared")
	}
	moveDirection, err := normalizeMoveDirection(layer.MoveDirection)
	if err != nil {
		return err
	}
	steps := m.stepsFromMM(layer.LayerThicknessMM)
	if steps <= 0 {
		return nil
	}
	return m.runSteps(ctx, moveDirection, steps)
}

func (m *motionNative) Home(ctx context.Context) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	if !m.prepared {
		return errors.New("motion module not prepared")
	}
	if m.cfg.HomeTopPin <= 0 {
		return errors.New("homeTopPin is not configured")
	}
	stop := m.cfg.HomeStopLevel
	cur, err := m.driver.ReadTop()
	if err != nil {
		return err
	}
	if cur == stop {
		return nil
	}
	homeDir, err := normalizeMoveDirection(m.cfg.HomeDirection)
	if err != nil {
		return err
	}
	if err := m.driver.SetDirection(homeDir); err != nil {
		return err
	}
	if err := m.driver.Enable(true); err != nil {
		return err
	}
	defer func() {
		_ = m.driver.Enable(false)
	}()

	chunk := m.cfg.HomeChunkStep
	if chunk <= 0 {
		chunk = 200
	}
	reportEvery := m.cfg.HomeReport
	if reportEvery <= 0 {
		reportEvery = 1000
	}

	total := 0
	for {
		if m.cfg.HomeMaxSteps > 0 && total >= m.cfg.HomeMaxSteps {
			return fmt.Errorf("home reached max steps: %d", total)
		}
		runCount := chunk
		if m.cfg.HomeMaxSteps > 0 && total+runCount > m.cfg.HomeMaxSteps {
			runCount = m.cfg.HomeMaxSteps - total
		}
		if runCount <= 0 {
			return fmt.Errorf("home reached max steps: %d", total)
		}
		if err := m.driver.MoveExact(ctx, runCount, m.homeFrequencyHz(), m.cfg.PulseWidthUS); err != nil {
			return err
		}
		total += runCount
		level, err := m.driver.ReadTop()
		if err != nil {
			return err
		}
		if level == stop {
			return nil
		}
		if total%reportEvery == 0 {
			m.logger.Printf("home progress: steps=%d level=%d", total, level)
		}
	}
}

func (m *motionNative) Finish(ctx context.Context) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	if !m.prepared {
		return nil
	}
	if m.driver != nil {
		_ = m.driver.Enable(false)
		_ = m.driver.Close()
		m.driver = nil
	}
	m.prepared = false
	return nil
}

func (m *motionNative) stepsFromMM(mm float64) int {
	if mm <= 0 || m.cfg.LeadMM <= 0 || m.cfg.StepsPerRev <= 0 {
		return 0
	}
	steps := int(math.Round((mm * float64(m.cfg.StepsPerRev)) / m.cfg.LeadMM))
	if steps < 0 {
		return 0
	}
	return steps
}

func (m *motionNative) runSteps(ctx context.Context, moveDirection string, steps int) error {
	if err := m.driver.SetDirection(moveDirection); err != nil {
		return err
	}
	if err := m.driver.Enable(true); err != nil {
		return err
	}
	defer func() {
		_ = m.driver.Enable(false)
	}()
	return m.driver.MoveExact(ctx, steps, m.moveFrequencyHz(), m.cfg.PulseWidthUS)
}

func (m *motionNative) moveFrequencyHz() int {
	freq := m.cfg.MoveFrequency
	if freq <= 0 {
		freq = m.cfg.FrequencyHz
	}
	if freq <= 0 {
		freq = 800
	}
	return freq
}

func (m *motionNative) homeFrequencyHz() int {
	freq := m.cfg.HomeFrequency
	if freq <= 0 {
		freq = m.cfg.FrequencyHz
	}
	if freq <= 0 {
		freq = 1600
	}
	return freq
}

type exposureNative struct {
	cfg    ExposureNativeConfig
	logger *log.Logger

	mu       sync.Mutex
	prepared bool
	serial   *serialNative
	fb       *framebufferNative
}

func newExposureNative(cfg ExposureNativeConfig, logger *log.Logger) *exposureNative {
	return &exposureNative{cfg: cfg, logger: logger}
}

func (e *exposureNative) Prepare(ctx context.Context) error {
	e.mu.Lock()
	defer e.mu.Unlock()
	if e.prepared {
		return nil
	}
	serialPort, err := openSerialNative(e.cfg.SerialPort, e.cfg.BaudRate, time.Duration(e.cfg.ReadTimeoutMS)*time.Millisecond)
	if err != nil {
		return err
	}
	fb, err := openFramebufferNative(e.cfg.FramebufferDevice)
	if err != nil {
		_ = serialPort.Close()
		return err
	}

	e.serial = serialPort
	e.fb = fb
	e.prepared = true

	if err := e.sendDLPCommand(dlpCmdHandshake, "handshake"); err != nil {
		return err
	}
	if err := e.sendDLPCommand(dlpCmdOn, "dlp_on"); err != nil {
		return err
	}
	if err := e.sendDLPCommand(dlpCmdLEDOff, "led_off"); err != nil {
		return err
	}
	return nil
}

func (e *exposureNative) ExposeLayer(ctx context.Context, layer LayerPlanItem) error {
	e.mu.Lock()
	defer e.mu.Unlock()
	if !e.prepared || e.serial == nil || e.fb == nil {
		return errors.New("exposure module not prepared")
	}
	if strings.TrimSpace(layer.ImagePath) == "" {
		return errors.New("image path is required")
	}
	if err := e.fb.RenderImage(layer.ImagePath); err != nil {
		return err
	}
	if e.cfg.FramebufferSettleM > 0 {
		if err := sleepCtx(ctx, time.Duration(e.cfg.FramebufferSettleM)*time.Millisecond); err != nil {
			return err
		}
	}
	if err := e.setBrightness(layer.ExposureIntensity); err != nil {
		return err
	}
	if err := e.sendDLPCommand(dlpCmdLEDOn, "led_on"); err != nil {
		return err
	}
	defer func() {
		_ = e.sendDLPCommand(dlpCmdLEDOff, "led_off")
	}()
	if layer.ExposureS > 0 {
		if err := sleepCtx(ctx, time.Duration(layer.ExposureS*float64(time.Second))); err != nil {
			return err
		}
	}
	return nil
}

func (e *exposureNative) Finish(ctx context.Context) error {
	e.mu.Lock()
	defer e.mu.Unlock()
	if !e.prepared {
		return nil
	}
	_ = e.sendDLPCommand(dlpCmdLEDOff, "led_off")
	_ = e.sendDLPCommand(dlpCmdOff, "dlp_off")
	if e.serial != nil {
		_ = e.serial.Close()
		e.serial = nil
	}
	if e.fb != nil {
		_ = e.fb.Close()
		e.fb = nil
	}
	e.prepared = false
	return nil
}

func (e *exposureNative) setBrightness(value int) error {
	v := value
	if v < 0 {
		v = 0
	}
	if v > 255 {
		v = 255
	}
	cmd := []byte{0xA6, 0x02, 0x10, byte(v)}
	return e.sendDLPCommand(cmd, fmt.Sprintf("brightness_%d", v))
}

func (e *exposureNative) sendDLPCommand(cmd []byte, desc string) error {
	respBytes := e.cfg.ResponseReadBytes
	if respBytes <= 0 {
		respBytes = 10
	}
	resp, err := e.serial.Send(cmd, respBytes)
	if err != nil {
		return fmt.Errorf("%s failed: %w", desc, err)
	}
	if len(resp) == 0 {
		return fmt.Errorf("%s timeout", desc)
	}
	if resp[len(resp)-1] == 0xE0 {
		return fmt.Errorf("%s rejected by dlp, response=%s", desc, bytesHex(resp))
	}
	return nil
}

type serialNative struct {
	fd int
	mu sync.Mutex
}

func openSerialNative(path string, baud int, timeout time.Duration) (*serialNative, error) {
	fd, err := unix.Open(path, unix.O_RDWR|unix.O_NOCTTY|unix.O_SYNC, 0o666)
	if err != nil {
		return nil, fmt.Errorf("open serial failed: %w", err)
	}
	tio, err := unix.IoctlGetTermios(fd, unix.TCGETS)
	if err != nil {
		_ = unix.Close(fd)
		return nil, fmt.Errorf("get serial termios failed: %w", err)
	}
	speed, err := baudToUnix(baud)
	if err != nil {
		_ = unix.Close(fd)
		return nil, err
	}

	// raw 8N1
	tio.Iflag &^= unix.IGNBRK | unix.BRKINT | unix.PARMRK | unix.ISTRIP | unix.INLCR | unix.IGNCR | unix.ICRNL | unix.IXON
	tio.Oflag &^= unix.OPOST
	tio.Lflag &^= unix.ECHO | unix.ECHONL | unix.ICANON | unix.ISIG | unix.IEXTEN
	tio.Cflag &^= unix.CSIZE | unix.PARENB
	tio.Cflag |= unix.CS8 | unix.CLOCAL | unix.CREAD

	tio.Cc[unix.VMIN] = 0
	vtime := int(timeout / (100 * time.Millisecond))
	if vtime <= 0 {
		vtime = 1
	}
	if vtime > 255 {
		vtime = 255
	}
	tio.Cc[unix.VTIME] = uint8(vtime)

	tio.Ispeed = speed
	tio.Ospeed = speed
	if err := unix.IoctlSetTermios(fd, unix.TCSETS, tio); err != nil {
		_ = unix.Close(fd)
		return nil, fmt.Errorf("set serial termios failed: %w", err)
	}
	return &serialNative{fd: fd}, nil
}

func (s *serialNative) Close() error {
	if s == nil || s.fd <= 0 {
		return nil
	}
	fd := s.fd
	s.fd = -1
	return unix.Close(fd)
}

func (s *serialNative) Send(cmd []byte, maxRead int) ([]byte, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.fd <= 0 {
		return nil, errors.New("serial not open")
	}
	if maxRead <= 0 {
		maxRead = 10
	}
	if err := flushSerialInput(s.fd); err != nil {
		return nil, err
	}
	n, err := unix.Write(s.fd, cmd)
	if err != nil {
		return nil, err
	}
	if n != len(cmd) {
		return nil, io.ErrShortWrite
	}
	buf := make([]byte, maxRead)
	rn, err := unix.Read(s.fd, buf)
	if err != nil {
		return nil, err
	}
	if rn <= 0 {
		return []byte{}, nil
	}
	return append([]byte(nil), buf[:rn]...), nil
}

func flushSerialInput(fd int) error {
	for i := 0; i < 4; i++ {
		buf := make([]byte, 64)
		_, err := unix.Read(fd, buf)
		if err != nil {
			if errors.Is(err, unix.EAGAIN) {
				return nil
			}
			return nil
		}
	}
	return nil
}

func baudToUnix(baud int) (uint32, error) {
	switch baud {
	case 9600:
		return unix.B9600, nil
	case 19200:
		return unix.B19200, nil
	case 38400:
		return unix.B38400, nil
	case 57600:
		return unix.B57600, nil
	case 115200:
		return unix.B115200, nil
	case 230400:
		return unix.B230400, nil
	default:
		return 0, fmt.Errorf("unsupported baud rate: %d", baud)
	}
}

type framebufferNative struct {
	path   string
	file   *os.File
	width  int
	height int
	stride int
	bpp    int
}

func openFramebufferNative(path string) (*framebufferNative, error) {
	if strings.TrimSpace(path) == "" {
		path = "/dev/fb0"
	}
	name := filepath.Base(path)
	sysRoot := filepath.Join("/sys/class/graphics", name)
	sizeRaw, err := os.ReadFile(filepath.Join(sysRoot, "virtual_size"))
	if err != nil {
		return nil, fmt.Errorf("read framebuffer size failed: %w", err)
	}
	parts := strings.Split(strings.TrimSpace(string(sizeRaw)), ",")
	if len(parts) != 2 {
		return nil, errors.New("invalid framebuffer virtual_size format")
	}
	width, err := strconv.Atoi(strings.TrimSpace(parts[0]))
	if err != nil {
		return nil, err
	}
	height, err := strconv.Atoi(strings.TrimSpace(parts[1]))
	if err != nil {
		return nil, err
	}
	bpp, err := readSysInt(filepath.Join(sysRoot, "bits_per_pixel"))
	if err != nil {
		return nil, err
	}
	stride, err := readSysInt(filepath.Join(sysRoot, "stride"))
	if err != nil {
		stride = width * (bpp / 8)
	}
	f, err := os.OpenFile(path, os.O_RDWR, 0)
	if err != nil {
		return nil, fmt.Errorf("open framebuffer failed: %w", err)
	}
	return &framebufferNative{
		path:   path,
		file:   f,
		width:  width,
		height: height,
		stride: stride,
		bpp:    bpp,
	}, nil
}

func (f *framebufferNative) Close() error {
	if f == nil || f.file == nil {
		return nil
	}
	err := f.file.Close()
	f.file = nil
	return err
}

func (f *framebufferNative) RenderImage(path string) error {
	srcFile, err := os.Open(filepath.Clean(path))
	if err != nil {
		return err
	}
	defer srcFile.Close()

	img, _, err := image.Decode(srcFile)
	if err != nil {
		return err
	}
	frame := fitImageNearest(img, f.width, f.height)
	raw, err := rgbaToFramebuffer(frame, f.bpp, f.stride)
	if err != nil {
		return err
	}
	_, err = f.file.WriteAt(raw, 0)
	return err
}

func fitImageNearest(src image.Image, width, height int) *image.RGBA {
	dst := image.NewRGBA(image.Rect(0, 0, width, height))
	sb := src.Bounds()
	sw := sb.Dx()
	sh := sb.Dy()
	if sw <= 0 || sh <= 0 {
		return dst
	}
	scale := math.Min(float64(width)/float64(sw), float64(height)/float64(sh))
	if scale <= 0 {
		scale = 1
	}
	tw := int(math.Round(float64(sw) * scale))
	th := int(math.Round(float64(sh) * scale))
	if tw < 1 {
		tw = 1
	}
	if th < 1 {
		th = 1
	}
	ox := (width - tw) / 2
	oy := (height - th) / 2
	for y := 0; y < th; y++ {
		sy := sb.Min.Y + int(float64(y)*float64(sh)/float64(th))
		if sy >= sb.Max.Y {
			sy = sb.Max.Y - 1
		}
		for x := 0; x < tw; x++ {
			sx := sb.Min.X + int(float64(x)*float64(sw)/float64(tw))
			if sx >= sb.Max.X {
				sx = sb.Max.X - 1
			}
			dst.Set(ox+x, oy+y, src.At(sx, sy))
		}
	}
	return dst
}

func rgbaToFramebuffer(img *image.RGBA, bpp, stride int) ([]byte, error) {
	w := img.Bounds().Dx()
	h := img.Bounds().Dy()
	if stride <= 0 {
		stride = w * (bpp / 8)
	}
	out := make([]byte, stride*h)
	switch bpp {
	case 16:
		for y := 0; y < h; y++ {
			srcOff := y * img.Stride
			dstOff := y * stride
			for x := 0; x < w; x++ {
				r := img.Pix[srcOff+x*4+0]
				g := img.Pix[srcOff+x*4+1]
				b := img.Pix[srcOff+x*4+2]
				v := uint16(r>>3)<<11 | uint16(g>>2)<<5 | uint16(b>>3)
				out[dstOff+x*2+0] = byte(v & 0xFF)
				out[dstOff+x*2+1] = byte(v >> 8)
			}
		}
	case 24:
		for y := 0; y < h; y++ {
			srcOff := y * img.Stride
			dstOff := y * stride
			for x := 0; x < w; x++ {
				out[dstOff+x*3+0] = img.Pix[srcOff+x*4+2]
				out[dstOff+x*3+1] = img.Pix[srcOff+x*4+1]
				out[dstOff+x*3+2] = img.Pix[srcOff+x*4+0]
			}
		}
	case 32:
		for y := 0; y < h; y++ {
			srcOff := y * img.Stride
			dstOff := y * stride
			for x := 0; x < w; x++ {
				out[dstOff+x*4+0] = img.Pix[srcOff+x*4+2]
				out[dstOff+x*4+1] = img.Pix[srcOff+x*4+1]
				out[dstOff+x*4+2] = img.Pix[srcOff+x*4+0]
				out[dstOff+x*4+3] = 0
			}
		}
	default:
		return nil, fmt.Errorf("unsupported framebuffer bpp: %d", bpp)
	}
	return out, nil
}

func readSysInt(path string) (int, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return 0, err
	}
	return strconv.Atoi(strings.TrimSpace(string(raw)))
}

func bytesHex(data []byte) string {
	if len(data) == 0 {
		return ""
	}
	parts := make([]string, len(data))
	for i := range data {
		parts[i] = fmt.Sprintf("%02X", data[i])
	}
	return strings.Join(parts, " ")
}
