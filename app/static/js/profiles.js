/** 配置方案页：方案 CRUD + 内嵌 Tab（概览/General/规则/Host/生成）
 *  平移自 profiles/general/generator 三页；General 表单与生成器按方案作用域化；弹窗交互共用 setupRuleModals */

let dnsPresets = [];
let profileRegions = [];
let profileServices = [];
// General 字段元数据不做持久缓存：目标（workstation/gateway/iOS）决定可配置字段
// 与 forced 集合，档案的 target 可被 PUT /api/configs/profiles/{id} 修改，档案也可
// 被删除——任何持久缓存都会在生命周期事件后过期。每次 render/open 与 save 前
// 都按当前 profile_id 重新 GET；仅同一调用窗口内 inflight 去重。
// generation（每档案单调递增）由档案 update/delete 推进：旧 generation 的响应
// 不得渲染、不得用于收集/提交（fail closed）。
const fieldMetaInflight = new Map();   // profileId → { gen, promise } 进行中的加载
const fieldMetaGen = new Map();        // profileId → 当前 generation（失效即 +1）
let serverIps = [];      // 生成 Tab 可选服务器地址（全局共享）
let allProfiles = [];    // 当前方案列表（loadData 后有效）

// 展开状态保持：profileId → activeTab，重渲染后恢复
const openSections = new Map();

const currentLocale = document.documentElement.lang === "en" ? "en" : "zh";
const SERVER_PORT = location.port || (location.protocol === "https:" ? "443" : "80");

// ===== 目标设备与 UDP 安全模式（规则与后端 profile_targets.py 一致）=====
// iOS 不允许 scoped：切换到 iOS 时选项裁剪并纠正当前值；默认模式随目标。
const ROLE_DEFAULT_TARGET = { work_computer: "mac_workstation", home_gateway: "mac_gateway" };
const UDP_MODES_BY_TARGET = {
  mac_workstation: ["strict", "scoped", "compatible"],
  mac_gateway: ["strict", "scoped", "compatible"],
  ios_mobile: ["strict", "compatible"],
};
const DEFAULT_UDP_MODE_BY_TARGET = { mac_workstation: "scoped", mac_gateway: "scoped", ios_mobile: "compatible" };
const TARGET_HINT_KEYS = {
  mac_workstation: "profile.target_hint_ws",
  mac_gateway: "profile.target_hint_gw",
  ios_mobile: "profile.target_hint_ios",
};
const TARGET_BADGE_KEYS = {
  mac_workstation: "profile.badge_target_ws",
  mac_gateway: "profile.badge_target_gw",
  ios_mobile: "profile.badge_target_ios",
};
const TARGET_BADGE_CLASS = { mac_workstation: "badge-primary", mac_gateway: "badge-success", ios_mobile: "" };
const MODE_LABEL_KEYS = { strict: "profile.mode_strict", scoped: "profile.mode_scoped", compatible: "profile.mode_compatible" };
const MODE_BADGE_KEYS = { strict: "profile.mode_short_strict", scoped: "profile.mode_short_scoped", compatible: "profile.mode_short_compatible" };

/** 按目标重建模式选项；preferred 在该目标下非法（iOS+scoped）时纠正为目标默认 */
function fillProfileModeOptions(target, preferred) {
  const sel = document.getElementById("profile-mode");
  if (!sel) return;
  const modes = UDP_MODES_BY_TARGET[target] || UDP_MODES_BY_TARGET.mac_workstation;
  sel.innerHTML = modes.map(m => `<option value="${m}">${I18N[MODE_LABEL_KEYS[m]]}</option>`).join("");
  sel.value = modes.includes(preferred) ? preferred : DEFAULT_UDP_MODE_BY_TARGET[target];
}

/** 目标切换联动：说明文案 + 模式选项裁剪（默认纠正为目标默认模式）。
 *  editProfile 显式传入档案现存模式以便回显；onchange 不带参 → 目标默认。 */
function onProfileTargetChange(preferredMode) {
  const target = document.getElementById("profile-target").value;
  const hint = document.getElementById("profile-target-hint");
  if (hint) hint.textContent = I18N[TARGET_HINT_KEYS[target]] || "";
  fillProfileModeOptions(target, preferredMode);
}

// ===== 基础数据 =====

async function loadProfileGroups() {
  try {
    const [regions, services] = await Promise.all([
      API.get("/api/groups/regions"),
      API.get("/api/groups/services"),
    ]);
    profileRegions = regions.filter(r => r.enabled);
    profileServices = services.filter(s => s.enabled);
  } catch (e) { /* ignore */ }
}

async function loadDnsPresets() {
  try {
    dnsPresets = await API.get("/api/configs/host-presets");
  } catch (e) { /* ignore */ }
}

/** 档案当前 metadata generation（未失效过 = 0） */
function generalMetaGen(profileId) {
  return fieldMetaGen.get(Number(profileId)) || 0;
}

/** 档案生命周期失效：update（含 target 变更）/删除成功后调用。
 *  推进 generation 并丢弃在途 inflight——仍在等待旧响应的 render/save
 *  在响应归来时按 generation 不匹配放弃，新调用也不会复用旧 inflight。 */
function invalidateGeneralMeta(profileId) {
  const pid = Number(profileId);
  fieldMetaGen.set(pid, generalMetaGen(pid) + 1);
  fieldMetaInflight.delete(pid);
}

/** 加载指定档案的 General 字段元数据（无持久缓存，每次都重新 GET）。
 *  必须携带 profile_id：不同目标的可配置字段与 forced 集合不同，
 *  禁止无参请求默认档案后跨档案共用。仅同一 generation 窗口内并发去重
 *  （共享同一 inflight promise）；完成后清槽位，下次调用重新请求。
 *  错误上抛，由调用方统一提示。 */
