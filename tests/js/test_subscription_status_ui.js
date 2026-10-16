/** 订阅状态 UI 行为测试（Task 4）
 *  （node vm + DOM stub，实际执行 proxies.js / index.js 函数，非源文本断言）
 *
 * 覆盖：
 *  - 渲染安全：订阅详情卡全部 DOM API 构建，name/tag/error 只进 textContent；
 *    恶意 payload（<img onerror> / <script> / 引号）原样为文本，树中无
 *    IMG/SCRIPT 元素、按钮无 onclick 属性、服务端值不进任何内联事件；
 *    颜色仅接受 #RRGGBB 否则 fallback，绝不进 style
 *  - 选中订阅渲染不发 detail URL（列表无 URL）；仅 editSub 请求并完整回填
 *    name/tag/color/url/enabled/auto_update/update_interval
 *  - 状态优先级 disabled > failed > never_synced > due > healthy；
 *    失败优先显示且仍保留上次成功
 *  - 时间：缓存单一 Intl.DateTimeFormat；null/invalid 安全占位；
 *    缺时区后缀按 UTC 解释
 *  - 错误码本地化：五码各有文案，未知码收敛 internal_error 文案
 *  - 保存失败不丢用户输入（modal 不关闭、输入保留）
 *  - 首页聚合：并行请求 status-summary 并以 textContent 写入四状态量
 *
 * 运行：node tests/js/test_subscription_status_ui.js
 */

"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const REPO_ROOT = path.resolve(__dirname, "..", "..");
const PROXIES_SRC = fs.readFileSync(path.join(REPO_ROOT, "app/static/js/proxies.js"), "utf8");
const INDEX_SRC = fs.readFileSync(path.join(REPO_ROOT, "app/static/js/index.js"), "utf8");

// ===== 最小 i18n（缺失键渲染 undefined 会被断言捕获）=====
const I18N_STUB = {
  "sub.empty": "还没有添加订阅源",
  "sub.not_synced": "未同步",
  "sub.not_scheduled": "未排期",
  "sub.time.invalid": "时间不可用",
  "sub.last_attempt": "上次尝试",
  "sub.last_success": "上次同步",
  "sub.next_update": "下次计划",
  "sub.node_count": "节点数",
  "sub.error_title": "上次错误",
  "sub.err.timeout": "拉取超时",
  "sub.err.http_status": "订阅源响应异常",
  "sub.err.invalid_content": "订阅内容无法解析",
  "sub.err.network_error": "网络错误",
  "sub.err.internal_error": "内部错误",
  "sub.status.due": "待同步",
  "sub.status.healthy": "正常",
  "sub.status.failed": "同步失败",
  "sub.status.never_synced": "从未同步",
  "sub.status.disabled": "已禁用",
  "btn.sync": "同步", "btn.edit": "编辑", "btn.delete": "删除", "btn.add": "添加",
  "btn.save": "保存",
  "msg.updated": "已更新", "msg.added": "已添加", "msg.required": "必填",
  "sub.sync_done": "已同步 {0} 个节点",
  "sub.sync_all_done": "同步完成: {0} 个订阅",
  "sub.confirm_delete": '确定删除订阅 "{0}"？相关节点也会被删除。',
  "proxies.search_placeholder": "搜索节点...",
  "proxies.all_nodes": "全部节点",
  "proxies.section_subs": "订阅源",
  "proxies.section_regions": "地区组",
  "proxies.section_services": "服务组",
  "proxies.new_group": "新建分组",
  "node.col_name": "节点名称", "node.col_type": "协议",
  "node.col_region": "地区", "node.col_pinned": "绑定服务",
  "node.col_pinned_tip": "提示", "node.col_action": "操作",
  "node.empty": "暂无节点", "node.total_count": "共 {0} 个节点",
  "node.pin_status": "绑定状态", "node.not_pinned": "未绑定", "node.has_pinned": "已绑定",
  "node.all_regions": "全部地区", "node.all_subs": "全部订阅",
  "node.no_region": "无地区", "node.pin_service_sep": "--", "node.pin_to": "钉到 {0}",
};

// ===== DOM stub =====

