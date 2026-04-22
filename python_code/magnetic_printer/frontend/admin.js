const apiBase = window.location.origin;
const tokenKey = "magnetic_printer_token";

const el = {
  authInfo: document.querySelector("#auth-info"),
  backBtn: document.querySelector("#back-btn"),
  messages: document.querySelector("#messages"),
  manualSection: document.querySelector("#manual-section"),
  programName: document.querySelector("#program-name"),
  addRootStepBtn: document.querySelector("#add-root-step-btn"),
  runProgramBtn: document.querySelector("#run-program-btn"),
  cancelProgramBtn: document.querySelector("#cancel-program-btn"),
  exportConfigBtn: document.querySelector("#export-config-btn"),
  importConfigFile: document.querySelector("#import-config-file"),
  importConfigBtn: document.querySelector("#import-config-btn"),
  stepRoot: document.querySelector("#step-root"),
  programStatus: document.querySelector("#program-status"),
  programEvents: document.querySelector("#program-events"),
  manualMagnetBits: document.querySelector("#manual-magnet-bits"),
  manualMagnetVoltage: document.querySelector("#manual-magnet-voltage"),
  manualMagnetHold: document.querySelector("#manual-magnet-hold"),
  manualMagnetRun: document.querySelector("#manual-magnet-run"),
  manualExposureImage: document.querySelector("#manual-exposure-image"),
  manualExposureIntensity: document.querySelector("#manual-exposure-intensity"),
  manualExposureSeconds: document.querySelector("#manual-exposure-seconds"),
  manualExposureRun: document.querySelector("#manual-exposure-run"),
  manualMoveDownThickness: document.querySelector("#manual-move-down-thickness"),
  manualMoveDownRun: document.querySelector("#manual-move-down-run"),
  manualMoveUpThickness: document.querySelector("#manual-move-up-thickness"),
  manualMoveUpRun: document.querySelector("#manual-move-up-run"),
  manualHomeRun: document.querySelector("#manual-home-run"),
  manualWaitSeconds: document.querySelector("#manual-wait-seconds"),
  manualWaitRun: document.querySelector("#manual-wait-run"),
};

const rootSteps = [];
let isAdminUser = false;
let packagePollTimer = null;
let knownPackages = [];
let packageSignature = "";

function getToken() {
  return localStorage.getItem(tokenKey);
}

function clearToken() {
  localStorage.removeItem(tokenKey);
}

function appendMessage(message) {
  const line = `[${new Date().toLocaleTimeString()}] ${message}`;
  el.messages.textContent = `${line}\n${el.messages.textContent}`.trim();
}