function loadGeneralFields(profileId) {
  const pid = Number(profileId);
  const gen = generalMetaGen(pid);
  const inflight = fieldMetaInflight.get(pid);
  if (inflight && inflight.gen === gen) return inflight.promise;
  const promise = API.get(`/api/general/fields?profile_id=${encodeURIComponent(pid)}`);
  fieldMetaInflight.set(pid, { gen, promise });
  promise.catch(() => {}).then(() => {
    // 完成即清：不留持久缓存；仅清自己的槽位（期间被失效/替换则不动）
    const cur = fieldMetaInflight.get(pid);
    if (cur && cur.promise === promise) fieldMetaInflight.delete(pid);
  });
  return promise;
}

async function loadServerIps() {
  try {
    const data = await API.get("/api/configs/server-ips");
    serverIps = data.ips || [];
  } catch (e) { serverIps = []; }
}

// ===== 方案卡片渲染 =====

const TABS = [
  { key: "overview", labelKey: "profile.tab_overview" },
  { key: "general", labelKey: "profile.tab_general" },
  { key: "rules", labelKey: "profile.tab_rules" },
  { key: "hosts", labelKey: "profile.tab_hosts" },
  { key: "generate", labelKey: "profile.tab_generate" },
];

function activeTabOf(profileId) {
  return openSections.get(profileId)?.tab
    || (localStorage.getItem(`profile-tab:${profileId}`) || "overview");
}

async function loadData() {
  try {
    const profiles = await API.get("/api/configs/profiles");
    allProfiles = profiles;
    const container = document.getElementById("profile-list");
    if (profiles.length === 0) {
      container.innerHTML = `<div class="empty"><div class="icon">🔧</div><p>${I18N["profile.empty"]}</p></div>`;
      return;
    }
    container.innerHTML = profiles.map(p => {
      const localeLabel = p.locale === 'en' ? 'English' : I18N["settings.lang_zh"];
      const activeTab = activeTabOf(p.id);
      // 目标 / UDP 模式 / Token 徽标：全部来自列表最小披露字段，无敏感值
      const targetBadge = p.target
        ? ` <span class="badge ${TARGET_BADGE_CLASS[p.target] || "badge-primary"}">${I18N[TARGET_BADGE_KEYS[p.target]] || escapeHtml(p.target)}</span>` : "";
      const modeBadge = p.udp_security_mode
        ? ` <span class="badge">${I18N[MODE_BADGE_KEYS[p.udp_security_mode]] || escapeHtml(p.udp_security_mode)}</span>` : "";
      const tokenBadge = ` <span class="badge ${p.token_configured ? "badge-success" : "badge-warning"}">${p.token_configured ? I18N["profile.token_configured"] : I18N["profile.token_not_configured"]}</span>`;
      return `
      <div class="card" style="margin-bottom:16px;">
        <div class="card-header">
          <div>
            <h3>${escapeHtml(p.name)} ${p.is_default ? `<span class="badge badge-warning">${I18N["profile.default"]}</span>` : ''} <span class="badge badge-primary">${localeLabel}</span>${targetBadge}${modeBadge}${tokenBadge}</h3>
            ${p.description ? `<p style="font-size:12px;color:var(--text2);margin-top:2px;">${escapeHtml(p.description)}</p>` : ''}
          </div>
          <div style="display:flex;gap:6px;flex-wrap:wrap;">
            <button class="btn btn-sm btn-outline" onclick='editProfile(${JSON.stringify(p)})'>${I18N["btn.edit"]}</button>
            <button class="btn btn-sm btn-success" onclick="setAsDefault(${p.id}, this)">${I18N["profile.set_default"]}</button>
            ${!p.is_default ? `<button class="btn btn-sm btn-danger" onclick="deleteProfile(${p.id}, this)">${I18N["btn.delete"]}</button>` : ''}
          </div>
        </div>
        <div class="tab-bar" id="tab-bar-${p.id}">
          ${TABS.map(t => `<button class="tab-btn${t.key === activeTab ? ' active' : ''}" data-tab="${t.key}"
            onclick="switchTab(${p.id}, '${t.key}')">${I18N[t.labelKey]}</button>`).join("")}
        </div>
        <div id="tab-content-${p.id}"></div>
      </div>`;
    }).join("");

    await Promise.all(profiles.map(p => loadProfileStats(p.id)));
    for (const p of profiles) {
      renderTabContent(p.id);
    }
  } catch (e) { toast(e.message, "error"); }
}

function switchTab(profileId, tab) {
  openSections.set(profileId, { tab });
  try { localStorage.setItem(`profile-tab:${profileId}`, tab); } catch { /* 隐私模式忽略 */ }
  document.querySelectorAll(`#tab-bar-${profileId} .tab-btn`).forEach(b => {
    b.classList.toggle("active", b.dataset.tab === tab);
  });
  renderTabContent(profileId);
}

function renderTabContent(profileId) {
  const tab = activeTabOf(profileId);
  const el = document.getElementById(`tab-content-${profileId}`);
  if (!el) return;
  if (tab === "general") renderGeneralTab(profileId);
  else if (tab === "rules") renderRulesTab(profileId);
  else if (tab === "hosts") renderHostsTab(profileId);
  else if (tab === "generate") renderGenerateTab(profileId);
  else renderOverviewTab(profileId);
}

// ===== 概览 Tab：统计 + MITM =====

