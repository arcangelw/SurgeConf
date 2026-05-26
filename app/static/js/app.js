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
  const el = document.getElementById(id);
  if (!el) return;
  el.classList.add("show");
  // 点击遮罩关闭
  el.onclick = function(e) {
    if (e.target === el) hideModal(id);
  };
}

function hideModal(id) {
  document.getElementById(id)?.classList.remove("show");
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
