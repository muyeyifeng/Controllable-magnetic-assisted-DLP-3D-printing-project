const uiState = {
  snapshot: null,
  manualMagnetEnabled: false,
  pollHandle: null,
  saveTimer: null,
};

function $(id) {
  return document.getElementById(id);
}

async function api(url, options = {}) {
  const response = await fetch(url, {
    headers: {
      "Content-Type": "application/json",
      ...(options.headers || {}),
    },
    ...options,
  });
  const data = await response.json();
  if (!response.ok || !data.success) {
    throw new Error(data.message || "请求失败");
  }
  return data;
}

function settingValue(key) {
  const el = document.querySelector(`[data-setting="${key}"]`);
  if (!el) return null;
  if (el.type === "checkbox") return el.checked;
  if (el.type === "number") return Number(el.value);
  return el.value;
}

function collectSettings() {
  const settings = {};
  document.querySelectorAll("[data-setting]").forEach((el) => {
    settings[el.dataset.setting] = el.type === "checkbox" ? el.checked : Number(el.value);
  });
  return settings;
}

function fillSettings(settings) {
  document.querySelectorAll("[data-setting]").forEach((el) => {
    const key = el.dataset.setting;
    if (!(key in settings)) return;
    if (el.type === "checkbox") {
      el.checked = Boolean(settings[key]);
    } else {
      el.value = settings[key];
    }
  });
  $("previewExposureTime").textContent = settings.exposure_time_s ?? 0;
  $("previewExposurePower").textContent = settings.exposure_power ?? 0;
}

function formatDeviceStatus(device) {
  if (!device) return "未连接";
  if (device.connected) return device.message || "已连接";
  return device.message || "未连接";
}

function renderLogs(logs = []) {
  const host = $("logContent");
  host.innerHTML = "";
  [...logs].reverse().forEach((item) => {
    const line = document.createElement("div");
    line.className = "cp-log-line";
    line.textContent = `[${item.time}] ${item.message}`;
    host.appendChild(line);
  });
}

function activePreviewIndex(snapshot) {
  if (!snapshot) return 1;
  if (snapshot.job.active && snapshot.job.current_layer > 0) {
    return snapshot.job.current_layer;
  }
  return snapshot.images.preview_index || 1;
}

function renderPreview(snapshot) {
  const files = snapshot.images.files || [];
  const index = activePreviewIndex(snapshot);
  const file = files[index - 1];

  $("currentLayerDisplay").textContent = String(index);
  $("totalLayersDisplay").textContent = String(snapshot.images.count || 0);
  $("progressText").textContent = `${snapshot.job.progress_percent || 0}%`;

  if (file) {
    $("previewImage").src = file.url;
    $("previewImage").style.display = "block";
    $("previewEmpty").style.display = "none";
  } else {
    $("previewImage").removeAttribute("src");
    $("previewImage").style.display = "none";
    $("previewEmpty").style.display = "grid";
  }
}

function renderInfo(snapshot) {
  const job = snapshot.job;
  const motor = snapshot.devices.motor || {};
  const magnet = snapshot.devices.magnet || {};
  const info = [
    `当前状态: ${snapshot.status_text}`,
    `任务阶段: ${job.phase}`,
    `已完成层数: ${job.completed_layers}/${job.total_layers}`,
    `当前图像: ${job.current_image_name || "-"}`,
    `电机位置: ${Number(motor.position_um || 0).toFixed(2)} um`,
    `电机步数: ${motor.position_steps ?? 0}`,
    `上限位状态: ${motor.top_limit_triggered ? "已触发" : "未触发"}`,
    `电机运动状态: ${motor.is_moving ? `运动中(${motor.direction || "-"})` : "空闲"}`,
    `磁场电压: ${Number(snapshot.devices.magnet.voltage || 0).toFixed(2)} V`,
    `磁场 IO 电平: ${magnet.io_level || "-"}`,
    `任务开始时间: ${job.started_at || "-"}`,
    `任务结束时间: ${job.finished_at || "-"}`,
    `错误信息: ${job.error || "-"}`,
  ];
  $("infoWindow").textContent = info.join("\n");
}

function renderSnapshot(snapshot) {
  uiState.snapshot = snapshot;
  $("statusText").textContent = snapshot.status_text;
  $("uvStatus").textContent = formatDeviceStatus(snapshot.devices.uv);
  $("motorStatus").textContent = formatDeviceStatus(snapshot.devices.motor);
  $("magnetStatus").textContent = formatDeviceStatus(snapshot.devices.magnet);
  $("motorPositionValue").textContent =
    `${Number(snapshot.devices.motor.position_um || 0).toFixed(2)} um / ${Number(snapshot.devices.motor.position_steps || 0)} steps`;
  $("machineStateValue").textContent = snapshot.machine_state;
  $("magnetStateValue").textContent = `${Number(snapshot.devices.magnet.voltage || 0).toFixed(2)} V`;
  uiState.manualMagnetEnabled = Boolean(snapshot.devices.magnet.enabled);

  fillSettings(snapshot.settings);
  renderPreview(snapshot);
  renderInfo(snapshot);
  renderLogs(snapshot.logs);

  $("pauseButton").disabled = !snapshot.job.active || snapshot.job.paused;
  $("resumeButton").disabled = !snapshot.job.paused;
  $("startPrintButton").disabled = snapshot.job.active;
  $("stopPrintButton").disabled = !snapshot.job.active;
}