function makeElement(initial) {
  const el = {
    tagName: "", type: "", value: "", placeholder: "", checked: false,
    textContent: "", innerHTML: "", className: "", style: {}, dataset: {},
    onclick: null, disabled: false, children: [], _handlers: {}, _attrs: {},
    focus() {}, select() {}, setSelectionRange() {},
    appendChild(child) { el.children.push(child); return child; },
    append(...nodes) { nodes.flat().forEach(n => el.children.push(n)); return el; },
    replaceChildren(...nodes) { el.children = [...nodes]; },
    addEventListener(evt, fn) { (el._handlers[evt] = el._handlers[evt] || []).push(fn); },
    removeEventListener() {},
    setAttribute(k, v) { el._attrs[k] = String(v); },
    getAttribute(k) { return k in el._attrs ? el._attrs[k] : null; },
    querySelector() { return null; }, querySelectorAll() { return []; },
    closest() { return null; }, remove() {},
  };
  const classList = {
    contains(c) { return el.className.split(/\s+/).filter(Boolean).includes(c); },
    add(c) { if (!classList.contains(c)) el.className = el.className ? `${el.className} ${c}` : c; },
    remove(c) { el.className = el.className.split(/\s+/).filter(x => x && x !== c).join(" "); },
    toggle(c) { classList.contains(c) ? classList.remove(c) : classList.add(c); },
  };
  el.classList = classList;
  return Object.assign(el, initial);
}

/** 深度遍历：标签集合 / textContent 全文 / onclick 属性计数 / dataset 计数 */
function inspectTree(el, acc = { tags: [], text: "", onclickAttrs: 0, datasets: 0 }) {
  if (!el || typeof el !== "object") return acc;
  if (el.tagName) acc.tags.push(el.tagName.toUpperCase());
  if (el.onclick !== null && el.onclick !== undefined) acc.onclickAttrs += 1;
  if (el.dataset && Object.keys(el.dataset).length > 0) acc.datasets += 1;
  if (typeof el.textContent === "string") acc.text += el.textContent + "\n";
  (el.children || []).forEach(c => inspectTree(c, acc));
  return acc;
}

function findFirst(el, predicate) {
  if (!el || typeof el !== "object") return null;
  if (predicate(el)) return el;
  for (const c of (el.children || [])) {
    const hit = findFirst(c, predicate);
    if (hit) return hit;
  }
  return null;
}

/** 可控 Intl.DateTimeFormat stub：记录构造次数，format 返回可断言标记 */
class IntlDateTimeFormatStub {
  static constructed = 0;
  constructor(localeArg, optionsArg) {
    IntlDateTimeFormatStub.constructed += 1;
    this.localeArg = localeArg;
    this.optionsArg = optionsArg;
  }
  format(ts) { return `T:${new Date(ts).toISOString()}`; }
}

/** 独立 proxies.js context；subs 为列表响应（无 URL），detailUrls 模拟详情端点 */
function createProxiesPage({ subs = [], detailUrls = {}, putFails = false } = {}) {
  const elements = new Map();
  const lazyEls = new Map(); // init 全流程会触达的页面骨架元素（真实页面始终存在）
  const calls = { gets: [], puts: [], posts: [], toasts: [] };
  let detailRequests = 0;

  const documentStub = {
    documentElement: { lang: "zh" },
    body: makeElement(),
    getElementById: (id) => {
      if (elements.has(id)) return elements.get(id);
      if (!lazyEls.has(id)) lazyEls.set(id, makeElement());
      return lazyEls.get(id);
    },
    querySelector: () => null,
    querySelectorAll: () => [],
    createElement: (tag) => makeElement({ tagName: tag }),
    addEventListener: () => {},
  };

  const API = {
    async get(url) {
      calls.gets.push(url);
      if (url === "/api/subscriptions") return subs;
      if (url === "/api/nodes/by-region") return { groups: {}, unassigned: [] };
      if (url === "/api/groups/regions" || url === "/api/groups/services") return [];
      if (url.startsWith("/api/nodes")) return [];
      const m = url.match(/^\/api\/subscriptions\/(\d+)$/);
      if (m) {
        detailRequests += 1;
        return detailUrls[Number(m[1])] ?? { url: `https://detail.example.com/${m[1]}` };
      }
      return [];
    },
    async put(url, body) {
      calls.puts.push({ url, body });
      if (putFails) throw new Error("保存失败");
      return {};
    },
    async post(url, body) { calls.posts.push({ url, body }); return {}; },
    async del(url) { return {}; },
  };

  const escapeHtml = (s) => String(s)
    .replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;").replaceAll("'", "&#39;");

  const sandbox = {
    document: documentStub,
    window: { innerWidth: 1024, addEventListener: () => {} },
    location: { hash: "", pathname: "/", search: "" },
    history: { replaceState: () => {} },
    Intl: { DateTimeFormat: IntlDateTimeFormatStub },
    I18N: I18N_STUB, API,
    escapeHtml, translateName: (s) => s,
    toast: (msg, type) => calls.toasts.push({ msg, type }),
    showModal: (id) => elements.get(id)?.classList.add("show"),
    hideModal: (id) => elements.get(id)?.classList.remove("show"),
    confirmDialog: async () => true,
    withLoading: async (btn, fn) => { try { return await fn(); } finally { if (btn) btn.disabled = false; } },
    showLoading: () => {}, hideLoading: () => {}, flashInput: () => {},
    compactActions: () => {}, console,
    setTimeout, clearTimeout,
  };
  vm.createContext(sandbox);
  vm.runInContext(PROXIES_SRC + `
;globalThis.__hooks = { renderSubDetail, editSub, saveSubscription, subStatus,
  formatSubTime, safeSubColor, subErrorCodeKey, buildStatusBadge, setSelection };`,
    sandbox, { filename: "proxies.js" });

  const reg = (id, initial) => { const el = makeElement(initial); elements.set(id, el); return el; };
  return { sandbox, elements, reg, calls, get detailRequests() { return detailRequests; } };
}

