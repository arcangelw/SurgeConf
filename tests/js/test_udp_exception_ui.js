/** UDP 安全页签 + 例外 CRUD UI 行为测试（Task 5 + Fix Round 1）
 *  （node vm + DOM stub，实际执行 profiles.js 函数，非源文本断言）
 *
 * 覆盖：
 *  - 目标感知渲染：workstation/gateway 显示例外表（进程/MAC 提示，
 *    gateway 另含 Fast Path 说明与指南链接）；iOS 保留 UDP 页签但仅
 *    模式（strict/compatible）+ 风险说明，绝不渲染例外标题/表/CRUD
 *  - 持久 XSS 防护：列表行全部 DOM API 构建；服务端 display_name /
 *    masked 只经 textContent；含 `TV's <img src=x onerror=...>` 的
 *    payload 原样显示为文本，行内无 IMG/SCRIPT 元素、按钮无 onclick/
 *    data-*，服务端值不进任何 DOM 属性
 *  - scoped 警告生命周期：无启用例外→显示；添加/启用→消失；禁用/删除
 *    最后一个→恢复；strict 档案不显示
 *  - 覆盖式编辑安全边界：编辑输入永远空白（留空=保持），PUT 仅在用户
 *    输入新值时含 match_value/match_type；新建必填；完整值只存在于
 *    输入框当下值，成功/失败/关闭后立即清空；失败后重载脱敏列表
 *  - 删除防双击：按钮从确认到 DELETE 结束禁用（withLoading 语义），
 *    双击两次 click handler 仅发 1 个 DELETE
 *  - 弹窗清空 observer：单一持久实例，多次开关不累积（_observers
 *    恒为 1），任意关闭路径敏感值立即清空
 *  - 模式保存：PUT /api/configs/profiles/{id} { udp_security_mode }
 *
 * 运行：node tests/js/test_udp_exception_ui.js
 */

"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const REPO_ROOT = path.resolve(__dirname, "..", "..");
const PROFILES_SRC = fs.readFileSync(path.join(REPO_ROOT, "app/static/js/profiles.js"), "utf8");

// ===== 最小 i18n（缺失键渲染 undefined 会被断言捕获）=====
const I18N_STUB = {
  "profile.new": "新建配置方案", "btn.edit": "编辑", "btn.save": "保存",
  "profile.name": "名称", "profile.default": "默认",
  "profile.set_default": "设为默认", "btn.delete": "删除",
  "profile.locale_label": "语言", "profile.locale_zh": "简体中文",
  "profile.target_label": "目标设备",
  "profile.target_mac_workstation": "Mac 工作站",
  "profile.target_mac_gateway": "Mac 网关",
  "profile.target_ios_mobile": "iOS",
  "profile.target_hint_ws": "hint-ws", "profile.target_hint_gw": "hint-gw",
  "profile.target_hint_ios": "hint-ios",
  "profile.mode_label": "UDP 安全模式",
  "profile.mode_strict": "strict", "profile.mode_scoped": "scoped",
  "profile.mode_compatible": "compatible",
  "profile.badge_target_ws": "Mac 工作站", "profile.badge_target_gw": "Mac 网关",
  "profile.badge_target_ios": "iOS",
  "profile.mode_short_strict": "UDP 严格", "profile.mode_short_scoped": "UDP 按策略",
  "profile.mode_short_compatible": "UDP 兼容",
  "profile.empty": "还没有配置方案",
  "msg.added": "已添加", "msg.updated": "已更新", "msg.required": "必填",
  "msg.deleted": "已删除",
  "profile.tab_udp": "UDP 安全",
  "profile.udp_mode_save": "保存模式",
  "profile.udp_exceptions_title": "UDP 例外",
  "profile.udp_add": "添加例外",
  "profile.udp_col_value": "匹配值",
  "profile.udp_col_display_name": "显示名称",
  "profile.udp_col_sort": "排序",
  "profile.udp_hint_ws": "hint-ws ExampleVideo",
  "profile.udp_hint_gw": "hint-gw 02:00:00:00:00:01",
  "profile.udp_fastpath_title": "Fast Path 与 Gateway VM",
  "profile.udp_fastpath_note": "fastpath-note",
  "profile.udp_guide_link": "guide-link",
  "profile.udp_scoped_warning": "scoped-warning",
  "profile.udp_ios_note": "ios-note",
  "profile.udp_ios_webrtc_note": "ios-webrtc-note",
  "profile.udp_exc_new": "添加 UDP 例外",
  "profile.udp_value_label_process": "进程名",
  "profile.udp_value_label_mac": "MAC 地址",
  "profile.udp_placeholder_process": "例：ExampleVideo",
  "profile.udp_placeholder_mac": "例：02:00:00:00:00:01",
  "profile.udp_hint_process_format": "hint-process",
  "profile.udp_hint_mac_format": "hint-mac",
  "profile.udp_keep_placeholder": "留空保持原值",
  "profile.udp_keep_hint": "keep-hint",
  "profile.udp_current_masked_label": "当前值（脱敏）",
  "profile.udp_display_name_label": "显示名称（可选）",
  "profile.udp_display_name_placeholder": "例：Example TV",
  "profile.udp_enabled_label": "启用",
  "profile.udp_sort_label": "排序序号（小的在前）",
  "profile.udp_confirm_delete": "确认删除例外",
  "sub.status": "状态", "node.col_action": "操作",
  "btn.enable": "启用", "btn.disable": "禁用",
  "profile.token_configured": "已配置", "profile.token_not_configured": "未配置",
  "rule.pre_title": "前置", "rule.general_title": "通用",
  "profile.hosts": "Host",
  "profile.mitm_config": "MITM",
};

