/** 配置方案页：档案 CRUD、分组折叠、规则/Host 列表、MITM（由 profiles.html 内联 JS 迁出；弹窗交互共用 setupRuleModals） */

let dnsPresets = [];
let profileRegions = [];
let profileServices = [];

// 展开状态保持：profileId → Set(sectionKey)，重渲染后恢复
const openSections = new Map();

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

async function loadData() {
  try {
    const profiles = await API.get("/api/configs/profiles");
    const container = document.getElementById("profile-list");
    if (profiles.length === 0) {
      container.innerHTML = `<div class="empty"><div class="icon">🔧</div><p>${I18N["profile.empty"]}</p></div>`;
      return;
    }
    container.innerHTML = profiles.map(p => {
      const localeLabel = p.locale === 'en' ? 'English' : I18N["settings.lang_zh"];
      const open = (key) => openSections.get(p.id)?.has(key) ? "block" : "none";
      return `
      <div class="card" style="margin-bottom:16px;">
        <div class="card-header">
          <div>
            <h3>${escapeHtml(p.name)} ${p.is_default ? `<span class="badge badge-warning">${I18N["profile.default"]}</span>` : ''} <span class="badge badge-primary">${localeLabel}</span></h3>
            ${p.description ? `<p style="font-size:12px;color:var(--text2);margin-top:2px;">${escapeHtml(p.description)}</p>` : ''}
          </div>
          <div style="display:flex;gap:6px;">
            <button class="btn btn-sm btn-outline" onclick='editProfile(${JSON.stringify(p)})'>${I18N["btn.edit"]}</button>
            <button class="btn btn-sm btn-success" onclick="setAsDefault(${p.id}, this)">${I18N["profile.set_default"]}</button>
            <button class="btn btn-sm btn-primary" onclick="generateForProfile(${p.id})">${I18N["btn.generate"]}</button>
            ${!p.is_default ? `<button class="btn btn-sm btn-danger" onclick="deleteProfile(${p.id}, this)">${I18N["btn.delete"]}</button>` : ''}
          </div>
        </div>
        <div style="padding:0 16px 8px;display:flex;gap:8px;flex-wrap:wrap;">
          <button class="btn btn-sm btn-outline" onclick="toggleSection('pre-rules-${p.id}', ${p.id}, 'pre')">
            ${I18N["rule.pre_title"]} <span id="pre-count-${p.id}" class="badge badge-primary" style="margin-left:4px;">0</span>
          </button>
          <button class="btn btn-sm btn-outline" onclick="toggleSection('general-rules-${p.id}', ${p.id}, 'general')">
            ${I18N["rule.general_title"]} <span id="general-count-${p.id}" class="badge badge-success" style="margin-left:4px;">0</span>
          </button>
          <button class="btn btn-sm btn-outline" onclick="toggleSection('hosts-${p.id}', ${p.id}, 'hosts')">
            ${I18N["profile.hosts"]} <span id="hosts-count-${p.id}" class="badge badge-warning" style="margin-left:4px;">0</span>
          </button>
          <button class="btn btn-sm btn-outline" onclick="toggleMitm(${p.id})">
            ${I18N["profile.mitm"]}
          </button>
        </div>
        <!-- 前置规则 -->
        <div id="pre-rules-${p.id}" style="display:${open('pre')};padding:0 16px 16px;">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
            <span style="font-size:12px;color:var(--text2);">${I18N["rule.pre_hint"]}</span>
            <button class="btn btn-sm btn-primary" onclick="showAddCustomRule('pre', ${p.id})">${I18N["rule.add_pre"]}</button>
          </div>
          <div class="table-wrap">
            <table>
              <thead><tr>
                <th>${I18N["rule.rule_text"]}</th>
                <th>${I18N["rule.comment"]}</th>
                <th>${I18N["sub.status"]}</th>
                <th>${I18N["node.col_action"]}</th>
              </tr></thead>
              <tbody id="pre-list-${p.id}"></tbody>
            </table>
          </div>
        </div>
        <!-- 通用规则 -->
        <div id="general-rules-${p.id}" style="display:${open('general')};padding:0 16px 16px;">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
            <span style="font-size:12px;color:var(--text2);">${I18N["rule.general_hint"]}</span>
            <button class="btn btn-sm btn-primary" onclick="showAddCustomRule('general', ${p.id})">${I18N["rule.add_general"]}</button>
          </div>
          <div class="table-wrap">
            <table>
              <thead><tr>
                <th>${I18N["rule.rule_text"]}</th>
                <th>${I18N["rule.comment"]}</th>
                <th>${I18N["sub.status"]}</th>
                <th>${I18N["node.col_action"]}</th>
              </tr></thead>
              <tbody id="general-list-${p.id}"></tbody>
            </table>
          </div>
        </div>
        <!-- Host 映射 -->
        <div id="hosts-${p.id}" style="display:${open('hosts')};padding:0 16px 16px;">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
            <span style="font-size:12px;color:var(--text2);">${I18N["profile.hosts_hint"]}</span>
            <button class="btn btn-sm btn-primary" onclick="showAddHost(${p.id})">${I18N["profile.add_host"]}</button>
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
              <tbody id="hosts-list-${p.id}"></tbody>
            </table>
          </div>
        </div>
        <!-- MITM 配置 -->
        <div id="mitm-${p.id}" style="display:${open('mitm')};padding:0 16px 16px;">
          <div style="display:flex;gap:16px;flex-wrap:wrap;margin-bottom:12px;">
            <label class="checkbox"><input type="checkbox" id="mitm-skip-${p.id}"> skip-server-cert-verify</label>
            <label class="checkbox"><input type="checkbox" id="mitm-tcp-${p.id}"> tcp-connection</label>
            <label class="checkbox"><input type="checkbox" id="mitm-h2-${p.id}"> h2</label>
          </div>
          <div class="form-group">
            <label>${I18N["profile.mitm_passphrase"]}</label>
            <input type="password" id="mitm-passphrase-${p.id}" placeholder="${I18N["profile.mitm_passphrase_placeholder"]}" style="max-width:300px;">
          </div>
          <div class="form-group">
            <label>${I18N["profile.mitm_cap12"]}</label>
            <div style="font-size:12px;color:var(--text2);margin-bottom:4px;">${I18N["profile.mitm_cap12_manual"]}</div>
            <input type="text" id="mitm-cap12-${p.id}" placeholder="${I18N["profile.mitm_cap12_placeholder"]}" style="width:100%;max-width:500px;margin-bottom:8px;">
            <div style="display:flex;align-items:center;gap:8px;">
              <span style="font-size:12px;color:var(--text2);">${I18N["profile.mitm_cap12_upload"]}</span>
              <label class="btn btn-sm btn-outline" style="cursor:pointer;margin:0;">
                ${I18N["btn.upload"]}
                <input type="file" accept=".p12" style="display:none;" onchange="uploadMitmCert(${p.id}, this)">
              </label>
            </div>
          </div>
          <div class="form-group">
            <label>${I18N["profile.mitm_hostname"]}</label>
            <textarea id="mitm-hostname-${p.id}" rows="3" placeholder="${I18N["profile.mitm_hostname_placeholder"]}"></textarea>
          </div>
          <div style="display:flex;gap:8px;margin-top:8px;">
            <button class="btn btn-sm btn-primary" onclick="saveMitm(${p.id})">${I18N["profile.mitm_save"]}</button>
            <button class="btn btn-sm btn-outline" onclick="resetMitm(${p.id})">${I18N["profile.mitm_reset"]}</button>
          </div>
        </div>
      </div>`;
    }).join("");

    // 并发加载各档案统计
    await Promise.all(profiles.map(p => loadProfileStats(p.id)));
    // 恢复展开态后加载对应列表
    for (const p of profiles) {
      const sections = openSections.get(p.id);
      if (!sections) continue;
      if (sections.has("pre")) loadProfileRuleList(p.id, "pre");
      if (sections.has("general")) loadProfileRuleList(p.id, "general");
      if (sections.has("hosts")) loadHostList(p.id);
      if (sections.has("mitm")) loadMitm(p.id);
    }
  } catch (e) { toast(e.message, "error"); }
}

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