const drain = () => new Promise((r) => setTimeout(r, 0));

function clickHandler(el) {
  const handlers = el._handlers.click || [];
  assert.ok(handlers.length >= 1, "按钮应至少绑定一个 click handler（addEventListener）");
  return handlers[0];
}

// ===== 基础元素（编辑弹窗）=====
function regModalEls(page) {
  page.reg("modal-title");
  page.reg("edit-id");
  page.reg("sub-name");
  page.reg("sub-tag");
  page.reg("sub-color", { value: "" });
  page.reg("sub-url");
  page.reg("sub-enabled", { checked: true });
  page.reg("sub-auto-update", { checked: true });
  page.reg("sub-interval", { value: "86400" });
  page.reg("sub-modal");
}

// =====================================================================
// 1. 渲染安全：恶意 name/tag/error 只文本；无元素注入；颜色 fallback
// =====================================================================

(async function testRenderSafeAgainstMaliciousServerValues() {
  const malicious = {
    id: 1,
    name: '<img src=x onerror="__xss=1">',
    tag: "TV's <script>alert(1)</script>",
    color: "javascript:alert(1)",
    enabled: true, auto_update: false,
    update_interval: 3600,
    last_attempt_at: "2026-10-16T01:02:03Z",
    last_success_at: "2026-10-15T08:00:00Z",
    next_update_at: null,
    last_error_code: "<script>boom</script>",
    last_node_count: 5,
  };
  const page = createProxiesPage({ subs: [malicious] });
  const main = page.reg("px-main");
  page.reg("px-nav");
  regModalEls(page);

  await drain(); await drain(); // 等 init/loadCore 完成
  page.sandbox.__hooks.renderSubDetail(1);
  await drain(); await drain();

  const acc = inspectTree(main);
  // 恶意标签不成为元素；payload 原样是文本
  assert.ok(!acc.tags.includes("IMG"), "恶意 name 不得变成 IMG 元素");
  assert.ok(!acc.tags.includes("SCRIPT"), "恶意 tag 不得变成 SCRIPT 元素");
  assert.ok(acc.text.includes('<img src=x onerror="__xss=1">'), "恶意 name 应作为纯文本显示");
  assert.ok(acc.text.includes("TV's <script>alert(1)</script>"), "恶意 tag 应作为纯文本显示");
  // 按钮无 onclick 属性（addEventListener 闭包传值），无 data-* 携带服务端值
  assert.equal(acc.onclickAttrs, 0, "详情卡内不得有内联 onclick 属性");
  assert.equal(acc.datasets, 0, "详情卡内不得用 data-* 携带服务端值");
  // 颜色 fallback：非法色不进 style
  const dot = findFirst(main, el => el.className === "sub-color-dot");
  assert.ok(dot, "应渲染颜色圆点");
  assert.equal(dot.style.background, "#6c5ce7", "非法颜色必须 fallback");
  // 未知错误码收敛 internal_error 文案，绝不原样回显
  assert.ok(acc.text.includes(I18N_STUB["sub.err.internal_error"]), "未知错误码应显示 internal_error 文案");
  assert.ok(!acc.text.includes("<script>boom</script>"), "脏错误码不得原样显示");
  console.log("✓ 恶意服务端值只文本、无元素注入、颜色 fallback");
})();

// =====================================================================
// 2. 渲染不发 detail URL；editSub 才请求并完整回填
// =====================================================================

