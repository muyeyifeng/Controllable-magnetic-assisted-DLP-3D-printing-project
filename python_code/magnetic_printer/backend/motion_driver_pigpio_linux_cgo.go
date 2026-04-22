//go:build linux && cgo

package main

/*
#cgo LDFLAGS: -lpigpio -lrt -lpthread
#include <pigpio.h>
*/
import "C"

import (
	"context"
	"errors"
	"fmt"
	"log"
	"time"
	"unsafe"
)

type motionDriverPigpio struct {
	cfg    MotionNativeConfig
	logger *log.Logger

	initialized bool
}

func newMotionDriver(cfg MotionNativeConfig, logger *log.Logger) (motionDriver, error) {
	return &motionDriverPigpio{
		cfg:    cfg,
		logger: logger,
	}, nil
}

func (d *motionDriverPigpio) Prepare() error {
	rc := C.gpioInitialise()
	if rc < 0 {
		return pigpioError("gpioInitialise", rc)
	}
	d.initialized = true
	if err := d.setMode(d.cfg.StepPin, C.PI_OUTPUT); err != nil {
		return err
	}
	if err := d.setMode(d.cfg.DirPin, C.PI_OUTPUT); err != nil {
		return err
	}
	if err := d.setMode(d.cfg.EnablePin, C.PI_OUTPUT); err != nil {
		return err
	}
	if d.cfg.HomeTopPin > 0 {
		if err := d.setMode(d.cfg.HomeTopPin, C.PI_INPUT); err != nil {
			return err
		}
	}
	if err := d.writePin(d.cfg.StepPin, 0); err != nil {
		return err
	}
	return d.Enable(false)
}

func (d *motionDriverPigpio) SetDirection(moveDirection string) error {
	moveDirection, err := normalizeMoveDirection(moveDirection)
	if err != nil {
		return err
	}
	level := 0
	if moveDirection == moveDirUp {
		level = 1
	}
	return d.writePin(d.cfg.DirPin, level)
}

func (d *motionDriverPigpio) Enable(enable bool) error {
	levelOn := 1
	if d.cfg.EnableLow {
		levelOn = 0
	}
	levelOff := 1 - levelOn
	if enable {
		return d.writePin(d.cfg.EnablePin, levelOn)
	}
	return d.writePin(d.cfg.EnablePin, levelOff)
}

func (d *motionDriverPigpio) ReadTop() (int, error) {
	if d.cfg.HomeTopPin <= 0 {
		return 0, errors.New("homeTopPin is not configured")
	}
	rc := C.gpioRead(C.uint(d.cfg.HomeTopPin))
	if rc < 0 {
		return 0, pigpioError("gpioRead", rc)
	}
	return int(rc), nil
}

func (d *motionDriverPigpio) MoveExact(ctx context.Context, steps int, freqHz int, pulseWidthUS int) error {
	if steps <= 0 {
		return nil
	}
	if freqHz <= 0 {
		return errors.New("frequency must be > 0")
	}
	if pulseWidthUS <= 0 {
		return errors.New("pulseWidthUs must be > 0")
	}
	periodUS := int(1_000_000 / freqHz)
	if periodUS <= 0 || pulseWidthUS >= periodUS {
		return fmt.Errorf("pulseWidthUs(%d) must be < periodUs(%d)", pulseWidthUS, periodUS)
	}
	lowUS := periodUS - pulseWidthUS

	stepMask := uint32(1 << uint(d.cfg.StepPin))
	pulses := []C.gpioPulse_t{
		{
			gpioOn:  C.uint(stepMask),
			gpioOff: C.uint(0),
			usDelay: C.uint(pulseWidthUS),
		},
		{
			gpioOn:  C.uint(0),
			gpioOff: C.uint(stepMask),
			usDelay: C.uint(lowUS),
		},
	}

	if rc := C.gpioWaveClear(); rc < 0 {
		return pigpioError("gpioWaveClear", rc)
	}
	if rc := C.gpioWaveAddGeneric(C.uint(len(pulses)), (*C.gpioPulse_t)(unsafe.Pointer(&pulses[0]))); rc < 0 {
		return pigpioError("gpioWaveAddGeneric", rc)
	}
	wid := C.gpioWaveCreate()
	if wid < 0 {
		return pigpioError("gpioWaveCreate", wid)
	}
	widU := C.uint(wid)
	defer func() {
		_ = C.gpioWaveDelete(widU)
	}()

	remaining := steps
	for remaining > 0 {
		chunk := remaining
		if chunk > 65535 {
			chunk = 65535
		}
		chain := []byte{
			255, 0, byte(widU),
			255, 1, byte(chunk & 0xFF), byte((chunk >> 8) & 0xFF),
		}
		rc := C.gpioWaveChain((*C.char)(unsafe.Pointer(&chain[0])), C.uint(len(chain)))
		if rc < 0 {
			return pigpioError("gpioWaveChain", rc)
		}
		for C.gpioWaveTxBusy() == 1 {
			select {
			case <-ctx.Done():
				C.gpioWaveTxStop()
				return ctx.Err()
			default:
				time.Sleep(500 * time.Microsecond)
			}
		}
		remaining -= chunk
	}
	return nil
}

