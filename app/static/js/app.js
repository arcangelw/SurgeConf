/** 公共 API 工具 */

const API = {
  async get(url) {
    const resp = await fetch(url);
    if (!resp.ok) throw new Error(`GET ${url}: ${resp.status}`);
    return resp.json();
  },
  async post(url, data) {
    const resp = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({ detail: resp.statusText }));
      throw new Error(err.detail || resp.statusText);
    }
    return resp.json();
  },
  async put(url, data) {
    const resp = await fetch(url, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({ detail: resp.statusText }));
      throw new Error(err.detail || resp.statusText);
    }
    return resp.json();
  },
  async del(url) {
    const resp = await fetch(url, { method: "DELETE" });
    if (!resp.ok) throw new Error(`DELETE ${url}: ${resp.status}`);
    return resp.json();
  },
  async getText(url) {
    const resp = await fetch(url);
    if (!resp.ok) throw new Error(`GET ${url}: ${resp.status}`);
    return resp.text();
  },
};

function toast(message, type = "success") {
  const container = document.querySelector(".toast-container") || (() => {
    const el = document.createElement("div");
    el.className = "toast-container";
    document.body.appendChild(el);
    return el;
  })();
  const el = document.createElement("div");
  el.className = `toast toast-${type}`;
  el.textContent = message;
  container.appendChild(el);
  setTimeout(() => el.remove(), 3000);
}

function showModal(id) {
  document.getElementById(id)?.classList.add("show");
}

function hideModal(id) {
  document.getElementById(id)?.classList.remove("show");
}

function escapeHtml(s) {
  const d = document.createElement("div");
  d.textContent = s;
  return d.innerHTML;
}

/** 翻译名称：在 LOCALE_NAMES 存在时（英文模式）翻译中文组名 */
function translateName(name) {
  if (LOCALE_NAMES && Object.keys(LOCALE_NAMES).length > 0) {
    if (name.endsWith("手动")) {
      const region = name.slice(0, -2);
      return (LOCALE_NAMES[region] || region) + " Manual";
    }
    return LOCALE_NAMES[name] || name;
  }
  return name;
}

/** 侧边栏导航高亮 */
document.addEventListener("DOMContentLoaded", () => {
  const path = window.location.pathname;
  document.querySelectorAll(".sidebar nav a").forEach(a => {
    if (a.getAttribute("href") === path) {
      a.classList.add("active");
    }
  });
});
