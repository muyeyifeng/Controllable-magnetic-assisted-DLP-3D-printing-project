const apiBase = window.location.origin;
const tokenKey = "magnetic_printer_token";

const el = {
  registerUser: document.querySelector("#register-username"),
  registerPass: document.querySelector("#register-password"),
  registerBtn: document.querySelector("#register-btn"),
  loginUser: document.querySelector("#login-username"),
  loginPass: document.querySelector("#login-password"),
  loginBtn: document.querySelector("#login-btn"),
  authInfo: document.querySelector("#auth-info"),
  messages: document.querySelector("#messages"),
};

function appendMessage(message) {
  const line = `[${new Date().toLocaleTimeString()}] ${message}`;
  el.messages.textContent = `${line}\n${el.messages.textContent}`.trim();
}

function setToken(token) {
  if (!token) {
    localStorage.removeItem(tokenKey);
    return;
  }
  localStorage.setItem(tokenKey, token);
}

function getToken() {
  return localStorage.getItem(tokenKey);
}

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (!headers["Content-Type"]) {
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
    throw new Error(body?.message || `${res.status} ${res.statusText}`);
  }
  return body;
}

async function tryAutoLogin() {
  const token = getToken();
  if (!token) {
    return;
  }
  try {
    const res = await fetch(`${apiBase}/api/auth/me`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (res.ok) {
      window.location.href = "/app.html";
      return;
    }
  } catch {
    // ignore
  }
  setToken(null);
}

el.registerBtn.addEventListener("click", async () => {
  const username = el.registerUser.value.trim();
  const password = el.registerPass.value;
  if (!username || !password) {
    appendMessage("注册失败: 用户名和密码不能为空");
    return;
  }
  try {
    const result = await api("/api/auth/register", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    });
    appendMessage(result.message || "注册成功");
    el.loginUser.value = username;
    el.loginPass.value = "";
  } catch (error) {
    appendMessage(`注册失败: ${error.message}`);
  }
});

el.loginBtn.addEventListener("click", async () => {
  const username = el.loginUser.value.trim();
  const password = el.loginPass.value;
  if (!username || !password) {
    appendMessage("登录失败: 用户名和密码不能为空");
    return;
  }
  try {
    const result = await api("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    });
    if (!result?.token) {
      throw new Error("服务端未返回 token");
    }
    setToken(result.token);
    el.authInfo.textContent = `已登录: ${result.username}`;
    appendMessage("登录成功，正在进入控制台...");
    window.location.href = "/app.html";
  } catch (error) {
    appendMessage(`登录失败: ${error.message}`);
  }
});

tryAutoLogin();