function toggleSection(sectionId, profileId, type) {
  const el = document.getElementById(sectionId);
  const key = type === "pre-rules" || type === "pre" ? "pre"
    : type === "general-rules" || type === "general" ? "general"
    : type;
  if (!openSections.has(profileId)) openSections.set(profileId, new Set());
  const sections = openSections.get(profileId);
  if (el.style.display === "none") {
    el.style.display = "block";
    sections.add(key);
    if (key === "hosts") loadHostList(profileId);
    else if (key === "mitm") loadMitm(profileId);
    else loadProfileRuleList(profileId, key);
  } else {
    el.style.display = "none";
    sections.delete(key);
  }
}

// ===== Profile 基础 CRUD =====

function showAddProfile() {
  document.getElementById("profile-modal-title").textContent = I18N["profile.new"];
  document.getElementById("profile-edit-id").value = "";
  document.getElementById("profile-name").value = "";
  document.getElementById("profile-desc").value = "";
  document.getElementById("profile-locale").value = document.documentElement.lang === "en" ? "en" : "zh";
  document.getElementById("profile-default").checked = false;
  showModal("profile-modal");
}

function editProfile(p) {
  document.getElementById("profile-modal-title").textContent = I18N["btn.edit"];
  document.getElementById("profile-edit-id").value = p.id;
  document.getElementById("profile-name").value = p.name;
  document.getElementById("profile-desc").value = p.description || "";
  document.getElementById("profile-locale").value = p.locale || "zh";
  document.getElementById("profile-default").checked = p.is_default;
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
  };
  try {
    if (id) {
      await API.put(`/api/configs/profiles/${id}`, data);
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
      toast(I18N["msg.deleted"]); loadData();
    } catch (e) { toast(e.message, "error"); }
  });
}

