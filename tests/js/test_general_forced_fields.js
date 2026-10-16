/** profiles.js General 强制字段（forced）只读行为测试（node vm + DOM stub）
 *
 * 覆盖（fix round 2 + round 3）：
 *  - renderField：服务端 metadata forced === true 时 checkbox/select/textarea/
 *    number/text 全部 disabled + data-forced="1" + badge-warning 徽标；
 *    非 forced 字段不带任何禁用痕迹
 *  - renderGeneralForm：fieldMeta 含 forced 字段时容器 HTML 带禁用属性
 *  - collectGeneralValues：按 fieldMeta（服务端元数据）排除 forced 键——
 *    即使 DOM 被篡改（移除 disabled/data-forced、改危险值）也不提交；
 *    自定义 JSON 重新注入的 forced 键（含 iOS/gateway 平台强制键）同样移除；
 *    普通 custom 键与表外未知键保留在 payload 交后端校验（未知/危险 422）
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

function createPage() {
  const formControls = [];   // collectGeneralValues/fillGeneralForm 的 [data-key] 查询结果
  const elements = new Map();

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
      "general.forced_badge": "锁定",
      "general.tags_placeholder": "每行一个或逗号分隔",
      "general.json_error": "自定义 JSON 格式错误",
    },
    API: {
      async get() { return []; },
      async post() { return {}; },
      async put() { return {}; },
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
  get fieldMeta() { return fieldMeta; },
  set fieldMeta(v) { fieldMeta = v; },
  renderField, renderGeneralForm, collectGeneralValues, fillGeneralForm,
};`, sandbox, { filename: "profiles.js" });

  const reg = (id, initial) => {
    const el = makeElement(initial);
    elements.set(id, el);
    return el;
  };
  return { sandbox, formControls, reg, toasts };
}

function control(formControls, key, type, extra) {
  const el = makeElement(Object.assign({ dataset: { key, type } }, extra));
  formControls.push(el);
  return el;
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

// ===== T2 renderGeneralForm：服务端 metadata 驱动整表渲染 =====

{
  const page = createPage();
  page.reg("groups-container-3");
  page.sandbox.__hooks.fieldMeta = [
    { key: "advanced", label_zh: "高级", label_en: "Advanced", fields: [
      { key: "block-quic", type: "select", label_zh: "QUIC", label_en: "QUIC",
        forced: true, options: [{ value: "per-policy", label_zh: "按策略", label_en: "Per Policy" }] },
      { key: "loglevel", type: "select", label_zh: "日志", label_en: "Log",
        options: [{ value: "notify", label_zh: "通知", label_en: "Notify" }] },
    ] },
  ];
  page.sandbox.__hooks.renderGeneralForm(3);
  const html = page.sandbox.document.getElementById("groups-container-3").innerHTML;
  assert.ok(/disabled data-forced="1"/.test(html), "整表渲染 forced 字段必须禁用");
  assert.ok((html.match(/data-forced="1"/g) || []).length === 1, "仅 forced 字段禁用");
  assert.ok(!html.includes("undefined"), "整表渲染不得出现 undefined 文案");
  console.log("T2 renderGeneralForm：metadata forced 驱动整表禁用 通过");
}

// ===== T3 collectGeneralValues：forced 键不收集（含 DOM 篡改场景）=====

{
  const page = createPage();
  page.sandbox.__hooks.fieldMeta = [
    { key: "g", label_zh: "", label_en: "", fields: [
      { key: "block-quic", type: "select", forced: true },
      { key: "auto-suspend", type: "bool", forced: true },
      { key: "compatibility-mode", type: "int", forced: true },
      { key: "gateway-restricted-to-lan", type: "bool", forced: true },
      { key: "wifi-assist", type: "bool" },
      { key: "loglevel", type: "select" },
    ] },
  ];
  // DOM 篡改：forced 控件的 disabled/data-forced 全被移除、值改成危险值
  const blockQuic = control(page.formControls, "block-quic", "select", { value: "proxy" });
  const autoSuspend = control(page.formControls, "auto-suspend", "bool", { checked: false });
  const compat = control(page.formControls, "compatibility-mode", "int", { value: "3" });
  control(page.formControls, "gateway-restricted-to-lan", "bool", { checked: false });
  const wifi = control(page.formControls, "wifi-assist", "bool", { checked: true });
  const log = control(page.formControls, "loglevel", "select", { value: "info" });
  page.reg("custom-json-5", { value: "" });

  const values = page.sandbox.__hooks.collectGeneralValues(5);
  assert.ok(!("block-quic" in values), "forced select（值被篡改为 proxy）不得收集");
  assert.ok(!("auto-suspend" in values), "forced bool（勾选被篡改）不得收集");
  assert.ok(!("compatibility-mode" in values), "forced int（值被篡改为 3）不得收集");
  assert.ok(!("gateway-restricted-to-lan" in values), "forced gateway 键不得收集");
  assert.equal(values["wifi-assist"], true, "普通 bool 照常收集");
  assert.equal(values["loglevel"], "info", "普通 select 照常收集");

  // 依据是服务端 metadata 而非 DOM：篡改元素属性不影响排除集合
  assert.equal(blockQuic.dataset.forced, undefined);
  assert.equal(autoSuspend.disabled, undefined);
  console.log("T3a collectGeneralValues：forced 排除 + DOM 篡改免疫 通过");
}

// ===== T3b custom JSON 重新注入 forced 键：合并后必须移除 =====

{
  const page = createPage();
  page.sandbox.__hooks.fieldMeta = [
    { key: "g", label_zh: "", label_en: "", fields: [
      { key: "block-quic", type: "select", forced: true },
      { key: "auto-suspend", type: "bool", forced: true },
      { key: "compatibility-mode", type: "int", forced: true },
      { key: "gateway-restricted-to-lan", type: "bool", forced: true },
    ] },
  ];
  control(page.formControls, "wifi-assist", "bool", { checked: true });
  page.reg("custom-json-6", {
    value: JSON.stringify({
      "block-quic": "proxy",                 // 公共强制键危险值
      "auto-suspend": false,                 // iOS 平台强制键
      "compatibility-mode": 3,               // iOS 平台强制键
      "gateway-restricted-to-lan": false,    // gateway 平台强制键
      "test-timeout": 9,                     // 普通 custom 键：应保留
      "some-future-key": "on",               // 表外未知键：保留交后端 422
    }),
  });

  const merged = page.sandbox.__hooks.collectGeneralValues(6);
  assert.ok(!("block-quic" in merged), "JSON 注入的公共强制键必须移除");
  assert.ok(!("auto-suspend" in merged), "JSON 注入的 iOS 强制键必须移除");
  assert.ok(!("compatibility-mode" in merged), "JSON 注入的 iOS 强制键必须移除");
  assert.ok(!("gateway-restricted-to-lan" in merged), "JSON 注入的 gateway 强制键必须移除");
  assert.equal(merged["wifi-assist"], true, "表单普通键照常收集");
  assert.equal(merged["test-timeout"], 9, "普通 custom 键保留在 payload 供后端校验");
  assert.equal(merged["some-future-key"], "on", "未知 custom 键保留（后端 422 拒绝）");
  console.log("T3b custom JSON：forced 键注入免疫 + 普通/未知键保留 通过");
}

// ===== T4 fillGeneralForm：disabled 控件仍展示服务端强制值 =====

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
  console.log("T4 fillGeneralForm：强制值展示不受禁用影响 通过");
}

console.log("JS 行为测试：forced 渲染禁用/徽标 + 收集排除（DOM 与 JSON 双免疫）+ 强制值展示 全部通过");
