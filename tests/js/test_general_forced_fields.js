/** profiles.js General 元数据按档案绑定 + 强制字段（forced）只读行为测试
 *  （node vm + DOM stub，实际执行脚本函数，非源文本断言）
 *
 * 覆盖（fix round 2 + round 3 + round 4）：
 *  - renderField：服务端 metadata forced === true 时 checkbox/select/textarea/
 *    number/text 全部 disabled + data-forced="1" + badge-warning 徽标；
 *    非 forced 字段不带任何禁用痕迹
 *  - renderGeneralTab（真实 open 路径）：先按 profile_id 请求
 *    /api/general/fields?profile_id=<id> 再渲染/填值；metadata 为后端
 *    general_groups_for_target 真实可达形态（workstation/iOS/gateway 三份
 *    独立响应，非单份全平台混合）
 *  - collectGeneralValues：forced 集合来自当前档案的 metadata（owner 绑定），
 *    不串档——iOS 的 forced 键（auto-suspend/compatibility-mode）在
 *    workstation 档案的 payload 中保留（交后端 422），gateway 同理；
 *    自定义 JSON 重新注入的当前目标 forced 键在合并后移除；
 *    普通 custom 键与表外未知键保留在 payload 交后端校验
 *  - 并发：乱序响应不串档（缓存键即档案 id）；同一档案 inflight 去重
 *  - owner guard：metadata 未加载的档案 collect 直接 fail closed（toast + null）；
 *    saveGeneralValues 保存前先 await 当前档案 metadata 再收集提交
 *  - fillGeneralForm：disabled 控件仍填充服务端强制值（展示）
 *  - I18N stub 为普通对象（非 Proxy 伪造任意键）：缺失翻译会渲染
 *    undefined 并被断言捕获；tags 无 hint 时 placeholder 使用真实键
 *
 * 运行：node tests/js/test_general_forced_fields.js
 */

"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const REPO_ROOT = path.resolve(__dirname, "..", "..");
const SOURCE = fs.readFileSync(path.join(REPO_ROOT, "app/static/js/profiles.js"), "utf8");

// ===== 真实可达 metadata（镜像后端 general_groups_for_target 的输出形态）=====
// profile 1 = mac_workstation（默认档案），2 = ios_mobile，3 = mac_gateway；
// 三份响应独立：forced 集合与字段集合均按目标不同，不存在单份全平台 metadata。

function selectField(key, label_zh, label_en, options, extra) {
  return Object.assign({
    key, label_zh, label_en, type: "select", hint_zh: "", hint_en: "",
    options: options.map(([value, zh, en]) => ({ value, label_zh: zh, label_en: en })),
  }, extra);
}

function boolField(key, label_zh, label_en, extra) {
  return Object.assign({
    key, label_zh, label_en, type: "bool", hint_zh: "", hint_en: "", options: [],
  }, extra);
}

function intField(key, label_zh, label_en, extra) {
  return Object.assign({
    key, label_zh, label_en, type: "int", hint_zh: "", hint_en: "", options: [],
  }, extra);
}

// 全目标公共强制字段：block-quic 收窄为 per-policy 单选项，UDP 行为收窄为 REJECT
const FORCED_BLOCK_QUIC = selectField("block-quic", "QUIC 处理策略", "Block QUIC",
  [["per-policy", "按策略", "Per Policy"]], { forced: true });
const FORCED_UDP_BEHAVIOUR = selectField("udp-policy-not-supported-behaviour",
  "UDP 不支持策略行为", "UDP Unsupported Policy",
  [["REJECT", "拒绝", "Reject"]], { forced: true });
const LOGLEVEL = selectField("loglevel", "日志级别", "Log Level",
  [["notify", "通知", "Notify"], ["warning", "警告", "Warning"]]);
const WIFI_ASSIST = boolField("wifi-assist", "Wi-Fi 辅助", "Wi-Fi Assist");
const TEST_TIMEOUT = intField("test-timeout", "测试超时（秒）", "Test Timeout (sec)");

const WS_META = [  // mac_workstation：Mac-only 在场，无 iOS/gateway 专属键
  { key: "network", label_zh: "网络与代理", label_en: "Network & Proxy",
    fields: [WIFI_ASSIST, TEST_TIMEOUT] },
  { key: "controller", label_zh: "控制器与 API", label_en: "Controller & API",
    fields: [boolField("http-api-tls", "HTTP API TLS", "HTTP API TLS")] },
  { key: "advanced", label_zh: "高级", label_en: "Advanced",
    fields: [LOGLEVEL, FORCED_BLOCK_QUIC, FORCED_UDP_BEHAVIOUR] },
];

