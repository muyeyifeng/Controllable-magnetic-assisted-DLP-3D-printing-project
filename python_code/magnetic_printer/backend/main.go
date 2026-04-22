package main

import (
	"archive/zip"
	"context"
	"crypto/aes"
	"crypto/cipher"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"log"
	"mime/multipart"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"
)

const (
	stateIdle      = "idle"
	stateRunning   = "running"
	stateCanceling = "canceling"
	stateCompleted = "completed"
	stateFailed    = "failed"
	stateCanceled  = "canceled"
)

const (
	xPositiveBits = "00001111"
	xNegativeBits = "11110000"
	yPositiveBits = "11000011"
	yNegativeBits = "00111100"
	moveDirDown   = "down"
	moveDirUp     = "up"
)

const (
	flowConfigMagic    = "MPRF1"
	flowConfigVersion  = 1
	flowConfigMaxBytes = 4 << 20
)

const (
	flowImageSourceManual  = "manual"
	flowImageSourceSlice   = "slice"
	flowMagnetSourceManual = "manual"
	flowMagnetSourceSlice  = "slice"
)

type AppConfig struct {
	ListenAddr   string         `json:"listenAddr"`
	DataRoot     string         `json:"dataRoot"`
	FrontendRoot string         `json:"frontendRoot"`
	AdminUsers   []string       `json:"adminUsers"`
	Hardware     HardwareConfig `json:"hardware"`
}

type HardwareConfig struct {
	UseMockHardware          bool     `json:"useMockHardware"`
	SkipWaitInMock           bool     `json:"skipWaitInMock"`
	ScriptsRoot              string   `json:"scriptsRoot"`
	CommandTimeoutSeconds    int      `json:"commandTimeoutSeconds"`
	MagnetCommandTemplate    []string `json:"magnetCommandTemplate"`
	ExposureCommandTemp      []string `json:"exposureCommandTemplate"`
	LayerMoveCommandTemp     []string `json:"layerMoveCommandTemplate"`
	LayerMoveDownCommandTemp []string `json:"layerMoveDownCommandTemplate"`
	LayerMoveUpCommandTemp   []string `json:"layerMoveUpCommandTemplate"`
	HomeCommandTemp          []string `json:"homeCommandTemplate"`
}

func defaultConfig() AppConfig {
	return AppConfig{
		ListenAddr:   ":5241",
		DataRoot:     "runtime_data",
		FrontendRoot: "../frontend",
		AdminUsers:   []string{"admin"},
		Hardware: HardwareConfig{
			UseMockHardware:       runtime.GOOS == "windows",
			SkipWaitInMock:        true,
			ScriptsRoot:           filepath.Join("..", ".."),
			CommandTimeoutSeconds: 120,
			MagnetCommandTemplate: []string{
				"python3",
				"{scripts_root}/tca_ch0_io_dac.py",
				"--io27",
				"{io27_csv}",
				"--dac",
				"{magnetic_voltage}",
				"--hold",
				"{magnetic_hold_s}",
			},
			ExposureCommandTemp: []string{
				"python3",
				"{scripts_root}/hdmi_dlp_exposure_test.py",
				"--image",
				"{image_path}",
				"--exposure",
				"{exposure_s}",
				"--brightness",
				"{exposure_intensity}",
				"--port",
				"/dev/ttyUSB0",
				"--baud",
				"115200",
				"--timeout",
				"1.0",
				"--tty",
				"1",
				"--fb",
				"/dev/fb0",
				"--settle",
				"0.25",
			},
			LayerMoveCommandTemp:     []string{},
			LayerMoveDownCommandTemp: []string{},
			LayerMoveUpCommandTemp:   []string{},
			HomeCommandTemp:          []string{},
		},
	}
}

func loadConfig(baseDir string) (AppConfig, error) {
	cfg := defaultConfig()
	cfgPath := filepath.Join(baseDir, "config.json")
	if _, err := os.Stat(cfgPath); err == nil {
		raw, err := os.ReadFile(cfgPath)
		if err != nil {
			return cfg, fmt.Errorf("read config.json failed: %w", err)
		}
		if err := json.Unmarshal(raw, &cfg); err != nil {
			return cfg, fmt.Errorf("parse config.json failed: %w", err)
		}
	}

	if strings.TrimSpace(cfg.ListenAddr) == "" {
		cfg.ListenAddr = ":5241"
	}
	if len(cfg.AdminUsers) == 0 {
		cfg.AdminUsers = []string{"admin"}
	}
	if cfg.Hardware.CommandTimeoutSeconds <= 0 {
		cfg.Hardware.CommandTimeoutSeconds = 120
	}

	cfg.DataRoot = resolvePath(baseDir, cfg.DataRoot)
	cfg.FrontendRoot = resolvePath(baseDir, cfg.FrontendRoot)
	cfg.Hardware.ScriptsRoot = resolvePath(baseDir, cfg.Hardware.ScriptsRoot)

	return cfg, nil
}

func resolvePath(baseDir, p string) string {
	if p == "" {
		return baseDir
	}
	if filepath.IsAbs(p) {
		return p
	}
	return filepath.Clean(filepath.Join(baseDir, p))
}

func isAdminUser(cfg AppConfig, username string) bool {
	for _, admin := range cfg.AdminUsers {
		if strings.EqualFold(strings.TrimSpace(admin), strings.TrimSpace(username)) {
			return true
		}
	}
	return false
}

type APIResult struct {
	Success bool   `json:"success"`
	Code    string `json:"code"`
	Message string `json:"message"`
}

type LoginResult struct {
	APIResult
	Token    string `json:"token,omitempty"`
	Username string `json:"username,omitempty"`
}

type UploadResult struct {
	APIResult
	Package *PackageSummary `json:"package,omitempty"`
}

type StartPrintResult struct {
	APIResult
	Status *DeviceStatus `json:"status,omitempty"`
}

type CancelPrintResult struct {
	APIResult
	Status *DeviceStatus `json:"status,omitempty"`
}

type RegisterRequest struct {
	Username string `json:"username"`
	Password string `json:"password"`
}

type LoginRequest struct {
	Username string `json:"username"`
	Password string `json:"password"`
}

type StartPrintRequest struct {
	PackageID string         `json:"packageId"`
	Overrides PrintOverrides `json:"overrides"`
}

type PrintOverrides struct {
	LayerThicknessMM  *float64 `json:"layerThicknessMm"`
	MagneticVoltage   *float64 `json:"magneticVoltage"`
	ExposureIntensity *int     `json:"exposureIntensity"`
	MagneticHoldS     float64  `json:"magneticHoldSeconds"`
	ExposureS         float64  `json:"exposureSeconds"`
}

type UserAccount struct {
	Username     string    `json:"username"`
	PasswordHash string    `json:"passwordHash"`
	Salt         string    `json:"salt"`
	CreatedAtUTC time.Time `json:"createdAtUtc"`
}

type AuthState struct {
	Users     map[string]UserAccount `json:"users"`
	LockOwner string                 `json:"lockOwner"`
}

type SessionInfo struct {
	Token        string
	Username     string
	CreatedAtUTC time.Time
}

type AuthService struct {
	mu        sync.Mutex
	stateFile string
	state     AuthState
	sessions  map[string]SessionInfo
}

func newAuthService(dataRoot string) (*AuthService, error) {
	s := &AuthService{
		stateFile: filepath.Join(dataRoot, "auth_state.json"),
		state: AuthState{
			Users: map[string]UserAccount{},
		},
		sessions: map[string]SessionInfo{},
	}
	if err := s.loadState(); err != nil {
		return nil, err
	}
	return s, nil
}

func (s *AuthService) loadState() error {
	s.mu.Lock()
	defer s.mu.Unlock()

	raw, err := os.ReadFile(s.stateFile)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return nil
		}
		return fmt.Errorf("read auth state failed: %w", err)
	}

	var state AuthState
	if err := json.Unmarshal(raw, &state); err != nil {
		return fmt.Errorf("parse auth state failed: %w", err)
	}
	if state.Users == nil {
		state.Users = map[string]UserAccount{}
	}
	s.state = state
	return nil
}

func (s *AuthService) saveStateLocked() error {
	tmp := s.stateFile + ".tmp"
	raw, err := json.MarshalIndent(s.state, "", "  ")
	if err != nil {
		return err
	}
	if err := os.WriteFile(tmp, raw, 0o644); err != nil {
		return err
	}
	return os.Rename(tmp, s.stateFile)
}

func (s *AuthService) Register(username, password string) APIResult {
	username = strings.TrimSpace(username)
	if len(username) < 3 || len(username) > 32 {
		return APIResult{Success: false, Code: "INVALID_USERNAME", Message: "Username must be 3-32 chars."}
	}
	if len(password) < 6 {
		return APIResult{Success: false, Code: "INVALID_PASSWORD", Message: "Password must be at least 6 chars."}
	}

	s.mu.Lock()
	defer s.mu.Unlock()

	key := strings.ToLower(username)
	if _, ok := s.state.Users[key]; ok {
		return APIResult{Success: false, Code: "USERNAME_EXISTS", Message: "Username already exists."}
	}

	salt, hash := hashPassword(password)
	s.state.Users[key] = UserAccount{
		Username:     username,
		PasswordHash: hash,
		Salt:         salt,
		CreatedAtUTC: time.Now().UTC(),
	}
	if err := s.saveStateLocked(); err != nil {
		return APIResult{Success: false, Code: "STATE_SAVE_FAILED", Message: err.Error()}
	}
	return APIResult{Success: true, Code: "OK", Message: "Registered."}
}

func (s *AuthService) Login(username, password string) LoginResult {
	username = strings.TrimSpace(username)
	if username == "" || password == "" {
		return LoginResult{APIResult: APIResult{Success: false, Code: "INVALID_INPUT", Message: "Username and password are required."}}
	}

	s.mu.Lock()
	defer s.mu.Unlock()

	key := strings.ToLower(username)
	user, ok := s.state.Users[key]
	if !ok || !verifyPassword(password, user.Salt, user.PasswordHash) {
		return LoginResult{APIResult: APIResult{Success: false, Code: "INVALID_CREDENTIALS", Message: "Invalid username or password."}}
	}

	if s.state.LockOwner != "" && !strings.EqualFold(s.state.LockOwner, user.Username) {
		return LoginResult{APIResult: APIResult{Success: false, Code: "DEVICE_LOCKED", Message: "Device is currently controlled by '" + s.state.LockOwner + "'."}}
	}

	if s.state.LockOwner == "" {
		s.state.LockOwner = user.Username
		if err := s.saveStateLocked(); err != nil {
			return LoginResult{APIResult: APIResult{Success: false, Code: "STATE_SAVE_FAILED", Message: err.Error()}}
		}
	}

	token := randomHex(32)
	s.sessions[token] = SessionInfo{
		Token:        token,
		Username:     user.Username,
		CreatedAtUTC: time.Now().UTC(),
	}

	return LoginResult{
		APIResult: APIResult{
			Success: true,
			Code:    "OK",
			Message: "Logged in.",
		},
		Token:    token,
		Username: user.Username,
	}
}

func (s *AuthService) Validate(token string) *SessionInfo {
	s.mu.Lock()
	defer s.mu.Unlock()
	session, ok := s.sessions[token]
	if !ok {
		return nil
	}
	out := session
	return &out
}

func (s *AuthService) Logout(token string, deviceBusy bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	delete(s.sessions, token)
	s.releaseLockIfNeededLocked(deviceBusy)
}

func (s *AuthService) ReleaseLockIfNoSessionAndNotBusy(deviceBusy bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.releaseLockIfNeededLocked(deviceBusy)
}

func (s *AuthService) releaseLockIfNeededLocked(deviceBusy bool) {
	if deviceBusy || s.state.LockOwner == "" {
		return
	}
	lockOwner := s.state.LockOwner
	hasOwnerSession := false
	for _, session := range s.sessions {
		if strings.EqualFold(session.Username, lockOwner) {
			hasOwnerSession = true
			break
		}
	}
	if !hasOwnerSession {
		s.state.LockOwner = ""
		_ = s.saveStateLocked()
	}
}

func (s *AuthService) LockOwner() string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.state.LockOwner
}

func (s *AuthService) IsLockOwner(username string) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.state.LockOwner != "" && strings.EqualFold(s.state.LockOwner, username)
}

func hashPassword(password string) (saltHex, hashHex string) {
	salt := make([]byte, 16)
	_, _ = rand.Read(salt)
	hash := derivePasswordHash(password, salt)
	return hex.EncodeToString(salt), hex.EncodeToString(hash)
}