async function loadState() {
  const response = await fetch("/api/state");
  const data = await response.json();
  renderSnapshot(data);
}

function scheduleSaveSettings() {
  clearTimeout(uiState.saveTimer);
  uiState.saveTimer = setTimeout(async () => {
    try {
      await api("/api/settings", {
        method: "POST",
        body: JSON.stringify({ settings: collectSettings() }),
      });
      await loadState();
    } catch (error) {
      console.error(error);
      alert(error.message);
    }
  }, 250);
}

async function reconnectDevice(device) {
  try {
    await api(`/api/devices/${device}/reconnect`, { method: "POST" });
    await loadState();
  } catch (error) {
    alert(error.message);
  }
}

async function sendManualMove(direction, speedKey, distanceKey) {
  try {
    await api("/api/manual/move", {
      method: "POST",
      body: JSON.stringify({
        direction,
        distance_um: settingValue(distanceKey),
        speed_um_s: settingValue(speedKey),
      }),
    });
    await loadState();
  } catch (error) {
    alert(error.message);
  }
}

async function manualExpose() {
  try {
    await api("/api/manual/expose", {
      method: "POST",
      body: JSON.stringify({
        power: settingValue("exposure_power"),
        duration_s: settingValue("exposure_time_s"),
      }),
    });
    await loadState();
  } catch (error) {
    alert(error.message);
  }
}

async function manualMagnetToggle() {
  try {
    uiState.manualMagnetEnabled = !uiState.manualMagnetEnabled;
    await api("/api/manual/magnet", {
      method: "POST",
      body: JSON.stringify({
        enabled: uiState.manualMagnetEnabled,
        voltage: settingValue("magnet_voltage"),
      }),
    });
    await loadState();
  } catch (error) {
    uiState.manualMagnetEnabled = !uiState.manualMagnetEnabled;
    alert(error.message);
  }
}

async function selectPreview(nextIndex) {
  if (!uiState.snapshot || !uiState.snapshot.images.count) return;
  const index = Math.min(Math.max(nextIndex, 1), uiState.snapshot.images.count);
  try {
    await api("/api/preview/select", {
      method: "POST",
      body: JSON.stringify({ index }),
    });
    await loadState();
  } catch (error) {
    alert(error.message);
  }
}

async function uploadZip(file) {
  const formData = new FormData();
  formData.append("zipFile", file);
  const response = await fetch("/api/upload", {
    method: "POST",
    body: formData,
  });
  const data = await response.json();
  if (!response.ok || !data.success) {
    throw new Error(data.message || "上传失败");
  }
  return data;
}

async function jobAction(url) {
  try {
    await api(url, { method: "POST" });
    await loadState();
  } catch (error) {
    alert(error.message);
  }
}

function bindEvents() {
  document.querySelectorAll("[data-device]").forEach((button) => {
    button.addEventListener("click", () => reconnectDevice(button.dataset.device));
  });

  document.querySelectorAll("[data-setting]").forEach((el) => {
    el.addEventListener("input", scheduleSaveSettings);
    el.addEventListener("change", scheduleSaveSettings);
  });

  document.querySelectorAll("[data-manual]").forEach((button) => {
    button.addEventListener("click", async () => {
      const action = button.dataset.manual;
      if (action === "fast-down") return sendManualMove("down", "fast_down_speed_um_s", "fast_down_distance_um");
      if (action === "fast-up") return sendManualMove("up", "fast_up_speed_um_s", "fast_up_distance_um");
      if (action === "slow-down") return sendManualMove("down", "slow_down_speed_um_s", "fast_down_distance_um");
      if (action === "slow-up") return sendManualMove("up", "slow_up_speed_um_s", "fast_up_distance_um");
      if (action === "expose") return manualExpose();
    });
  });

  $("homeButton").addEventListener("click", () => jobAction("/api/manual/home"));
  $("pullUpButton").addEventListener("click", () => sendManualMove("up", "fast_up_speed_um_s", "fast_up_distance_um"));
  $("pauseButton").addEventListener("click", () => jobAction("/api/print/pause"));
  $("resumeButton").addEventListener("click", () => jobAction("/api/print/resume"));
  $("magnetToggleButton").addEventListener("click", manualMagnetToggle);
  $("startPrintButton").addEventListener("click", () => jobAction("/api/print/start"));
  $("stopPrintButton").addEventListener("click", () => jobAction("/api/print/stop"));

  $("prevLayerButton").addEventListener("click", () => selectPreview(activePreviewIndex(uiState.snapshot) - 1));
  $("nextLayerButton").addEventListener("click", () => selectPreview(activePreviewIndex(uiState.snapshot) + 1));

  $("zipUploadBtn").addEventListener("click", () => $("zipFileInput").click());
  $("zipFileInput").addEventListener("change", async (event) => {
    const file = event.target.files[0];
    if (!file) return;
    try {
      await uploadZip(file);
      await loadState();
      alert("切片包上传成功");
    } catch (error) {
      alert(error.message);
    } finally {
      event.target.value = "";
    }
  });
}

async function init() {
  bindEvents();
  await loadState();
  uiState.pollHandle = setInterval(loadState, 1000);
}

window.addEventListener("DOMContentLoaded", init);