const IOS_META = [  // ios_mobile：controller 组整组跳过；两 iOS 强制键在场
  { key: "network", label_zh: "网络与代理", label_en: "Network & Proxy",
    fields: [WIFI_ASSIST,
      intField("compatibility-mode", "兼容模式", "Compatibility Mode", { forced: true }),
      boolField("auto-suspend", "自动挂起", "Auto Suspend", { forced: true }),
      TEST_TIMEOUT] },
  { key: "advanced", label_zh: "高级", label_en: "Advanced",
    fields: [LOGLEVEL, FORCED_BLOCK_QUIC, FORCED_UDP_BEHAVIOUR] },
];

const GW_META = [  // mac_gateway：gateway-restricted-to-lan 在场且 forced
  { key: "network", label_zh: "网络与代理", label_en: "Network & Proxy",
    fields: [WIFI_ASSIST, TEST_TIMEOUT] },
  { key: "advanced", label_zh: "高级", label_en: "Advanced",
    fields: [LOGLEVEL, FORCED_BLOCK_QUIC, FORCED_UDP_BEHAVIOUR,
      boolField("gateway-restricted-to-lan", "网关限制到局域网",
        "Gateway Restricted to LAN", { forced: true })] },
];

const FIELDS_BY_PROFILE = { 1: WS_META, 2: IOS_META, 3: GW_META };
const VALUES_BY_PROFILE = {
  1: { "wifi-assist": false, "block-quic": "per-policy",
       "udp-policy-not-supported-behaviour": "REJECT", "loglevel": "notify" },
  2: { "wifi-assist": true, "compatibility-mode": 1, "auto-suspend": true,
       "block-quic": "per-policy", "udp-policy-not-supported-behaviour": "REJECT",
       "loglevel": "warning" },
  3: { "wifi-assist": false, "block-quic": "per-policy",
       "udp-policy-not-supported-behaviour": "REJECT",
       "gateway-restricted-to-lan": true, "loglevel": "notify" },
};

// ===== 最小 DOM stub（与 test_profiles_token_dialog.js 同模式）=====

function makeElement(initial) {
  const el = {
    value: "", placeholder: "", checked: false, textContent: "",
    className: "", style: {}, dataset: {}, innerHTML: "", onclick: null,
    focus() {}, select() {}, setSelectionRange() {},
  };
  el.classList = {
    contains(c) { return el.className.split(/\s+/).filter(Boolean).includes(c); },
    add(c) { if (!el.classList.contains(c)) el.className = el.className ? `${el.className} ${c}` : c; },
    remove(c) { el.className = el.className.split(/\s+/).filter(x => x && x !== c).join(" "); },
  };
  return Object.assign(el, initial);
}