func verifyPassword(password, saltHex, hashHex string) bool {
	salt, err := hex.DecodeString(saltHex)
	if err != nil {
		return false
	}
	expected, err := hex.DecodeString(hashHex)
	if err != nil {
		return false
	}
	actual := derivePasswordHash(password, salt)
	return subtleEqual(expected, actual)
}

func derivePasswordHash(password string, salt []byte) []byte {
	buf := make([]byte, 0, len(salt)+len(password))
	buf = append(buf, salt...)
	buf = append(buf, []byte(password)...)
	sum := sha256.Sum256(buf)
	out := sum[:]
	for i := 0; i < 120000; i++ {
		h := sha256.New()
		h.Write(salt)
		h.Write(out)
		out = h.Sum(nil)
	}
	return out
}

func subtleEqual(a, b []byte) bool {
	if len(a) != len(b) {
		return false
	}
	var v byte
	for i := range a {
		v |= a[i] ^ b[i]
	}
	return v == 0
}

func randomHex(n int) string {
	b := make([]byte, n)
	_, _ = rand.Read(b)
	return hex.EncodeToString(b)
}

type PackageSummary struct {
	ID               string    `json:"id"`
	Name             string    `json:"name"`
	UploadedBy       string    `json:"uploadedBy"`
	UploadedAtUTC    time.Time `json:"uploadedAtUtc"`
	LayerCount       int       `json:"layerCount"`
	LayerThicknessMM float64   `json:"layerThicknessMm"`
}

type StoredPackage struct {
	PackageSummary
	ExtractedDirectory string `json:"extractedDirectory"`
	ManifestPath       string `json:"manifestPath"`
}

type PackageState struct {
	Packages []StoredPackage `json:"packages"`
}

type SliceManifest struct {
	LayerThicknessMM float64       `json:"layer_thickness_mm"`
	LayerCount       int           `json:"layer_count"`
	Records          []SliceRecord `json:"records"`
}

type SliceRecord struct {
	Layer          int         `json:"layer"`
	File           string      `json:"file"`
	LightIntensity int         `json:"light_intensity"`
	Strength       *float64    `json:"strength"`
	Field          *SliceField `json:"field"`
}

type SliceField struct {
	X        float64  `json:"x"`
	Y        float64  `json:"y"`
	Z        float64  `json:"z"`
	Strength *float64 `json:"strength"`
}

type PackageService struct {
	mu          sync.Mutex
	stateFile   string
	packagesDir string
	state       PackageState
}

func newPackageService(dataRoot string) (*PackageService, error) {
	packagesDir := filepath.Join(dataRoot, "packages")
	if err := os.MkdirAll(packagesDir, 0o755); err != nil {
		return nil, fmt.Errorf("create packages dir failed: %w", err)
	}

	s := &PackageService{
		stateFile:   filepath.Join(dataRoot, "packages_state.json"),
		packagesDir: packagesDir,
		state: PackageState{
			Packages: []StoredPackage{},
		},
	}
	if err := s.loadState(); err != nil {
		return nil, err
	}
	return s, nil
}

func (s *PackageService) loadState() error {
	s.mu.Lock()
	defer s.mu.Unlock()

	raw, err := os.ReadFile(s.stateFile)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return nil
		}
		return fmt.Errorf("read package state failed: %w", err)
	}

	var state PackageState
	if err := json.Unmarshal(raw, &state); err != nil {
		return fmt.Errorf("parse package state failed: %w", err)
	}
	if state.Packages == nil {
		state.Packages = []StoredPackage{}
	}
	s.state = state
	return nil
}

func (s *PackageService) saveStateLocked() error {
	tmp := s.stateFile + ".tmp"
	raw, err := json.MarshalIndent(s.state, "", "  ")
	if err != nil {
		return err
	}
	if err := os.WriteFile(tmp, raw, 0o644); err != nil {
		return err
	}
	return os.Rename(tmp, s.stateFile)
}

func (s *PackageService) ListPackages() []PackageSummary {
	s.mu.Lock()
	defer s.mu.Unlock()

	out := make([]PackageSummary, 0, len(s.state.Packages))
	for _, p := range s.state.Packages {
		out = append(out, p.PackageSummary)
	}
	sort.Slice(out, func(i, j int) bool {
		return out[i].UploadedAtUTC.After(out[j].UploadedAtUTC)
	})
	return out
}

func (s *PackageService) GetPackage(packageID string) *StoredPackage {
	s.mu.Lock()
	defer s.mu.Unlock()
	for _, p := range s.state.Packages {
		if strings.EqualFold(p.ID, packageID) {
			cp := p
			return &cp
		}
	}
	return nil
}

func (s *PackageService) LoadManifest(pkg *StoredPackage) (*SliceManifest, error) {
	raw, err := os.ReadFile(pkg.ManifestPath)
	if err != nil {
		return nil, fmt.Errorf("read manifest failed: %w", err)
	}
	var manifest SliceManifest
	if err := json.Unmarshal(raw, &manifest); err != nil {
		return nil, fmt.Errorf("parse manifest failed: %w", err)
	}
	return &manifest, nil
}

func (s *PackageService) UploadPackage(file multipart.File, header *multipart.FileHeader, uploadedBy string) UploadResult {
	if !strings.EqualFold(filepath.Ext(header.Filename), ".zip") {
		return UploadResult{
			APIResult: APIResult{Success: false, Code: "BAD_EXTENSION", Message: "Only .zip package is supported."},
		}
	}

	id := randomHex(16)
	packageRoot := filepath.Join(s.packagesDir, id)
	zipPath := filepath.Join(packageRoot, "source.zip")
	extractDir := filepath.Join(packageRoot, "extract")

	if err := os.MkdirAll(extractDir, 0o755); err != nil {
		return UploadResult{
			APIResult: APIResult{Success: false, Code: "CREATE_DIR_FAILED", Message: err.Error()},
		}
	}

	if err := writeUploadedFile(zipPath, file); err != nil {
		safeRemoveDir(packageRoot)
		return UploadResult{
			APIResult: APIResult{Success: false, Code: "SAVE_FILE_FAILED", Message: err.Error()},
		}
	}

	if err := extractZipSafe(zipPath, extractDir); err != nil {
		safeRemoveDir(packageRoot)
		return UploadResult{
			APIResult: APIResult{Success: false, Code: "ZIP_EXTRACT_FAILED", Message: err.Error()},
		}
	}

	manifestPath, err := findManifestFile(extractDir)
	if err != nil {
		safeRemoveDir(packageRoot)
		return UploadResult{
			APIResult: APIResult{Success: false, Code: "MANIFEST_MISSING", Message: err.Error()},
		}
	}

	rawManifest, err := os.ReadFile(manifestPath)
	if err != nil {
		safeRemoveDir(packageRoot)
		return UploadResult{
			APIResult: APIResult{Success: false, Code: "MANIFEST_INVALID", Message: err.Error()},
		}
	}
	var manifest SliceManifest
	if err := json.Unmarshal(rawManifest, &manifest); err != nil {
		safeRemoveDir(packageRoot)
		return UploadResult{
			APIResult: APIResult{Success: false, Code: "MANIFEST_INVALID", Message: err.Error()},
		}
	}

	layerCount := manifest.LayerCount
	if layerCount <= 0 {
		layerCount = len(manifest.Records)
	}

	stored := StoredPackage{
		PackageSummary: PackageSummary{
			ID:               id,
			Name:             header.Filename,
			UploadedBy:       uploadedBy,
			UploadedAtUTC:    time.Now().UTC(),
			LayerCount:       layerCount,
			LayerThicknessMM: manifest.LayerThicknessMM,
		},
		ExtractedDirectory: extractDir,
		ManifestPath:       manifestPath,
	}

	s.mu.Lock()
	s.state.Packages = append(s.state.Packages, stored)
	err = s.saveStateLocked()
	s.mu.Unlock()
	if err != nil {
		safeRemoveDir(packageRoot)
		return UploadResult{
			APIResult: APIResult{Success: false, Code: "STATE_SAVE_FAILED", Message: err.Error()},
		}
	}

	return UploadResult{
		APIResult: APIResult{Success: true, Code: "OK", Message: "Package uploaded."},
		Package:   &stored.PackageSummary,
	}
}

func writeUploadedFile(path string, file multipart.File) error {
	dst, err := os.Create(path)
	if err != nil {
		return err
	}
	defer dst.Close()
	_, err = io.Copy(dst, file)
	return err
}

func extractZipSafe(zipPath, dstRoot string) error {
	r, err := zip.OpenReader(zipPath)
	if err != nil {
		return err
	}
	defer r.Close()

	dstAbs, err := filepath.Abs(dstRoot)
	if err != nil {
		return err
	}

	for _, f := range r.File {
		cleanName := filepath.Clean(f.Name)
		if strings.HasPrefix(cleanName, "..") {
			return fmt.Errorf("zip contains invalid path traversal entry: %s", f.Name)
		}

		target := filepath.Join(dstRoot, cleanName)
		targetAbs, err := filepath.Abs(target)
		if err != nil {
			return err
		}
		if targetAbs != dstAbs && !strings.HasPrefix(targetAbs, dstAbs+string(os.PathSeparator)) {
			return fmt.Errorf("zip entry escaped destination: %s", f.Name)
		}

		if f.FileInfo().IsDir() {
			if err := os.MkdirAll(targetAbs, 0o755); err != nil {
				return err
			}
			continue
		}

		if err := os.MkdirAll(filepath.Dir(targetAbs), 0o755); err != nil {
			return err
		}

		rc, err := f.Open()
		if err != nil {
			return err
		}

		out, err := os.OpenFile(targetAbs, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, 0o644)
		if err != nil {
			rc.Close()
			return err
		}
		_, cpErr := io.Copy(out, rc)
		closeErr1 := out.Close()
		closeErr2 := rc.Close()
		if cpErr != nil {
			return cpErr
		}
		if closeErr1 != nil {
			return closeErr1
		}
		if closeErr2 != nil {
			return closeErr2
		}
	}
	return nil
}

func findManifestFile(root string) (string, error) {
	const manifestName = "slice_magnetic_manifest.json"
	foundErr := errors.New("manifest found")
	var manifestPath string

	err := filepath.WalkDir(root, func(path string, d fs.DirEntry, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		if d.IsDir() {
			return nil
		}
		if strings.EqualFold(d.Name(), manifestName) {
			manifestPath = path
			return foundErr
		}
		return nil
	})
	if err != nil && !errors.Is(err, foundErr) {
		return "", err
	}
	if manifestPath == "" {
		return "", errors.New("slice_magnetic_manifest.json not found in package")
	}
	return manifestPath, nil
}

func safeRemoveDir(path string) {
	_ = os.RemoveAll(path)
}

type LayerPlanItem struct {
	LayerIndex        int
	ImagePath         string
	LayerThicknessMM  float64
	MoveDirection     string
	DirectionBits     string
	MagneticVoltage   float64
	ExposureIntensity int
	MagneticHoldS     float64
	ExposureS         float64
}

type JobRuntime struct {
	JobID           string    `json:"jobId,omitempty"`
	Owner           string    `json:"owner,omitempty"`
	PackageID       string    `json:"packageId,omitempty"`
	StartedAtUTC    time.Time `json:"startedAtUtc,omitempty"`
	FinishedAtUTC   time.Time `json:"finishedAtUtc,omitempty"`
	State           string    `json:"state"`
	Message         string    `json:"message"`
	TotalLayers     int       `json:"totalLayers"`
	CompletedLayers int       `json:"completedLayers"`
	CurrentLayer    int       `json:"currentLayer"`
	RecentEvents    []string  `json:"recentEvents"`
}

type DeviceStatus struct {
	IsBusy           bool       `json:"isBusy"`
	LockOwner        string     `json:"lockOwner,omitempty"`
	CanControlDevice bool       `json:"canControlDevice"`
	RequestUser      string     `json:"requestUser,omitempty"`
	Job              JobRuntime `json:"job"`
}

type PrintService struct {
	mu      sync.Mutex
	auth    *AuthService
	pkg     *PackageService
	cfg     AppConfig
	logger  *log.Logger
	running bool
	cancel  context.CancelFunc
	job     JobRuntime
}

func newPrintService(auth *AuthService, pkg *PackageService, cfg AppConfig, logger *log.Logger) *PrintService {
	return &PrintService{
		auth:   auth,
		pkg:    pkg,
		cfg:    cfg,
		logger: logger,
		job: JobRuntime{
			State:        stateIdle,
			Message:      "idle",
			RecentEvents: []string{},
		},
	}
}