function makeElement(initial) {
  const el = {
    tagName: "", type: "", value: "", placeholder: "", checked: false,
    textContent: "", innerHTML: "", className: "", style: {}, dataset: {},
    onclick: null, disabled: false, children: [], _observers: [], _handlers: {},
    focus() {}, select() {}, setSelectionRange() {},
    appendChild(child) { el.children.push(child); return child; },
    replaceChildren(...nodes) { el.children = [...nodes]; },
    addEventListener(evt, fn) { (el._handlers[evt] = el._handlers[evt] || []).push(fn); },
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
  constructor(cb) { this.cb = cb; this.alive = true; }
  observe(target) { if (this.alive && target?._observers) target._observers.push(this); }
  disconnect() { this.alive = false; }
}

/** 深度遍历元素树（stub children），返回 {tagNames, texts, attrs} */
function inspectTree(el, acc = { tags: [], onclickAttrs: 0, datasets: 0 }) {
  if (!el || typeof el !== "object") return acc;
  if (el.tagName) acc.tags.push(el.tagName.toUpperCase());
  if (el.onclick !== null && el.onclick !== undefined) acc.onclickAttrs += 1;
  if (el.dataset && Object.keys(el.dataset).length > 0) acc.datasets += 1;
  (el.children || []).forEach(c => inspectTree(c, acc));
  return acc;
}

/** 独立 profiles.js context；profiles 驱动列表，udpExceptions 为模拟后端存储 */
function createUdpPage({ profiles = [], udpExceptions = [], putFails = false, confirm = true } = {}) {
  const elements = new Map();
  const storage = new Map();
  const calls = { post: [], put: [], del: [], toasts: [], storage: [], udpGets: 0 };
  let nextExcId = 100;

  // 模拟后端：列表响应只含白名单字段 + 掩码值（绝不回完整值）
  const excView = (e) => ({
    id: e.id, profile_id: e.profile_id, match_type: e.match_type,
    match_value_masked: e.match_value_masked,
    display_name: e.display_name ?? null, enabled: e.enabled, sort_order: e.sort_order,
  });

  const documentStub = {
    documentElement: { lang: "zh" },
    body: makeElement(),
    getElementById: (id) => elements.get(id) || null,
    querySelector: () => null,
    querySelectorAll: () => [],
    createElement: (tag) => makeElement({ tagName: tag }),
    addEventListener: () => {},
  };

  const fireObservers = (el) => { [...el._observers].forEach(o => { if (o.alive !== false) o.cb(); }); };

  const showModal = (id) => {
    const el = elements.get(id);
    if (el) { el.classList.add("show"); fireObservers(el); }
  };
  const hideModal = (id) => {
    const el = elements.get(id);
    if (el) { el.classList.remove("show"); fireObservers(el); }
  };

  const API = {
    async get(url) {
      if (url.includes("/api/auth/session")) {
        return { authenticated: true, installation_role: "work_computer" };
      }
      if (url === "/api/configs/profiles") return profiles;
      if (url.includes("/udp-exceptions")) {
        calls.udpGets += 1;
        const pid = Number(url.match(/profiles\/(\d+)\/udp-exceptions/)[1]);
        return udpExceptions.filter(e => e.profile_id === pid).map(excView);
      }
      if (url.includes("custom-rules") || url.includes("host-mappings")) return [];
      if (url.includes("server-ips")) return { ips: [] };
      if (url.includes("/api/configs/profiles/")) return { mitm: {} };
      return [];
    },
    async post(url, body) {
      calls.post.push({ url, body });
      const m = url.match(/profiles\/(\d+)\/udp-exceptions$/);
      if (m) {
        const e = { id: nextExcId++, profile_id: Number(m[1]),
          match_type: body.match_type, match_value_masked: "EX****01",
          display_name: body.display_name ?? null, enabled: body.enabled,
          sort_order: body.sort_order };
        udpExceptions.push(e);
        return excView(e);
      }
      return { id: 99, name: "n" };
    },
    async put(url, body) {
      calls.put.push({ url, body });
      if (putFails) throw new Error("服务暂时不可用，请稍后重试");
      const m = url.match(/profiles\/(\d+)\/udp-exceptions\/(\d+)$/);
      if (m) {
        const e = udpExceptions.find(x => x.id === Number(m[2]));
        if (e) {
          if (body.match_value !== undefined) e.match_value_masked = "NE****02";
          if (body.display_name !== undefined) e.display_name = body.display_name;
          if (body.enabled !== undefined) e.enabled = body.enabled;
          if (body.sort_order !== undefined) e.sort_order = body.sort_order;
        }
      }
      return {};
    },
    async del(url) {
      calls.del.push(url);
      const m = url.match(/profiles\/(\d+)\/udp-exceptions\/(\d+)$/);
      if (m) {
        const idx = udpExceptions.findIndex(e => e.id === Number(m[2]));
        if (idx >= 0) udpExceptions.splice(idx, 1);
      }
      return {};
    },
  };

  const sandbox = {
    document: documentStub, MutationObserver: MutationObserverStub,
    window: { innerWidth: 1024 },
    localStorage: {
      getItem: (k) => storage.get(k) ?? null,
      setItem: (k, v) => { storage.set(k, v); calls.storage.push([k, v]); },
      removeItem: (k) => storage.delete(k),
    },
    location: { protocol: "http:", port: "61830", host: "127.0.0.1:61830" },
    navigator: {}, I18N: I18N_STUB, API,
    showModal, hideModal,
    confirmDialog: async () => confirm,
    toast: (msg, type) => calls.toasts.push({ msg, type }),
    // 与生产 app.js withLoading 逐行为一致：进入即禁用，finally 恢复快照
    withLoading: async (btn, fn) => {
      if (!btn || !fn) return fn?.();
      const prev = btn.disabled;
      btn.disabled = true;
      btn.classList.add("btn-loading");
      try { return await fn(); }
      finally { btn.disabled = prev; btn.classList.remove("btn-loading"); }
    },
    escapeHtml: (s) => String(s), translateName: (s) => s,
    setupRuleModals: () => {}, showLoading: () => {}, hideLoading: () => {},
    flashInput: (el) => { calls.toasts.push({ msg: `flash:${el.id}`, type: "flash" }); },
    compactActions: () => {}, console,
  };
  vm.createContext(sandbox);
  vm.runInContext(PROFILES_SRC + `
;globalThis.__hooks = { loadData, renderTabContent, renderUdpTab, loadUdpExceptions,
  showAddUdpException, editUdpException, saveUdpException, closeUdpExceptionDialog,
  toggleUdpException, deleteUdpException, saveUdpMode, switchTab };`, sandbox,
    { filename: "profiles.js" });
  const reg = (id, initial) => { const el = makeElement(initial); elements.set(id, el); return el; };
  return { sandbox, elements, reg, calls, udpExceptions, fireObservers };
}

const drain = () => new Promise((r) => setTimeout(r, 0));

function makeProfile(id, target, mode) {
  return { id, name: `档案${id}`, description: "", is_default: false, locale: "zh",
    target, udp_security_mode: mode, token_configured: false };
}

/** 注册 UDP Tab 骨架元素（renderUdpTab 的 innerHTML 在 stub 中不解析 DOM） */
function regUdpTab(page, profileId) {
  page.reg(`tab-content-${profileId}`);
  page.reg(`udp-list-${profileId}`);
  page.reg(`udp-mode-${profileId}`);
  return page.reg(`udp-scoped-warn-${profileId}`);
}

/** 注册 UDP 例外弹窗全量元素 */
function regUdpDialog(page) {
  const dlg = page.reg("udp-exception-modal");
  page.reg("udp-exception-modal-title");
  page.reg("udp-exc-edit-id");
  page.reg("udp-exc-profile-id");
  const valueInput = page.reg("udp-exc-value");
  page.reg("udp-exc-value-label");
  page.reg("udp-exc-value-hint");
  page.reg("udp-exc-current-row");
  page.reg("udp-exc-current-masked");
  page.reg("udp-exc-display-name");
  page.reg("udp-exc-enabled", { checked: false });
  page.reg("udp-exc-sort", { value: "0" });
  return { dlg, valueInput };
}

(async () => {
  // ===== 1. 页签可见性：全部目标（含 iOS）都有 UDP 页签 =====
  {
    const page = createUdpPage({ profiles: [
      makeProfile(1, "mac_workstation", "scoped"),
      makeProfile(2, "ios_mobile", "compatible"),
    ]});
    const list = page.reg("profile-list");
    await page.sandbox.__hooks.loadData();
    const html = String(list.innerHTML);
    const bar1 = html.slice(html.indexOf('id="tab-bar-1"'), html.indexOf('id="tab-content-1"'));
    const bar2 = html.slice(html.indexOf('id="tab-bar-2"'), html.indexOf('id="tab-content-2"'));
    assert.ok(bar1.includes("UDP 安全"), "Mac 档案页签栏含 UDP");
    assert.ok(bar2.includes("UDP 安全"), "iOS 档案页签栏也含 UDP（页签保留）");
  }

  // ===== 2. renderUdpTab 目标感知面板 =====
  {
    const page = createUdpPage({ profiles: [makeProfile(1, "mac_workstation", "scoped")] });
    await page.sandbox.__hooks.loadData();
    const tab = page.reg("tab-content-1");
    page.reg("udp-list-1"); page.reg("udp-mode-1");
    await page.sandbox.__hooks.renderUdpTab(1);
    const html = String(tab.innerHTML);
    assert.ok(html.includes("hint-ws ExampleVideo"), "workstation 显示进程名提示");
    assert.ok(html.includes("添加例外"), "workstation 显示添加按钮");
    assert.ok(html.includes("UDP 例外"), "workstation 显示例外标题");
    assert.ok(!html.includes("Fast Path 与 Gateway VM"), "workstation 不显示 FastPath 说明");
    assert.ok(html.includes('id="udp-scoped-warn-1"'), "含 scoped 警告容器");
    await drain();
  }
  {
    const page = createUdpPage({ profiles: [makeProfile(1, "mac_gateway", "scoped")] });
    await page.sandbox.__hooks.loadData();
    const tab = page.reg("tab-content-1");
    page.reg("udp-list-1"); page.reg("udp-mode-1");
    await page.sandbox.__hooks.renderUdpTab(1);
    const html = String(tab.innerHTML);
    assert.ok(html.includes("hint-gw 02:00:00:00:00:01"), "gateway 显示 MAC 提示");
    assert.ok(html.includes("Fast Path 与 Gateway VM"), "gateway 显示 FastPath 说明");
    assert.ok(html.includes("/guide/udp-compatibility"), "gateway 提供指南链接");
    await drain();
  }
  {
    // iOS：UDP 页签保留模式管理 + 风险说明；绝不渲染例外控件
    const page = createUdpPage({ profiles: [makeProfile(1, "ios_mobile", "compatible")] });
    await page.sandbox.__hooks.loadData();
    const tab = page.reg("tab-content-1");
    page.reg("udp-mode-1");
    await page.sandbox.__hooks.renderUdpTab(1);
    const html = String(tab.innerHTML);
    assert.ok(html.includes('id="udp-mode-1"'), "iOS 保留模式选择");
    assert.ok(html.includes("保存模式"), "iOS 保留模式保存按钮");
    assert.ok(html.includes("ios-note"), "iOS 显示能力说明");
    assert.ok(html.includes("ios-webrtc-note"), "iOS 显示 WebRTC 风险提示");
    assert.ok(!html.includes("UDP 例外"), "iOS 不渲染例外标题");
    assert.ok(!html.includes("udp-list-1"), "iOS 不渲染例外表");
    assert.ok(!html.includes("添加例外"), "iOS 不渲染添加按钮（无 CRUD 入口）");
    await drain();
  }
  {
    // iOS + 残留/显式 udp 展开状态：renderTabContent 正常渲染 UDP 面板（状态一致）
    const page = createUdpPage({ profiles: [makeProfile(1, "ios_mobile", "compatible")] });
    await page.sandbox.__hooks.loadData();
    const tab = page.reg("tab-content-1");
    page.reg("udp-mode-1");
    page.sandbox.localStorage.setItem("profile-tab:1", "udp");
    await page.sandbox.__hooks.renderTabContent(1);
    assert.ok(String(tab.innerHTML).includes('id="udp-mode-1"'),
      "残留 udp 状态下 iOS 直接渲染 UDP 面板（不回退、无陈旧控件）");
    await drain();
  }

  // ===== 3. scoped 警告生命周期 =====
  {
    const page = createUdpPage({
      profiles: [makeProfile(3, "mac_workstation", "scoped")],
      udpExceptions: [
        { id: 11, profile_id: 3, match_type: "process", match_value_masked: "EX****01",
          display_name: "Example TV", enabled: true, sort_order: 0 },
      ],
    });
    await page.sandbox.__hooks.loadData();
    const warn = regUdpTab(page, 3);
    // 3a. 删除唯一启用例外 → 警告恢复
    await page.sandbox.__hooks.deleteUdpException(11, 3, makeElement());
    await drain();
    assert.equal(page.udpExceptions.length, 0, "例外已删除");
    assert.equal(warn.style.display, "", "禁用/删除最后一个启用例外后警告恢复");
    // 3b. 添加启用例外 → 警告消失
    const { valueInput } = regUdpDialog(page);
    page.sandbox.__hooks.showAddUdpException(3);
    valueInput.value = "ExampleVideo";
    await page.sandbox.__hooks.saveUdpException(makeElement());
    await drain();
    assert.equal(page.udpExceptions.length, 1, "例外已创建");
    assert.equal(warn.style.display, "none", "添加启用例外后警告消失");
    // 3c. 禁用唯一例外 → 警告恢复
    const excId = page.udpExceptions[0].id;
    await page.sandbox.__hooks.toggleUdpException(excId, false, 3);
    await drain();
    assert.equal(warn.style.display, "", "禁用最后一个启用例外后警告恢复");
    // 3d. 重新启用 → 消失
    await page.sandbox.__hooks.toggleUdpException(excId, true, 3);
    await drain();
    assert.equal(warn.style.display, "none", "重新启用后警告消失");
  }
  {
    // strict 档案：空列表也不警告
    const page = createUdpPage({ profiles: [makeProfile(4, "mac_workstation", "strict")] });
    await page.sandbox.__hooks.loadData();
    const warn = regUdpTab(page, 4);
    await page.sandbox.__hooks.loadUdpExceptions(4);
    await drain();
    assert.equal(warn.style.display, "none", "strict 档案不显示 scoped 警告");
  }

  // ===== 4. 列表 DOM 构建 + 持久 XSS 防护 =====
  {
    // 4a. 恶意 display_name：原样文本显示，行内无 IMG/SCRIPT、无事件属性
    const payload = `TV's <img src=x onerror="window.__xss=1"> <script>1</script>`;
    const page = createUdpPage({
      profiles: [makeProfile(5, "mac_workstation", "scoped")],
      udpExceptions: [
        { id: 21, profile_id: 5, match_type: "process", match_value_masked: "EX****01",
          display_name: payload, enabled: true, sort_order: 1 },
      ],
    });
    await page.sandbox.__hooks.loadData();
    regUdpTab(page, 5);
    const tbody = page.elements.get("udp-list-5");
    await page.sandbox.__hooks.loadUdpExceptions(5);
    await drain();
    assert.equal(tbody.children.length, 1, "渲染一行");
    const row = tbody.children[0];
    const acc = inspectTree(row);
    // 结构仅 tr/td/code/span/button：无 IMG、无 SCRIPT
    assert.deepEqual(acc.tags.filter(t => t === "IMG" || t === "SCRIPT"), [],
      "行内不产生 IMG/SCRIPT 元素");
    assert.ok(acc.tags.includes("TR") && acc.tags.includes("TD") && acc.tags.includes("BUTTON"),
      "行由基础元素构成");
    assert.equal(acc.onclickAttrs, 0, "无内联 onclick 属性");
    assert.equal(acc.datasets, 0, "无 data-* 属性承载服务端值");
    // display_name 原样保留为文本（未截断未执行）
    const nameTd = row.children[1];
    assert.equal(nameTd.textContent, payload, "恶意 display_name 原样文本显示");
    const maskedTd = row.children[0];
    assert.equal(maskedTd.children[0].textContent, "EX****01", "masked 值经 code.textContent");
    // 4b. 按钮回调闭包可触发且指向正确的例外
    const actionsTd = row.children[4];
    const delBtn = actionsTd.children[2];
    assert.ok(delBtn._handlers.click && delBtn._handlers.click.length === 1,
      "删除按钮经 addEventListener 绑定（无 onclick 属性）");
    assert.equal(delBtn.textContent, "删除");
  }
  {
    // 4c. 行操作回调：edit 打开弹窗并携带服务端视图；toggle 发 PUT
    const page = createUdpPage({
      profiles: [makeProfile(5, "mac_workstation", "scoped")],
      udpExceptions: [
        { id: 22, profile_id: 5, match_type: "process", match_value_masked: "EX****01",
          display_name: "Example TV", enabled: true, sort_order: 0 },
      ],
    });
    await page.sandbox.__hooks.loadData();
    regUdpTab(page, 5);
    regUdpDialog(page);
    const tbody = page.elements.get("udp-list-5");
    await page.sandbox.__hooks.loadUdpExceptions(5);
    await drain();
    const row = tbody.children[0];
    row.children[4].children[0]._handlers.click[0]();       // 编辑
    assert.equal(page.elements.get("udp-exc-edit-id").value, 22, "编辑回调携带例外 id");
    assert.equal(page.elements.get("udp-exc-current-masked").textContent, "EX****01",
      "编辑回调携带脱敏值");
    await row.children[4].children[1]._handlers.click[0](); // 禁用 toggle
    await drain();
    assert.equal(page.calls.put[page.calls.put.length - 1].body.enabled, false,
      "toggle 回调发出 PUT {enabled:false}");
  }

  // ===== 5. create：载荷形状 + 成功后清空与重载 =====
  {
    const page = createUdpPage({ profiles: [makeProfile(3, "mac_workstation", "scoped")] });
    await page.sandbox.__hooks.loadData();
    regUdpTab(page, 3);
    const { valueInput } = regUdpDialog(page);
    const nameInput = page.elements.get("udp-exc-display-name");
    const enabledEl = page.elements.get("udp-exc-enabled");
    const sortEl = page.elements.get("udp-exc-sort");
    page.sandbox.__hooks.showAddUdpException(3);
    assert.equal(valueInput.value, "", "新建打开时输入空白");
    assert.equal(valueInput.placeholder, "例：ExampleVideo", "workstation 新建占位为进程名示例");
    valueInput.value = "ExampleVideo";
    nameInput.value = "Example TV";
    enabledEl.checked = true;
    sortEl.value = "7";
    await page.sandbox.__hooks.saveUdpException(makeElement());
    await drain();
    const post = page.calls.post.find(c => c.url.includes("/udp-exceptions"));
    assert.ok(post, "发出创建 POST");
    assert.equal(post.body.match_type, "process");
    assert.equal(post.body.match_value, "ExampleVideo");
    assert.equal(post.body.display_name, "Example TV");
    assert.equal(post.body.enabled, true);
    assert.equal(post.body.sort_order, 7);
    assert.equal(valueInput.value, "", "保存成功后完整值立即清空");
    assert.ok(page.calls.toasts.some(t => t.type !== "error"), "成功 toast");
    // 新建默认 sort = 当前例外数
    page.sandbox.__hooks.showAddUdpException(3);
    assert.ok(sortEl.value == 1, `新增默认排序 = 已有例外数（实际 ${sortEl.value}）`);
  }

  // ===== 6. create 必填：空值不发请求 =====
  {
    const page = createUdpPage({ profiles: [makeProfile(3, "mac_workstation", "scoped")] });
    await page.sandbox.__hooks.loadData();
    regUdpTab(page, 3);
    const { valueInput } = regUdpDialog(page);
    page.sandbox.__hooks.showAddUdpException(3);
    valueInput.value = "   ";
    await page.sandbox.__hooks.saveUdpException(makeElement());
    await drain();
    assert.equal(page.calls.post.filter(c => c.url.includes("/udp-exceptions")).length, 0,
      "空匹配值不发创建请求");
    assert.ok(page.calls.toasts.some(t => t.type === "error"), "必填错误提示");
  }

  // ===== 7. edit：覆盖式输入永不回填 + 留空/覆盖两种 PUT =====
  {
    const page = createUdpPage({ profiles: [makeProfile(3, "mac_gateway", "scoped")] });
    await page.sandbox.__hooks.loadData();
    regUdpTab(page, 3);
    const { valueInput } = regUdpDialog(page);
    const currentRow = page.elements.get("udp-exc-current-row");
    const currentMasked = page.elements.get("udp-exc-current-masked");
    const nameInput = page.elements.get("udp-exc-display-name");
    page.sandbox.__hooks.editUdpException(
      { id: 31, match_type: "mac_address", match_value_masked: "02****01",
        display_name: "Example TV", enabled: true, sort_order: 2 }, 3);
    assert.equal(valueInput.value, "", "编辑打开时完整值输入永远空白");
    assert.equal(valueInput.placeholder, "留空保持原值", "编辑占位为保持语义");
    assert.equal(currentRow.style.display, "", "编辑显示当前脱敏值行");
    assert.equal(currentMasked.textContent, "02****01", "脱敏当前值以 textContent 渲染");
    // 7a. 留空保存：PUT 不含 match_value / match_type
    nameInput.value = "Example TV 2";
    await page.sandbox.__hooks.saveUdpException(makeElement());
    await drain();
    const put1 = page.calls.put[page.calls.put.length - 1];
    assert.ok(put1.url.includes("/udp-exceptions/31"), "更新打到例外端点");
    assert.ok(!("match_value" in put1.body) && !("match_type" in put1.body),
      "留空时 PATCH 形状不含匹配值键");
    assert.equal(put1.body.display_name, "Example TV 2");
    assert.equal(valueInput.value, "", "保存后完整值输入清空");
    // 7b. 输入新值：PUT 含 match_value + 目标决定的 match_type
    page.sandbox.__hooks.editUdpException(
      { id: 31, match_type: "mac_address", match_value_masked: "02****01",
        display_name: null, enabled: true, sort_order: 2 }, 3);
    valueInput.value = "02-00-00-00-00-01";
    await page.sandbox.__hooks.saveUdpException(makeElement());
    await drain();
    const put2 = page.calls.put[page.calls.put.length - 1];
    assert.equal(put2.body.match_value, "02-00-00-00-00-01", "输入新值才提交匹配值");
    assert.equal(put2.body.match_type, "mac_address", "gateway 类型为 mac_address");
  }

  // ===== 8. 失败路径：清空敏感输入 + 重载脱敏列表 + 稳定错误 =====
  {
    const page = createUdpPage({
      profiles: [makeProfile(3, "mac_workstation", "scoped")], putFails: true });
    await page.sandbox.__hooks.loadData();
    regUdpTab(page, 3);
    const { valueInput } = regUdpDialog(page);
    const beforeGets = page.calls.udpGets;   // renderUdpTab 初次渲染已 GET 一次
    page.sandbox.__hooks.editUdpException(
      { id: 41, match_type: "process", match_value_masked: "EX****01",
        display_name: null, enabled: true, sort_order: 0 }, 3);
    valueInput.value = "ExampleNew";
    await page.sandbox.__hooks.saveUdpException(makeElement());
    await drain();
    assert.equal(valueInput.value, "", "失败后完整值输入立即清空");
    assert.ok(page.calls.toasts.some(t => t.type === "error" && t.msg === "服务暂时不可用，请稍后重试"),
      "失败呈现服务端稳定错误文案（503 静态 detail，不含输入值）");
    assert.equal(page.calls.udpGets, beforeGets + 1,
      "失败后重载脱敏列表（对 /udp-exceptions 再次 GET）");
  }

  // ===== 9. 弹窗清空 observer：单一持久、无累积、关闭即清空 =====
  {
    const page = createUdpPage({ profiles: [makeProfile(3, "mac_workstation", "scoped")] });
    await page.sandbox.__hooks.loadData();
    regUdpTab(page, 3);
    const { dlg, valueInput } = regUdpDialog(page);
    // 第一次打开：创建 observer
    page.sandbox.__hooks.showAddUdpException(3);
    valueInput.value = "ExampleLeak1";
    dlg.classList.remove("show");          // 模拟任意关闭路径的 class 变化
    page.fireObservers(dlg);
    assert.equal(valueInput.value, "", "第一次关闭即清空完整值");
    assert.equal(dlg._observers.length, 1, "仅一个 observer（单一持久）");
    assert.equal(dlg._observers[0].alive, true, "持久 observer 未被 disconnect");
    // 多轮开关：不累积，仍即清空
    for (let i = 0; i < 3; i++) {
      page.sandbox.__hooks.showAddUdpException(3);
      valueInput.value = `ExampleLeak${i + 2}`;
      page.sandbox.__hooks.editUdpException(
        { id: 1, match_type: "process", match_value_masked: "EX****01",
          display_name: null, enabled: true, sort_order: 0 }, 3);
      dlg.classList.remove("show");
      page.fireObservers(dlg);
      assert.equal(valueInput.value, "", `第 ${i + 2} 次关闭即清空`);
    }
    assert.equal(dlg._observers.length, 1, "多轮开关后 observer 仍只有一个（无累积）");
    assert.ok(dlg._observers.every(o => o.alive), "全部 observer 存活（单一持久设计）");
    page.sandbox.__hooks.closeUdpExceptionDialog();
    page.fireObservers(dlg);
    assert.equal(valueInput.value, "", "closeUdpExceptionDialog 路径同样清空");
  }

  // ===== 10. delete：确认 + 双击防重（用户可见路径：按钮 click handler）=====
  {
    const page = createUdpPage({
      profiles: [makeProfile(3, "mac_workstation", "scoped")],
      confirm: true,
      udpExceptions: [
        { id: 51, profile_id: 3, match_type: "process", match_value_masked: "EX****01",
          display_name: null, enabled: true, sort_order: 0 },
      ],
    });
    await page.sandbox.__hooks.loadData();
    regUdpTab(page, 3);
    const tbody = page.elements.get("udp-list-3");
    await page.sandbox.__hooks.loadUdpExceptions(3);
    await drain();
    const delBtn = tbody.children[0].children[4].children[2];
    const handlers = () => delBtn._handlers.click || [];
    assert.equal(handlers().length, 1, "删除按钮单个 click handler");
    // 连续两次触发 click（模拟双击）：第一击同步进入 withLoading 即置 disabled，
    // 第二击被 handler 的 disabled 守卫直接吞掉
    handlers().forEach(h => h());
    handlers().forEach(h => h());
    await drain();
    assert.equal(page.calls.del.length, 1, "双击仅发出 1 个 DELETE");
    assert.equal(page.calls.del[0].includes("/udp-exceptions/51"), true);
    assert.equal(page.udpExceptions.length, 0, "例外已删除");
    assert.equal(delBtn.disabled, false, "结束后按钮恢复可用");
  }
  {
    // 确认取消：不发删除
    const page = createUdpPage({ confirm: false });
    await page.sandbox.__hooks.deleteUdpException(52, 3, makeElement());
    await drain();
    assert.equal(page.calls.del.length, 0, "取消确认不发删除");
  }

  // ===== 11. 模式保存：PUT 档案端点 + 成功后重载 =====
  {
    const page = createUdpPage({ profiles: [makeProfile(3, "mac_workstation", "scoped")] });
    await page.sandbox.__hooks.loadData();
    regUdpTab(page, 3);
    const sel = page.elements.get("udp-mode-3");
    sel.value = "strict";
    await page.sandbox.__hooks.saveUdpMode(3, makeElement());
    await drain();
    const put = page.calls.put.find(c => c.url === "/api/configs/profiles/3");
    assert.ok(put, "模式保存打到档案端点");
    assert.equal(put.body.udp_security_mode, "strict", "载荷为新模式");
    assert.equal(Object.keys(put.body).length, 1, "载荷只含模式字段");
    assert.ok(page.calls.toasts.some(t => t.msg === "已更新"), "保存成功提示");
  }

  console.log("test_udp_exception_ui: all assertions passed");
})().catch((e) => { console.error(e); process.exit(1); });
