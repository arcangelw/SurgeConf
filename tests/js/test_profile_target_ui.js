/** 档案目标（target）+ UDP 模式 + 实例角色 UI 行为测试
 *  （node vm + DOM stub，实际执行 profiles.js / settings.js 函数，非源文本断言）
 *
 * 覆盖（Task 6）：
 *  - showAddProfile：新建默认目标按实例角色映射（work_computer→mac_workstation，
 *    home_gateway→mac_gateway），会话读取失败 fail-closed 回落 mac_gateway；
 *    mode 默认随目标（mac→scoped / ios→compatible）
 *  - 目标切换联动：切到 iOS 时模式选项裁剪为 strict/compatible，当前 scoped
 *    被纠正为 compatible；切回 Mac 恢复三选项
 *  - editProfile：target/mode 回显（选项按目标裁剪后取档案现存值）
 *  - saveProfile：POST/PUT 载荷携带 target + udp_security_mode
 *  - loadData 列表徽标：target / UDP 模式 / token_configured 徽标渲染；
 *    列表数据不含任何 token 明文字段
 *  - settings.js：loadAccess 回显 installation_role；saveAccess 提交所选角色
 *    （不再硬编码 home_gateway）
 *
 * 运行：node tests/js/test_profile_target_ui.js
 */

"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const REPO_ROOT = path.resolve(__dirname, "..", "..");
const PROFILES_SRC = fs.readFileSync(path.join(REPO_ROOT, "app/static/js/profiles.js"), "utf8");
const SETTINGS_SRC = fs.readFileSync(path.join(REPO_ROOT, "app/static/js/settings.js"), "utf8");

// ===== 最小 i18n（普通对象：缺失键渲染 undefined 会被断言捕获）=====
const I18N_STUB = {
  "profile.new": "新建配置方案", "btn.edit": "编辑", "btn.save": "保存",
  "profile.name": "名称", "profile.desc": "描述", "profile.default": "默认",
  "profile.set_default": "设为默认", "btn.delete": "删除",
  "profile.locale_label": "语言", "profile.locale_zh": "简体中文",
  "profile.target_label": "目标设备",
  "profile.target_mac_workstation": "Mac 工作站 — 日常桌面使用",
  "profile.target_mac_gateway": "Mac 网关 — 局域网共享代理",
  "profile.target_ios_mobile": "iPhone / iPad 移动设备",
  "profile.target_hint_ws": "hint-ws", "profile.target_hint_gw": "hint-gw",
  "profile.target_hint_ios": "hint-ios",
  "profile.mode_label": "UDP 安全模式",
  "profile.mode_strict": "strict", "profile.mode_scoped": "scoped",
  "profile.mode_compatible": "compatible",
  "profile.badge_target_ws": "Mac 工作站", "profile.badge_target_gw": "Mac 网关",
  "profile.badge_target_ios": "iOS",
  "profile.mode_short_strict": "UDP 严格", "profile.mode_short_scoped": "UDP 按策略",
  "profile.mode_short_compatible": "UDP 兼容",
  "profile.token_configured": "已配置", "profile.token_not_configured": "未配置",
  "profile.empty": "还没有配置方案",
  "msg.added": "已添加", "msg.updated": "已更新", "msg.required": "必填",
  "msg.deleted": "已删除", "profile.confirm_delete": "确认删除？",
  "profile.set_default_done": "已设为默认",
  "tab.overview": "概览", "tab.general": "General",
  "settings.access_local_hint": "local", "settings.access_lan_hint": "lan",
  "settings.access_password_required": "需要密码", "settings.access_saved": "已保存",
  "settings.access_load_failed": "访问设置加载失败，表单暂不可用；请刷新页面重试。",
  "profile.session_load_failed": "无法读取管理会话，请刷新页面重试；未确认实例角色前不打开新建弹窗。",
};

function makeElement(initial) {
  const el = {
    value: "", placeholder: "", checked: false, textContent: "", innerHTML: "",
    className: "", style: {}, dataset: {}, onclick: null, _observers: [],
    focus() {}, select() {}, setSelectionRange() {},
  };
  const classList = {
    contains(c) { return el.className.split(/\s+/).filter(Boolean).includes(c); },
    add(c) { if (!classList.contains(c)) el.className = el.className ? `${el.className} ${c}` : c; },
    remove(c) { el.className = el.className.split(/\s+/).filter(x => x && x !== c).join(" "); },
  };
  el.classList = classList;
  return Object.assign(el, initial);
}

