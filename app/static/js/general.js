/** General 配置页：分组表单渲染与折叠持久化、Skip-Proxy 管理（由 general.html 内联 JS 迁出） */

let fieldMeta = [];
let currentLocale = document.documentElement.lang === "en" ? "en" : "zh";

function label(f) {
  return currentLocale === "en" ? (f.label_en || f.label_zh) : f.label_zh;
}

function hint(f) {
  return currentLocale === "en" ? (f.hint_en || f.hint_zh) : f.hint_zh;
}

function optLabel(opt) {
  return currentLocale === "en" ? (opt.label_en || opt.label_zh) : opt.label_zh;
}

function groupTitle(g) {
  return currentLocale === "en" ? (g.label_en || g.label_zh) : g.label_zh;
}

async function loadFields() {
  try {
    fieldMeta = await API.get("/api/general/fields");
    renderForm();
  } catch (e) { toast(e.message, "error"); }
}

/** 折叠状态持久化：key = general-collapse:<groupId>，值 "1" 表示折叠 */
function isGroupCollapsed(groupId) {
  try { return localStorage.getItem(`general-collapse:${groupId}`) === "1"; } catch { return false; }
}

function setGroupCollapsed(groupId, collapsed) {
  try {
    if (collapsed) localStorage.setItem(`general-collapse:${groupId}`, "1");
    else localStorage.removeItem(`general-collapse:${groupId}`);
  } catch { /* 隐私模式等场景忽略 */ }
}

function renderForm() {
  const container = document.getElementById("groups-container");
  container.innerHTML = fieldMeta.map(g => {
    const groupId = `group-${g.key}`;
    const collapsed = isGroupCollapsed(groupId);
    return `
    <div class="card gen-section">
      <div class="card-header" style="cursor:pointer;" onclick="toggleGroup('${groupId}')">
        <h3 class="gen-card-title">${groupTitle(g)}</h3>
        <span class="gen-toggle" id="${groupId}-arrow">${collapsed ? "▶" : "▼"}</span>
      </div>
      <div id="${groupId}-body" style="${collapsed ? "display:none;" : ""}">
        <div class="gen-grid">
          ${g.fields.map(f => renderField(f)).join("")}
        </div>
      </div>
    </div>`;
  }).join("");
}

function renderField(f) {
  const id = `field-${f.key}`;
  const hintText = hint(f) ? `<p class="gen-hint">${hint(f)}</p>` : "";

  if (f.type === "bool") {
    return `
    <div class="gen-field">
      <label class="gen-label" for="${id}">${label(f)}</label>
      <label class="toggle-wrap">
        <input type="checkbox" id="${id}" data-key="${f.key}" data-type="bool">
        <span class="toggle-slider"></span>
      </label>
      ${hintText}
    </div>`;
  }

  if (f.type === "select") {
    const opts = f.options.map(o =>
      `<option value="${o.value}">${optLabel(o)}</option>`
    ).join("");
    return `
    <div class="gen-field">
      <label class="gen-label" for="${id}">${label(f)}</label>
      <select id="${id}" data-key="${f.key}" data-type="select">${opts}</select>
      ${hintText}
    </div>`;
  }

  if (f.type === "tags") {
    return `
    <div class="gen-field gen-field-wide">
      <label class="gen-label" for="${id}">${label(f)}</label>
      <div class="tags-input-wrap">
        <textarea id="${id}" data-key="${f.key}" data-type="tags" rows="2"
          placeholder="${hint(f) || I18N["general.tags_placeholder"]}"></textarea>
      </div>
      ${hintText}
    </div>`;
  }

  if (f.type === "int") {
    return `
    <div class="gen-field">
      <label class="gen-label" for="${id}">${label(f)}</label>
      <input type="number" id="${id}" data-key="${f.key}" data-type="int">
      ${hintText}
    </div>`;
  }

  // text
  return `
  <div class="gen-field${f.key.includes("url") || f.key.includes("skip") ? " gen-field-wide" : ""}">
    <label class="gen-label" for="${id}">${label(f)}</label>
    <input type="text" id="${id}" data-key="${f.key}" data-type="text">
    ${hintText}
  </div>`;
}

function toggleGroup(groupId) {
  const body = document.getElementById(`${groupId}-body`);
  const arrow = document.getElementById(`${groupId}-arrow`);
  if (body.style.display === "none") {
    body.style.display = "";
    arrow.textContent = "▼";
    setGroupCollapsed(groupId, false);
  } else {
    body.style.display = "none";
    arrow.textContent = "▶";
    setGroupCollapsed(groupId, true);
  }
}

