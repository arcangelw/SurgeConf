/** 公共 API 工具 */

/** 从页面 meta 读取当前会话 CSRF 值 */
function csrfToken() {
  return document.querySelector('meta[name="csrf-token"]')?.content || "";
}

/** 统一 fetch：登录态失效跳登录页 */
async function apiFetch(url, options = {}) {
  const resp = await fetch(url, options);
  if (resp.status === 401 && !url.startsWith("/api/auth/")) {
    window.location.href = "/login";
  }
  return resp;
}

const API = {
  async get(url) {
    const resp = await apiFetch(url);
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.detail || `GET ${url}: ${resp.status}`);
    }
    return resp.json();
  },
  async post(url, data) {
    const resp = await apiFetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken() },
      body: JSON.stringify(data),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({ detail: resp.statusText }));
      throw new Error(err.detail || resp.statusText);
    }
    return resp.json();
  },
  async put(url, data) {
    const resp = await apiFetch(url, {
      method: "PUT",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken() },
      body: JSON.stringify(data),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({ detail: resp.statusText }));
      throw new Error(err.detail || resp.statusText);
    }
    return resp.json();
  },
  async del(url) {
    const resp = await apiFetch(url, { method: "DELETE", headers: { "X-CSRF-Token": csrfToken() } });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.detail || `DELETE ${url}: ${resp.status}`);
    }
    return resp.json();
  },
  async getText(url) {
    const resp = await apiFetch(url);
    if (!resp.ok) throw new Error(`GET ${url}: ${resp.status}`);
    return resp.text();
  },
};

/** 退出登录：走统一 API（自动携带 CSRF），清除会话后回登录页 */
async function logout() {
  try { await API.post("/api/auth/logout"); } catch (e) { /* 失败也继续跳转 */ }
  window.location.href = "/login";
}

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
  const el = document.getElementById(id);
  if (!el) return;
  el.classList.add("show");
  // 点击遮罩关闭
  el.onclick = function(e) {
    if (e.target === el) hideModal(id);
  };
  // 聚焦弹窗内第一个输入控件
  const first = el.querySelector("input:not([type=hidden]), select, textarea");
  if (first) first.focus();
}

function hideModal(id) {
  document.getElementById(id)?.classList.remove("show");
}

/** ESC 关闭当前展示的弹窗（自绘确认框优先处理） */
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  const shown = document.querySelector(".modal-overlay.show");
  if (shown?.id) hideModal(shown.id);
});

/** 自绘确认弹窗：Promise<boolean>；ESC/遮罩点击 resolve(false) */
function confirmDialog(message, opts = {}) {
  return new Promise((resolve) => {
    const overlay = document.createElement("div");
    overlay.className = "modal-overlay";
    overlay.innerHTML = `
      <div class="modal confirm-modal" style="max-width:400px;">
        <h3>${escapeHtml(opts.title || "")}</h3>
        <p style="margin:12px 0 20px;line-height:1.6;">${escapeHtml(message)}</p>
        <div class="modal-actions" style="display:flex;gap:10px;justify-content:flex-end;">
          <button class="btn cd-cancel">${I18N["btn.cancel"]}</button>
          <button class="btn ${opts.danger ? "btn-danger" : "btn-primary"} cd-ok">${I18N["btn.confirm"]}</button>
        </div>
      </div>`;
    const close = (result) => {
      overlay.remove();
      document.removeEventListener("keydown", onKey);
      resolve(result);
    };
    const onKey = (e) => {
      if (e.key === "Escape") { e.stopPropagation(); close(false); }
    };
    overlay.querySelector(".cd-ok").onclick = () => close(true);
    overlay.querySelector(".cd-cancel").onclick = () => close(false);
    overlay.addEventListener("click", (e) => {
      if (e.target === overlay) close(false);
    });
    document.addEventListener("keydown", onKey, true);
    document.body.appendChild(overlay);
    requestAnimationFrame(() => overlay.classList.add("show"));
    overlay.querySelector(".cd-ok").focus();
  });
}