/** fieldsHandler(url) 可覆盖默认 fields 分发（如乱序响应场景） */
function createPage({ fieldsHandler } = {}) {
  const formControls = [];   // collectGeneralValues/fillGeneralForm 的 [data-key] 查询结果
  const elements = new Map();
  const apiCalls = [];       // { method, url, body? } 全部 API 调用记录

  const documentStub = {
    documentElement: { lang: "zh" },
    body: makeElement(),
    getElementById: (id) => elements.get(id) || null,
    querySelector: () => null,
    querySelectorAll: (sel) => sel.includes("[data-key]") ? [...formControls] : [],
    createElement: () => makeElement(),
    addEventListener: () => {},
    execCommand: () => true,
  };

  const toasts = [];
  const sandbox = {
    document: documentStub,
    MutationObserver: class { observe() {} disconnect() {} },
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    location: { protocol: "http:", port: "61830", host: "127.0.0.1:61830" },
    navigator: {},
    // 普通对象而非 Proxy：缺失翻译键会渲染出 undefined，可被断言捕获
    I18N: {
      "btn.save": "保存",
      "msg.saved": "已保存",
      "general.json_override": "自定义 JSON 覆盖",
      "general.json_hint": "输入 JSON 会覆盖上方表单中的同名字段。谨慎使用。",
      "general.forced_badge": "锁定",
      "general.tags_placeholder": "每行一个或逗号分隔",
      "general.json_error": "自定义 JSON 格式错误",
      "general.meta_missing": "字段元数据未加载，请重新打开此标签页",
    },
    API: {
      async get(url) {
        apiCalls.push({ method: "GET", url });
        if (url.startsWith("/api/general/fields")) {
          if (fieldsHandler) return fieldsHandler(url);
          const m = /profile_id=(\d+)/.exec(url);
          return FIELDS_BY_PROFILE[Number(m[1])];
        }
        if (url.startsWith("/api/general/values")) {
          const m = /profile_id=(\d+)/.exec(url);
          const id = Number(m[1]);
          return { profile_id: id, profile_name: `p${id}`,
                   values: VALUES_BY_PROFILE[id] || {} };
        }
        if (url.includes("server-ips")) return { ips: [] };
        return [];
      },
      async post() { return {}; },
      async put(url, body) { apiCalls.push({ method: "PUT", url, body }); return {}; },
      async del() { return {}; },
    },
    showModal() {}, hideModal() {},
    confirmDialog: async () => true,
    toast: (msg, type) => toasts.push({ msg, type }),
    withLoading: async (_btn, fn) => fn(),
    escapeHtml: (s) => String(s),
    translateName: (s) => s,
    setupRuleModals() {},
    showLoading() {}, hideLoading() {},
    console,
  };
  vm.createContext(sandbox);
  vm.runInContext(SOURCE + `
;globalThis.__hooks = {
  fieldMetaByProfile, loadGeneralFields, renderGeneralTab, saveGeneralValues,
  renderField, renderGeneralForm, collectGeneralValues, fillGeneralForm,
};`, sandbox, { filename: "profiles.js" });

  const reg = (id, initial) => {
    const el = makeElement(initial);
    elements.set(id, el);
    return el;
  };
  reg("profile-list");  // init() 的 loadData 落点
  const fieldsCalls = () =>
    apiCalls.filter(c => c.method === "GET" && c.url.startsWith("/api/general/fields"));
  return { sandbox, formControls, reg, toasts, apiCalls, fieldsCalls };
}

function control(formControls, key, type, extra) {
  const el = makeElement(Object.assign({ dataset: { key, type } }, extra));
  formControls.push(el);
  return el;
}

/** 注册某档案 General Tab 的骨架元素（tab-content / groups-container / custom-json） */
function regGeneralTab(page, profileId, customJson) {
  page.reg(`tab-content-${profileId}`);
  page.reg(`groups-container-${profileId}`);
  page.reg(`custom-json-${profileId}`, { value: customJson || "" });
}

// ===== T1 renderField：forced → disabled + data-forced + 徽标（全类型）=====

const FIELD_TYPES = [
  ["bool", { key: "auto-suspend", forced: true }, /<input type="checkbox"[^>]* disabled data-forced="1"/],
  ["select", { key: "block-quic", forced: true, options: [{ value: "per-policy" }] },
   /<select[^>]* disabled data-forced="1"/],
  ["tags", { key: "some-tags", forced: true }, /<textarea[^>]* disabled data-forced="1"/],
  ["int", { key: "compatibility-mode", forced: true }, /<input type="number"[^>]* disabled data-forced="1"/],
  ["text", { key: "some-text", forced: true }, /<input type="text"[^>]* disabled data-forced="1"/],
];

{
  const page = createPage();
  for (const [type, f, disabledRe] of FIELD_TYPES) {
    const html = page.sandbox.__hooks.renderField(
      Object.assign({ type, label_zh: "字段", label_en: "Field", hint_zh: "", hint_en: "" }, f), 7);
    assert.ok(disabledRe.test(html), `${type} forced 字段必须 disabled + data-forced`);
    assert.ok(html.includes("badge-warning"), `${type} forced 字段必须带锁定徽标`);
    assert.ok(html.includes("锁定"), `${type} forced 徽标使用 I18N 实际文案`);
  }

  // 非 forced 字段：无任何禁用痕迹
  const plain = page.sandbox.__hooks.renderField(
    { type: "bool", key: "wifi-assist", label_zh: "Wi-Fi", label_en: "Wi-Fi" }, 7);
  assert.ok(!plain.includes("disabled"), "非 forced 字段不得 disabled");
  assert.ok(!plain.includes("data-forced"), "非 forced 字段不得带 data-forced");
  assert.ok(!plain.includes("badge-warning"), "非 forced 字段不得带锁定徽标");

  // tags 无 hint：placeholder 用真实 tags_placeholder 键，缺失翻译会露出 undefined
  const tagsHtml = page.sandbox.__hooks.renderField(
    { type: "tags", key: "plain-tags", label_zh: "标签", label_en: "Tags" }, 7);
  assert.ok(tagsHtml.includes("每行一个或逗号分隔"),
    "tags 无 hint 时 placeholder 必须来自 general.tags_placeholder");
  assert.ok(!tagsHtml.includes("undefined"), "缺失翻译不得渲染出 undefined");
  console.log("T1 renderField：五类型 forced 全禁用 + 徽标 + tags placeholder 真实键 通过");
}

