//go:build linux && !cgo

package main

import (
	"errors"
	"log"
)

func newMotionDriver(cfg MotionNativeConfig, logger *log.Logger) (motionDriver, error) {
	_ = cfg
	_ = logger
	return nil, errors.New("motion backend requires pigpio C API; rebuild linux binary with CGO_ENABLED=1 and libpigpio installed")
}
