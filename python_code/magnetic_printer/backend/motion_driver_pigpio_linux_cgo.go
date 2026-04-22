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
	defer func() {
		_ = C.gpioWaveDelete(wid)
	}()

	remaining := steps
	for remaining > 0 {
		chunk := remaining
		if chunk > 65535 {
			chunk = 65535
		}
		chain := []byte{
			255, 0, byte(wid),
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
	msg := C.GoString(C.gpioError(rc))
	if msg == "" {
		return fmt.Errorf("%s failed: rc=%d", op, int(rc))
	}
	return fmt.Errorf("%s failed: rc=%d (%s)", op, int(rc), msg)
}