func (s *PrintService) IsBusy() bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.running
}

func (s *PrintService) GetStatusForUser(username string) DeviceStatus {
	s.mu.Lock()
	job := cloneJob(s.job)
	busy := s.running
	s.mu.Unlock()

	return DeviceStatus{
		IsBusy:           busy,
		LockOwner:        s.auth.LockOwner(),
		CanControlDevice: s.auth.IsLockOwner(username),
		RequestUser:      username,
		Job:              job,
	}
}

func (s *PrintService) Start(user string, req StartPrintRequest) (StartPrintResult, int) {
	if !s.auth.IsLockOwner(user) {
		return StartPrintResult{
			APIResult: APIResult{Success: false, Code: "DEVICE_LOCKED", Message: "Only lock owner can start print."},
		}, http.StatusLocked
	}
	if strings.TrimSpace(req.PackageID) == "" {
		return StartPrintResult{
			APIResult: APIResult{Success: false, Code: "BAD_PACKAGE", Message: "PackageId is required."},
		}, http.StatusBadRequest
	}
	if req.Overrides.MagneticHoldS < 0 || req.Overrides.ExposureS < 0 {
		return StartPrintResult{
			APIResult: APIResult{Success: false, Code: "BAD_OVERRIDE", Message: "Time values must be >= 0."},
		}, http.StatusBadRequest
	}
	if req.Overrides.ExposureIntensity != nil {
		if *req.Overrides.ExposureIntensity < 0 || *req.Overrides.ExposureIntensity > 255 {
			return StartPrintResult{
				APIResult: APIResult{Success: false, Code: "BAD_OVERRIDE", Message: "Exposure intensity must be 0..255."},
			}, http.StatusBadRequest
		}
	}

	s.mu.Lock()
	if s.running {
		s.mu.Unlock()
		status := s.GetStatusForUser(user)
		return StartPrintResult{
			APIResult: APIResult{Success: false, Code: "BUSY", Message: "Device is busy."},
			Status:    &status,
		}, http.StatusConflict
	}
	s.mu.Unlock()

	pkg := s.pkg.GetPackage(req.PackageID)
	if pkg == nil {
		return StartPrintResult{
			APIResult: APIResult{Success: false, Code: "PACKAGE_NOT_FOUND", Message: "Package not found."},
		}, http.StatusBadRequest
	}

	manifest, err := s.pkg.LoadManifest(pkg)
	if err != nil {
		return StartPrintResult{
			APIResult: APIResult{Success: false, Code: "MANIFEST_INVALID", Message: err.Error()},
		}, http.StatusBadRequest
	}

	plan, err := buildLayerPlan(manifest, pkg, req.Overrides)
	if err != nil {
		return StartPrintResult{
			APIResult: APIResult{Success: false, Code: "PLAN_BUILD_FAILED", Message: err.Error()},
		}, http.StatusBadRequest
	}
	if len(plan) == 0 {
		return StartPrintResult{
			APIResult: APIResult{Success: false, Code: "EMPTY_PLAN", Message: "No layers found in manifest."},
		}, http.StatusBadRequest
	}

	ctx, cancel := context.WithCancel(context.Background())
	jobID := randomHex(16)
	now := time.Now().UTC()
	job := JobRuntime{
		JobID:           jobID,
		Owner:           user,
		PackageID:       pkg.ID,
		StartedAtUTC:    now,
		State:           stateRunning,
		Message:         "print_started",
		TotalLayers:     len(plan),
		CompletedLayers: 0,
		CurrentLayer:    0,
		RecentEvents:    []string{fmt.Sprintf("%s print started", now.Format(time.RFC3339))},
	}

	s.mu.Lock()
	s.running = true
	s.cancel = cancel
	s.job = job
	s.mu.Unlock()

	go s.runPlan(ctx, jobID, plan)

	status := s.GetStatusForUser(user)
	return StartPrintResult{
		APIResult: APIResult{Success: true, Code: "OK", Message: "Print started."},
		Status:    &status,
	}, http.StatusOK
}

func (s *PrintService) Cancel(user string) (CancelPrintResult, int) {
	if !s.auth.IsLockOwner(user) {
		return CancelPrintResult{
			APIResult: APIResult{Success: false, Code: "DEVICE_LOCKED", Message: "Only lock owner can cancel print."},
		}, http.StatusLocked
	}

	s.mu.Lock()
	if !s.running {
		s.mu.Unlock()
		status := s.GetStatusForUser(user)
		return CancelPrintResult{
			APIResult: APIResult{Success: false, Code: "NOT_RUNNING", Message: "No running job."},
			Status:    &status,
		}, http.StatusConflict
	}

	s.job.State = stateCanceling
	s.job.Message = "cancel_requested"
	s.appendEventLocked("cancel requested")
	if s.cancel != nil {
		s.cancel()
	}
	s.mu.Unlock()

	status := s.GetStatusForUser(user)
	return CancelPrintResult{
		APIResult: APIResult{Success: true, Code: "OK", Message: "Cancel requested."},
		Status:    &status,
	}, http.StatusOK
}

func (s *PrintService) runPlan(ctx context.Context, jobID string, plan []LayerPlanItem) {
	hardware := newHardwareController(s.cfg.Hardware, s.logger)
	defer func() {
		finishCtx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		defer cancel()
		_ = hardware.Finish(finishCtx)
		s.mu.Lock()
		s.running = false
		s.cancel = nil
		s.mu.Unlock()
		s.auth.ReleaseLockIfNoSessionAndNotBusy(false)
	}()

	if err := hardware.Prepare(ctx); err != nil {
		s.failJob(jobID, err)
		return
	}

	for i := range plan {
		select {
		case <-ctx.Done():
			s.cancelJob(jobID)
			return
		default:
		}

		layer := plan[i]
		s.updateCurrentLayer(jobID, i+1)

		if err := hardware.MoveLayer(ctx, layer); err != nil {
			s.failJob(jobID, err)
			return
		}
		if err := hardware.ApplyMagneticField(ctx, layer); err != nil {
			s.failJob(jobID, err)
			return
		}
		if err := hardware.ExposeLayer(ctx, layer); err != nil {
			s.failJob(jobID, err)
			return
		}
		s.completeLayer(jobID, i+1)
	}

	s.mu.Lock()
	defer s.mu.Unlock()
	if s.job.JobID != jobID {
		return
	}
	s.job.State = stateCompleted
	s.job.Message = "print_completed"
	s.job.FinishedAtUTC = time.Now().UTC()
	s.appendEventLocked("print completed")
}

func (s *PrintService) updateCurrentLayer(jobID string, layer int) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.job.JobID != jobID {
		return
	}
	s.job.CurrentLayer = layer
	s.job.Message = fmt.Sprintf("running_layer_%d", layer)
	s.appendEventLocked(fmt.Sprintf("running layer %d/%d", layer, s.job.TotalLayers))
}

func (s *PrintService) completeLayer(jobID string, layer int) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.job.JobID != jobID {
		return
	}
	s.job.CompletedLayers = layer
	s.job.Message = fmt.Sprintf("layer_done_%d", layer)
	s.appendEventLocked(fmt.Sprintf("layer done %d/%d", layer, s.job.TotalLayers))
}

func (s *PrintService) failJob(jobID string, err error) {
	s.logger.Printf("print job failed: %v", err)
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.job.JobID != jobID {
		return
	}
	s.job.State = stateFailed
	s.job.Message = "print_failed: " + err.Error()
	s.job.FinishedAtUTC = time.Now().UTC()
	s.appendEventLocked("print failed: " + err.Error())
}

func (s *PrintService) cancelJob(jobID string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.job.JobID != jobID {
		return
	}
	s.job.State = stateCanceled
	s.job.Message = "print_canceled"
	s.job.FinishedAtUTC = time.Now().UTC()
	s.appendEventLocked("print canceled")
}

func (s *PrintService) appendEventLocked(evt string) {
	line := fmt.Sprintf("%s %s", time.Now().UTC().Format(time.RFC3339), evt)
	s.job.RecentEvents = append(s.job.RecentEvents, line)
	if len(s.job.RecentEvents) > 40 {
		s.job.RecentEvents = s.job.RecentEvents[len(s.job.RecentEvents)-40:]
	}
}

func cloneJob(job JobRuntime) JobRuntime {
	out := job
	if job.RecentEvents != nil {
		out.RecentEvents = append([]string(nil), job.RecentEvents...)
	}
	return out
}

type ManualMagnetRequest struct {
	DirectionBits   string  `json:"directionBits"`
	MagneticVoltage float64 `json:"magneticVoltage"`
	HoldSeconds     float64 `json:"holdSeconds"`
}

type ManualExposureRequest struct {
	ImagePath          string  `json:"imagePath"`
	ExposureIntensity  int     `json:"exposureIntensity"`
	ExposureSeconds    float64 `json:"exposureSeconds"`
	DirectionBits      string  `json:"directionBits"`
	LayerThicknessMM   float64 `json:"layerThicknessMm"`
	MagneticVoltage    float64 `json:"magneticVoltage"`
	MagneticHoldSecond float64 `json:"magneticHoldSeconds"`
}

type ManualMoveRequest struct {
	LayerThicknessMM float64 `json:"layerThicknessMm"`
	MoveDirection    string  `json:"moveDirection"`
}

type ManualWaitRequest struct {
	WaitSeconds float64 `json:"waitSeconds"`
}

type ManualHomeRequest struct{}

type FlowProgramRequest struct {
	Name  string     `json:"name"`
	Steps []FlowStep `json:"steps"`
}

type FlowStep struct {
	ID       string         `json:"id,omitempty"`
	Type     string         `json:"type"`
	Repeat   int            `json:"repeat,omitempty"`
	Params   FlowStepParams `json:"params"`
	Children []FlowStep     `json:"children,omitempty"`
}

type FlowProgramConfig struct {
	Version      int        `json:"version"`
	CreatedAtUTC time.Time  `json:"createdAtUtc"`
	Name         string     `json:"name"`
	Steps        []FlowStep `json:"steps"`
}

type FlowStepParams struct {
	LayerThicknessMM  float64 `json:"layerThicknessMm"`
	MoveDirection     string  `json:"moveDirection"`
	MagnetSource      string  `json:"magnetSource"`
	DirectionBits     string  `json:"directionBits"`
	UseSliceDirection bool    `json:"useSliceDirection"`
	UseSliceStrength  bool    `json:"useSliceStrength"`
	SliceAdvance      bool    `json:"sliceAdvance"`
	MagneticVoltage   float64 `json:"magneticVoltage"`
	HoldSeconds       float64 `json:"holdSeconds"`
	ImageSource       string  `json:"imageSource"`
	SlicePackageID    string  `json:"slicePackageId"`
	UseSliceIntensity bool    `json:"useSliceIntensity"`
	UseSliceMagnet    bool    `json:"useSliceMagnet"`
	ImagePath         string  `json:"imagePath"`
	ExposureIntensity int     `json:"exposureIntensity"`
	ExposureSeconds   float64 `json:"exposureSeconds"`
	WaitSeconds       float64 `json:"waitSeconds"`
}

type AdminProgramStatus struct {
	Running         bool      `json:"running"`
	Name            string    `json:"name,omitempty"`
	StartedAtUTC    time.Time `json:"startedAtUtc,omitempty"`
	FinishedAtUTC   time.Time `json:"finishedAtUtc,omitempty"`
	CurrentStep     string    `json:"currentStep,omitempty"`
	PendingAsyncOps int       `json:"pendingAsyncOps"`
	LastError       string    `json:"lastError,omitempty"`
	LastResult      string    `json:"lastResult,omitempty"`
	RecentEvents    []string  `json:"recentEvents"`
}

type AdminProgramResult struct {
	APIResult
	Status *AdminProgramStatus `json:"status,omitempty"`
}

type AdminService struct {
	mu       sync.Mutex
	cfg      AppConfig
	logger   *log.Logger
	printSvc *PrintService
	pkgSvc   *PackageService
	running  bool
	cancel   context.CancelFunc
	status   AdminProgramStatus
}

type FlowConfigService struct {
	mu      sync.Mutex
	keyFile string
	key     []byte
}

type asyncMagnetRunner struct {
	mu            sync.Mutex
	ctx           context.Context
	hw            *hardwareController
	appendEvent   func(string)
	updatePending func(int)
	pending       int
	magnetRunning bool
	firstErr      error
	idleCh        chan struct{}
}