function requireToken() {
  if (!getToken()) {
    window.location.href = "/index.html";
    return false;
  }
  return true;
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

function handleUnauthorized() {
  if (packagePollTimer) {
    clearInterval(packagePollTimer);
    packagePollTimer = null;
  }
  clearToken();
  window.location.href = "/index.html";
}

function requireAdminManual() {
  if (isAdminUser) {
    return true;
  }
  appendMessage("当前账号无管理员权限，仅可使用流程编排功能。");
  return false;
}

function defaultParamsByType(type) {
  if (type === "magnet" || type === "magnet_async") {
    return {
      magnetSource: "manual",
      slicePackageId: "",
      useSliceDirection: true,
      useSliceStrength: true,
      sliceAdvance: false,
      directionBits: "00001111",
      magneticVoltage: 1.0,
      holdSeconds: 1.0,
    };
  }
  if (type === "exposure") {
    return {
      imageSource: "manual",
      slicePackageId: "",
      useSliceIntensity: true,
      useSliceMagnet: true,
      imagePath: "",
      exposureIntensity: 80,
      exposureSeconds: 1.0,
      holdSeconds: 1.0,
      magneticVoltage: 0,
    };
  }
  if (type === "move" || type === "move_down") {
    return {
      layerThicknessMm: 0.05,
      moveDirection: "down",
    };
  }
  if (type === "move_up") {
    return {
      layerThicknessMm: 0.05,
      moveDirection: "up",
    };
  }
  if (type === "home") {
    return {};
  }
  if (type === "wait_all_idle") {
    return {};
  }
  if (type === "wait") {
    return {
      waitSeconds: 1.0,
    };
  }
  return {};
}

function normalizeMagnetParams(params) {
  const next = { ...(params || {}) };
  const sourceRaw = String(next.magnetSource || (next.slicePackageId ? "slice" : "manual")).trim().toLowerCase();
  next.magnetSource = sourceRaw === "slice" ? "slice" : "manual";
  next.slicePackageId = String(next.slicePackageId || "").trim();
  if (typeof next.useSliceDirection !== "boolean") {
    next.useSliceDirection = true;
  }
  if (typeof next.useSliceStrength !== "boolean") {
    next.useSliceStrength = true;
  }
  if (typeof next.sliceAdvance !== "boolean") {
    next.sliceAdvance = false;
  }
  next.directionBits = String(next.directionBits || "00001111").trim();
  if (!Number.isFinite(Number(next.magneticVoltage))) {
    next.magneticVoltage = 1.0;
  } else {
    next.magneticVoltage = Number(next.magneticVoltage);
  }
  if (!Number.isFinite(Number(next.holdSeconds))) {
    next.holdSeconds = 1.0;
  } else {
    next.holdSeconds = Number(next.holdSeconds);
  }
  return next;
}

function normalizeExposureParams(params) {
  const next = { ...(params || {}) };
  const sourceRaw = String(next.imageSource || (next.slicePackageId ? "slice" : "manual")).trim().toLowerCase();
  next.imageSource = sourceRaw === "slice" ? "slice" : "manual";
  next.slicePackageId = String(next.slicePackageId || "").trim();
  if (typeof next.useSliceIntensity !== "boolean") {
    next.useSliceIntensity = true;
  }
  if (typeof next.useSliceMagnet !== "boolean") {
    next.useSliceMagnet = true;
  }
  if (!Number.isFinite(Number(next.exposureIntensity))) {
    next.exposureIntensity = 80;
  } else {
    next.exposureIntensity = Math.round(Number(next.exposureIntensity));
  }
  if (!Number.isFinite(Number(next.exposureSeconds))) {
    next.exposureSeconds = 1.0;
  } else {
    next.exposureSeconds = Number(next.exposureSeconds);
  }
  if (!Number.isFinite(Number(next.holdSeconds))) {
    next.holdSeconds = 1.0;
  } else {
    next.holdSeconds = Number(next.holdSeconds);
  }
  if (!Number.isFinite(Number(next.magneticVoltage))) {
    next.magneticVoltage = 0;
  } else {
    next.magneticVoltage = Number(next.magneticVoltage);
  }
  if (!next.imagePath) {
    next.imagePath = "";
  }
  return next;
}

function createStep(type = "magnet") {
  return {
    id: `step_${Date.now()}_${Math.floor(Math.random() * 100000)}`,
    type,
    repeat: 2,
    params: defaultParamsByType(type),
    children: [],
  };
}

function addInput(parent, labelText, value, onChange) {
  const label = document.createElement("label");
  label.textContent = labelText;
  const input = document.createElement("input");
  input.value = value;
  input.addEventListener("input", () => onChange(input.value));
  label.appendChild(input);
  parent.appendChild(label);
}

function addNumber(parent, labelText, value, step, onChange) {
  const label = document.createElement("label");
  label.textContent = labelText;
  const input = document.createElement("input");
  input.type = "number";
  input.step = String(step);
  input.value = value;
  input.addEventListener("input", () => onChange(Number(input.value || 0)));
  label.appendChild(input);
  parent.appendChild(label);
}

function addSelect(parent, labelText, value, items, onChange) {
  const label = document.createElement("label");
  label.textContent = labelText;
  const select = document.createElement("select");
  items.forEach((item) => {
    const option = document.createElement("option");
    option.value = item.value;
    option.textContent = item.label;
    if (String(item.value) === String(value)) {
      option.selected = true;
    }
    select.appendChild(option);
  });
  select.addEventListener("change", () => onChange(select.value));
  label.appendChild(select);
  parent.appendChild(label);
}

function addCheckbox(parent, text, checked, onChange) {
  const label = document.createElement("label");
  label.style.flexDirection = "row";
  label.style.alignItems = "center";
  label.style.gap = "8px";
  const input = document.createElement("input");
  input.type = "checkbox";
  input.checked = !!checked;
  input.addEventListener("change", () => onChange(input.checked));
  const span = document.createElement("span");
  span.textContent = text;
  label.appendChild(input);
  label.appendChild(span);
  parent.appendChild(label);
}

function renderParamFields(step, card) {
  if (step.type === "magnet" || step.type === "magnet_async") {
    step.params = normalizeMagnetParams(step.params);
    addSelect(
      card,
      "磁场参数来源",
      step.params.magnetSource,
      [
        { value: "manual", label: "手动输入" },
        { value: "slice", label: "切片记录" },
      ],
      (v) => {
        step.params.magnetSource = v;
        renderSteps();
      },
    );
    if (step.params.magnetSource === "slice") {
      const packageOptions = knownPackages.map((pkg) => ({
        value: pkg.id,
        label: `${pkg.id} | ${pkg.name} | 层数=${pkg.layerCount}`,
      }));
      if (packageOptions.length === 0) {
        const p = document.createElement("p");
        p.className = "hint";
        p.textContent = "暂无可用切片包，请先在主控制台上传。";
        card.appendChild(p);
      } else {
        let selectedPackageId = step.params.slicePackageId;
        if (!selectedPackageId || !packageOptions.some((op) => op.value === selectedPackageId)) {
          selectedPackageId = packageOptions[0].value;
          step.params.slicePackageId = selectedPackageId;
        }
        addSelect(card, "切片包", selectedPackageId, packageOptions, (v) => {
          step.params.slicePackageId = v;
        });
      }
      addCheckbox(card, "方向使用切片配置", step.params.useSliceDirection, (checked) => {
        step.params.useSliceDirection = checked;
        renderSteps();
      });
      if (!step.params.useSliceDirection) {
        addInput(card, "方向 bits(8位)", step.params.directionBits || "00001111", (v) => {
          step.params.directionBits = v.trim();
        });
      }
      addCheckbox(card, "强度使用切片配置", step.params.useSliceStrength, (checked) => {
        step.params.useSliceStrength = checked;
        renderSteps();
      });
      if (!step.params.useSliceStrength) {
        addNumber(card, "磁场电压(V)", step.params.magneticVoltage ?? 1.0, 0.01, (v) => {
          step.params.magneticVoltage = v;
        });
      } else {
        addNumber(card, "磁场电压覆写(V,0=按切片)", step.params.magneticVoltage ?? 0, 0.01, (v) => {
          step.params.magneticVoltage = v;
        });
      }
      addCheckbox(card, "执行后消耗切片记录", step.params.sliceAdvance, (checked) => {
        step.params.sliceAdvance = checked;
      });
      const p = document.createElement("p");
      p.className = "hint";
      p.textContent = "建议磁场步骤不消耗记录，曝光步骤消耗记录，这样可在位移前/后都绑定同一条切片。";
      card.appendChild(p);
    } else {
      addInput(card, "方向 bits(8位)", step.params.directionBits || "00001111", (v) => {
        step.params.directionBits = v.trim();
      });
      addNumber(card, "磁场电压(V)", step.params.magneticVoltage ?? 1.0, 0.01, (v) => {
        step.params.magneticVoltage = v;
      });
    }
    addNumber(card, "保持时间(s)", step.params.holdSeconds ?? 1.0, 0.1, (v) => {
      step.params.holdSeconds = v;
    });
    return;
  }

  if (step.type === "exposure") {
    step.params = normalizeExposureParams(step.params);
    addSelect(
      card,
      "图片来源",
      step.params.imageSource,
      [
        { value: "manual", label: "手动路径" },
        { value: "slice", label: "切片包自动推进" },
      ],
      (v) => {
        step.params.imageSource = v;
        renderSteps();
      },
    );
    if (step.params.imageSource === "slice") {
      const packageOptions = knownPackages.map((pkg) => ({
        value: pkg.id,
        label: `${pkg.id} | ${pkg.name} | 层数=${pkg.layerCount}`,
      }));
      if (packageOptions.length === 0) {
        const p = document.createElement("p");
        p.className = "hint";
        p.textContent = "暂无可用切片包，请先在主控制台上传。";
        card.appendChild(p);
      } else {
        let selectedPackageId = step.params.slicePackageId;
        if (!selectedPackageId || !packageOptions.some((op) => op.value === selectedPackageId)) {
          selectedPackageId = packageOptions[0].value;
          step.params.slicePackageId = selectedPackageId;
        }
        addSelect(card, "切片包", selectedPackageId, packageOptions, (v) => {
          step.params.slicePackageId = v;
        });
      }
      addCheckbox(card, "曝光强度使用切片配置", step.params.useSliceIntensity, (checked) => {
        step.params.useSliceIntensity = checked;
        renderSteps();
      });
      if (!step.params.useSliceIntensity) {
        addNumber(card, "曝光强度覆写(0-255)", step.params.exposureIntensity ?? 80, 1, (v) => {
          step.params.exposureIntensity = Math.round(v);
        });
      }
      addCheckbox(card, "磁场方向/强度使用切片配置", step.params.useSliceMagnet, (checked) => {
        step.params.useSliceMagnet = checked;
        renderSteps();
      });
      if (step.params.useSliceMagnet) {
        addNumber(card, "磁场保持时间(s)", step.params.holdSeconds ?? 1.0, 0.1, (v) => {
          step.params.holdSeconds = v;
        });
        addNumber(card, "磁场电压覆写(V,0=按切片)", step.params.magneticVoltage ?? 0, 0.01, (v) => {
          step.params.magneticVoltage = v;
        });
      }
      const p = document.createElement("p");
      p.className = "hint";
      p.textContent = "每次执行该曝光步骤会自动读取切片中的下一条记录，支持同一层多次曝光。若需“位移前/后加磁场”，请用切片源 magnet/magnet_async 步骤与本步骤配合。";
      card.appendChild(p);
    } else {
      addInput(card, "图片路径", step.params.imagePath || "", (v) => {
        step.params.imagePath = v;
      });
      addNumber(card, "曝光强度(0-255)", step.params.exposureIntensity ?? 80, 1, (v) => {
        step.params.exposureIntensity = Math.round(v);
      });
    }
    addNumber(card, "曝光时间(s)", step.params.exposureSeconds ?? 1.0, 0.1, (v) => {
      step.params.exposureSeconds = v;
    });
    return;
  }

  if (step.type === "move" || step.type === "move_down" || step.type === "move_up") {
    if (step.type === "move_up") {
      step.params.moveDirection = "up";
    } else {
      step.params.moveDirection = "down";
    }
    addNumber(card, "位移层厚(mm)", step.params.layerThicknessMm ?? 0.05, 0.001, (v) => {
      step.params.layerThicknessMm = v;
    });
    return;
  }

  if (step.type === "home") {
    const p = document.createElement("p");
    p.className = "hint";
    p.textContent = "归零步骤不需要额外参数。";
    card.appendChild(p);
    return;
  }

  if (step.type === "wait_all_idle") {
    const p = document.createElement("p");
    p.className = "hint";
    p.textContent = "等待全部子设备空闲后再继续。";
    card.appendChild(p);
    return;
  }

  if (step.type === "wait") {
    addNumber(card, "等待时间(s)", step.params.waitSeconds ?? 1.0, 0.1, (v) => {
      step.params.waitSeconds = v;
    });
  }
}

function renderStepList(steps, parentNode) {
  steps.forEach((step, index) => {
    const card = document.createElement("div");
    card.className = "step-card";

    const header = document.createElement("div");
    header.className = "row";

    const typeSelect = document.createElement("select");
    ["magnet", "magnet_async", "exposure", "move_down", "move_up", "home", "wait_all_idle", "wait", "loop"].forEach((t) => {
      const op = document.createElement("option");
      op.value = t;
      op.textContent = t;
      if (step.type === t) op.selected = true;
      typeSelect.appendChild(op);
    });
    typeSelect.addEventListener("change", () => {
      step.type = typeSelect.value;
      if (step.type === "loop") {
        step.params = {};
        if (!Array.isArray(step.children)) {
          step.children = [];
        }
      } else {
        step.params = defaultParamsByType(step.type);
        step.children = [];
      }
      renderSteps();
    });
    header.appendChild(typeSelect);

    const upBtn = document.createElement("button");
    upBtn.type = "button";
    upBtn.textContent = "上移";
    upBtn.addEventListener("click", () => {
      if (index <= 0) return;
      [steps[index - 1], steps[index]] = [steps[index], steps[index - 1]];
      renderSteps();
    });
    header.appendChild(upBtn);

    const downBtn = document.createElement("button");
    downBtn.type = "button";
    downBtn.textContent = "下移";
    downBtn.addEventListener("click", () => {
      if (index >= steps.length - 1) return;
      [steps[index + 1], steps[index]] = [steps[index], steps[index + 1]];
      renderSteps();
    });
    header.appendChild(downBtn);

    const addBtn = document.createElement("button");
    addBtn.type = "button";
    addBtn.textContent = "后面插入";
    addBtn.addEventListener("click", () => {
      steps.splice(index + 1, 0, createStep("magnet"));
      renderSteps();
    });
    header.appendChild(addBtn);

    const delBtn = document.createElement("button");
    delBtn.type = "button";
    delBtn.textContent = "删除";
    delBtn.addEventListener("click", () => {
      steps.splice(index, 1);
      renderSteps();
    });
    header.appendChild(delBtn);

    card.appendChild(header);

    const title = document.createElement("p");
    title.textContent = `步骤 ${index + 1} / 类型: ${step.type}`;
    card.appendChild(title);

    if (step.type === "loop") {
      const repeatLabel = document.createElement("label");
      repeatLabel.textContent = "循环次数";
      const repeatInput = document.createElement("input");
      repeatInput.type = "number";
      repeatInput.min = "1";
      repeatInput.value = step.repeat || 1;
      repeatInput.addEventListener("input", () => {
        step.repeat = Math.max(1, Number(repeatInput.value || 1));
      });
      repeatLabel.appendChild(repeatInput);
      card.appendChild(repeatLabel);

      const addChildBtn = document.createElement("button");
      addChildBtn.type = "button";
      addChildBtn.textContent = "新增子步骤";
      addChildBtn.addEventListener("click", () => {
        step.children.push(createStep("magnet"));
        renderSteps();
      });
      card.appendChild(addChildBtn);

      const childBox = document.createElement("div");
      childBox.className = "step-children";
      if (!step.children || step.children.length === 0) {
        const empty = document.createElement("p");
        empty.className = "hint";
        empty.textContent = "循环体为空";
        childBox.appendChild(empty);
      } else {
        renderStepList(step.children, childBox);
      }
      card.appendChild(childBox);
    } else {
      renderParamFields(step, card);
    }

    parentNode.appendChild(card);
  });
}

function renderSteps() {
  el.stepRoot.innerHTML = "";
  if (rootSteps.length === 0) {
    const p = document.createElement("p");
    p.className = "hint";
    p.textContent = "还没有步骤，点击“新增根步骤”。";
    el.stepRoot.appendChild(p);
    return;
  }
  const container = document.createElement("div");
  renderStepList(rootSteps, container);
  el.stepRoot.appendChild(container);
}

function cloneStep(step) {
  return {
    id: step.id,
    type: step.type,
    repeat: step.repeat || 1,
    params: { ...(step.params || {}) },
    children: (step.children || []).map((c) => cloneStep(c)),
  };
}

function exportProgram() {
  return rootSteps.map((s) => cloneStep(s));
}

function normalizeStepType(type) {
  const t = String(type || "").trim().toLowerCase();
  const supported = new Set(["magnet", "magnet_async", "exposure", "move", "move_down", "move_up", "home", "wait_all_idle", "wait", "loop"]);
  if (supported.has(t)) {
    return t;
  }
  return "magnet";
}

function hydrateStep(rawStep) {
  const stepType = normalizeStepType(rawStep?.type);
  const step = createStep(stepType);
  if (rawStep?.id) {
    step.id = String(rawStep.id);
  }
  if (stepType === "loop") {
    step.repeat = Math.max(1, Number(rawStep?.repeat || 1));
    const children = Array.isArray(rawStep?.children) ? rawStep.children : [];
    step.children = children.map((c) => hydrateStep(c));
    step.params = {};
    return step;
  }
  step.params = { ...step.params, ...(rawStep?.params || {}) };
  if (stepType === "magnet" || stepType === "magnet_async") {
    step.params = normalizeMagnetParams(step.params);
  }
  if (stepType === "exposure") {
    step.params = normalizeExposureParams(step.params);
  }
  step.children = [];
  return step;
}

function replaceProgramFromConfig(config) {
  const steps = Array.isArray(config?.steps) ? config.steps : [];
  rootSteps.length = 0;
  steps.forEach((s) => rootSteps.push(hydrateStep(s)));
  if (!rootSteps.length) {
    rootSteps.push(createStep("magnet"));
  }
  const name = String(config?.name || "").trim();
  el.programName.value = name || "admin-flow";
  renderSteps();
}

async function refreshAdminStatus() {
  try {
    const st = await api("/api/admin/status");
    el.programStatus.textContent =
      `运行中: ${st.running ? "是" : "否"} | 名称: ${st.name || "-"} | 当前步骤: ${st.currentStep || "-"} | 异步挂起: ${st.pendingAsyncOps || 0} | 最近结果: ${st.lastResult || "-"}` +
      (st.lastError ? ` | 错误: ${st.lastError}` : "");
    el.programEvents.textContent = (st.recentEvents || []).slice(-40).join("\n");
  } catch (error) {
    if (error.message === "UNAUTHORIZED") {
      handleUnauthorized();
      return;
    }
    appendMessage(`刷新管理员状态失败: ${error.message}`);
  }
}

async function loadUserContext() {
  try {
    const me = await api("/api/auth/me");
    isAdminUser = !!me.isAdmin;
    el.authInfo.textContent = `用户: ${me.username} | 管理员: ${isAdminUser ? "是" : "否"} | 设备占用: ${me.lockOwner || "无"}`;
    if (!isAdminUser && el.manualSection) {
      el.manualSection.style.display = "none";
    }
    return true;
  } catch (error) {
    if (error.message === "UNAUTHORIZED") {
      handleUnauthorized();
      return false;
    }
    appendMessage(`鉴权失败: ${error.message}`);
    return false;
  }
}

async function refreshPackages() {
  try {
    const list = await api("/api/packages");
    knownPackages = Array.isArray(list) ? list : [];
    const nextSignature = knownPackages.map((pkg) => String(pkg.id || "")).join("|");
    if (nextSignature !== packageSignature) {
      packageSignature = nextSignature;
      renderSteps();
    }
  } catch (error) {
    if (error.message === "UNAUTHORIZED") {
      handleUnauthorized();
      return;
    }
    appendMessage(`刷新切片包列表失败: ${error.message}`);
  }
}

el.backBtn.addEventListener("click", () => {
  window.location.href = "/app.html";
});

el.addRootStepBtn.addEventListener("click", () => {
  rootSteps.push(createStep("magnet"));
  renderSteps();
});

el.runProgramBtn.addEventListener("click", async () => {
  const payload = {
    name: el.programName.value.trim() || "admin-flow",
    steps: exportProgram(),
  };
  if (!payload.steps.length) {
    appendMessage("请先添加至少一个步骤。");
    return;
  }
  try {
    const result = await api("/api/admin/program/run", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    appendMessage(result.message || "流程已启动");
    await refreshAdminStatus();
  } catch (error) {
    if (error.message === "UNAUTHORIZED") {
      handleUnauthorized();
      return;
    }
    appendMessage(`运行流程失败: ${error.message}`);
  }
});

el.cancelProgramBtn.addEventListener("click", async () => {
  try {
    const result = await api("/api/admin/program/cancel", { method: "POST" });
    appendMessage(result.message || "已发送停止请求");
    await refreshAdminStatus();
  } catch (error) {
    if (error.message === "UNAUTHORIZED") {
      handleUnauthorized();
      return;
    }
    appendMessage(`停止流程失败: ${error.message}`);
  }
});

el.exportConfigBtn.addEventListener("click", async () => {
  const payload = {
    name: el.programName.value.trim() || "flow-program",
    steps: exportProgram(),
  };
  if (!payload.steps.length) {
    appendMessage("请先添加至少一个步骤再导出。");
    return;
  }
  try {
    const token = getToken();
    const headers = {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
    };
    const res = await fetch(`${apiBase}/api/program/config/export`, {
      method: "POST",
      headers,
      body: JSON.stringify(payload),
    });
    if (res.status === 401) {
      throw new Error("UNAUTHORIZED");
    }
    if (!res.ok) {
      let errMsg = `${res.status} ${res.statusText}`;
      try {
        const body = await res.json();
        errMsg = body?.message || errMsg;
      } catch {
        // ignore json parse failure
      }
      throw new Error(errMsg);
    }
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    const safeName = (payload.name || "flow-program").replace(/[^a-zA-Z0-9_-]/g, "_");
    a.href = url;
    a.download = `${safeName}.mpcfg`;
    a.click();
    URL.revokeObjectURL(url);
    appendMessage("已导出加密流程配置。");
  } catch (error) {
    if (error.message === "UNAUTHORIZED") {
      handleUnauthorized();
      return;
    }
    appendMessage(`导出配置失败: ${error.message}`);
  }
});

el.importConfigBtn.addEventListener("click", async () => {
  const file = el.importConfigFile.files?.[0];
  if (!file) {
    appendMessage("请先选择导入文件。");
    return;
  }
  try {
    const token = getToken();
    const headers = {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/octet-stream",
    };
    const res = await fetch(`${apiBase}/api/program/config/import`, {
      method: "POST",
      headers,
      body: file,
    });
    if (res.status === 401) {
      throw new Error("UNAUTHORIZED");
    }
    let body = null;
    try {
      body = await res.json();
    } catch {
      body = null;
    }
    if (!res.ok) {
      throw new Error(body?.message || `${res.status} ${res.statusText}`);
    }
    replaceProgramFromConfig(body?.config || {});
    appendMessage(body?.message || "已导入流程配置。");
  } catch (error) {
    if (error.message === "UNAUTHORIZED") {
      handleUnauthorized();
      return;
    }
    appendMessage(`导入配置失败: ${error.message}`);
  }
});

el.manualMagnetRun.addEventListener("click", async () => {
  if (!requireAdminManual()) {
    return;
  }
  const payload = {
    directionBits: el.manualMagnetBits.value.trim(),
    magneticVoltage: Number(el.manualMagnetVoltage.value || 0),
    holdSeconds: Number(el.manualMagnetHold.value || 0),
  };
  try {
    const result = await api("/api/admin/manual/magnet", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    appendMessage(result.message || "磁场执行完成");
    await refreshAdminStatus();
  } catch (error) {
    if (error.message === "UNAUTHORIZED") {
      handleUnauthorized();
      return;
    }
    appendMessage(`磁场执行失败: ${error.message}`);
  }
});

el.manualExposureRun.addEventListener("click", async () => {
  if (!requireAdminManual()) {
    return;
  }
  const payload = {
    imagePath: el.manualExposureImage.value.trim(),
    exposureIntensity: Number(el.manualExposureIntensity.value || 0),
    exposureSeconds: Number(el.manualExposureSeconds.value || 0),
  };
  try {
    const result = await api("/api/admin/manual/exposure", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    appendMessage(result.message || "曝光执行完成");
    await refreshAdminStatus();
  } catch (error) {
    if (error.message === "UNAUTHORIZED") {
      handleUnauthorized();
      return;
    }
    appendMessage(`曝光执行失败: ${error.message}`);
  }
});

el.manualMoveDownRun.addEventListener("click", async () => {
  if (!requireAdminManual()) {
    return;
  }
  const payload = {
    layerThicknessMm: Number(el.manualMoveDownThickness.value || 0),
    moveDirection: "down",
  };
  try {
    const result = await api("/api/admin/manual/move", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    appendMessage(result.message || "下移执行完成");
    await refreshAdminStatus();
  } catch (error) {
    if (error.message === "UNAUTHORIZED") {
      handleUnauthorized();
      return;
    }
    appendMessage(`下移执行失败: ${error.message}`);
  }
});

el.manualMoveUpRun.addEventListener("click", async () => {
  if (!requireAdminManual()) {
    return;
  }
  const payload = {
    layerThicknessMm: Number(el.manualMoveUpThickness.value || 0),
    moveDirection: "up",
  };
  try {
    const result = await api("/api/admin/manual/move", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    appendMessage(result.message || "上移执行完成");
    await refreshAdminStatus();
  } catch (error) {
    if (error.message === "UNAUTHORIZED") {
      handleUnauthorized();
      return;
    }
    appendMessage(`上移执行失败: ${error.message}`);
  }
});

el.manualHomeRun.addEventListener("click", async () => {
  if (!requireAdminManual()) {
    return;
  }
  try {
    const result = await api("/api/admin/manual/home", { method: "POST" });
    appendMessage(result.message || "归零执行完成");
    await refreshAdminStatus();
  } catch (error) {
    if (error.message === "UNAUTHORIZED") {
      handleUnauthorized();
      return;
    }
    appendMessage(`归零执行失败: ${error.message}`);
  }
});

el.manualWaitRun.addEventListener("click", async () => {
  if (!requireAdminManual()) {
    return;
  }
  const payload = {
    waitSeconds: Number(el.manualWaitSeconds.value || 0),
  };
  try {
    const result = await api("/api/admin/manual/wait", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    appendMessage(result.message || "等待执行完成");
    await refreshAdminStatus();
  } catch (error) {
    if (error.message === "UNAUTHORIZED") {
      handleUnauthorized();
      return;
    }
    appendMessage(`等待执行失败: ${error.message}`);
  }
});

if (requireToken()) {
  loadUserContext().then((ok) => {
    if (!ok) return;
    refreshPackages();
    if (packagePollTimer) {
      clearInterval(packagePollTimer);
    }
    packagePollTimer = setInterval(refreshPackages, 15000);
    if (rootSteps.length === 0) {
      rootSteps.push(createStep("magnet"));
    }
    renderSteps();
    refreshAdminStatus();
    setInterval(refreshAdminStatus, 1200);
  });
}
