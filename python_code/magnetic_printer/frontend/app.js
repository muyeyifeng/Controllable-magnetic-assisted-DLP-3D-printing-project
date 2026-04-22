const apiBase = window.location.origin;
const tokenKey = "magnetic_printer_token";
let eventSource = null;
let statusPollTimer = null;
let packagePollTimer = null;
let lastStatusError = "";

const el = {
  authInfo: document.querySelector("#auth-info"),
  adminBtn: document.querySelector("#admin-btn"),
  logoutBtn: document.querySelector("#logout-btn"),
  deviceStatus: document.querySelector("#device-status"),
  jobStatus: document.querySelector("#job-status"),
  jobEvents: document.querySelector("#job-events"),
  packageFile: document.querySelector("#package-file"),
  uploadBtn: document.querySelector("#upload-btn"),
  packageSelect: document.querySelector("#package-select"),
  overrideThickness: document.querySelector("#override-thickness"),
  overrideMagnetic: document.querySelector("#override-magnetic"),
  overrideIntensity: document.querySelector("#override-intensity"),
  globalHold: document.querySelector("#global-hold"),
  globalExposure: document.querySelector("#global-exposure"),
  startBtn: document.querySelector("#start-btn"),
  cancelBtn: document.querySelector("#cancel-btn"),
  messages: document.querySelector("#messages"),
};

function getToken() {
  return localStorage.getItem(tokenKey);
}

function clearToken() {
  localStorage.removeItem(tokenKey);
}

function requireToken() {
  if (!getToken()) {
    window.location.href = "/index.html";
    return false;
  }
  return true;
}

function appendMessage(message) {
  const line = `[${new Date().toLocaleTimeString()}] ${message}`;
  el.messages.textContent = `${line}\n${el.messages.textContent}`.trim();
}

async function api(path, options = {}) {
  const token = getToken();
  const headers = { ...(options.headers || {}) };
  if (token) {
    headers.Authorization = `Bearer ${token}`;
  }
  if (!(options.body instanceof FormData) && !headers["Content-Type"]) {
    headers["Content-Type"] = "application/json";
  }

  const res = await fetch(`${apiBase}${path}`, { ...options, headers });
  let body = null;
  try {
    body = await res.json();
  } catch {
    body = null;
  }
  if (!res.ok) {
    if (res.status === 401) {
      throw new Error("UNAUTHORIZED");
    }
    throw new Error(body?.message || `${res.status} ${res.statusText}`);
  }
  return body;
}

function stopTimers() {
  if (statusPollTimer) {
    clearInterval(statusPollTimer);
    statusPollTimer = null;
  }
  if (packagePollTimer) {
    clearInterval(packagePollTimer);
    packagePollTimer = null;
  }
}

function handleUnauthorized() {
  stopTimers();
  closeStream();
  clearToken();
  window.location.href = "/index.html";
}

function updateStatus(status) {
  if (!status) return;
  const owner = status.lockOwner || status.job?.owner || "无";
  el.deviceStatus.textContent =
    `设备: ${status.isBusy ? "忙碌中" : "空闲"} | 占用用户: ${owner} | 当前登录用户可控: ${status.canControlDevice ? "是" : "否"}`;

  const job = status.job || {};
  el.jobStatus.textContent =
    `任务状态: ${job.state ?? "-"} | owner=${job.owner || "-"} | package=${job.packageId || "-"} | 当前层: ${job.currentLayer || 0}/${job.totalLayers || 0} | 已完成: ${job.completedLayers || 0}`;
  el.jobEvents.textContent = (job.recentEvents || []).slice(-20).join("\n");
}

async function refreshMe() {
  try {
    const me = await api("/api/auth/me");
    el.authInfo.textContent = `用户: ${me.username} | 设备占用用户: ${me.lockOwner || "无"} | 可控制: ${me.canControlDevice ? "是" : "否"}`;
    el.adminBtn.style.display = "inline-block";
    el.adminBtn.textContent = me.isAdmin ? "管理员调试/流程编排" : "流程编排";
    return me;
  } catch (error) {
    if (error.message === "UNAUTHORIZED") {
      handleUnauthorized();
      return null;
    }
    appendMessage(`获取用户信息失败: ${error.message}`);
    return null;
  }
}