type flowRunContext struct {
	pkgSvc  *PackageService
	cursors map[string]*flowSliceCursor
}

type flowSliceCursor struct {
	pkg            *StoredPackage
	manifest       *SliceManifest
	orderedIndices []int
	next           int
}

func newAdminService(cfg AppConfig, printSvc *PrintService, pkgSvc *PackageService, logger *log.Logger) *AdminService {
	return &AdminService{
		cfg:      cfg,
		logger:   logger,
		printSvc: printSvc,
		pkgSvc:   pkgSvc,
		status: AdminProgramStatus{
			Running:      false,
			LastResult:   "idle",
			RecentEvents: []string{},
		},
	}
}

func newFlowConfigService(dataRoot string) *FlowConfigService {
	return &FlowConfigService{
		keyFile: filepath.Join(dataRoot, "flow_config.key"),
	}
}

func (s *FlowConfigService) loadOrCreateKeyLocked() error {
	if len(s.key) == 32 {
		return nil
	}
	raw, err := os.ReadFile(s.keyFile)
	if err == nil {
		if len(raw) != 32 {
			return errors.New("invalid flow config key length")
		}
		s.key = append([]byte(nil), raw...)
		return nil
	}
	if !errors.Is(err, os.ErrNotExist) {
		return fmt.Errorf("read flow config key failed: %w", err)
	}
	key := make([]byte, 32)
	if _, err := rand.Read(key); err != nil {
		return fmt.Errorf("generate flow config key failed: %w", err)
	}
	tmp := s.keyFile + ".tmp"
	if err := os.WriteFile(tmp, key, 0o600); err != nil {
		return fmt.Errorf("write flow config key failed: %w", err)
	}
	if err := os.Rename(tmp, s.keyFile); err != nil {
		return fmt.Errorf("save flow config key failed: %w", err)
	}
	s.key = append([]byte(nil), key...)
	return nil
}

func (s *FlowConfigService) encryptConfig(req FlowProgramRequest) ([]byte, error) {
	if len(req.Steps) == 0 {
		return nil, errors.New("program has no steps")
	}
	cfg := FlowProgramConfig{
		Version:      flowConfigVersion,
		CreatedAtUTC: time.Now().UTC(),
		Name:         strings.TrimSpace(req.Name),
		Steps:        append([]FlowStep(nil), req.Steps...),
	}
	plain, err := json.Marshal(cfg)
	if err != nil {
		return nil, err
	}
	if len(plain) > flowConfigMaxBytes {
		return nil, errors.New("config payload too large")
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if err := s.loadOrCreateKeyLocked(); err != nil {
		return nil, err
	}
	block, err := aes.NewCipher(s.key)
	if err != nil {
		return nil, err
	}
	gcm, err := cipher.NewGCM(block)
	if err != nil {
		return nil, err
	}
	nonce := make([]byte, gcm.NonceSize())
	if _, err := rand.Read(nonce); err != nil {
		return nil, err
	}
	encrypted := gcm.Seal(nil, nonce, plain, []byte(flowConfigMagic))
	out := make([]byte, 0, len(flowConfigMagic)+len(nonce)+len(encrypted))
	out = append(out, []byte(flowConfigMagic)...)
	out = append(out, nonce...)
	out = append(out, encrypted...)
	return out, nil
}

func (s *FlowConfigService) decryptConfig(payload []byte) (FlowProgramRequest, error) {
	var out FlowProgramRequest
	if len(payload) < len(flowConfigMagic) {
		return out, errors.New("invalid config payload")
	}
	if string(payload[:len(flowConfigMagic)]) != flowConfigMagic {
		return out, errors.New("invalid config header")
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if err := s.loadOrCreateKeyLocked(); err != nil {
		return out, err
	}
	block, err := aes.NewCipher(s.key)
	if err != nil {
		return out, err
	}
	gcm, err := cipher.NewGCM(block)
	if err != nil {
		return out, err
	}
	offset := len(flowConfigMagic)
	nonceSize := gcm.NonceSize()
	if len(payload) < offset+nonceSize+gcm.Overhead() {
		return out, errors.New("invalid config payload length")
	}
	nonce := payload[offset : offset+nonceSize]
	cipherData := payload[offset+nonceSize:]
	plain, err := gcm.Open(nil, nonce, cipherData, []byte(flowConfigMagic))
	if err != nil {
		return out, errors.New("decrypt config failed")
	}
	if len(plain) > flowConfigMaxBytes {
		return out, errors.New("config payload too large")
	}
	var cfg FlowProgramConfig
	if err := json.Unmarshal(plain, &cfg); err != nil {
		return out, errors.New("decode config failed")
	}
	if cfg.Version != flowConfigVersion {
		return out, errors.New("unsupported config version")
	}
	out.Name = strings.TrimSpace(cfg.Name)
	out.Steps = append([]FlowStep(nil), cfg.Steps...)
	return out, nil
}

func (s *AdminService) GetStatus() AdminProgramStatus {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := s.status
	if s.status.RecentEvents != nil {
		out.RecentEvents = append([]string(nil), s.status.RecentEvents...)
	}
	return out
}

func newAsyncMagnetRunner(ctx context.Context, hw *hardwareController, appendEvent func(string), updatePending func(int)) *asyncMagnetRunner {
	ch := make(chan struct{})
	close(ch)
	return &asyncMagnetRunner{
		ctx:           ctx,
		hw:            hw,
		appendEvent:   appendEvent,
		updatePending: updatePending,
		idleCh:        ch,
	}
}

func (r *asyncMagnetRunner) firstError() error {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.firstErr
}

func (r *asyncMagnetRunner) startMagnet(layer LayerPlanItem) error {
	r.mu.Lock()
	if r.firstErr != nil {
		err := r.firstErr
		r.mu.Unlock()
		return err
	}
	if r.magnetRunning {
		r.mu.Unlock()
		return errors.New("previous async magnet step is still running")
	}
	if r.pending == 0 {
		r.idleCh = make(chan struct{})
	}
	r.pending++
	pending := r.pending
	r.magnetRunning = true
	r.mu.Unlock()

	if r.updatePending != nil {
		r.updatePending(pending)
	}
	if r.appendEvent != nil {
		r.appendEvent(fmt.Sprintf("async magnet started (pending=%d)", pending))
	}

	go func() {
		err := r.hw.ApplyMagneticField(r.ctx, layer)

		r.mu.Lock()
		if err != nil && r.firstErr == nil {
			r.firstErr = err
		}
		r.magnetRunning = false
		if r.pending > 0 {
			r.pending--
		}
		pendingNow := r.pending
		shouldClose := r.pending == 0
		idleCh := r.idleCh
		r.mu.Unlock()

		if shouldClose {
			close(idleCh)
		}
		if r.updatePending != nil {
			r.updatePending(pendingNow)
		}
		if r.appendEvent != nil {
			if err != nil {
				r.appendEvent("async magnet failed: " + err.Error())
			} else {
				r.appendEvent(fmt.Sprintf("async magnet completed (pending=%d)", pendingNow))
			}
		}
	}()

	return nil
}

func (r *asyncMagnetRunner) waitAllIdle(ctx context.Context) error {
	for {
		r.mu.Lock()
		pending := r.pending
		idleCh := r.idleCh
		firstErr := r.firstErr
		r.mu.Unlock()

		if pending == 0 {
			return firstErr
		}

		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-idleCh:
		}
	}
}

func newFlowRunContext(pkgSvc *PackageService) *flowRunContext {
	return &flowRunContext{
		pkgSvc:  pkgSvc,
		cursors: map[string]*flowSliceCursor{},
	}
}

func manifestSortedRecordIndices(manifest *SliceManifest) []int {
	if manifest == nil || len(manifest.Records) == 0 {
		return []int{}
	}
	indices := make([]int, len(manifest.Records))
	for i := range manifest.Records {
		indices[i] = i
	}
	sort.SliceStable(indices, func(i, j int) bool {
		left := manifest.Records[indices[i]]
		right := manifest.Records[indices[j]]
		if left.Layer == right.Layer {
			return indices[i] < indices[j]
		}
		return left.Layer < right.Layer
	})
	return indices
}

func (f *flowRunContext) getOrLoadCursor(packageID string) (*flowSliceCursor, error) {
	if f == nil {
		return nil, errors.New("flow run context is nil")
	}
	id := strings.TrimSpace(packageID)
	if id == "" {
		return nil, errors.New("slicePackageId is required for slice exposure")
	}
	if cursor, ok := f.cursors[id]; ok {
		return cursor, nil
	}
	if f.pkgSvc == nil {
		return nil, errors.New("package service is unavailable")
	}
	pkg := f.pkgSvc.GetPackage(id)
	if pkg == nil {
		return nil, fmt.Errorf("slice package not found: %s", id)
	}
	manifest, err := f.pkgSvc.LoadManifest(pkg)
	if err != nil {
		return nil, fmt.Errorf("load slice manifest failed: %w", err)
	}
	ordered := manifestSortedRecordIndices(manifest)
	if len(ordered) == 0 {
		return nil, fmt.Errorf("slice package has no records: %s", id)
	}
	cursor := &flowSliceCursor{
		pkg:            pkg,
		manifest:       manifest,
		orderedIndices: ordered,
		next:           0,
	}
	f.cursors[id] = cursor
	return cursor, nil
}

func (f *flowRunContext) resolveSliceRecord(packageID string, advance bool) (SliceRecord, string, error) {
	cursor, err := f.getOrLoadCursor(packageID)
	if err != nil {
		return SliceRecord{}, "", err
	}
	if cursor.next >= len(cursor.orderedIndices) {
		return SliceRecord{}, "", fmt.Errorf("slice records exhausted in package %s (total=%d)", strings.TrimSpace(packageID), len(cursor.orderedIndices))
	}
	rec := cursor.manifest.Records[cursor.orderedIndices[cursor.next]]
	if advance {
		cursor.next++
	}
	fileRel := strings.TrimSpace(rec.File)
	if fileRel == "" {
		return SliceRecord{}, "", errors.New("slice record image file is empty")
	}
	imagePath := filepath.Clean(filepath.Join(cursor.pkg.ExtractedDirectory, filepath.FromSlash(fileRel)))
	absImagePath, err := filepath.Abs(imagePath)
	if err != nil {
		return SliceRecord{}, "", err
	}
	return rec, absImagePath, nil
}

func (s *AdminService) RunProgram(req FlowProgramRequest) (AdminProgramResult, int) {
	if s.printSvc.IsBusy() {
		return AdminProgramResult{
			APIResult: APIResult{Success: false, Code: "PRINT_BUSY", Message: "Cannot run admin program while print is running."},
			Status:    toStatusPtr(s.GetStatus()),
		}, http.StatusConflict
	}
	if len(req.Steps) == 0 {
		return AdminProgramResult{
			APIResult: APIResult{Success: false, Code: "EMPTY_PROGRAM", Message: "Program has no steps."},
			Status:    toStatusPtr(s.GetStatus()),
		}, http.StatusBadRequest
	}

	name := strings.TrimSpace(req.Name)
	if name == "" {
		name = "flow-program"
	}

	ctx, cancel := context.WithCancel(context.Background())
	s.mu.Lock()
	if s.running {
		s.mu.Unlock()
		return AdminProgramResult{
			APIResult: APIResult{Success: false, Code: "ADMIN_BUSY", Message: "Another admin program is running."},
			Status:    toStatusPtr(s.GetStatus()),
		}, http.StatusConflict
	}
	s.running = true
	s.cancel = cancel
	s.status.Running = true
	s.status.Name = name
	s.status.StartedAtUTC = time.Now().UTC()
	s.status.FinishedAtUTC = time.Time{}
	s.status.CurrentStep = ""
	s.status.PendingAsyncOps = 0
	s.status.LastError = ""
	s.status.LastResult = "program_started"
	s.appendEventLocked("program started: " + name)
	s.mu.Unlock()

	steps := append([]FlowStep(nil), req.Steps...)
	flowCtx := newFlowRunContext(s.pkgSvc)
	go s.runProgramLoop(ctx, name, steps, flowCtx)

	return AdminProgramResult{
		APIResult: APIResult{Success: true, Code: "OK", Message: "Program started."},
		Status:    toStatusPtr(s.GetStatus()),
	}, http.StatusOK
}

func (s *AdminService) CancelProgram() (AdminProgramResult, int) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if !s.running {
		return AdminProgramResult{
			APIResult: APIResult{Success: false, Code: "NOT_RUNNING", Message: "No admin program is running."},
			Status:    toStatusPtr(s.status),
		}, http.StatusConflict
	}
	if s.cancel != nil {
		s.cancel()
	}
	s.status.LastResult = "cancel_requested"
	s.appendEventLocked("cancel requested")
	return AdminProgramResult{
		APIResult: APIResult{Success: true, Code: "OK", Message: "Cancel requested."},
		Status:    toStatusPtr(s.status),
	}, http.StatusOK
}