func (d *motionDriverPigpio) Close() error {
	if !d.initialized {
		return nil
	}
	C.gpioWaveTxStop()
	C.gpioWaveClear()
	C.gpioTerminate()
	d.initialized = false
	return nil
}

func (d *motionDriverPigpio) setMode(pin int, mode C.uint) error {
	rc := C.gpioSetMode(C.uint(pin), mode)
	if rc < 0 {
		return pigpioError("gpioSetMode", rc)
	}
	return nil
}

func (d *motionDriverPigpio) writePin(pin, level int) error {
	rc := C.gpioWrite(C.uint(pin), C.uint(level))
	if rc < 0 {
		return pigpioError("gpioWrite", rc)
	}
	return nil
}

func pigpioError(op string, rc C.int) error {
	msg := pigpioErrorText(int(rc))
	if msg == "" {
		return fmt.Errorf("%s failed: rc=%d", op, int(rc))
	}
	return fmt.Errorf("%s failed: rc=%d (%s)", op, int(rc), msg)
}

func pigpioErrorText(code int) string {
	switch code {
	case -1:
		return "PI_INIT_FAILED"
	case -2:
		return "PI_BAD_USER_GPIO"
	case -3:
		return "PI_BAD_GPIO"
	case -4:
		return "PI_BAD_MODE"
	case -5:
		return "PI_BAD_LEVEL"
	case -6:
		return "PI_BAD_PUD"
	case -7:
		return "PI_BAD_PULSEWIDTH"
	case -8:
		return "PI_BAD_DUTYCYCLE"
	case -67:
		return "PI_BAD_WAVE_ID"
	case -68:
		return "PI_TOO_MANY_CBS"
	case -69:
		return "PI_TOO_MANY_OOL"
	case -70:
		return "PI_EMPTY_WAVEFORM"
	case -73:
		return "PI_BAD_CHAIN_LOOP"
	case -74:
		return "PI_CHAIN_COUNTER"
	case -75:
		return "PI_BAD_CHAIN_CMD"
	case -76:
		return "PI_BAD_CHAIN_DELAY"
	case -77:
		return "PI_CHAIN_NESTING"
	case -78:
		return "PI_CHAIN_TOO_BIG"
	case -79:
		return "PI_DEPRECATED"
	case -80:
		return "PI_BAD_SER_OFFSET"
	case -81:
		return "PI_GPIO_IN_USE"
	case -82:
		return "PI_BAD_SERIAL_COUNT"
	case -83:
		return "PI_BAD_PARAM_NUM"
	case -84:
		return "PI_DUP_TAG"
	case -85:
		return "PI_TOO_MANY_TAGS"
	case -86:
		return "PI_BAD_SCRIPT_ID"
	case -87:
		return "PI_BAD_SER_DEVICE"
	case -88:
		return "PI_BAD_SER_SPEED"
	case -89:
		return "PI_BAD_PARAM"
	case -90:
		return "PI_NOT_HALTED"
	case -91:
		return "PI_SCRIPT_NOT_READY"
	case -92:
		return "PI_BAD_TAG"
	case -93:
		return "PI_BAD_MICS_DELAY"
	case -94:
		return "PI_BAD_MILS_DELAY"
	case -95:
		return "PI_BAD_WAVE_ID"
	case -96:
		return "PI_TOO_MANY_CBS"
	case -97:
		return "PI_TOO_MANY_OOL"
	case -98:
		return "PI_EMPTY_WAVEFORM"
	case -99:
		return "PI_NO_WAVEFORM_ID"
	default:
		return ""
	}
}