function renderOverviewTab(profileId) {
  const el = document.getElementById(`tab-content-${profileId}`);
  el.innerHTML = `
    <div style="display:flex;gap:10px;flex-wrap:wrap;margin-bottom:16px;">
      <span class="badge badge-primary">${I18N["rule.pre_title"]} <span id="pre-count-${profileId}">0</span></span>
      <span class="badge badge-success">${I18N["rule.general_title"]} <span id="general-count-${profileId}">0</span></span>
      <span class="badge badge-warning">${I18N["profile.hosts"]} <span id="hosts-count-${profileId}">0</span></span>
    </div>
    <h3 style="font-size:14px;margin-bottom:8px;">${I18N["profile.mitm_config"]}</h3>
    <div style="display:flex;gap:16px;flex-wrap:wrap;margin-bottom:12px;">
      <label class="checkbox"><input type="checkbox" id="mitm-skip-${profileId}"> skip-server-cert-verify</label>
      <label class="checkbox"><input type="checkbox" id="mitm-tcp-${profileId}"> tcp-connection</label>
      <label class="checkbox"><input type="checkbox" id="mitm-h2-${profileId}"> h2</label>
    </div>
    <div class="form-group">
      <label>${I18N["profile.mitm_passphrase"]}</label>
      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;">
        <input type="password" id="mitm-passphrase-${profileId}" placeholder="${I18N["profile.mitm_passphrase_placeholder"]}" style="max-width:300px;">
        <button class="btn btn-sm btn-outline" onclick="clearMitmSecret(${profileId}, 'ca-passphrase')">${I18N["profile.mitm_clear"]}</button>
      </div>
    </div>
    <div class="form-group">
      <label>${I18N["profile.mitm_cap12"]}</label>
      <div style="font-size:12px;color:var(--text2);margin-bottom:4px;">${I18N["profile.mitm_cap12_manual"]}</div>
      <input type="text" id="mitm-cap12-${profileId}" placeholder="${I18N["profile.mitm_cap12_placeholder"]}" style="width:100%;max-width:500px;margin-bottom:8px;">
      <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
        <span style="font-size:12px;color:var(--text2);">${I18N["profile.mitm_cap12_upload"]}</span>
        <label class="btn btn-sm btn-outline" style="cursor:pointer;margin:0;">
          ${I18N["btn.upload"]}
          <input type="file" accept=".p12" style="display:none;" onchange="uploadMitmCert(${profileId}, this)">
        </label>
        <button class="btn btn-sm btn-outline" onclick="clearMitmSecret(${profileId}, 'ca-p12')">${I18N["profile.mitm_clear"]}</button>
      </div>
    </div>
    <div class="form-group">
      <label>${I18N["profile.mitm_hostname"]}</label>
      <textarea id="mitm-hostname-${profileId}" rows="3" placeholder="${I18N["profile.mitm_hostname_placeholder"]}"></textarea>
    </div>
    <div style="display:flex;gap:8px;margin-top:8px;">
      <button class="btn btn-sm btn-primary" onclick="saveMitm(${profileId})">${I18N["profile.mitm_save"]}</button>
      <button class="btn btn-sm btn-outline" onclick="resetMitm(${profileId})">${I18N["profile.mitm_reset"]}</button>
    </div>`;
  loadProfileStats(profileId);
  loadMitm(profileId);
}

// ===== General Tab（general.js 平移，按方案作用域化） =====

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

async function renderGeneralTab(profileId) {
  const el = document.getElementById(`tab-content-${profileId}`);
  el.innerHTML = `
    <div style="display:flex;justify-content:flex-end;margin-bottom:8px;">
      <button class="btn btn-primary btn-sm" onclick="saveGeneralValues(${profileId}, this)">${I18N["btn.save"]}</button>
    </div>
    <div id="groups-container-${profileId}"></div>
    <details style="margin-top:16px;">
      <summary style="cursor:pointer;font-size:13px;color:var(--primary);">${I18N["general.json_override"]}</summary>
      <div class="card" style="margin-top:8px;">
        <div class="form-group" style="margin-bottom:0;">
          <textarea id="custom-json-${profileId}" rows="4" placeholder='{"key": "value"}'></textarea>
          <p style="font-size:11px;color:var(--text2);margin-top:4px;">${I18N["general.json_hint"]}</p>
        </div>
      </div>
    </details>`;
  // 先取当前档案的字段元数据（目标决定可配置字段与 forced 集合）再渲染/填值；
  // 加载失败不渲染表单——没有 metadata 就无法判定 forced 键，必须 fail closed。
  // 等待期间档案被更新/删除（generation 推进）则放弃本次渲染：
  // 旧响应可能来自旧 target，渲染它等于向用户展示过期字段集合
  const gen = generalMetaGen(profileId);
  let meta;
  try {
    meta = await loadGeneralFields(profileId);
  } catch (e) {
    toast(e.message, "error");
    return;
  }
  if (generalMetaGen(profileId) !== gen) return;
  renderGeneralForm(profileId, meta);
  await loadGeneralValues(profileId);
}