func (s *AdminService) ManualMagnet(req ManualMagnetRequest) (APIResult, int) {
	layer := LayerPlanItem{
		LayerIndex:      0,
		DirectionBits:   normalizeDirectionBits(req.DirectionBits),
		MagneticVoltage: req.MagneticVoltage,
		MagneticHoldS:   req.HoldSeconds,
	}
	if err := validateDirectionBits(layer.DirectionBits); err != nil {
		return APIResult{Success: false, Code: "BAD_DIRECTION", Message: err.Error()}, http.StatusBadRequest
	}
	if req.HoldSeconds < 0 {
		return APIResult{Success: false, Code: "BAD_HOLD", Message: "holdSeconds must be >= 0"}, http.StatusBadRequest
	}
	return s.runManual("manual_magnet", func(ctx context.Context, hw *hardwareController) error {
		if err := hw.Prepare(ctx); err != nil {
			return err
		}
		if err := hw.ApplyMagneticField(ctx, layer); err != nil {
			return err
		}
		return hw.Finish(ctx)
	})
}

func (s *AdminService) ManualExposure(req ManualExposureRequest) (APIResult, int) {
	imagePath := strings.TrimSpace(req.ImagePath)
	if imagePath == "" {
		return APIResult{Success: false, Code: "BAD_IMAGE", Message: "imagePath is required."}, http.StatusBadRequest
	}
	if req.ExposureSeconds < 0 {
		return APIResult{Success: false, Code: "BAD_EXPOSURE", Message: "exposureSeconds must be >= 0"}, http.StatusBadRequest
	}
	absPath, err := filepath.Abs(filepath.Clean(imagePath))
	if err != nil {
		return APIResult{Success: false, Code: "BAD_IMAGE", Message: err.Error()}, http.StatusBadRequest
	}
	intensity := req.ExposureIntensity
	if intensity <= 0 {
		intensity = 80
	}
	if intensity > 255 {
		return APIResult{Success: false, Code: "BAD_INTENSITY", Message: "exposureIntensity must be 0..255"}, http.StatusBadRequest
	}

	layer := LayerPlanItem{
		LayerIndex:        0,
		ImagePath:         absPath,
		ExposureIntensity: intensity,
		ExposureS:         req.ExposureSeconds,
	}

	return s.runManual("manual_exposure", func(ctx context.Context, hw *hardwareController) error {
		if err := hw.Prepare(ctx); err != nil {
			return err
		}
		if err := hw.ExposeLayer(ctx, layer); err != nil {
			return err
		}
		return hw.Finish(ctx)
	})
}

func (s *AdminService) ManualMove(req ManualMoveRequest) (APIResult, int) {
	if req.LayerThicknessMM <= 0 {
		return APIResult{Success: false, Code: "BAD_MOVE", Message: "layerThicknessMm must be > 0"}, http.StatusBadRequest
	}
	moveDirection, err := normalizeMoveDirection(req.MoveDirection)
	if err != nil {
		return APIResult{Success: false, Code: "BAD_MOVE_DIRECTION", Message: err.Error()}, http.StatusBadRequest
	}
	layer := LayerPlanItem{
		LayerIndex:       0,
		LayerThicknessMM: req.LayerThicknessMM,
		MoveDirection:    moveDirection,
		DirectionBits:    normalizeDirectionBits(""),
	}
	return s.runManual("manual_move", func(ctx context.Context, hw *hardwareController) error {
		if err := hw.Prepare(ctx); err != nil {
			return err
		}
		if err := hw.MoveLayer(ctx, layer); err != nil {
			return err
		}
		return hw.Finish(ctx)
	})
}

func (s *AdminService) ManualHome(_ ManualHomeRequest) (APIResult, int) {
	return s.runManual("manual_home", func(ctx context.Context, hw *hardwareController) error {
		if err := hw.Prepare(ctx); err != nil {
			return err
		}
		if err := hw.Home(ctx); err != nil {
			return err
		}
		return hw.Finish(ctx)
	})
}

func (s *AdminService) ManualWait(req ManualWaitRequest) (APIResult, int) {
	if req.WaitSeconds < 0 {
		return APIResult{Success: false, Code: "BAD_WAIT", Message: "waitSeconds must be >= 0"}, http.StatusBadRequest
	}
	return s.runManual("manual_wait", func(ctx context.Context, hw *hardwareController) error {
		_ = hw
		return sleepCtx(ctx, time.Duration(req.WaitSeconds*float64(time.Second)))
	})
}

func (s *AdminService) runProgramLoop(ctx context.Context, name string, steps []FlowStep, flowCtx *flowRunContext) {
	ctx, cancelProgram := context.WithCancel(ctx)
	defer cancelProgram()

	hw := newHardwareController(s.cfg.Hardware, s.logger)
	appendProgramEvent := func(evt string) {
		s.mu.Lock()
		s.appendEventLocked(evt)
		s.mu.Unlock()
	}
	updatePending := func(count int) {
		s.mu.Lock()
		s.status.PendingAsyncOps = count
		s.mu.Unlock()
	}
	asyncRunner := newAsyncMagnetRunner(ctx, hw, appendProgramEvent, updatePending)

	setDone := func(lastResult, lastError string) {
		s.mu.Lock()
		s.running = false
		s.cancel = nil
		s.status.Running = false
		s.status.FinishedAtUTC = time.Now().UTC()
		s.status.CurrentStep = ""
		s.status.PendingAsyncOps = 0
		s.status.LastResult = lastResult
		s.status.LastError = lastError
		if lastError == "" {
			s.appendEventLocked("program completed")
		} else {
			s.appendEventLocked("program failed: " + lastError)
		}
		s.mu.Unlock()
	}

	if err := hw.Prepare(ctx); err != nil {
		setDone("failed", err.Error())
		return
	}
	defer func() {
		_ = hw.Finish(context.Background())
	}()

	err := s.executeSteps(ctx, hw, asyncRunner, steps, 0, name, flowCtx)
	if err != nil {
		cancelProgram()
		cleanupCtx, cleanupCancel := context.WithTimeout(context.Background(), 3*time.Second)
		_ = asyncRunner.waitAllIdle(cleanupCtx)
		cleanupCancel()
		if errors.Is(err, context.Canceled) {
			setDone("canceled", "")
			return
		}
		setDone("failed", err.Error())
		return
	}

	if err := asyncRunner.waitAllIdle(ctx); err != nil {
		if errors.Is(err, context.Canceled) {
			setDone("canceled", "")
			return
		}
		setDone("failed", err.Error())
		return
	}
	setDone("completed", "")
}

func (s *AdminService) executeSteps(ctx context.Context, hw *hardwareController, asyncRunner *asyncMagnetRunner, steps []FlowStep, depth int, prefix string, flowCtx *flowRunContext) error {
	if depth > 8 {
		return errors.New("loop depth exceeds limit")
	}
	for idx := range steps {
		select {
		case <-ctx.Done():
			return ctx.Err()
		default:
		}
		if asyncErr := asyncRunner.firstError(); asyncErr != nil {
			return fmt.Errorf("async task failed: %w", asyncErr)
		}

		step := steps[idx]
		stepType := normalizeStepType(step.Type)
		stepName := fmt.Sprintf("%s/%d:%s", prefix, idx+1, stepType)
		s.mu.Lock()
		s.status.CurrentStep = stepName
		s.appendEventLocked("running " + stepName)
		s.mu.Unlock()

		if stepType == "loop" {
			repeat := step.Repeat
			if repeat <= 0 {
				repeat = 1
			}
			if len(step.Children) == 0 {
				return fmt.Errorf("loop step has no children: %s", stepName)
			}
			for i := 0; i < repeat; i++ {
				select {
				case <-ctx.Done():
					return ctx.Err()
				default:
				}
				s.mu.Lock()
				s.appendEventLocked(fmt.Sprintf("loop %s (%d/%d)", stepName, i+1, repeat))
				s.mu.Unlock()
				if err := s.executeSteps(ctx, hw, asyncRunner, step.Children, depth+1, fmt.Sprintf("%s.loop%d", stepName, i+1), flowCtx); err != nil {
					return err
				}
			}
			continue
		}

		if err := executeFlowStep(ctx, hw, asyncRunner, flowCtx, stepType, step.Params); err != nil {
			return fmt.Errorf("%s failed: %w", stepName, err)
		}
	}
	return nil
}

func executeFlowStep(ctx context.Context, hw *hardwareController, asyncRunner *asyncMagnetRunner, flowCtx *flowRunContext, stepType string, p FlowStepParams) error {
	switch stepType {
	case "magnet":
		layer, err := buildMagnetLayerForStep(flowCtx, p)
		if err != nil {
			return err
		}
		return hw.ApplyMagneticField(ctx, layer)
	case "magnet_async":
		if asyncRunner == nil {
			return errors.New("async runner is nil")
		}
		layer, err := buildMagnetLayerForStep(flowCtx, p)
		if err != nil {
			return err
		}
		if p.HoldSeconds < 0 {
			return errors.New("holdSeconds must be >= 0")
		}
		return asyncRunner.startMagnet(layer)
	case "exposure":
		return executeFlowExposure(ctx, hw, flowCtx, p)
	case "move":
		if p.LayerThicknessMM <= 0 {
			return errors.New("layerThicknessMm must be > 0")
		}
		moveDirection, err := normalizeMoveDirection(p.MoveDirection)
		if err != nil {
			return err
		}
		layer := LayerPlanItem{
			LayerThicknessMM: p.LayerThicknessMM,
			MoveDirection:    moveDirection,
		}
		return hw.MoveLayer(ctx, layer)
	case "move_up":
		if p.LayerThicknessMM <= 0 {
			return errors.New("layerThicknessMm must be > 0")
		}
		layer := LayerPlanItem{
			LayerThicknessMM: p.LayerThicknessMM,
			MoveDirection:    moveDirUp,
		}
		return hw.MoveLayer(ctx, layer)
	case "move_down":
		if p.LayerThicknessMM <= 0 {
			return errors.New("layerThicknessMm must be > 0")
		}
		layer := LayerPlanItem{
			LayerThicknessMM: p.LayerThicknessMM,
			MoveDirection:    moveDirDown,
		}
		return hw.MoveLayer(ctx, layer)
	case "home":
		return hw.Home(ctx)
	case "wait_all_idle":
		if asyncRunner == nil {
			return errors.New("async runner is nil")
		}
		return asyncRunner.waitAllIdle(ctx)
	case "wait":
		if p.WaitSeconds < 0 {
			return errors.New("waitSeconds must be >= 0")
		}
		return sleepCtx(ctx, time.Duration(p.WaitSeconds*float64(time.Second)))
	default:
		return fmt.Errorf("unsupported step type: %s", stepType)
	}
}

func buildMagnetLayerForStep(flowCtx *flowRunContext, p FlowStepParams) (LayerPlanItem, error) {
	source := strings.ToLower(strings.TrimSpace(p.MagnetSource))
	if source == "" {
		source = flowMagnetSourceManual
		if strings.TrimSpace(p.SlicePackageID) != "" && (p.UseSliceDirection || p.UseSliceStrength) {
			source = flowMagnetSourceSlice
		}
	}

	bits := normalizeDirectionBits(p.DirectionBits)
	magV := p.MagneticVoltage
	switch source {
	case flowMagnetSourceManual:
		if err := validateDirectionBits(bits); err != nil {
			return LayerPlanItem{}, err
		}
	case flowMagnetSourceSlice:
		if flowCtx == nil {
			return LayerPlanItem{}, errors.New("flow run context is unavailable for slice magnet")
		}
		advance := p.SliceAdvance
		rec, _, err := flowCtx.resolveSliceRecord(p.SlicePackageID, advance)
		if err != nil {
			return LayerPlanItem{}, err
		}
		if p.UseSliceDirection || strings.TrimSpace(p.DirectionBits) == "" {
			x := 0.0
			y := 0.0
			if rec.Field != nil {
				x = rec.Field.X
				y = rec.Field.Y
			}
			bits = resolveDirectionBits(x, y)
		}
		if p.UseSliceStrength {
			if p.MagneticVoltage > 0 {
				magV = p.MagneticVoltage
			} else {
				magV = resolveSliceStrength(rec)
			}
		}
		if err := validateDirectionBits(bits); err != nil {
			return LayerPlanItem{}, err
		}
		if magV <= 0 {
			return LayerPlanItem{}, errors.New("slice magnetic voltage is missing; provide manifest strength or set magneticVoltage override")
		}
	default:
		return LayerPlanItem{}, errors.New("magnetSource must be manual or slice")
	}

	layer := LayerPlanItem{
		DirectionBits:   bits,
		MagneticVoltage: magV,
		MagneticHoldS:   p.HoldSeconds,
	}
	return layer, nil
}