/** 按钮防双击：async 执行期间禁用按钮，结束恢复 */
async function withLoading(btn, fn) {
  if (!btn || !fn) return fn?.();
  const prev = btn.disabled;
  btn.disabled = true;
  btn.classList.add("btn-loading");
  try {
    return await fn();
  } finally {
    btn.disabled = prev;
    btn.classList.remove("btn-loading");
  }
}

/** 输入校验失败反馈：红框 2 秒 */
function flashInput(el) {
  if (!el) return;
  el.classList.add("input-error");
  setTimeout(() => el.classList.remove("input-error"), 2000);
}

function escapeHtml(s) {
  const d = document.createElement("div");
  d.textContent = s;
  return d.innerHTML;
}

/** 侧边栏切换（移动端） */
function toggleSidebar() {
  const nav = document.getElementById("sidebar-nav");
  const btn = document.getElementById("hamburger");
  const overlay = document.getElementById("sidebar-overlay");
  const isOpen = nav.classList.toggle("open");
  btn.classList.toggle("open");
  if (overlay) overlay.classList.toggle("show", isOpen);
  document.body.style.overflow = isOpen ? "hidden" : "";
}

function closeSidebar() {
  const nav = document.getElementById("sidebar-nav");
  const btn = document.getElementById("hamburger");
  const overlay = document.getElementById("sidebar-overlay");
  nav.classList.remove("open");
  btn.classList.remove("open");
  if (overlay) overlay.classList.remove("show");
  document.body.style.overflow = "";
}

/** Loading 指示器 */
function showLoading() {
  document.getElementById("loading-overlay")?.classList.add("show");
}
function hideLoading() {
  document.getElementById("loading-overlay")?.classList.remove("show");
}

/** 点击导航链接后自动关闭侧边栏（移动端） */
document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll("#sidebar-nav a").forEach(a => {
    a.addEventListener("click", () => {
      if (window.innerWidth <= 768) closeSidebar();
    });
  });
  compactActions();
});

/** 移动端：注入卡片列表标签 + 显式显示操作按钮 */
function compactActions() {
  if (window.innerWidth > 768) return;
  addTableLabels();
  // 确保操作按钮可见（不受之前可能设置的 display:none 影响）
  document.querySelectorAll('td:last-child .btn-sm').forEach(btn => {
    btn.style.display = '';
  });
}

/** 为卡片列表模式注入 data-label / data-role */
function addTableLabels() {
  document.querySelectorAll('tbody').forEach(tbody => {
    const table = tbody.closest('table');
    if (!table) return;
    const headers = [];
    table.querySelectorAll('thead th').forEach(th => {
      headers.push(th.textContent.trim().replace(/\s*\?\s*$/, ''));
    });
    if (headers.length === 0) return;
    tbody.querySelectorAll('tr').forEach(tr => {
      const tds = tr.querySelectorAll('td');
      tds.forEach((td, i) => {
        if (i >= headers.length) return;
        if (i === tds.length - 1) return;
        if (i === 0 && (td.textContent.includes('⠿') || td.textContent.includes('␿'))) {
          td.dataset.role = 'handle';
          return;
        }
        // 复选框格
        if (td.querySelector('input[type="checkbox"]')) {
          td.dataset.role = 'checkbox';
          return;
        }
        const label = headers[i];
        if (label) td.dataset.label = label;
      });
    });
  });
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

/** 用 SortableJS 初始化可拖拽表格（支持触屏） */
function enableSortable(tbodyId, onUpdate) {
  const el = document.getElementById(tbodyId);
  if (!el || typeof Sortable === "undefined") return;
  return new Sortable(el, {
    animation: 200,
    handle: "td:first-child, tr",
    ghostClass: "sortable-ghost",
    onEnd: function (evt) {
      if (typeof onUpdate === "function") {
        const items = [...el.children].map(tr => ({
          id: parseInt(tr.dataset.id),
          sort_order: Array.from(el.children).indexOf(tr),
        }));
        onUpdate(items);
      }
    },
  });
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