(async function testDetailUrlOnlyOnEdit() {
  const sub = {
    id: 7, name: "机场A", tag: "主力", color: "#E17055",
    enabled: false, auto_update: false, update_interval: 7200,
    last_attempt_at: null, last_success_at: null,
    next_update_at: null, last_error_code: null, last_node_count: null,
  };
  const page = createProxiesPage({
    subs: [sub],
    detailUrls: { 7: { url: "https://provider.example.com/secret" } },
  });
  const main = page.reg("px-main");
  page.reg("px-nav");
  regModalEls(page);

  // 选中渲染：只有列表请求，无 detail URL
  await drain(); await drain();
  page.sandbox.__hooks.renderSubDetail(7);
  await drain(); await drain();
  assert.equal(page.detailRequests, 0, "渲染订阅详情卡不得请求 detail URL");
  const mainText = inspectTree(main).text;
  assert.ok(!mainText.includes("provider.example.com"), "详情卡不得展示 URL");

  // 点击编辑：请求 detail，完整回填全部字段（含此前遗漏的 enabled/auto/interval）
  await page.sandbox.__hooks.editSub(sub);
  await drain();
  assert.equal(page.detailRequests, 1, "编辑时按需请求 detail URL");
  assert.equal(page.elements.get("edit-id").value, 7);
  assert.equal(page.elements.get("sub-name").value, "机场A");
  assert.equal(page.elements.get("sub-tag").value, "主力");
  assert.equal(page.elements.get("sub-color").value, "#e17055", "合法颜色应回填（收敛小写）");
  assert.equal(page.elements.get("sub-url").value, "https://provider.example.com/secret");
  assert.equal(page.elements.get("sub-enabled").checked, false, "enabled 必须回填");
  assert.equal(page.elements.get("sub-auto-update").checked, false, "auto_update 必须回填");
  assert.equal(page.elements.get("sub-interval").value, 7200, "update_interval 必须回填");
  assert.ok(page.elements.get("sub-modal").classList.contains("show"));
  console.log("✓ 渲染不发 detail URL；编辑才请求并完整回填");
})();

// =====================================================================
// 3. 状态优先级 disabled > failed > never_synced > due > healthy
// =====================================================================

(async function testStatusPrecedence() {
  const past = new Date(Date.now() - 3600_000).toISOString();
  const future = new Date(Date.now() + 3600_000).toISOString();
  const cases = [
    // 禁用：即使 error+due 全占，也显示 disabled
    { sub: { enabled: false, auto_update: true, next_update_at: past,
             last_success_at: past, last_error_code: "timeout" }, expect: "disabled" },
    // failed：error 非空优先（即使已到期且曾成功）
    { sub: { enabled: true, auto_update: true, next_update_at: past,
             last_success_at: past, last_error_code: "http_status" }, expect: "failed" },
    // never_synced：无 error 且从未成功（即使已到期）
    { sub: { enabled: true, auto_update: true, next_update_at: past,
             last_success_at: null, last_error_code: null }, expect: "never_synced" },
    // due：曾成功、无错误、到期
    { sub: { enabled: true, auto_update: true, next_update_at: past,
             last_success_at: past, last_error_code: null }, expect: "due" },
    // healthy：曾成功、无错误、未到期
    { sub: { enabled: true, auto_update: true, next_update_at: future,
             last_success_at: past, last_error_code: null }, expect: "healthy" },
    // auto off + 未排期 + 曾成功 → healthy（next null 不计 due）
    { sub: { enabled: true, auto_update: false, next_update_at: null,
             last_success_at: past, last_error_code: null }, expect: "healthy" },
  ];
  const page = createProxiesPage({ subs: [] });
  const { subStatus } = page.sandbox.__hooks;
  for (const { sub, expect } of cases) {
    assert.equal(subStatus(sub), expect, JSON.stringify(sub));
  }
  console.log("✓ 状态优先级 disabled > failed > never_synced > due > healthy");
})();

// =====================================================================
// 4. 失败优先显示本地化错误码，且仍显示上次成功
// =====================================================================