// ===== T2 renderGeneralTab（真实 open 路径）：按 profile_id 取字段再渲染/填值 =====

async function t2() {
  const page = createPage();
  regGeneralTab(page, 2);
  // innerHTML 在真实浏览器会解析出控件；stub 中以 formControls 模拟渲染产物
  const autoSuspend = control(page.formControls, "auto-suspend", "bool", { checked: false });
  const compat = control(page.formControls, "compatibility-mode", "int", { value: "" });
  const blockQuic = control(page.formControls, "block-quic", "select", { value: "" });
  const loglevel = control(page.formControls, "loglevel", "select", { value: "" });

  await page.sandbox.__hooks.renderGeneralTab(2);

  // 请求必须携带当前 profile id，且恰好一次（无无参首屏加载）
  assert.deepEqual(page.fieldsCalls().map(c => c.url),
    ["/api/general/fields?profile_id=2"], "open 必须按 profile_id=2 请求字段且仅一次");

  const html = page.sandbox.document.getElementById("groups-container-2").innerHTML;
  assert.equal((html.match(/data-forced="1"/g) || []).length, 4,
    "iOS metadata 的 4 个 forced 字段（auto-suspend/compatibility-mode/block-quic/UDP 行为）必须禁用");
  assert.ok(!html.includes("http-api"), "iOS metadata 不得含 Mac-only 字段");
  assert.ok(!html.includes("undefined"), "整表渲染不得出现 undefined 文案");

  // 填值：values 请求同样带 profile_id，disabled 控件仍展示服务端强制值
  assert.ok(page.apiCalls.some(c =>
    c.method === "GET" && c.url === "/api/general/values?profile_id=2"),
    "values 请求必须携带 profile_id=2");
  assert.equal(autoSuspend.checked, true, "disabled checkbox 仍填充 iOS 强制值");
  assert.equal(compat.value, 1, "disabled number 仍填充 iOS 强制值");
  assert.equal(blockQuic.value, "per-policy", "disabled select 仍填充公共强制值");
  assert.equal(loglevel.value, "warning", "普通 select 照常填充");

  // 缓存生效：再次打开同一档案不再请求 fields
  await page.sandbox.__hooks.renderGeneralTab(2);
  assert.equal(page.fieldsCalls().length, 1, "metadata 命中缓存不得重复请求");
  console.log("T2 renderGeneralTab：按 profile_id 加载 → 渲染禁用 → 强制值展示 + 缓存 通过");
}

// ===== T3 collect：forced 集合按档案隔离（不串档）+ DOM/JSON 双免疫 =====