func resolveSliceStrength(rec SliceRecord) float64 {
	if rec.Field != nil && rec.Field.Strength != nil {
		return *rec.Field.Strength
	}
	if rec.Strength != nil {
		return *rec.Strength
	}
	return 0
}

func executeFlowExposure(ctx context.Context, hw *hardwareController, flowCtx *flowRunContext, p FlowStepParams) error {
	mode := strings.ToLower(strings.TrimSpace(p.ImageSource))
	if mode == "" {
		if strings.TrimSpace(p.SlicePackageID) != "" {
			mode = flowImageSourceSlice
		} else {
			mode = flowImageSourceManual
		}
	}

	layer := LayerPlanItem{
		ExposureS: p.ExposureSeconds,
	}
	intensity := p.ExposureIntensity

	switch mode {
	case flowImageSourceSlice:
		if flowCtx == nil {
			return errors.New("flow run context is unavailable for slice exposure")
		}
		rec, imagePath, err := flowCtx.resolveSliceRecord(p.SlicePackageID, true)
		if err != nil {
			return err
		}
		layer.ImagePath = imagePath
		if p.UseSliceIntensity || intensity <= 0 {
			intensity = rec.LightIntensity
		}
		if p.UseSliceMagnet {
			x := 0.0
			y := 0.0
			if rec.Field != nil {
				x = rec.Field.X
				y = rec.Field.Y
			}
			magV := resolveSliceStrength(rec)
			if p.MagneticVoltage > 0 {
				magV = p.MagneticVoltage
			}
			if magV <= 0 {
				return errors.New("slice magnetic voltage is missing; provide manifest strength or set magneticVoltage override")
			}
			magLayer := LayerPlanItem{
				DirectionBits:   resolveDirectionBits(x, y),
				MagneticVoltage: magV,
				MagneticHoldS:   p.HoldSeconds,
			}
			if err := hw.ApplyMagneticField(ctx, magLayer); err != nil {
				return err
			}
		}
	case flowImageSourceManual:
		if strings.TrimSpace(p.ImagePath) == "" {
			return errors.New("imagePath is required for exposure")
		}
		imagePath, err := filepath.Abs(filepath.Clean(p.ImagePath))
		if err != nil {
			return err
		}
		layer.ImagePath = imagePath
		if intensity <= 0 {
			intensity = 80
		}
	default:
		return errors.New("imageSource must be manual or slice")
	}

	if intensity < 0 || intensity > 255 {
		return errors.New("exposureIntensity must be 0..255")
	}
	layer.ExposureIntensity = intensity
	return hw.ExposeLayer(ctx, layer)
}

func (s *AdminService) runManual(name string, op func(ctx context.Context, hw *hardwareController) error) (APIResult, int) {
	if s.printSvc.IsBusy() {
		return APIResult{Success: false, Code: "PRINT_BUSY", Message: "Cannot run manual action while printing."}, http.StatusConflict
	}

	s.mu.Lock()
	if s.running {
		s.mu.Unlock()
		return APIResult{Success: false, Code: "ADMIN_BUSY", Message: "Admin program is running."}, http.StatusConflict
	}
	s.running = true
	s.cancel = nil
	s.status.Running = true
	s.status.Name = name
	s.status.StartedAtUTC = time.Now().UTC()
	s.status.FinishedAtUTC = time.Time{}
	s.status.CurrentStep = name
	s.status.PendingAsyncOps = 0
	s.status.LastError = ""
	s.status.LastResult = "running"
	s.appendEventLocked("manual action started: " + name)
	s.mu.Unlock()

	defer func() {
		s.mu.Lock()
		s.running = false
		s.status.Running = false
		s.status.CurrentStep = ""
		s.status.FinishedAtUTC = time.Now().UTC()
		s.mu.Unlock()
	}()

	timeout := time.Duration(s.cfg.Hardware.CommandTimeoutSeconds) * time.Second
	if timeout <= 0 {
		timeout = 120 * time.Second
	}
	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	defer cancel()

	hw := newHardwareController(s.cfg.Hardware, s.logger)
	err := op(ctx, hw)
	if err != nil {
		s.mu.Lock()
		s.status.LastResult = "failed"
		s.status.LastError = err.Error()
		s.appendEventLocked("manual action failed: " + err.Error())
		s.mu.Unlock()
		return APIResult{Success: false, Code: "MANUAL_FAILED", Message: err.Error()}, http.StatusBadRequest
	}

	s.mu.Lock()
	s.status.LastResult = "completed"
	s.status.LastError = ""
	s.appendEventLocked("manual action completed: " + name)
	s.mu.Unlock()
	return APIResult{Success: true, Code: "OK", Message: "Manual action completed."}, http.StatusOK
}

func (s *AdminService) appendEventLocked(evt string) {
	line := fmt.Sprintf("%s %s", time.Now().UTC().Format(time.RFC3339), evt)
	s.status.RecentEvents = append(s.status.RecentEvents, line)
	if len(s.status.RecentEvents) > 120 {
		s.status.RecentEvents = s.status.RecentEvents[len(s.status.RecentEvents)-120:]
	}
}

func toStatusPtr(st AdminProgramStatus) *AdminProgramStatus {
	out := st
	out.RecentEvents = append([]string(nil), st.RecentEvents...)
	return &out
}

func normalizeDirectionBits(bits string) string {
	b := strings.TrimSpace(bits)
	if b == "" {
		return xPositiveBits
	}
	return b
}

func validateDirectionBits(bits string) error {
	if len(bits) != 8 {
		return errors.New("directionBits must be 8-bit binary string")
	}
	for i := 0; i < len(bits); i++ {
		if bits[i] != '0' && bits[i] != '1' {
			return errors.New("directionBits must contain only 0/1")
		}
	}
	return nil
}

func normalizeStepType(stepType string) string {
	return strings.ToLower(strings.TrimSpace(stepType))
}

func normalizeMoveDirection(moveDirection string) (string, error) {
	d := strings.ToLower(strings.TrimSpace(moveDirection))
	if d == "" {
		return moveDirDown, nil
	}
	switch d {
	case moveDirDown, "movedown", "downward", "d":
		return moveDirDown, nil
	case moveDirUp, "moveup", "upward", "u":
		return moveDirUp, nil
	default:
		return "", errors.New("moveDirection must be up/down")
	}
}

func buildLayerPlan(manifest *SliceManifest, pkg *StoredPackage, ov PrintOverrides) ([]LayerPlanItem, error) {
	if manifest == nil {
		return nil, errors.New("manifest is nil")
	}
	if len(manifest.Records) == 0 {
		return []LayerPlanItem{}, nil
	}

	thickness := manifest.LayerThicknessMM
	if ov.LayerThicknessMM != nil {
		thickness = *ov.LayerThicknessMM
	}
	if thickness <= 0 {
		return nil, errors.New("layer thickness must be > 0")
	}

	indices := manifestSortedRecordIndices(manifest)
	plan := make([]LayerPlanItem, 0, len(indices))
	for _, idx := range indices {
		rec := manifest.Records[idx]

		x := 0.0
		y := 0.0
		if rec.Field != nil {
			x = rec.Field.X
			y = rec.Field.Y
		}
		directionBits := resolveDirectionBits(x, y)

		magV := 0.0
		if ov.MagneticVoltage != nil {
			magV = *ov.MagneticVoltage
		} else if rec.Field != nil && rec.Field.Strength != nil {
			magV = *rec.Field.Strength
		} else if rec.Strength != nil {
			magV = *rec.Strength
		}

		exposureIntensity := rec.LightIntensity
		if ov.ExposureIntensity != nil {
			exposureIntensity = *ov.ExposureIntensity
		}

		imagePath := filepath.Clean(filepath.Join(pkg.ExtractedDirectory, filepath.FromSlash(rec.File)))
		absImagePath, err := filepath.Abs(imagePath)
		if err != nil {
			return nil, err
		}

		plan = append(plan, LayerPlanItem{
			LayerIndex:        rec.Layer + 1,
			ImagePath:         absImagePath,
			LayerThicknessMM:  thickness,
			MoveDirection:     moveDirDown,
			DirectionBits:     directionBits,
			MagneticVoltage:   magV,
			ExposureIntensity: exposureIntensity,
			MagneticHoldS:     ov.MagneticHoldS,
			ExposureS:         ov.ExposureS,
		})
	}
	return plan, nil
}

func resolveDirectionBits(x, y float64) string {
	absX := x
	if absX < 0 {
		absX = -absX
	}
	absY := y
	if absY < 0 {
		absY = -absY
	}
	if absX < 0.0001 && absY < 0.0001 {
		return xPositiveBits
	}
	if absX >= absY {
		if x >= 0 {
			return xPositiveBits
		}
		return xNegativeBits
	}
	if y >= 0 {
		return yPositiveBits
	}
	return yNegativeBits
}

func directionBitsToCSV(bits string) (string, error) {
	if len(bits) != 8 {
		return "", errors.New("direction bits must be 8 chars")
	}
	items := make([]string, 8)
	for i := 0; i < 8; i++ {
		ch := bits[i]
		if ch != '0' && ch != '1' {
			return "", errors.New("direction bits must contain only 0 or 1")
		}
		items[i] = string(ch)
	}
	return strings.Join(items, ","), nil
}

type hardwareController struct {
	cfg    HardwareConfig
	logger *log.Logger
}

func newHardwareController(cfg HardwareConfig, logger *log.Logger) *hardwareController {
	return &hardwareController{cfg: cfg, logger: logger}
}

func (h *hardwareController) isMock() bool {
	return h.cfg.UseMockHardware || runtime.GOOS == "windows"
}

func (h *hardwareController) Prepare(ctx context.Context) error {
	if h.isMock() {
		h.logger.Println("mock hardware prepare")
	}
	return nil
}

func (h *hardwareController) MoveLayer(ctx context.Context, layer LayerPlanItem) error {
	moveDirection, err := normalizeMoveDirection(layer.MoveDirection)
	if err != nil {
		return err
	}
	if h.isMock() {
		h.logger.Printf("mock move layer=%d direction=%s thickness_mm=%.4f", layer.LayerIndex, moveDirection, layer.LayerThicknessMM)
		return nil
	}
	var commandTemplate []string
	switch moveDirection {
	case moveDirUp:
		if len(h.cfg.LayerMoveUpCommandTemp) > 0 {
			commandTemplate = h.cfg.LayerMoveUpCommandTemp
		}
	default:
		if len(h.cfg.LayerMoveDownCommandTemp) > 0 {
			commandTemplate = h.cfg.LayerMoveDownCommandTemp
		}
	}
	if len(commandTemplate) == 0 {
		commandTemplate = h.cfg.LayerMoveCommandTemp
	}
	if len(commandTemplate) == 0 {
		return nil
	}
	layer.MoveDirection = moveDirection
	params, err := buildTemplateParams(h.cfg.ScriptsRoot, layer)
	if err != nil {
		return err
	}
	return h.runCommandTemplate(ctx, commandTemplate, params)
}

func (h *hardwareController) Home(ctx context.Context) error {
	if h.isMock() {
		h.logger.Println("mock home")
		return nil
	}
	if len(h.cfg.HomeCommandTemp) == 0 {
		return nil
	}
	params := buildBaseTemplateParams(h.cfg.ScriptsRoot)
	return h.runCommandTemplate(ctx, h.cfg.HomeCommandTemp, params)
}