(async function testFailedShowsLocalizedErrorAndKeepsSuccess() {
  const sub = {
    id: 3, name: "失败订阅", tag: null, color: "#0984e3",
    enabled: true, auto_update: true, update_interval: 3600,
    last_attempt_at: "2026-10-16T12:00:00Z",
    last_success_at: "2026-10-01T08:00:00Z",
    next_update_at: "2026-10-16T12:15:00Z",
    last_error_code: "timeout",
    last_node_count: 12,
  };
  const page = createProxiesPage({ subs: [sub] });
  const main = page.reg("px-main");
  page.reg("px-nav");
  await drain(); await drain();
  page.sandbox.__hooks.renderSubDetail(3);
  await drain(); await drain();

  const acc = inspectTree(main);
  const badge = findFirst(main, el => (el.className || "").includes("status-badge"));
  assert.ok(badge, "应渲染状态徽章");
  assert.ok(badge.className.includes("status-failed"), "error 非空时状态为 failed");
  assert.ok(acc.text.includes(I18N_STUB["sub.err.timeout"]), "timeout 错误码应显示本地化文案");
  assert.ok(!acc.text.includes("timeout"), "不得显示原始错误码字符串");
  // 失败仍保留上次成功（历史成功有参考价值）
  assert.ok(acc.text.includes(I18N_STUB["sub.last_success"]), "失败时应仍显示上次成功");
  assert.ok(acc.text.includes("T:2026-10-01T08:00:00.000Z"), "上次成功时间应被本地化格式化");
  // 节点数
  assert.ok(acc.text.includes("12"), "应显示上次成功节点数");
  console.log("✓ 失败显示本地化错误码并保留上次成功/节点数");
})();

// =====================================================================
// 5. 时间格式化：缓存、占位、UTC 语义
// =====================================================================

(async function testFormatSubTime() {
  const page = createProxiesPage({ subs: [] });
  const { formatSubTime } = page.sandbox.__hooks;
  IntlDateTimeFormatStub.constructed = 0;

  // null → 空值占位（调用方指定语义键）
  assert.equal(formatSubTime(null, "sub.not_synced"), "未同步");
  assert.equal(formatSubTime(undefined, "sub.not_scheduled"), "未排期");
  // 非法值 → 时间不可用
  assert.equal(formatSubTime("not-a-date", "sub.not_synced"), "时间不可用");
  // 带 Z：按 UTC 瞬间格式化
  assert.equal(formatSubTime("2026-10-17T08:00:00Z", "sub.not_synced"), "T:2026-10-17T08:00:00.000Z");
  // 缺时区后缀：同样按 UTC 解释（不按本地时区漂移）
  assert.equal(formatSubTime("2026-10-17T08:00:00", "sub.not_synced"), "T:2026-10-17T08:00:00.000Z");
  // 缓存：多次调用只构造一个 DateTimeFormat
  for (let i = 0; i < 5; i++) formatSubTime("2026-10-17T08:00:00Z", "sub.not_synced");
  assert.equal(IntlDateTimeFormatStub.constructed, 1, "Intl.DateTimeFormat 应被缓存");
  console.log("✓ 时间格式化：缓存单一 formatter、占位安全、UTC 语义");
})();

// =====================================================================
// 6. 编辑保存失败不丢用户输入
// =====================================================================

(async function testSaveFailureKeepsInputs() {
  const page = createProxiesPage({ subs: [], putFails: true });
  regModalEls(page);
  const nameEl = page.elements.get("sub-name");
  const urlEl = page.elements.get("sub-url");
  const intervalEl = page.elements.get("sub-interval");
  const modal = page.elements.get("sub-modal");
  modal.classList.add("show");

  page.elements.get("edit-id").value = "5"; // 编辑已有订阅 → PUT 失败路径
  nameEl.value = "用户输入的名称";
  urlEl.value = "https://user-typed.example.com/sub";
  intervalEl.value = "1800";
  page.sandbox.__hooks.saveSubscription();
  await drain(); await drain();

  assert.ok(page.calls.toasts.some(t => t.type === "error"), "保存失败应 toast 错误");
  assert.ok(modal.classList.contains("show"), "保存失败后弹窗不得关闭");
  assert.equal(nameEl.value, "用户输入的名称", "失败后名称输入保留");
  assert.equal(urlEl.value, "https://user-typed.example.com/sub", "失败后 URL 输入保留");
  assert.equal(intervalEl.value, "1800", "失败后间隔输入保留");
  console.log("✓ 保存失败不丢用户输入");
})();

// =====================================================================
// 7. 按钮事件：addEventListener 闭包传 sub 对象（无字符串注入路径）
// =====================================================================