class MutationObserverStub {
  constructor(cb) { this.cb = cb; this._entry = null; }
  observe(target, opts) { this._entry = { cb: this.cb, opts, alive: true }; target._observers.push(this._entry); }
  disconnect() { if (this._entry) this._entry.alive = false; }
}

const optionValues = (sel) =>
  [...String(sel.innerHTML).matchAll(/<option value="([^"]*)"/g)].map(m => m[1]);

/** 独立 profiles.js context；role 控制 /api/auth/session，profiles 控制列表 */
function createProfilesPage({ role = "home_gateway", sessionFails = false, unauthenticated = false, profiles = [] } = {}) {
  const elements = new Map();
  const calls = { post: [], put: [], del: [], modals: [], toasts: [] };
  const documentStub = {
    documentElement: { lang: "zh" },
    body: makeElement(),
    getElementById: (id) => elements.get(id) || null,
    querySelector: () => null,
    querySelectorAll: () => [],
    createElement: () => makeElement(),
    addEventListener: () => {},
  };
  const API = {
    async get(url) {
      if (url.includes("/api/auth/session")) {
        if (sessionFails) throw new Error("session unavailable");
        return { authenticated: !unauthenticated, installation_role: role,
                 admin_access: "local_only", password_configured: false };
      }
      if (url === "/api/configs/profiles") return profiles;
      if (url.includes("custom-rules") || url.includes("host-mappings")) return [];
      if (url.includes("server-ips")) return { ips: [] };
      return [];
    },
    async post(url, body) { calls.post.push({ url, body }); return { id: 99, name: "n" }; },
    async put(url, body) { calls.put.push({ url, body }); return {}; },
    async del(url) { calls.del.push(url); return {}; },
  };
  const sandbox = {
    document: documentStub, MutationObserver: MutationObserverStub,
    localStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
    location: { protocol: "http:", port: "61830", host: "127.0.0.1:61830" },
    navigator: {}, I18N: I18N_STUB, API,
    showModal: (id) => calls.modals.push(["show", id]),
    hideModal: (id) => calls.modals.push(["hide", id]),
    confirmDialog: async () => true,
    toast: (msg, type) => calls.toasts.push({ msg, type }),
    withLoading: async (_btn, fn) => fn(),
    escapeHtml: (s) => String(s), translateName: (s) => s,
    setupRuleModals: () => {}, showLoading: () => {}, hideLoading: () => {},
    flashInput: () => {}, console,
  };
  vm.createContext(sandbox);
  vm.runInContext(PROFILES_SRC + `
;globalThis.__hooks = { showAddProfile, editProfile, saveProfile, loadData,
  onProfileTargetChange, fillProfileModeOptions };`, sandbox, { filename: "profiles.js" });
  const reg = (id, initial) => { const el = makeElement(initial); elements.set(id, el); return el; };
  return { sandbox, elements, reg, calls, documentStub };
}

/** 独立 settings.js context */
function createSettingsPage({ role = "work_computer", passwordConfigured = false, sessionFails = false } = {}) {
  const elements = new Map();
  const sessionState = { fails: sessionFails };
  const calls = { put: [], toasts: [] };
  const documentStub = {
    documentElement: { lang: "zh" },
    getElementById: (id) => elements.get(id) || null,
    querySelector: () => null, querySelectorAll: () => [],
    createElement: () => makeElement(), addEventListener: () => {},
  };
  const API = {
    async get(url) {
      if (url.includes("/api/auth/session")) {
        if (sessionState.fails) throw new Error("session unavailable");
        return { authenticated: true,
          installation_role: role, admin_access: "local_only",
          password_configured: passwordConfigured };
      }
      if (url.includes("/api/settings/config")) return { exclusion_keywords: "" };
      if (url.includes("/api/settings/skip-proxy")) return { items: [] };
      if (url.includes("/api/settings/backups")) return [];
      return {};
    },
    async post(url, body) { return {}; },
    async put(url, body) { calls.put.push({ url, body }); return {}; },
    async del() { return {}; },
  };
  const sandbox = {
    document: documentStub, MutationObserver: MutationObserverStub,
    localStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
    location: { protocol: "http:", port: "61830", host: "127.0.0.1:61830" },
    navigator: {}, I18N: I18N_STUB, API,
    toast: (msg, type) => calls.toasts.push({ msg, type }),
    withLoading: async (_btn, fn) => fn(),
    confirmDialog: async () => true,
    console,
  };
  vm.createContext(sandbox);
  vm.runInContext(SETTINGS_SRC + `
;globalThis.__hooks = { saveAccess, loadAccess };`, sandbox, { filename: "settings.js" });
  const reg = (id, initial) => { const el = makeElement(initial); elements.set(id, el); return el; };
  return { sandbox, elements, reg, calls, failSession: () => { sessionState.fails = true; } };
}

