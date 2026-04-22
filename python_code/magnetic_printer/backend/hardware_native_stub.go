//go:build !linux

package main

import (
	"errors"
	"log"
)

func newNativeHardwareBackend(cfg HardwareConfig, logger *log.Logger) (hardwareBackend, error) {
	_ = cfg
	_ = logger
	return nil, errors.New("native hardware backend is supported on linux only; set useMockHardware=true on this platform")
}