async function refreshPackages() {
  try {
    const list = await api("/api/packages");
    const selected = el.packageSelect.value;
    el.packageSelect.innerHTML = "";

    list.forEach((pkg) => {
      const option = document.createElement("option");
      option.value = pkg.id;
      option.textContent = `${pkg.id} | ${pkg.name} | 层数=${pkg.layerCount} | 层厚=${pkg.layerThicknessMm}mm`;
      el.packageSelect.appendChild(option);
    });

    if (selected) {
      el.packageSelect.value = selected;
    }
  } catch (error) {
    if (error.message === "UNAUTHORIZED") {
      handleUnauthorized();
      return;
    }
    appendMessage(`刷新包列表失败: ${error.message}`);
  }
}

async function refreshStatus() {
  try {
    const status = await api("/api/device/status");
    lastStatusError = "";
    updateStatus(status);
  } catch (error) {
    if (error.message === "UNAUTHORIZED") {
      handleUnauthorized();
      return;
    }
    if (error.message !== lastStatusError) {
      lastStatusError = error.message;
      appendMessage(`刷新状态失败: ${error.message}`);
    }
  }
}

function openStream() {
  closeStream();
  const token = getToken();
  if (!token) return;

  eventSource = new EventSource(`${apiBase}/api/device/stream?access_token=${encodeURIComponent(token)}`);
  eventSource.addEventListener("status", (event) => {
    try {
      const payload = JSON.parse(event.data);
      updateStatus(payload);
    } catch {
      // ignore invalid event payload
    }
  });
  eventSource.onerror = () => {
    closeStream();
    setTimeout(openStream, 2000);
  };
}

function closeStream() {
  if (eventSource) {
    eventSource.close();
    eventSource = null;
  }
}

el.adminBtn.addEventListener("click", () => {
  window.location.href = "/admin.html";
});

el.logoutBtn.addEventListener("click", async () => {
  try {
    await api("/api/auth/logout", { method: "POST" });
  } catch {
    // ignore network/auth errors on logout
  }
  handleUnauthorized();
});

el.uploadBtn.addEventListener("click", async () => {
  if (!el.packageFile.files?.length) {
    appendMessage("请选择 zip 文件");
    return;
  }
  const file = el.packageFile.files[0];
  const formData = new FormData();
  formData.append("file", file);

  try {
    const result = await api("/api/packages/upload", {
      method: "POST",
      body: formData,
      headers: {},
    });
    appendMessage(`上传成功: ${result.package.id}`);
    await refreshPackages();
  } catch (error) {
    if (error.message === "UNAUTHORIZED") {
      handleUnauthorized();
      return;
    }
    appendMessage(`上传失败: ${error.message}`);
  }
});

el.startBtn.addEventListener("click", async () => {
  const payload = {
    packageId: el.packageSelect.value,
    overrides: {
      layerThicknessMm: el.overrideThickness.value ? Number(el.overrideThickness.value) : null,
      magneticVoltage: el.overrideMagnetic.value ? Number(el.overrideMagnetic.value) : null,
      exposureIntensity: el.overrideIntensity.value ? Number(el.overrideIntensity.value) : null,
      magneticHoldSeconds: Number(el.globalHold.value || 0),
      exposureSeconds: Number(el.globalExposure.value || 0),
    },
  };

  try {
    const result = await api("/api/print/start", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    appendMessage(result.message || "任务已开始");
    updateStatus(result.status);
  } catch (error) {
    if (error.message === "UNAUTHORIZED") {
      handleUnauthorized();
      return;
    }
    appendMessage(`启动失败: ${error.message}`);
  }
});

el.cancelBtn.addEventListener("click", async () => {
  try {
    const result = await api("/api/print/cancel", { method: "POST" });
    appendMessage(result.message || "取消请求已提交");
    updateStatus(result.status);
  } catch (error) {
    if (error.message === "UNAUTHORIZED") {
      handleUnauthorized();
      return;
    }
    appendMessage(`取消失败: ${error.message}`);
  }
});

if (requireToken()) {
  refreshMe();
  refreshPackages();
  refreshStatus();
  openStream();
  statusPollTimer = setInterval(refreshStatus, 3000);
  packagePollTimer = setInterval(refreshPackages, 15000);
}