func (h *hardwareController) ApplyMagneticField(ctx context.Context, layer LayerPlanItem) error {
	if h.isMock() {
		h.logger.Printf("mock magnet layer=%d bits=%s voltage=%.3f hold=%.3fs", layer.LayerIndex, layer.DirectionBits, layer.MagneticVoltage, layer.MagneticHoldS)
		if !h.cfg.SkipWaitInMock && layer.MagneticHoldS > 0 {
			return sleepCtx(ctx, time.Duration(layer.MagneticHoldS*float64(time.Second)))
		}
		return nil
	}
	if len(h.cfg.MagnetCommandTemplate) == 0 {
		if layer.MagneticHoldS > 0 {
			return sleepCtx(ctx, time.Duration(layer.MagneticHoldS*float64(time.Second)))
		}
		return nil
	}
	params, err := buildTemplateParams(h.cfg.ScriptsRoot, layer)
	if err != nil {
		return err
	}
	return h.runCommandTemplate(ctx, h.cfg.MagnetCommandTemplate, params)
}

func (h *hardwareController) ExposeLayer(ctx context.Context, layer LayerPlanItem) error {
	if h.isMock() {
		h.logger.Printf("mock exposure layer=%d intensity=%d exposure=%.3fs image=%s", layer.LayerIndex, layer.ExposureIntensity, layer.ExposureS, layer.ImagePath)
		if !h.cfg.SkipWaitInMock && layer.ExposureS > 0 {
			return sleepCtx(ctx, time.Duration(layer.ExposureS*float64(time.Second)))
		}
		return nil
	}
	if len(h.cfg.ExposureCommandTemp) == 0 {
		if layer.ExposureS > 0 {
			return sleepCtx(ctx, time.Duration(layer.ExposureS*float64(time.Second)))
		}
		return nil
	}
	params, err := buildTemplateParams(h.cfg.ScriptsRoot, layer)
	if err != nil {
		return err
	}
	return h.runCommandTemplate(ctx, h.cfg.ExposureCommandTemp, params)
}

func (h *hardwareController) Finish(ctx context.Context) error {
	if h.isMock() {
		h.logger.Println("mock hardware finish")
	}
	return nil
}

func buildTemplateParams(scriptsRoot string, layer LayerPlanItem) (map[string]string, error) {
	bits := normalizeDirectionBits(layer.DirectionBits)
	if err := validateDirectionBits(bits); err != nil {
		return nil, err
	}
	io27CSV, err := directionBitsToCSV(bits)
	if err != nil {
		return nil, err
	}
	moveDirection, err := normalizeMoveDirection(layer.MoveDirection)
	if err != nil {
		return nil, err
	}
	params := buildBaseTemplateParams(scriptsRoot)
	params["layer_mm"] = formatFloat(layer.LayerThicknessMM)
	params["layer_um"] = formatFloat(layer.LayerThicknessMM * 1000.0)
	params["io27_bits"] = bits
	params["io27_csv"] = io27CSV
	params["magnetic_voltage"] = formatFloat(layer.MagneticVoltage)
	params["magnetic_hold_s"] = formatFloat(layer.MagneticHoldS)
	params["exposure_intensity"] = strconv.Itoa(layer.ExposureIntensity)
	params["exposure_s"] = formatFloat(layer.ExposureS)
	params["image_path"] = layer.ImagePath
	params["move_direction"] = moveDirection
	return params, nil
}

func buildBaseTemplateParams(scriptsRoot string) map[string]string {
	return map[string]string{
		"scripts_root":       scriptsRoot,
		"layer_mm":           "0",
		"layer_um":           "0",
		"io27_bits":          xPositiveBits,
		"io27_csv":           "0,0,0,0,1,1,1,1",
		"magnetic_voltage":   "0",
		"magnetic_hold_s":    "0",
		"exposure_intensity": "0",
		"exposure_s":         "0",
		"image_path":         "",
		"move_direction":     moveDirDown,
	}
}

func formatFloat(v float64) string {
	return strconv.FormatFloat(v, 'f', -1, 64)
}

func (h *hardwareController) runCommandTemplate(ctx context.Context, template []string, params map[string]string) error {
	if len(template) == 0 {
		return nil
	}
	cmdParts := make([]string, len(template))
	for i, part := range template {
		value := part
		for k, v := range params {
			value = strings.ReplaceAll(value, "{"+k+"}", v)
		}
		cmdParts[i] = value
	}
	executable := cmdParts[0]
	args := cmdParts[1:]
	timeout := time.Duration(h.cfg.CommandTimeoutSeconds) * time.Second
	if timeout <= 0 {
		timeout = 120 * time.Second
	}
	cmdCtx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	cmd := exec.CommandContext(cmdCtx, executable, args...)
	output, err := cmd.CombinedOutput()
	h.logger.Printf("hardware command: %s %s", executable, strings.Join(args, " "))
	if err != nil {
		return fmt.Errorf("command failed: %w; output=%s", err, strings.TrimSpace(string(output)))
	}
	return nil
}

func sleepCtx(ctx context.Context, d time.Duration) error {
	if d <= 0 {
		return nil
	}
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-t.C:
		return nil
	}
}

func parseJSONBody(r *http.Request, dst any) error {
	defer r.Body.Close()
	dec := json.NewDecoder(io.LimitReader(r.Body, 1<<20))
	dec.DisallowUnknownFields()
	if err := dec.Decode(dst); err != nil {
		return err
	}
	return nil
}

func writeJSON(w http.ResponseWriter, status int, payload any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	enc := json.NewEncoder(w)
	enc.SetEscapeHTML(false)
	_ = enc.Encode(payload)
}

func writeError(w http.ResponseWriter, status int, code, message string) {
	writeJSON(w, status, APIResult{Success: false, Code: code, Message: message})
}

func getAuthorizedUser(r *http.Request, auth *AuthService) (username, token string, ok bool) {
	token = ""
	ah := r.Header.Get("Authorization")
	if strings.HasPrefix(strings.ToLower(ah), "bearer ") {
		token = strings.TrimSpace(ah[7:])
	}
	if token == "" {
		token = strings.TrimSpace(r.URL.Query().Get("access_token"))
	}
	if token == "" {
		return "", "", false
	}
	session := auth.Validate(token)
	if session == nil {
		return "", "", false
	}
	return session.Username, token, true
}

func ensureLockOwner(user string, auth *AuthService) error {
	if !auth.IsLockOwner(user) {
		return errors.New("only current lock owner can control flow")
	}
	return nil
}

func withCORS(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Access-Control-Allow-Origin", "*")
		w.Header().Set("Access-Control-Allow-Headers", "Authorization, Content-Type")
		w.Header().Set("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
		if r.Method == http.MethodOptions {
			w.WriteHeader(http.StatusOK)
			return
		}
		next.ServeHTTP(w, r)
	})
}