(async function testButtonsUseClosures() {
  const sub = {
    id: 9, name: '名"; onclick="alert(1)', tag: null, color: "#00b894",
    enabled: true, auto_update: false, update_interval: 3600,
    last_attempt_at: null, last_success_at: "2026-10-10T00:00:00Z",
    next_update_at: null, last_error_code: null, last_node_count: 1,
  };
  const page = createProxiesPage({ subs: [sub], detailUrls: { 9: { url: "https://d.example/9" } } });
  const main = page.reg("px-main");
  page.reg("px-nav");
  regModalEls(page);

  await drain(); await drain();
  page.sandbox.__hooks.renderSubDetail(9);
  await drain(); await drain();

  const buttons = [];
  (function collect(el) {
    if (!el || typeof el !== "object") return;
    if (typeof el.tagName === "string" && el.tagName.toUpperCase() === "BUTTON") buttons.push(el);
    (el.children || []).forEach(collect);
  })(main);
  assert.equal(buttons.length, 3, "详情卡应有 同步/编辑/删除 三按钮");
  for (const btn of buttons) {
    assert.equal(btn.onclick, null, "按钮不得用内联 onclick");
    assert.ok((btn._handlers.click || []).length === 1, "按钮应经 addEventListener 绑定");
    assert.equal(typeof btn.textContent, "string");
  }
  // 编辑按钮触发 editSub（detail 请求发出）——闭包传 sub 对象而非序列化字符串
  const editBtn = buttons.find(b => b.textContent === "编辑");
  await clickHandler(editBtn)();
  await drain();
  assert.equal(page.detailRequests, 1, "编辑按钮闭包应触发 detail 请求");
  assert.equal(page.elements.get("sub-name").value, sub.name, "恶意名称经闭包安全回填");
  // innerHTML 模板中不含序列化对象痕迹
  const tableCard = main.children.find(c => (c.innerHTML || "").includes("node-table"));
  assert.ok(tableCard, "节点表外壳存在");
  assert.ok(!tableCard.innerHTML.includes("onclick=\"editSub("), "不得有 JSON.stringify 进 onclick");
  console.log("✓ 按钮经 addEventListener 闭包传值，无字符串注入路径");
})();

// =====================================================================
// 8. 首页聚合：并行请求并写 textContent
// =====================================================================

(async function testDashboardAggregatesViaTextContent() {
  const elements = new Map();
  const calls = { gets: [] };
  const API = {
    async get(url) {
      calls.gets.push(url);
      if (url === "/api/subscriptions") return [{ id: 1 }];
      if (url === "/api/nodes/by-region") return { groups: { "HK": [{}, {}] }, unassigned: [{}] };
      if (url === "/api/groups/regions") return [{}];
      if (url === "/api/groups/services") return [{}, {}];
      if (url === "/api/configs/rules") return [{}];
      if (url === "/api/configs/profiles") return [];
      if (url === "/api/subscriptions/status-summary") {
        return { due: 2, healthy: 5, failed: 1, never_synced: 3 };
      }
      return [];
    },
    async post() { return { results: [] }; },
  };
  const documentStub = {
    getElementById: (id) => elements.get(id) || null,
    createElement: (tag) => makeElement({ tagName: tag }),
  };
  const sandbox = {
    document: documentStub, API, I18N: I18N_STUB,
    toast: () => {}, escapeHtml: (s) => String(s), translateName: (s) => s,
    withLoading: async (btn, fn) => fn(),
    console, setTimeout,
  };
  vm.createContext(sandbox);
  vm.runInContext(INDEX_SRC + `
;globalThis.__hooks = { refreshStats, syncAll };`, sandbox, { filename: "index.js" });

  const ids = ["stat-subs", "stat-nodes", "stat-regions", "stat-services",
    "stat-rules", "stat-profiles", "region-chart",
    "stat-due", "stat-healthy", "stat-failed", "stat-never-synced"];
  for (const id of ids) elements.set(id, makeElement({ children: [] }));

  sandbox.__hooks.refreshStats();
  await drain(); await drain();

  assert.ok(calls.gets.includes("/api/subscriptions/status-summary"), "应请求聚合端点");
  assert.equal(elements.get("stat-due").textContent, 2);
  assert.equal(elements.get("stat-healthy").textContent, 5);
  assert.equal(elements.get("stat-failed").textContent, 1);
  assert.equal(elements.get("stat-never-synced").textContent, 3);
  assert.equal(elements.get("stat-subs").textContent, 1);
  assert.equal(elements.get("stat-nodes").textContent, 3);
  console.log("✓ 首页并行请求聚合并以 textContent 写入四状态量");
})();

// ===== 汇总 =====
process.on("exit", (code) => {
  if (code === 0) console.log("\n全部订阅状态 UI 行为测试通过");
});