async function loadProfiles() {
  try {
    const profiles = await API.get("/api/configs/profiles");
    const select = document.getElementById("profile-select");
    select.innerHTML = "";
    profiles.forEach(p => {
      const opt = document.createElement("option");
      opt.value = p.id;
      opt.textContent = `${p.name}${p.is_default ? ` ${I18N["msg.default"]}` : ""}`;
      select.appendChild(opt);
    });
  } catch (e) { /* ignore */ }
}

async function loadValues() {
  const profileId = document.getElementById("profile-select").value;
  try {
    const data = await API.get(`/api/general/values?profile_id=${profileId}`);
    fillForm(data.values);
  } catch (e) { toast(e.message, "error"); }
}

function fillForm(values) {
  document.querySelectorAll("[data-key]").forEach(el => {
    const key = el.dataset.key;
    const type = el.dataset.type;
    const val = values[key];

    if (type === "bool") {
      el.checked = bool(val);
    } else if (type === "tags") {
      if (typeof val === "string" && val) {
        el.value = val.split(",").map(s => s.trim()).join("\n");
      } else {
        el.value = "";
      }
    } else if (type === "int") {
      el.value = val ?? "";
    } else if (type === "select") {
      el.value = val ?? "";
    } else {
      el.value = val ?? "";
    }
  });
}

function bool(v) {
  if (v === true || v === "true") return true;
  return false;
}

function collectValues() {
  const values = {};
  document.querySelectorAll("[data-key]").forEach(el => {
    const key = el.dataset.key;
    const type = el.dataset.type;

    if (type === "bool") {
      values[key] = el.checked;
    } else if (type === "tags") {
      const raw = el.value.trim();
      if (raw) {
        values[key] = raw.split(/[\n,]/).map(s => s.trim()).filter(Boolean).join(", ");
      }
    } else if (type === "int") {
      const n = parseInt(el.value);
      if (!isNaN(n)) values[key] = n;
    } else {
      values[key] = el.value;
    }
  });

  const jsonStr = document.getElementById("custom-json").value.trim();
  if (jsonStr) {
    try {
      const custom = JSON.parse(jsonStr);
      Object.assign(values, custom);
    } catch {
      toast(I18N["general.json_error"], "error");
      return null;
    }
  }
  return values;
}

async function saveAll(btn) {
  const profileId = parseInt(document.getElementById("profile-select").value);
  if (!profileId) { toast(I18N["general.select_profile"], "error"); return; }

  const values = collectValues();
  if (!values) return;

  await withLoading(btn, async () => {
    try {
      await API.put(`/api/general/values?profile_id=${profileId}`, values);
      toast(I18N["msg.saved"]);
    } catch (e) { toast(e.message, "error"); }
  });
}

// ===== 自定义 Skip-Proxy（全局） =====

let skipProxyItems = [];

function escapeHtmlSimple(s) {
  return String(s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}

async function loadSkipProxy() {
  try {
    const data = await API.get("/api/general/skip-proxy");
    skipProxyItems = data.custom || [];
    renderSkipProxy();
    const defaultsEl = document.getElementById("skip-proxy-defaults");
    defaultsEl.innerHTML = (data.defaults || []).map(d =>
      `<span class="badge">${escapeHtmlSimple(d)}</span>`
    ).join("");
  } catch (e) { toast(e.message, "error"); }
}

function renderSkipProxy() {
  const el = document.getElementById("skip-proxy-list");
  if (!skipProxyItems.length) {
    el.innerHTML = `<span style="font-size:12px;color:var(--text2);">${I18N["general.skip_proxy_empty"]}</span>`;
    return;
  }
  el.innerHTML = skipProxyItems.map((item, i) =>
    `<span class="badge badge-primary" style="display:inline-flex;align-items:center;gap:4px;">${escapeHtmlSimple(item)} <a style="cursor:pointer;margin-left:2px;color:var(--text2);" onclick="removeSkipProxyItem(${i})">✕</a></span>`
  ).join("");
}

async function addSkipProxyItem() {
  const input = document.getElementById("skip-proxy-input");
  const val = input.value.trim();
  if (!val) {
    toast(I18N["msg.required"], "error"); flashInput(input); return;
  }
  if (skipProxyItems.includes(val)) { toast(I18N["general.skip_proxy_dup"], "error"); return; }
  skipProxyItems.push(val);
  input.value = "";
  renderSkipProxy();
  await saveSkipProxy();
}

async function removeSkipProxyItem(i) {
  skipProxyItems.splice(i, 1);
  renderSkipProxy();
  await saveSkipProxy();
}

async function saveSkipProxy() {
  try {
    await API.put("/api/general/skip-proxy", { items: skipProxyItems });
    toast(I18N["msg.saved"]);
  } catch (e) { toast(e.message, "error"); }
}

async function init() {
  await loadProfiles();
  await loadFields();
  await loadValues();
  await loadSkipProxy();
}
init();