const drain = () => new Promise((r) => setTimeout(r, 0));

(async () => {
  // ===== profiles.js：新建默认目标按实例角色 =====
  for (const [role, expectedTarget, expectedMode] of [
    ["work_computer", "mac_workstation", "scoped"],
    ["home_gateway", "mac_gateway", "scoped"],
  ]) {
    const page = createProfilesPage({ role });
    page.reg("profile-modal-title"); page.reg("profile-edit-id", { value: "x" });
    page.reg("profile-name"); page.reg("profile-desc");
    page.reg("profile-locale"); page.reg("profile-default");
    const target = page.reg("profile-target", { value: "" });
    const mode = page.reg("profile-mode");
    page.reg("profile-target-hint");
    await page.sandbox.__hooks.showAddProfile();
    assert.equal(target.value, expectedTarget, `角色 ${role} 新建默认目标`);
    assert.equal(mode.value, expectedMode, `角色 ${role} 默认模式随目标`);
    assert.deepEqual(page.calls.modals, [["show", "profile-modal"]]);
    await drain();
  }

  // 会话读取失败：fail-closed 回落默认角色 home_gateway → mac_gateway
  {
    const page = createProfilesPage({ sessionFails: true });
    page.reg("profile-modal-title"); page.reg("profile-edit-id");
    page.reg("profile-name"); page.reg("profile-desc");
    page.reg("profile-locale"); page.reg("profile-default");
    const target = page.reg("profile-target", { value: "" });
    page.reg("profile-mode"); page.reg("profile-target-hint");
    await page.sandbox.__hooks.showAddProfile();
    assert.ok(page.calls.toasts.some(t => t.type === "error"), "会话不可得必须报错");
    assert.deepEqual(page.calls.modals.filter(m => m[0] === "show"), [], "会话不可得不打开弹窗");
    assert.equal(page.calls.post.length, 0, "会话不可得绝不发出创建 POST");
    assert.equal(target.value, "", "不预填任何目标（用户未明确选择）");
    await drain();
  }
  {
    // 未认证会话（authenticated: false）同样 fail closed
    const page = createProfilesPage({ unauthenticated: true });
    page.reg("profile-modal-title"); page.reg("profile-edit-id");
    page.reg("profile-name"); page.reg("profile-desc");
    page.reg("profile-locale"); page.reg("profile-default");
    page.reg("profile-target", { value: "" });
    page.reg("profile-mode"); page.reg("profile-target-hint");
    await page.sandbox.__hooks.showAddProfile();
    assert.ok(page.calls.toasts.some(t => t.type === "error"), "未认证会话必须报错");
    assert.deepEqual(page.calls.modals.filter(m => m[0] === "show"), [], "未认证不打开弹窗");
    assert.equal(page.calls.post.length, 0, "未认证绝不发出创建 POST");
    await drain();
  }

  // ===== 目标切换联动：iOS 裁剪 + scoped 纠正 + 恢复 =====
  {
    const page = createProfilesPage();
    page.reg("profile-modal-title"); page.reg("profile-edit-id");
    page.reg("profile-name"); page.reg("profile-desc");
    page.reg("profile-locale"); page.reg("profile-default");
    const target = page.reg("profile-target", { value: "mac_workstation" });
    const mode = page.reg("profile-mode");
    const hint = page.reg("profile-target-hint");

    page.sandbox.__hooks.onProfileTargetChange();  // mac：三选项 + hint
    assert.deepEqual(optionValues(mode), ["strict", "scoped", "compatible"]);
    assert.equal(mode.value, "scoped");
    assert.equal(hint.textContent, "hint-ws");

    target.value = "ios_mobile";
    page.sandbox.__hooks.onProfileTargetChange("scoped");  // 档案现存 scoped 在 iOS 非法
    assert.deepEqual(optionValues(mode), ["strict", "compatible"], "iOS 仅 strict/compatible");
    assert.equal(mode.value, "compatible", "iOS 下 scoped 纠正为 compatible");
    assert.equal(hint.textContent, "hint-ios");

    target.value = "ios_mobile";
    page.sandbox.__hooks.onProfileTargetChange("strict");  // 合法偏好保留
    assert.equal(mode.value, "strict");

    target.value = "mac_gateway";
    page.sandbox.__hooks.onProfileTargetChange("strict");
    assert.deepEqual(optionValues(mode), ["strict", "scoped", "compatible"], "切回 Mac 恢复三选项");
    assert.equal(mode.value, "strict", "合法偏好保留");
    await drain();
  }

  // ===== editProfile 回显 =====
  {
    const page = createProfilesPage();
    page.reg("profile-modal-title"); page.reg("profile-edit-id");
    page.reg("profile-name"); page.reg("profile-desc");
    page.reg("profile-locale"); page.reg("profile-default");
    const target = page.reg("profile-target", { value: "" });
    const mode = page.reg("profile-mode"); page.reg("profile-target-hint");
    page.sandbox.__hooks.editProfile({ id: 7, name: "iOS 档", description: "",
      is_default: false, locale: "zh", target: "ios_mobile",
      udp_security_mode: "compatible" });
    assert.equal(target.value, "ios_mobile");
    assert.deepEqual(optionValues(mode), ["strict", "compatible"]);
    assert.equal(mode.value, "compatible");
    await drain();
  }

  // ===== saveProfile：载荷携带 target + udp_security_mode =====
  {
    const page = createProfilesPage();
    page.reg("profile-modal-title"); page.reg("profile-edit-id", { value: "" });
    page.reg("profile-name", { value: "新档" }); page.reg("profile-desc", { value: "" });
    page.reg("profile-locale", { value: "zh" }); page.reg("profile-default", { checked: false });
    page.reg("profile-target", { value: "mac_gateway" });
    page.reg("profile-mode", { value: "strict" });
    await page.sandbox.__hooks.saveProfile();
    assert.equal(page.calls.post.length, 1);
    assert.equal(page.calls.post[0].url, "/api/configs/profiles");
    assert.equal(page.calls.post[0].body.target, "mac_gateway");
    assert.equal(page.calls.post[0].body.udp_security_mode, "strict");
    await drain();
  }

  // ===== loadData 列表徽标：target / mode / token_configured；无 token 明文 =====
  {
    const page = createProfilesPage({ profiles: [
      { id: 1, name: "工作档", description: "", is_default: true, locale: "zh",
        target: "mac_workstation", udp_security_mode: "scoped", token_configured: true },
      { id: 2, name: "手机档", description: "", is_default: false, locale: "en",
        target: "ios_mobile", udp_security_mode: "compatible", token_configured: false },
    ]});
    const list = page.reg("profile-list");
    await page.sandbox.__hooks.loadData();
    const html = list.innerHTML;
    assert.ok(html.includes("Mac 工作站"), "列表含目标徽标");
    assert.ok(html.includes("iOS"), "列表含 iOS 目标徽标");
    assert.ok(html.includes("UDP 按策略"), "列表含 UDP 模式徽标");
    assert.ok(html.includes("UDP 兼容"), "列表含 iOS 模式徽标");
    assert.ok(html.includes("已配置") && html.includes("未配置"), "列表含 token 状态徽标");
    // token_configured 是布尔状态（JSON.stringify(p) 合法携带字段名）；
    // 敏感断言：绝不出现哈希字段与 token 值形态
    assert.ok(!html.includes("subscription_token_hash"), "不渲染哈希字段");
    assert.ok(!/token=[A-Za-z0-9_-]{8,}/.test(html) && !/"token"\s*:/.test(html), "绝不渲染 token 明文值");
    await drain();
  }

  // ===== settings.js：三态门控（未完成禁用 / 失败禁用+报错 / 成功启用）=====
  const regAccessForm = (page, { roleValue = "home_gateway" } = {}) => {
    const els = {
      role: page.reg("installation-role", { value: roleValue, disabled: true }),
      mode: page.reg("access-mode", { value: "local_only", disabled: true }),
      save: page.reg("access-save-btn", { disabled: true }),
      current: page.reg("access-current-password", { value: "", disabled: true }),
      next: page.reg("access-new-password", { value: "", disabled: true }),
    };
    page.reg("access-mode-hint"); page.reg("access-current-group");
    return els;
  };
  const allDisabled = (els) => [els.role, els.mode, els.save, els.current, els.next]
    .every(e => e.disabled === true);

  {
    // 态1：session 请求未完成——控件保持 HTML 初始禁用（真实异步：不 await）
    const page = createSettingsPage({ role: "work_computer" });
    const els = regAccessForm(page);
    const pending = page.sandbox.__hooks.loadAccess();  // 不等待
    assert.ok(allDisabled(els), "session 未完成时表单全部禁用");
    assert.equal(page.calls.put.length, 0, "禁用态 saveAccess 不应已被程序触发");
    await pending; await drain();
    assert.ok(!allDisabled(els), "session 成功后表单启用");
  }
  {
    // 态2：session 请求失败——toast 报错且保持禁用；saveAccess guard 拒绝提交
    const page = createSettingsPage({ sessionFails: true });
    const els = regAccessForm(page);
    await page.sandbox.__hooks.loadAccess();
    assert.ok(page.calls.toasts.some(t => t.msg.includes("加载失败")), "加载失败明确报错");
    assert.ok(allDisabled(els), "失败后表单保持禁用");
    // 程序触发 saveAccess（绕过 disabled 控件）：guard 仍拒绝
    await page.sandbox.__hooks.saveAccess(await Promise.resolve({}));
    assert.equal(page.calls.put.length, 0, "guard 拒绝未加载状态的提交");
    assert.ok(page.calls.toasts.some(t => t.msg.includes("加载失败")), "guard 报错提示");
  }
  {
    // 态2b：成功启用后 session 再失效——回锁表单 + guard 复位（浏览器重验发现的缺口）
    const page = createSettingsPage({ role: "work_computer" });
    const els = regAccessForm(page);
    await page.sandbox.__hooks.loadAccess();
    assert.ok(!allDisabled(els), "先成功启用");
    page.failSession();  // 切换 stub 为失败
    await page.sandbox.__hooks.loadAccess();
    assert.ok(allDisabled(els), "成功后再失败必须回锁全部控件");
    await page.sandbox.__hooks.saveAccess(await Promise.resolve({}));
    assert.equal(page.calls.put.length, 0, "guard 复位后拒绝提交");
  }
  {
    // 态3：session 成功——回显角色 + 启用 + saveAccess 提交所选角色（非硬编码）
    const page = createSettingsPage({ role: "work_computer" });
    const els = regAccessForm(page, { roleValue: "home_gateway" });
    await page.sandbox.__hooks.loadAccess();
    assert.equal(els.role.value, "work_computer", "loadAccess 回显实例角色");
    assert.ok(!allDisabled(els), "成功后全部启用");
    await page.sandbox.__hooks.saveAccess(await Promise.resolve({}));
    assert.equal(page.calls.put.length, 1);
    assert.equal(page.calls.put[0].url, "/api/settings/access");
    assert.equal(page.calls.put[0].body.installation_role, "work_computer",
      "saveAccess 提交 select 当前值（回显后的角色）");
    // 用户显式改选后提交所选值（非硬编码）
    els.role.value = "home_gateway";
    await page.sandbox.__hooks.saveAccess(await Promise.resolve({}));
    assert.equal(page.calls.put.length, 2);
    assert.equal(page.calls.put[1].body.installation_role, "home_gateway",
      "saveAccess 提交用户改选的角色");
  }
  {
    // 非法角色回落 home_gateway（后端已收敛，前端防御）
    const page = createSettingsPage({ role: "linux_toaster" });
    const els = regAccessForm(page, { roleValue: "work_computer" });
    await page.sandbox.__hooks.loadAccess();
    assert.equal(els.role.value, "home_gateway", "非法角色回落默认");
  }

  console.log("test_profile_target_ui.js: all assertions passed");
})().catch((e) => { console.error(e); process.exit(1); });