func main() {
	baseDir, err := os.Getwd()
	if err != nil {
		log.Fatalf("getwd failed: %v", err)
	}

	cfg, err := loadConfig(baseDir)
	if err != nil {
		log.Fatalf("load config failed: %v", err)
	}

	if err := os.MkdirAll(cfg.DataRoot, 0o755); err != nil {
		log.Fatalf("create data root failed: %v", err)
	}

	logger := log.New(os.Stdout, "", log.LstdFlags)

	authSvc, err := newAuthService(cfg.DataRoot)
	if err != nil {
		log.Fatalf("init auth service failed: %v", err)
	}
	pkgSvc, err := newPackageService(cfg.DataRoot)
	if err != nil {
		log.Fatalf("init package service failed: %v", err)
	}
	printSvc := newPrintService(authSvc, pkgSvc, cfg, logger)
	adminSvc := newAdminService(cfg, printSvc, pkgSvc, logger)
	flowCfgSvc := newFlowConfigService(cfg.DataRoot)

	apiMux := http.NewServeMux()

	apiMux.HandleFunc("/api/auth/register", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			writeError(w, http.StatusMethodNotAllowed, "METHOD_NOT_ALLOWED", "POST required")
			return
		}
		var req RegisterRequest
		if err := parseJSONBody(r, &req); err != nil {
			writeError(w, http.StatusBadRequest, "BAD_REQUEST", err.Error())
			return
		}
		result := authSvc.Register(req.Username, req.Password)
		if result.Success {
			writeJSON(w, http.StatusOK, result)
		} else {
			writeJSON(w, http.StatusBadRequest, result)
		}
	})

	apiMux.HandleFunc("/api/auth/login", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			writeError(w, http.StatusMethodNotAllowed, "METHOD_NOT_ALLOWED", "POST required")
			return
		}
		var req LoginRequest
		if err := parseJSONBody(r, &req); err != nil {
			writeError(w, http.StatusBadRequest, "BAD_REQUEST", err.Error())
			return
		}
		result := authSvc.Login(req.Username, req.Password)
		if result.Success {
			writeJSON(w, http.StatusOK, result)
			return
		}
		if result.Code == "DEVICE_LOCKED" {
			writeJSON(w, http.StatusLocked, result)
			return
		}
		writeJSON(w, http.StatusBadRequest, result)
	})

	apiMux.HandleFunc("/api/auth/logout", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			writeError(w, http.StatusMethodNotAllowed, "METHOD_NOT_ALLOWED", "POST required")
			return
		}
		_, token, ok := getAuthorizedUser(r, authSvc)
		if !ok {
			writeError(w, http.StatusUnauthorized, "UNAUTHORIZED", "Unauthorized")
			return
		}
		authSvc.Logout(token, printSvc.IsBusy())
		writeJSON(w, http.StatusOK, map[string]string{"message": "logged_out"})
	})

	apiMux.HandleFunc("/api/auth/me", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			writeError(w, http.StatusMethodNotAllowed, "METHOD_NOT_ALLOWED", "GET required")
			return
		}
		user, _, ok := getAuthorizedUser(r, authSvc)
		if !ok {
			writeError(w, http.StatusUnauthorized, "UNAUTHORIZED", "Unauthorized")
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{
			"username":         user,
			"lockOwner":        authSvc.LockOwner(),
			"canControlDevice": authSvc.IsLockOwner(user),
			"isBusy":           printSvc.IsBusy(),
			"isAdmin":          isAdminUser(cfg, user),
		})
	})

	apiMux.HandleFunc("/api/device/status", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			writeError(w, http.StatusMethodNotAllowed, "METHOD_NOT_ALLOWED", "GET required")
			return
		}
		user, _, ok := getAuthorizedUser(r, authSvc)
		if !ok {
			writeError(w, http.StatusUnauthorized, "UNAUTHORIZED", "Unauthorized")
			return
		}
		writeJSON(w, http.StatusOK, printSvc.GetStatusForUser(user))
	})

	apiMux.HandleFunc("/api/device/stream", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			writeError(w, http.StatusMethodNotAllowed, "METHOD_NOT_ALLOWED", "GET required")
			return
		}
		user, _, ok := getAuthorizedUser(r, authSvc)
		if !ok {
			writeError(w, http.StatusUnauthorized, "UNAUTHORIZED", "Unauthorized")
			return
		}
		flusher, ok := w.(http.Flusher)
		if !ok {
			writeError(w, http.StatusInternalServerError, "SSE_UNSUPPORTED", "Streaming unsupported")
			return
		}

		w.Header().Set("Content-Type", "text/event-stream")
		w.Header().Set("Cache-Control", "no-cache")
		w.Header().Set("Connection", "keep-alive")

		ticker := time.NewTicker(1 * time.Second)
		defer ticker.Stop()

		send := func() bool {
			status := printSvc.GetStatusForUser(user)
			raw, err := json.Marshal(status)
			if err != nil {
				return false
			}
			if _, err := fmt.Fprintf(w, "event: status\ndata: %s\n\n", string(raw)); err != nil {
				return false
			}
			flusher.Flush()
			return true
		}
		if !send() {
			return
		}

		for {
			select {
			case <-r.Context().Done():
				return
			case <-ticker.C:
				if !send() {
					return
				}
			}
		}
	})

	apiMux.HandleFunc("/api/packages", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			writeError(w, http.StatusMethodNotAllowed, "METHOD_NOT_ALLOWED", "GET required")
			return
		}
		user, _, ok := getAuthorizedUser(r, authSvc)
		if !ok {
			writeError(w, http.StatusUnauthorized, "UNAUTHORIZED", "Unauthorized")
			return
		}
		_ = user
		writeJSON(w, http.StatusOK, pkgSvc.ListPackages())
	})

	apiMux.HandleFunc("/api/packages/upload", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			writeError(w, http.StatusMethodNotAllowed, "METHOD_NOT_ALLOWED", "POST required")
			return
		}
		user, _, ok := getAuthorizedUser(r, authSvc)
		if !ok {
			writeError(w, http.StatusUnauthorized, "UNAUTHORIZED", "Unauthorized")
			return
		}

		if err := r.ParseMultipartForm(512 << 20); err != nil {
			writeError(w, http.StatusBadRequest, "BAD_FORM", err.Error())
			return
		}
		file, header, err := r.FormFile("file")
		if err != nil {
			writeError(w, http.StatusBadRequest, "MISSING_FILE", "Upload field 'file' is required.")
			return
		}
		defer file.Close()

		result := pkgSvc.UploadPackage(file, header, user)
		if result.Success {
			writeJSON(w, http.StatusOK, result)
		} else {
			writeJSON(w, http.StatusBadRequest, result)
		}
	})

	apiMux.HandleFunc("/api/print/start", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			writeError(w, http.StatusMethodNotAllowed, "METHOD_NOT_ALLOWED", "POST required")
			return
		}
		user, _, ok := getAuthorizedUser(r, authSvc)
		if !ok {
			writeError(w, http.StatusUnauthorized, "UNAUTHORIZED", "Unauthorized")
			return
		}
		var req StartPrintRequest
		if err := parseJSONBody(r, &req); err != nil {
			writeError(w, http.StatusBadRequest, "BAD_REQUEST", err.Error())
			return
		}
		result, status := printSvc.Start(user, req)
		writeJSON(w, status, result)
	})

	apiMux.HandleFunc("/api/print/cancel", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			writeError(w, http.StatusMethodNotAllowed, "METHOD_NOT_ALLOWED", "POST required")
			return
		}
		user, _, ok := getAuthorizedUser(r, authSvc)
		if !ok {
			writeError(w, http.StatusUnauthorized, "UNAUTHORIZED", "Unauthorized")
			return
		}
		result, status := printSvc.Cancel(user)
		writeJSON(w, status, result)
	})

	apiMux.HandleFunc("/api/direction-map", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			writeError(w, http.StatusMethodNotAllowed, "METHOD_NOT_ALLOWED", "GET required")
			return
		}
		writeJSON(w, http.StatusOK, map[string]string{
			"xPositive": xPositiveBits,
			"xNegative": xNegativeBits,
			"yPositive": yPositiveBits,
			"yNegative": yNegativeBits,
		})
	})

	apiMux.HandleFunc("/api/admin/status", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			writeError(w, http.StatusMethodNotAllowed, "METHOD_NOT_ALLOWED", "GET required")
			return
		}
		user, _, ok := getAuthorizedUser(r, authSvc)
		if !ok {
			writeError(w, http.StatusUnauthorized, "UNAUTHORIZED", "Unauthorized")
			return
		}
		_ = user
		writeJSON(w, http.StatusOK, adminSvc.GetStatus())
	})

	apiMux.HandleFunc("/api/admin/manual/magnet", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			writeError(w, http.StatusMethodNotAllowed, "METHOD_NOT_ALLOWED", "POST required")
			return
		}
		user, _, ok := getAuthorizedUser(r, authSvc)
		if !ok {
			writeError(w, http.StatusUnauthorized, "UNAUTHORIZED", "Unauthorized")
			return
		}
		if !isAdminUser(cfg, user) {
			writeError(w, http.StatusForbidden, "FORBIDDEN", "Admin only")
			return
		}
		var req ManualMagnetRequest
		if err := parseJSONBody(r, &req); err != nil {
			writeError(w, http.StatusBadRequest, "BAD_REQUEST", err.Error())
			return
		}
		result, status := adminSvc.ManualMagnet(req)
		writeJSON(w, status, result)
	})

	apiMux.HandleFunc("/api/admin/manual/exposure", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			writeError(w, http.StatusMethodNotAllowed, "METHOD_NOT_ALLOWED", "POST required")
			return
		}
		user, _, ok := getAuthorizedUser(r, authSvc)
		if !ok {
			writeError(w, http.StatusUnauthorized, "UNAUTHORIZED", "Unauthorized")
			return
		}
		if !isAdminUser(cfg, user) {
			writeError(w, http.StatusForbidden, "FORBIDDEN", "Admin only")
			return
		}
		var req ManualExposureRequest
		if err := parseJSONBody(r, &req); err != nil {
			writeError(w, http.StatusBadRequest, "BAD_REQUEST", err.Error())
			return
		}
		result, status := adminSvc.ManualExposure(req)
		writeJSON(w, status, result)
	})

	apiMux.HandleFunc("/api/admin/manual/move", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			writeError(w, http.StatusMethodNotAllowed, "METHOD_NOT_ALLOWED", "POST required")
			return
		}
		user, _, ok := getAuthorizedUser(r, authSvc)
		if !ok {
			writeError(w, http.StatusUnauthorized, "UNAUTHORIZED", "Unauthorized")
			return
		}
		if !isAdminUser(cfg, user) {
			writeError(w, http.StatusForbidden, "FORBIDDEN", "Admin only")
			return
		}
		var req ManualMoveRequest
		if err := parseJSONBody(r, &req); err != nil {
			writeError(w, http.StatusBadRequest, "BAD_REQUEST", err.Error())
			return
		}
		result, status := adminSvc.ManualMove(req)
		writeJSON(w, status, result)
	})

	apiMux.HandleFunc("/api/admin/manual/home", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			writeError(w, http.StatusMethodNotAllowed, "METHOD_NOT_ALLOWED", "POST required")
			return
		}
		user, _, ok := getAuthorizedUser(r, authSvc)
		if !ok {
			writeError(w, http.StatusUnauthorized, "UNAUTHORIZED", "Unauthorized")
			return
		}
		if !isAdminUser(cfg, user) {
			writeError(w, http.StatusForbidden, "FORBIDDEN", "Admin required")
			return
		}
		result, status := adminSvc.ManualHome(ManualHomeRequest{})
		writeJSON(w, status, result)
	})

	apiMux.HandleFunc("/api/admin/manual/wait", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			writeError(w, http.StatusMethodNotAllowed, "METHOD_NOT_ALLOWED", "POST required")
			return
		}
		user, _, ok := getAuthorizedUser(r, authSvc)
		if !ok {
			writeError(w, http.StatusUnauthorized, "UNAUTHORIZED", "Unauthorized")
			return
		}
		if !isAdminUser(cfg, user) {
			writeError(w, http.StatusForbidden, "FORBIDDEN", "Admin only")
			return
		}
		var req ManualWaitRequest
		if err := parseJSONBody(r, &req); err != nil {
			writeError(w, http.StatusBadRequest, "BAD_REQUEST", err.Error())
			return
		}
		result, status := adminSvc.ManualWait(req)
		writeJSON(w, status, result)
	})

	apiMux.HandleFunc("/api/admin/program/run", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			writeError(w, http.StatusMethodNotAllowed, "METHOD_NOT_ALLOWED", "POST required")
			return
		}
		user, _, ok := getAuthorizedUser(r, authSvc)
		if !ok {
			writeError(w, http.StatusUnauthorized, "UNAUTHORIZED", "Unauthorized")
			return
		}
		if err := ensureLockOwner(user, authSvc); err != nil {
			writeError(w, http.StatusForbidden, "FORBIDDEN", err.Error())
			return
		}
		var req FlowProgramRequest
		if err := parseJSONBody(r, &req); err != nil {
			writeError(w, http.StatusBadRequest, "BAD_REQUEST", err.Error())
			return
		}
		result, status := adminSvc.RunProgram(req)
		writeJSON(w, status, result)
	})

	apiMux.HandleFunc("/api/admin/program/cancel", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			writeError(w, http.StatusMethodNotAllowed, "METHOD_NOT_ALLOWED", "POST required")
			return
		}
		user, _, ok := getAuthorizedUser(r, authSvc)
		if !ok {
			writeError(w, http.StatusUnauthorized, "UNAUTHORIZED", "Unauthorized")
			return
		}
		if err := ensureLockOwner(user, authSvc); err != nil {
			writeError(w, http.StatusForbidden, "FORBIDDEN", err.Error())
			return
		}
		result, status := adminSvc.CancelProgram()
		writeJSON(w, status, result)
	})

	apiMux.HandleFunc("/api/program/config/export", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			writeError(w, http.StatusMethodNotAllowed, "METHOD_NOT_ALLOWED", "POST required")
			return
		}
		user, _, ok := getAuthorizedUser(r, authSvc)
		if !ok {
			writeError(w, http.StatusUnauthorized, "UNAUTHORIZED", "Unauthorized")
			return
		}
		if err := ensureLockOwner(user, authSvc); err != nil {
			writeError(w, http.StatusForbidden, "FORBIDDEN", err.Error())
			return
		}
		var req FlowProgramRequest
		if err := parseJSONBody(r, &req); err != nil {
			writeError(w, http.StatusBadRequest, "BAD_REQUEST", err.Error())
			return
		}
		enc, err := flowCfgSvc.encryptConfig(req)
		if err != nil {
			writeError(w, http.StatusBadRequest, "CONFIG_EXPORT_FAILED", err.Error())
			return
		}
		name := strings.TrimSpace(req.Name)
		if name == "" {
			name = "flow-program"
		}
		safeName := strings.Map(func(r rune) rune {
			if (r >= 'a' && r <= 'z') || (r >= 'A' && r <= 'Z') || (r >= '0' && r <= '9') || r == '_' || r == '-' {
				return r
			}
			return '_'
		}, name)
		w.Header().Set("Content-Type", "application/octet-stream")
		w.Header().Set("Content-Disposition", fmt.Sprintf("attachment; filename=\"%s.mpcfg\"", safeName))
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write(enc)
	})

	apiMux.HandleFunc("/api/program/config/import", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			writeError(w, http.StatusMethodNotAllowed, "METHOD_NOT_ALLOWED", "POST required")
			return
		}
		user, _, ok := getAuthorizedUser(r, authSvc)
		if !ok {
			writeError(w, http.StatusUnauthorized, "UNAUTHORIZED", "Unauthorized")
			return
		}
		if err := ensureLockOwner(user, authSvc); err != nil {
			writeError(w, http.StatusForbidden, "FORBIDDEN", err.Error())
			return
		}
		raw, err := io.ReadAll(io.LimitReader(r.Body, flowConfigMaxBytes+1))
		if err != nil {
			writeError(w, http.StatusBadRequest, "BAD_REQUEST", err.Error())
			return
		}
		if len(raw) > flowConfigMaxBytes {
			writeError(w, http.StatusBadRequest, "CONFIG_IMPORT_FAILED", "config payload too large")
			return
		}
		req, err := flowCfgSvc.decryptConfig(raw)
		if err != nil {
			writeError(w, http.StatusBadRequest, "CONFIG_IMPORT_FAILED", err.Error())
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{
			"success": true,
			"code":    "OK",
			"message": "Config imported.",
			"config":  req,
		})
	})

	frontendRoot := cfg.FrontendRoot
	frontendAbs, err := filepath.Abs(frontendRoot)
	if err != nil {
		logger.Fatalf("resolve frontend path failed: %v", err)
	}
	fileSrv := http.FileServer(http.Dir(frontendRoot))
	rootMux := http.NewServeMux()

	rootMux.Handle("/api/", apiMux)
	rootMux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		if strings.HasPrefix(r.URL.Path, "/api/") {
			apiMux.ServeHTTP(w, r)
			return
		}
		reqPath := r.URL.Path
		if reqPath == "/" {
			reqPath = "/index.html"
		}
		cleanPath := filepath.Clean(strings.TrimPrefix(reqPath, "/"))
		target := filepath.Join(frontendRoot, cleanPath)
		targetAbs, err := filepath.Abs(target)
		if err != nil {
			http.NotFound(w, r)
			return
		}
		if targetAbs != frontendAbs && !strings.HasPrefix(targetAbs, frontendAbs+string(os.PathSeparator)) {
			http.NotFound(w, r)
			return
		}
		if st, err := os.Stat(targetAbs); err == nil && !st.IsDir() {
			fileSrv.ServeHTTP(w, r)
			return
		}
		http.NotFound(w, r)
	})

	server := &http.Server{
		Addr:              cfg.ListenAddr,
		Handler:           withCORS(rootMux),
		ReadHeaderTimeout: 10 * time.Second,
	}

	logger.Printf("Go backend listening on http://localhost%s", cfg.ListenAddr)
	logger.Printf("Frontend root: %s", frontendRoot)
	logger.Printf("Data root: %s", cfg.DataRoot)
	logger.Printf("UseMockHardware: %v", cfg.Hardware.UseMockHardware || runtime.GOOS == "windows")
	if err := server.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		logger.Fatalf("server failed: %v", err)
	}
}