async function t3() {
  const page = createPage();

  // --- T3a iOS（profile 2）：DOM 篡改免疫 + JSON 注入当前目标 forced 键被移除 ---
  regGeneralTab(page, 2, JSON.stringify({
    "block-quic": "proxy",               // 公共强制键危险值
    "auto-suspend": false,               // iOS 平台强制键
    "compatibility-mode": 3,             // iOS 平台强制键
    "test-timeout": 9,                   // 普通 custom 键：应保留
    "some-future-key": "on",             // 表外未知键：保留交后端 422
  }));
  await page.sandbox.__hooks.renderGeneralTab(2);
  // DOM 篡改：forced 控件不设 disabled、值改成危险值（stub 默认即无禁用属性）
  const blockQuic = control(page.formControls, "block-quic", "select", { value: "proxy" });
  const autoSuspend = control(page.formControls, "auto-suspend", "bool", { checked: false });
  control(page.formControls, "compatibility-mode", "int", { value: "3" });
  control(page.formControls, "wifi-assist", "bool", { checked: true });
  control(page.formControls, "loglevel", "select", { value: "info" });

  const iosValues = page.sandbox.__hooks.collectGeneralValues(2);
  assert.ok(!("block-quic" in iosValues), "forced select（值被篡改为 proxy）不得收集");
  assert.ok(!("auto-suspend" in iosValues), "JSON 注入的 iOS 强制键必须移除");
  assert.ok(!("compatibility-mode" in iosValues), "JSON 注入的 iOS 强制键必须移除");
  assert.equal(iosValues["wifi-assist"], true, "普通 bool 照常收集");
  assert.equal(iosValues["loglevel"], "info", "普通 select 照常收集");
  assert.equal(iosValues["test-timeout"], 9, "普通 custom 键保留在 payload 供后端校验");
  assert.equal(iosValues["some-future-key"], "on", "未知 custom 键保留（后端 422 拒绝）");
  // 判定依据是服务端 metadata 而非 DOM：元素属性不含禁用痕迹不影响排除集合
  assert.equal(blockQuic.dataset.forced, undefined);
  assert.equal(autoSuspend.disabled, undefined);

  // --- T3b workstation（profile 1）：iOS/gateway 强制键不串档 ---
  // 同一页面已加载过 iOS metadata；若 forced 集合串档，auto-suspend 等会被误删。
  // JSON 注入值与 T3a 注册的同名表单控件值不同（true/0 vs false/"3"），
  // 断言 JSON 覆盖值存活即可证明键未被 forced 集合误删。
  regGeneralTab(page, 1, JSON.stringify({
    "block-quic": "proxy",               // 公共强制键：workstation 同样 forced，必须移除
    "auto-suspend": true,                // iOS-only：对 workstation 非 forced → 保留交后端 422
    "compatibility-mode": 0,             // 同上
    "gateway-restricted-to-lan": false,  // gateway-only：同上
    "test-timeout": 5,
  }));
  await page.sandbox.__hooks.renderGeneralTab(1);
  assert.ok(page.fieldsCalls().some(c => c.url === "/api/general/fields?profile_id=1"),
    "切到 workstation 档案必须重新按 profile_id=1 请求字段");

  const wsValues = page.sandbox.__hooks.collectGeneralValues(1);
  assert.ok(!("block-quic" in wsValues), "workstation 的公共 forced 键必须移除");
  assert.equal(wsValues["auto-suspend"], true,
    "iOS 强制键对 workstation 非 forced：不得用 iOS metadata 误删（不串档）");
  assert.equal(wsValues["compatibility-mode"], 0, "iOS-only 键保留交后端 422");
  assert.equal(wsValues["gateway-restricted-to-lan"], false, "gateway-only 键保留交后端 422");
  assert.equal(wsValues["test-timeout"], 5, "普通 custom 键照常保留");
  console.log("T3 collect：iOS/workstation forced 集合隔离 + DOM/JSON 双免疫 通过");
}

// ===== T4 乱序响应不串档 + 同一档案 inflight 去重 =====

async function t4() {
  let resolveWs;
  const page = createPage({
    fieldsHandler(url) {
      const m = /profile_id=(\d+)/.exec(url);
      const id = Number(m[1]);
      if (id === 1) {  // workstation 响应人为延迟（后到）
        return new Promise((resolve) => { resolveWs = () => resolve(WS_META); });
      }
      return FIELDS_BY_PROFILE[id];
    },
  });

  // 并发：ws 慢、iOS 快；iOS 先发两次（第二次应命中 inflight，不重复请求）
  const pWs = page.sandbox.__hooks.loadGeneralFields(1);
  const pIos1 = page.sandbox.__hooks.loadGeneralFields(2);
  const pIos2 = page.sandbox.__hooks.loadGeneralFields(2);

  await Promise.all([pIos1, pIos2]);
  const cache = page.sandbox.__hooks.fieldMetaByProfile;
  assert.equal(cache.get(2), IOS_META, "iOS 响应先完成即缓存");
  assert.equal(cache.get(1), undefined, "workstation 响应未归，不得预填/错填");

  resolveWs();  // 慢响应后到：只写自己的槽位，不得覆盖 iOS 缓存
  await pWs;
  assert.equal(cache.get(1), WS_META, "workstation 慢响应写入自己的槽位");
  assert.equal(cache.get(2), IOS_META, "乱序响应不得覆盖其他档案的缓存");

  const byProfile = (id) => page.fieldsCalls()
    .filter(c => c.url === `/api/general/fields?profile_id=${id}`).length;
  assert.equal(byProfile(1), 1, "workstation 字段恰好请求一次");
  assert.equal(byProfile(2), 1, "iOS 并发加载必须 inflight 去重（恰好一次）");

  // 缓存命中不再发请求
  await page.sandbox.__hooks.loadGeneralFields(2);
  assert.equal(byProfile(2), 1, "缓存命中不得重复请求");
  console.log("T4 乱序响应不串档 + inflight 去重 通过");
}