function renderGeneralForm(profileId, meta) {
  const container = document.getElementById(`groups-container-${profileId}`);
  if (!container) return;  // 异步返回时 Tab 已被切换/卸载，放弃本次渲染
  container.innerHTML = meta.map(g => {
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
          ${g.fields.map(f => renderField(f, profileId)).join("")}
        </div>
      </div>
    </div>`;
  }).join("");
}

/** 强制安全字段（服务端 metadata forced === true）：控件禁用 + 锁定徽标 */
function forcedAttrs(f) {
  return f.forced === true ? ' disabled data-forced="1"' : "";
}

function forcedBadge(f) {
  return f.forced === true
    ? ` <span class="badge badge-warning">${I18N["general.forced_badge"]}</span>`
    : "";
}

function renderField(f, profileId) {
  const id = `field-${profileId}-${f.key}`;
  const hintText = hint(f) ? `<p class="gen-hint">${hint(f)}</p>` : "";

  if (f.type === "bool") {
    const inlineHint = hint(f) ? ` <span class="gen-label-hint">${hint(f)}</span>` : "";
    return `
    <div class="gen-field gen-field-bool">
      <label class="gen-label" for="${id}">${label(f)}${inlineHint}${forcedBadge(f)}</label>
      <label class="toggle-wrap" title="${hint(f) || ""}">
        <input type="checkbox" id="${id}" data-key="${f.key}" data-type="bool"${forcedAttrs(f)}>
        <span class="toggle-slider"></span>
      </label>
    </div>`;
  }

  if (f.type === "select") {
    const opts = f.options.map(o =>
      `<option value="${o.value}">${optLabel(o)}</option>`
    ).join("");
    return `
    <div class="gen-field">
      <label class="gen-label" for="${id}">${label(f)}${forcedBadge(f)}</label>
      <select id="${id}" data-key="${f.key}" data-type="select"${forcedAttrs(f)}>${opts}</select>
      ${hintText}
    </div>`;
  }

  if (f.type === "tags") {
    return `
    <div class="gen-field gen-field-wide">
      <label class="gen-label" for="${id}">${label(f)}${forcedBadge(f)}</label>
      <div class="tags-input-wrap">
        <textarea id="${id}" data-key="${f.key}" data-type="tags" rows="2"${forcedAttrs(f)}
          placeholder="${hint(f) || I18N["general.tags_placeholder"]}"></textarea>
      </div>
      ${hintText}
    </div>`;
  }

  if (f.type === "int") {
    return `
    <div class="gen-field">
      <label class="gen-label" for="${id}">${label(f)}${forcedBadge(f)}</label>
      <input type="number" id="${id}" data-key="${f.key}" data-type="int"${forcedAttrs(f)}>
      ${hintText}
    </div>`;
  }

  // text
  return `
  <div class="gen-field${f.key.includes("url") || f.key.includes("skip") ? " gen-field-wide" : ""}">
    <label class="gen-label" for="${id}">${label(f)}${forcedBadge(f)}</label>
    <input type="text" id="${id}" data-key="${f.key}" data-type="text"${forcedAttrs(f)}>
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

async function loadGeneralValues(profileId) {
  try {
    const data = await API.get(`/api/general/values?profile_id=${encodeURIComponent(profileId)}`);
    fillGeneralForm(profileId, data.values);
  } catch (e) { toast(e.message, "error"); }
}

function fillGeneralForm(profileId, values) {
  document.querySelectorAll(`#tab-content-${profileId} [data-key]`).forEach(el => {
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

function collectGeneralValues(profileId, meta) {
  const values = {};
  // 服务端强制安全字段不收集：后端保存时无条件写入强制值；
  // 依据调用方刚按当前档案拉取的 metadata（显式传入）而非 DOM 属性判定，
  // DOM 篡改也不会提交；metadata 缺失说明 owner 不确定，fail closed 拒绝收集
  if (!Array.isArray(meta)) {
    toast(I18N["general.meta_missing"], "error");
    return null;
  }
  const forcedKeys = new Set(meta.flatMap(g =>
    g.fields.filter(f => f.forced === true).map(f => f.key)));
  document.querySelectorAll(`#tab-content-${profileId} [data-key]`).forEach(el => {
    const key = el.dataset.key;
    if (forcedKeys.has(key)) return;
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

  const jsonStr = document.getElementById(`custom-json-${profileId}`).value.trim();
  if (jsonStr) {
    try {
      const custom = JSON.parse(jsonStr);
      Object.assign(values, custom);
    } catch {
      toast(I18N["general.json_error"], "error");
      return null;
    }
  }
  // custom JSON 重新注入的 forced 键同样移除：以可信 metadata 为准；
  // 普通键与表外未知键保留在 payload，交由后端校验（未知/危险键 422）
  for (const key of forcedKeys) delete values[key];
  return values;
}

async function saveGeneralValues(profileId, btn) {
  await withLoading(btn, async () => {
    try {
      // 保存前重新拉取当前档案 metadata（无持久缓存，必发 fresh GET）；
      // 拉取失败或等待期间档案被更新/删除（generation 变化）则中止提交，
      // 不用旧 target 的 forced 集合做判定。后端并发变更导致的 422
      // 走 catch 统一报错：fail closed，不重试不静默提交
      const gen = generalMetaGen(profileId);
      const meta = await loadGeneralFields(profileId);
      if (generalMetaGen(profileId) !== gen) {
        toast(I18N["general.meta_stale"], "error");
        return;
      }
      const values = collectGeneralValues(profileId, meta);
      if (!values) return;
      await API.put(`/api/general/values?profile_id=${encodeURIComponent(profileId)}`, values);
      toast(I18N["msg.saved"]);
    } catch (e) { toast(e.message, "error"); }
  });
}

// ===== 规则 Tab：前置 + 通用两段 =====

function renderRulesTab(profileId) {
  const el = document.getElementById(`tab-content-${profileId}`);
  el.innerHTML = `
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
      <span style="font-size:12px;color:var(--text2);">${I18N["rule.pre_hint"]}</span>
      <button class="btn btn-sm btn-primary" onclick="showAddCustomRule('pre', ${profileId})">${I18N["rule.add_pre"]}</button>
    </div>
    <div class="table-wrap" style="margin-bottom:16px;">
      <table>
        <thead><tr>
          <th>${I18N["rule.rule_text"]}</th>
          <th>${I18N["rule.comment"]}</th>
          <th>${I18N["sub.status"]}</th>
          <th>${I18N["node.col_action"]}</th>
        </tr></thead>
        <tbody id="pre-list-${profileId}"></tbody>
      </table>
    </div>
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
      <span style="font-size:12px;color:var(--text2);">${I18N["rule.general_hint"]}</span>
      <button class="btn btn-sm btn-primary" onclick="showAddCustomRule('general', ${profileId})">${I18N["rule.add_general"]}</button>
    </div>
    <div class="table-wrap">
      <table>
        <thead><tr>
          <th>${I18N["rule.rule_text"]}</th>
          <th>${I18N["rule.comment"]}</th>
          <th>${I18N["sub.status"]}</th>
          <th>${I18N["node.col_action"]}</th>
        </tr></thead>
        <tbody id="general-list-${profileId}"></tbody>
      </table>
    </div>`;
  loadProfileRuleList(profileId, "pre");
  loadProfileRuleList(profileId, "general");
}

// ===== Host Tab =====

function renderHostsTab(profileId) {
  const el = document.getElementById(`tab-content-${profileId}`);
  el.innerHTML = `
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
      <span style="font-size:12px;color:var(--text2);">${I18N["profile.hosts_hint"]}</span>
      <button class="btn btn-sm btn-primary" onclick="showAddHost(${profileId})">${I18N["profile.add_host"]}</button>
    </div>
    <div class="table-wrap">
      <table>
        <thead><tr>
          <th>${I18N["profile.host_domain"]}</th>
          <th>${I18N["profile.host_target"]}</th>
          <th>${I18N["profile.host_skip_proxy_col"]}</th>
          <th>${I18N["sub.status"]}</th>
          <th>${I18N["node.col_action"]}</th>
        </tr></thead>
        <tbody id="hosts-list-${profileId}"></tbody>
      </table>
    </div>`;
  loadHostList(profileId);
}

// ===== 生成 Tab（generator.js 平移，profile 固定为当前方案） =====

function renderGenerateTab(profileId) {
  const el = document.getElementById(`tab-content-${profileId}`);
  const ipOptions = [`<option value="127.0.0.1">${I18N["generator.localhost"]}</option>`]
    .concat(serverIps.map(ip => `<option value="${ip}">${I18N["generator.lan_ip"].replace("{0}", ip)}</option>`))
    .join("");
  el.innerHTML = `
    <div class="card gen-section">
      <h3 class="gen-card-title">${I18N["generator.download"]}</h3>
      <div class="gen-row">
        <input type="text" id="filename-input-${profileId}" placeholder="Surge.conf" value="Surge.conf" class="gen-filename-input">
        <button class="btn btn-success" onclick="downloadConfig(${profileId})">${I18N["generator.btn_download"]}</button>
      </div>
      <p class="gen-hint">${I18N["generator.download_hint"]}</p>
    </div>
    <div class="card gen-section">
      <h3 class="gen-card-title">${I18N["generator.subscribe"]}</h3>
      <div class="form-group">
        <label>${I18N["generator.server_ip"]}</label>
        <select id="ip-select-${profileId}" class="gen-ip-select">${ipOptions}</select>
      </div>
      <div class="form-group">
        <label>${I18N["profile.token_title"]}</label>
        <div class="gen-row" style="flex-wrap:wrap;">
          <span id="token-status-${profileId}" class="badge"></span>
          <button class="btn btn-outline" id="token-btn-${profileId}" onclick="generateProfileToken(${profileId}, this)"></button>
        </div>
      </div>
      <p class="gen-hint">${I18N["generator.subscribe_hint"]}</p>
    </div>
    <div class="card gen-section">
      <div class="gen-row" style="justify-content:flex-start;">
        <button class="btn btn-primary" onclick="previewConfig(${profileId})">${I18N["generator.btn_preview"]}</button>
        <span style="font-size:12px;color:var(--text2);margin-left:8px;" id="config-size-${profileId}"></span>
      </div>
    </div>`;
  const ipSel = document.getElementById(`ip-select-${profileId}`);
  if (serverIps.length > 0) ipSel.value = serverIps[0];
  renderTokenStatus(profileId);
}

// ===== 订阅 Token：明文仅存在于一次性对话框，关闭即清空 =====

function formatSubscribeHost(ip) {
  // IPv6 字面量须以方括号包裹（host 位置冒号不允许百分号编码）；
  // 普通 hostname/IP 走 encodeURIComponent（字母数字.-_ 不变，其余安全编码）
  const raw = String(ip);
  return raw.includes(":") && !raw.startsWith("[") ? `[${raw}]` : encodeURIComponent(raw);
}

function buildSubscribeUrl(profileId, token) {
  // 纯函数：协议/端口来自当前页面，地址取当前下拉选择；token 逐参数安全编码
  const ip = document.getElementById(`ip-select-${profileId}`)?.value || "127.0.0.1";
  // 端口省略必须结合协议：仅协议默认端口可省；交叉组合（http:443 / https:80）必须保留
  const isDefaultPort = (location.protocol === "http:" && SERVER_PORT === "80")
    || (location.protocol === "https:" && SERVER_PORT === "443");
  const port = isDefaultPort ? "" : `:${SERVER_PORT}`;
  return `${location.protocol}//${formatSubscribeHost(ip)}${port}`
    + `/api/configs/profiles/${Number(profileId)}/subscribe?token=${encodeURIComponent(token)}`;
}

function renderTokenStatus(profileId) {
  const profile = allProfiles.find(p => p.id === profileId);
  const configured = !!profile?.token_configured;
  const badge = document.getElementById(`token-status-${profileId}`);
  if (badge) {
    badge.className = `badge ${configured ? "badge-success" : "badge-warning"}`;
    badge.textContent = configured ? I18N["profile.token_configured"] : I18N["profile.token_not_configured"];
  }
  const btn = document.getElementById(`token-btn-${profileId}`);
  if (btn) btn.textContent = configured ? I18N["profile.token_rotate"] : I18N["profile.token_generate"];
}

async function generateProfileToken(profileId, btn) {
  const profile = allProfiles.find(p => p.id === profileId);
  if (profile?.token_configured &&
      !(await confirmDialog(I18N["profile.token_rotate_confirm"], { danger: true }))) return;
  await withLoading(btn, async () => {
    try {
      const resp = await API.post(`/api/configs/profiles/${profileId}/token`);
      if (profile) profile.token_configured = true;
      renderTokenStatus(profileId);
      showTokenDialog(profileId, resp.token);
    } catch (e) { toast(e.message, "error"); }
  });
}

function showTokenDialog(profileId, token) {
  const dlg = document.getElementById("token-modal");
  const input = document.getElementById("token-url-input");
  input.value = buildSubscribeUrl(profileId, token);
  // 对话框以任何方式（按钮/ESC/遮罩）离开 show 状态都立即丢弃明文
  const observer = new MutationObserver(() => {
    if (!dlg.classList.contains("show")) {
      input.value = "";
      observer.disconnect();
    }
  });
  observer.observe(dlg, { attributes: true, attributeFilter: ["class"] });
  showModal("token-modal");
  input.focus();
  input.select();
  input.setSelectionRange(0, 99999);
}

function closeTokenDialog() {
  hideModal("token-modal"); // MutationObserver 负责清空明文
}

function copyTokenUrl() {
  const input = document.getElementById("token-url-input");
  if (!input.value) { toast(I18N["profile.token_copy_first"], "error"); return; }
  // 先尝试同步 execCommand（HTTP 局域网环境可用，需在用户手势内执行）
  try {
    if (document.execCommand("copy")) {
      toast(I18N["generator.copied"]);
      return;
    }
  } catch (e) {}
  // 再尝试异步 Clipboard API（HTTPS 环境）；两者都失败时诚实报错并保持选中态供手动 Cmd+C
  navigator.clipboard.writeText(input.value).then(() => {
    toast(I18N["generator.copied"]);
  }).catch(() => {
    input.select();
    input.setSelectionRange(0, 99999);
    toast(I18N["generator.copy_failed"], "error");
  });
}

async function previewConfig(profileId) {
  try {
    showLoading();
    const result = await API.get(`/api/configs/preview?profile_id=${profileId}`);
    const sizeLabel = `${(result.size / 1024).toFixed(1)} KB`;
    document.getElementById(`config-size-${profileId}`).textContent = sizeLabel;
    document.getElementById("fullscreen-content").textContent = result.content;
    document.getElementById("fullscreen-size").textContent = sizeLabel;
    document.getElementById("fullscreen-overlay").classList.add("show");
    document.body.style.overflow = "hidden";
  } catch (e) {
    toast(e.message, "error");
  } finally {
    hideLoading();
  }
}

function downloadConfig(profileId) {
  const filename = document.getElementById(`filename-input-${profileId}`).value.trim() || "Surge.conf";
  window.open(`/api/configs/generate?filename=${encodeURIComponent(filename)}&profile_id=${profileId}`, "_blank");
}


function toggleFullscreen() {
  const overlay = document.getElementById("fullscreen-overlay");
  if (overlay.classList.contains("show")) {
    overlay.classList.remove("show");
    document.body.style.overflow = "";
  }
}

function closeFullscreen(e) {
  if (e.target === document.getElementById("fullscreen-overlay")) {
    toggleFullscreen();
  }
}

document.addEventListener("keydown", e => {
  if (e.key === "Escape") toggleFullscreen();
});

// ===== 统计 =====

async function loadProfileStats(profileId) {
  try {
    const [rules, hosts] = await Promise.all([
      API.get(`/api/configs/custom-rules?profile_id=${profileId}`),
      API.get(`/api/configs/host-mappings?profile_id=${profileId}`),
    ]);
    const preRules = rules.filter(r => r.position === "pre");
    const generalRules = rules.filter(r => r.position === "general");
    const el = (id) => document.getElementById(id);
    if (el(`pre-count-${profileId}`)) el(`pre-count-${profileId}`).textContent = preRules.length;
    if (el(`general-count-${profileId}`)) el(`general-count-${profileId}`).textContent = generalRules.length;
    if (el(`hosts-count-${profileId}`)) el(`hosts-count-${profileId}`).textContent = hosts.length;
  } catch (e) { /* ignore */ }
}

// ===== Profile 基础 CRUD =====

async function showAddProfile() {
  document.getElementById("profile-modal-title").textContent = I18N["profile.new"];
  document.getElementById("profile-edit-id").value = "";
  document.getElementById("profile-name").value = "";
  document.getElementById("profile-desc").value = "";
  document.getElementById("profile-locale").value = document.documentElement.lang === "en" ? "en" : "zh";
  document.getElementById("profile-default").checked = false;
  // 新建默认目标 = 实例角色映射（与后端省略 target 时同一规则）；会话读取
  // 失败按默认角色 fail-closed 回落 mac_gateway
  let role = "home_gateway";
  try {
    const s = await API.get("/api/auth/session");
    if (s && s.authenticated && ROLE_DEFAULT_TARGET[s.installation_role]) role = s.installation_role;
  } catch (e) { /* 会话不可得：用默认角色 */ }
  document.getElementById("profile-target").value = ROLE_DEFAULT_TARGET[role];
  onProfileTargetChange();
  showModal("profile-modal");
}

function editProfile(p) {
  document.getElementById("profile-modal-title").textContent = I18N["btn.edit"];
  document.getElementById("profile-edit-id").value = p.id;
  document.getElementById("profile-name").value = p.name;
  document.getElementById("profile-desc").value = p.description || "";
  document.getElementById("profile-locale").value = p.locale || "zh";
  document.getElementById("profile-default").checked = p.is_default;
  // 回显目标与模式：模式选项先按目标裁剪，再取档案现存值（目标下非法时纠正）
  document.getElementById("profile-target").value = p.target || "mac_workstation";
  onProfileTargetChange(p.udp_security_mode);
  showModal("profile-modal");
}

async function saveProfile() {
  const id = document.getElementById("profile-edit-id").value;
  const nameEl = document.getElementById("profile-name");
  if (!nameEl.value.trim()) {
    toast(I18N["msg.required"], "error"); flashInput(nameEl); return;
  }
  const data = {
    name: nameEl.value.trim(),
    description: document.getElementById("profile-desc").value || null,
    is_default: document.getElementById("profile-default").checked,
    locale: document.getElementById("profile-locale").value,
    target: document.getElementById("profile-target").value,
    udp_security_mode: document.getElementById("profile-mode").value,
  };
  try {
    if (id) {
      await API.put(`/api/configs/profiles/${id}`, data);
      // 档案已更新（PUT 可改 target 等字段）：General metadata 可能随 target
      // 变化，立即失效——在途响应按旧 generation 丢弃，下次打开/保存重拉；
      // loadData 重渲染会按新 target 重新 GET /api/general/fields?profile_id=
      invalidateGeneralMeta(id);
      toast(I18N["msg.updated"]);
    } else {
      await API.post("/api/configs/profiles", data);
      toast(I18N["msg.added"]);
    }
    hideModal("profile-modal"); loadData();
  } catch (e) { toast(e.message, "error"); }
}

async function setAsDefault(id, btn) {
  await withLoading(btn, async () => {
    try {
      await API.put(`/api/configs/profiles/${id}/set-default`);
      toast(I18N["profile.set_default_done"]); loadData();
    } catch (e) { toast(e.message, "error"); }
  });
}

function generateForProfile(id) { window.open(`/api/configs/generate?profile_id=${id}`, "_blank"); }

async function deleteProfile(id, btn) {
  if (!(await confirmDialog(I18N["profile.confirm_delete"], { danger: true }))) return;
  await withLoading(btn, async () => {
    try {
      // 后端单事务级联删除：定制规则 + Host 映射 + 档案本体
      await API.del(`/api/configs/profiles/${id}`);
      openSections.delete(id);
      // 档案已删除：失效该档案 metadata/generation，在途响应不得回填或供使用
      invalidateGeneralMeta(id);
      toast(I18N["msg.deleted"]); loadData();
    } catch (e) { toast(e.message, "error"); }
  });
}

// ===== Profile 自定义规则列表 =====

async function loadProfileRuleList(profileId, position) {
  try {
    const rules = await API.get(`/api/configs/custom-rules?profile_id=${profileId}&position=${position}`);
    const tbody = document.getElementById(`${position}-list-${profileId}`);
    if (!tbody) return;
    tbody.innerHTML = rules.map(r => `
      <tr>
        <td><code style="font-size:12px;">${escapeHtml(r.rule_text)}</code></td>
        <td style="font-size:12px;color:var(--text2);">${escapeHtml(r.comment || '-')}</td>
        <td><span class="badge ${r.enabled ? 'badge-success' : 'badge-danger'}">${r.enabled ? I18N["btn.enable"] : I18N["btn.disable"]}</span></td>
        <td>
          <button class="btn btn-sm btn-outline" onclick='editCustomRule(${JSON.stringify(r)}, ${profileId})'>${I18N["btn.edit"]}</button>
          <button class="btn btn-sm ${r.enabled ? 'btn-outline' : 'btn-success'}" onclick="toggleProfileRule(${r.id}, ${!r.enabled}, ${profileId}, '${position}')">${r.enabled ? I18N["btn.disable"] : I18N["btn.enable"]}</button>
          <button class="btn btn-sm btn-danger" onclick="deleteCustomRule(${r.id}, ${profileId}, '${position}')">${I18N["btn.delete"]}</button>
        </td>
      </tr>
    `).join("");
    compactActions();
  } catch (e) { toast(e.message, "error"); }
}

async function toggleProfileRule(ruleId, enabled, profileId, position) {
  try {
    await API.put(`/api/configs/custom-rules/${ruleId}`, { enabled });
    loadProfileRuleList(profileId, position);
  } catch (e) { toast(e.message, "error"); }
}

// ===== Profile Host 映射列表 =====

async function loadHostList(profileId) {
  try {
    const mappings = await API.get(`/api/configs/host-mappings?profile_id=${profileId}`);
    const tbody = document.getElementById(`hosts-list-${profileId}`);
    if (!tbody) return;
    tbody.innerHTML = mappings.map(m => `
      <tr>
        <td><code style="font-size:12px;">${escapeHtml(m.domain)}</code></td>
        <td><span class="badge badge-primary">${escapeHtml(m.target)}</span></td>
        <td><span class="badge ${m.skip_proxy ? 'badge-success' : ''}">${m.skip_proxy ? '✓' : '—'}</span></td>
        <td><span class="badge ${m.enabled ? 'badge-success' : 'badge-danger'}">${m.enabled ? I18N["btn.enable"] : I18N["btn.disable"]}</span></td>
        <td>
          <button class="btn btn-sm btn-outline" onclick='editHost(${JSON.stringify(m)}, ${profileId})'>${I18N["btn.edit"]}</button>
          <button class="btn btn-sm ${m.enabled ? 'btn-outline' : 'btn-success'}" onclick="toggleHost(${m.id}, ${!m.enabled}, ${profileId})">${m.enabled ? I18N["btn.disable"] : I18N["btn.enable"]}</button>
          <button class="btn btn-sm btn-danger" onclick="deleteHost(${m.id}, ${profileId})">${I18N["btn.delete"]}</button>
        </td>
      </tr>
    `).join("");
    compactActions();
  } catch (e) { toast(e.message, "error"); }
}

async function toggleHost(mappingId, enabled, profileId) {
  try {
    await API.put(`/api/configs/host-mappings/${mappingId}`, { enabled });
    loadHostList(profileId);
  } catch (e) { toast(e.message, "error"); }
}

// ===== MITM 配置管理 =====

async function loadMitm(profileId) {
  // 秘密字段详情端点只返回 configured 状态：已配置时输入框留空、占位提示保留语义
  try {
    const detail = await API.get(`/api/configs/profiles/${profileId}`);
    const mitm = detail.mitm || {};
    document.getElementById(`mitm-skip-${profileId}`).checked = mitm['skip-server-cert-verify'] !== false;
    document.getElementById(`mitm-tcp-${profileId}`).checked = mitm['tcp-connection'] !== false;
    document.getElementById(`mitm-h2-${profileId}`).checked = mitm['h2'] !== false;
    const passEl = document.getElementById(`mitm-passphrase-${profileId}`);
    passEl.value = "";
    passEl.placeholder = mitm['ca-passphrase']?.configured
      ? I18N["profile.mitm_secret_configured"]
      : I18N["profile.mitm_passphrase_placeholder"];
    const p12El = document.getElementById(`mitm-cap12-${profileId}`);
    p12El.value = "";
    p12El.placeholder = mitm['ca-p12']?.configured
      ? I18N["profile.mitm_secret_configured"]
      : I18N["profile.mitm_cap12_placeholder"];
    document.getElementById(`mitm-hostname-${profileId}`).value = mitm['hostname'] || '';
  } catch (e) { toast(e.message, "error"); }
}

async function saveMitm(profileId) {
  const mitm = {};
  mitm['skip-server-cert-verify'] = document.getElementById(`mitm-skip-${profileId}`).checked;
  mitm['tcp-connection'] = document.getElementById(`mitm-tcp-${profileId}`).checked;
  mitm['h2'] = document.getElementById(`mitm-h2-${profileId}`).checked;
  // 秘密字段留空 = 不发送该键（后端覆盖式语义：空输入保留现值）
  const passphrase = document.getElementById(`mitm-passphrase-${profileId}`).value.trim();
  if (passphrase) mitm['ca-passphrase'] = passphrase;
  const cap12 = document.getElementById(`mitm-cap12-${profileId}`).value.trim();
  if (cap12) mitm['ca-p12'] = cap12;
  const hostname = document.getElementById(`mitm-hostname-${profileId}`).value.trim();
  if (hostname) mitm['hostname'] = hostname;
  try {
    await API.put(`/api/configs/profiles/${profileId}`, { mitm });
    // 档案 PUT 成功：General metadata generation 失效（下次打开/保存重拉）
    invalidateGeneralMeta(profileId);
    toast(I18N["profile.mitm_save"]);
    loadMitm(profileId);
  } catch (e) { toast(e.message, "error"); }
}

async function resetMitm(profileId) {
  if (!(await confirmDialog(`${I18N["profile.mitm_reset"]}?`, { danger: true }))) return;
  try {
    // 显式整体重置（独立动作）：清空全部 MITM 覆盖，回到生成器默认值
    await API.put(`/api/configs/profiles/${profileId}`, { mitm_reset: true });
    // 档案 PUT 成功：General metadata generation 失效（下次打开/保存重拉）
    invalidateGeneralMeta(profileId);
    toast(I18N["profile.mitm_reset"]);
    loadMitm(profileId);
  } catch (e) { toast(e.message, "error"); }
}

async function clearMitmSecret(profileId, key) {
  if (!(await confirmDialog(I18N["profile.mitm_clear_confirm"].replace("{0}", key), { danger: true }))) return;
  try {
    // 显式清除指定秘密键（独立字段），与保存互不影响
    await API.put(`/api/configs/profiles/${profileId}`, { mitm_clear: [key] });
    // 档案 PUT 成功：General metadata generation 失效（下次打开/保存重拉）
    invalidateGeneralMeta(profileId);
    toast(I18N["profile.mitm_cleared"].replace("{0}", key));
    loadMitm(profileId);
  } catch (e) { toast(e.message, "error"); }
}

async function uploadMitmCert(profileId, input) {
  const file = input.files[0];
  if (!file) return;
  try {
    showLoading();
    const base64 = await new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result.split(',')[1]);
      reader.onerror = reject;
      reader.readAsDataURL(file);
    });
    document.getElementById(`mitm-cap12-${profileId}`).value = base64;
    toast(I18N["profile.mitm_cert_uploaded"]);
  } catch (e) { toast(e.message, "error"); }
  finally {
    hideLoading();
    input.value = "";
  }
}

// ===== 启动 =====

setupRuleModals({
  getServices: () => profileServices,
  getRegions: () => profileRegions,
  getDnsPresets: () => dnsPresets,
  onRuleSaved: (profileId, position) => {
    if (profileId) {
      loadProfileRuleList(profileId, position);
      loadProfileStats(profileId);
    }
  },
  onHostSaved: (profileId) => {
    if (profileId) {
      loadHostList(profileId);
      loadProfileStats(profileId);
    }
  },
});

async function init() {
  // General 字段元数据不再首屏无参加载：打开各档案 General Tab 时
  // 由 renderGeneralTab 按 profile_id 拉取（不同目标字段/forced 集合不同）
  await Promise.all([loadDnsPresets(), loadProfileGroups(), loadServerIps()]);
  loadData();
}
init();