// ===== Profile 自定义规则列表 =====

async function loadProfileRuleList(profileId, position) {
  try {
    const rules = await API.get(`/api/configs/custom-rules?profile_id=${profileId}&position=${position}`);
    const tbody = document.getElementById(`${position}-list-${profileId}`);
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

function toggleMitm(profileId) {
  const el = document.getElementById(`mitm-${profileId}`);
  if (el.style.display === "none") {
    el.style.display = "block";
    if (!openSections.has(profileId)) openSections.set(profileId, new Set());
    openSections.get(profileId).add("mitm");
    loadMitm(profileId);
  } else {
    el.style.display = "none";
    openSections.get(profileId)?.delete("mitm");
  }
}

async function loadMitm(profileId) {
  try {
    const profiles = await API.get("/api/configs/profiles");
    const p = profiles.find(x => x.id === profileId);
    if (!p) return;
    const mitm = p.mitm || {};
    document.getElementById(`mitm-skip-${profileId}`).checked = mitm['skip-server-cert-verify'] !== false;
    document.getElementById(`mitm-tcp-${profileId}`).checked = mitm['tcp-connection'] !== false;
    document.getElementById(`mitm-h2-${profileId}`).checked = mitm['h2'] !== false;
    document.getElementById(`mitm-passphrase-${profileId}`).value = mitm['ca-passphrase'] || '';
    document.getElementById(`mitm-cap12-${profileId}`).value = mitm['ca-p12'] || '';
    document.getElementById(`mitm-hostname-${profileId}`).value = mitm['hostname'] || '';
  } catch (e) { toast(e.message, "error"); }
}

async function saveMitm(profileId) {
  const mitm = {};
  mitm['skip-server-cert-verify'] = document.getElementById(`mitm-skip-${profileId}`).checked;
  mitm['tcp-connection'] = document.getElementById(`mitm-tcp-${profileId}`).checked;
  mitm['h2'] = document.getElementById(`mitm-h2-${profileId}`).checked;
  const passphrase = document.getElementById(`mitm-passphrase-${profileId}`).value.trim();
  if (passphrase) mitm['ca-passphrase'] = passphrase;
  const cap12 = document.getElementById(`mitm-cap12-${profileId}`).value.trim();
  if (cap12) mitm['ca-p12'] = cap12;
  const hostname = document.getElementById(`mitm-hostname-${profileId}`).value.trim();
  if (hostname) mitm['hostname'] = hostname;
  try {
    await API.put(`/api/configs/profiles/${profileId}`, { mitm });
    toast(I18N["profile.mitm_save"]);
  } catch (e) { toast(e.message, "error"); }
}

async function resetMitm(profileId) {
  if (!(await confirmDialog(`${I18N["profile.mitm_reset"]}?`, { danger: true }))) return;
  try {
    await API.put(`/api/configs/profiles/${profileId}`, { mitm: {} });
    toast(I18N["profile.mitm_reset"]);
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
  await Promise.all([loadDnsPresets(), loadProfileGroups()]);
  loadData();
}
init();