// ===== T5 owner guard：未加载档案 fail closed；save 先取 metadata 再提交 =====

async function t5() {
  const page = createPage();

  // 未加载 metadata 的档案直接 collect：fail closed，不产出 payload
  page.reg("custom-json-9", { value: "" });
  const blocked = page.sandbox.__hooks.collectGeneralValues(9);
  assert.equal(blocked, null, "metadata 缺失必须拒绝收集（fail closed）");
  assert.ok(page.toasts.some(t =>
    t.type === "error" && t.msg === "字段元数据未加载，请重新打开此标签页"),
    "fail closed 必须提示 metadata 缺失");
  assert.ok(!page.apiCalls.some(c => c.method === "PUT"), "fail closed 不得发出 PUT");

  // save 真实路径（gateway profile 3）：保存前先 await 当前档案 metadata
  regGeneralTab(page, 3, JSON.stringify({
    "gateway-restricted-to-lan": false,  // gateway 平台强制键：必须移除
    "block-quic": "proxy",               // 公共强制键：必须移除
    "test-timeout": 5,                   // 普通 custom 键：保留
  }));
  control(page.formControls, "loglevel", "select", { value: "warning" });
  await page.sandbox.__hooks.saveGeneralValues(3, makeElement());

  const getIdx = page.apiCalls.findIndex(c =>
    c.method === "GET" && c.url === "/api/general/fields?profile_id=3");
  const putIdx = page.apiCalls.findIndex(c => c.method === "PUT");
  assert.ok(getIdx !== -1, "save 必须按 profile_id=3 请求字段");
  assert.ok(putIdx !== -1, "save 必须发出 PUT");
  assert.ok(getIdx < putIdx, "metadata 必须先于 PUT 就位（owner 匹配后才可以收集提交）");
  const put = page.apiCalls[putIdx];
  assert.equal(put.url, "/api/general/values?profile_id=3", "PUT 必须携带 profile_id=3");
  assert.ok(!("gateway-restricted-to-lan" in put.body), "gateway 强制键不得提交");
  assert.ok(!("block-quic" in put.body), "公共强制键不得提交");
  assert.equal(put.body["test-timeout"], 5, "普通 custom 键照常提交");
  assert.equal(put.body["loglevel"], "warning", "表单普通键照常提交");
  assert.ok(page.toasts.some(t => t.msg === "已保存"), "保存成功必须提示");
  console.log("T5 owner guard：fail closed + save 先取 metadata 再提交 通过");
}

// ===== T6 fillGeneralForm：disabled 控件仍展示服务端强制值 =====

{
  const page = createPage();
  const autoSuspend = control(page.formControls, "auto-suspend", "bool", { checked: false });
  const blockQuic = control(page.formControls, "block-quic", "select", { value: "" });
  const wifi = control(page.formControls, "wifi-assist", "bool", { checked: false });

  page.sandbox.__hooks.fillGeneralForm(9, {
    "auto-suspend": true, "block-quic": "per-policy", "wifi-assist": true,
  });
  assert.equal(autoSuspend.checked, true, "disabled checkbox 仍填充强制值");
  assert.equal(blockQuic.value, "per-policy", "disabled select 仍填充强制值");
  assert.equal(wifi.checked, true, "普通 checkbox 照常填充");
  console.log("T6 fillGeneralForm：强制值展示不受禁用影响 通过");
}

(async () => {
  await t2();
  await t3();
  await t4();
  await t5();
  console.log("JS 行为测试：forced 渲染禁用/徽标 + 元数据按档案绑定（open/collect/save 真实路径）"
    + " + 乱序不串档 + fail closed 全部通过");
})().catch((e) => { console.error(e); process.exit(1); });
