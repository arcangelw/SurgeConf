/** profiles.js General 元数据生命周期 + 强制字段（forced）只读行为测试
 *  （node vm + DOM stub，实际执行脚本函数，非源文本断言）
 *
 * 覆盖（fix round 2 + 3 + 4 + 5）：
 *  - renderField：服务端 metadata forced === true 时 checkbox/select/textarea/
 *    number/text 全部 disabled + data-forced="1" + badge-warning 徽标；
 *    非 forced 字段不带任何禁用痕迹
 *  - metadata 来源：tests/js/fixtures/general_fields_meta.json 是后端
 *    general_groups_for_target 对三目标的完整真实输出（Python 测试
 *    test_general_capabilities.py::TestGeneralMetaFixture 锁定逐字节一致），
 *    非手写裁剪子集
 *  - 无持久缓存：每次 renderGeneralTab / saveGeneralValues 都重新
 *    GET /api/general/fields?profile_id=<id>；仅同一调用窗口 inflight 去重
 *  - 档案生命周期（generation 状态机）：saveProfile/deleteProfile 等
 *    PUT|DEL /api/configs/profiles/{id} 成功后失效——旧 generation 响应
 *    不得渲染（旧响应晚到不覆盖）、不得供 save 使用；失效后新调用不复用
 *    旧 inflight；target 更新后 reload 使用新 forced 集合
 *  - collectGeneralValues：metadata 显式传入（save 前 fresh GET），
 *    forced 集合按当前目标隔离不串档；自定义 JSON 重新注入的当前目标
 *    forced 键在合并后移除；普通 custom 键与表外未知键保留交后端 422
 *  - fail closed：metadata 缺失拒绝收集；save 等待期间档案变更中止提交；
 *    PUT 422 只报错不重试不静默
 *  - fillGeneralForm：disabled 控件仍填充服务端强制值（展示）
 *  - I18N stub 为普通对象（非 Proxy 伪造任意键）：缺失翻译会渲染
 *    undefined 并被断言捕获
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

// ===== 后端真实完整 metadata（Python 测试锁定与 general_groups_for_target 一致）=====
const META_FIXTURE = JSON.parse(fs.readFileSync(
  path.join(REPO_ROOT, "tests/js/fixtures/general_fields_meta.json"), "utf8"));
const WS_META = META_FIXTURE.mac_workstation;   // profile 1 初始 target
const IOS_META = META_FIXTURE.ios_mobile;       // profile 2
const GW_META = META_FIXTURE.mac_gateway;       // profile 3

const forcedKeysOf = (meta) =>
  new Set(meta.flatMap(g => g.fields.filter(f => f.forced === true).map(f => f.key)));
const countFields = (meta) => meta.reduce((n, g) => n + g.fields.length, 0);

// fixture 前提自检（Python 侧另有逐字节锁定，这里保证 JS 断言前提成立）
assert.deepEqual(forcedKeysOf(IOS_META), new Set([
  "block-quic", "udp-policy-not-supported-behaviour", "compatibility-mode", "auto-suspend"]));
assert.deepEqual(forcedKeysOf(GW_META), new Set([
  "block-quic", "udp-policy-not-supported-behaviour", "gateway-restricted-to-lan"]));
assert.deepEqual(forcedKeysOf(WS_META), new Set([
  "block-quic", "udp-policy-not-supported-behaviour"]));
assert.ok(!JSON.stringify(IOS_META).includes("http-api"), "iOS 不得含 Mac-only 字段");

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

/** createPage 选项：
 *  - fieldsHandler(url, targets)：覆盖默认 fields 分发（乱序/延迟/404 场景）
 *  - valuesHandler(url, targets)：覆盖默认 values 分发
 *  - putFails(url)：PUT 该 url 时抛 422（fail closed 场景）
 *  targets：page 级 target 映射（profileId → ProfileTarget.value），
 *  模拟后端档案 target 的当前状态；测试中修改即代表后端已变更。 */
function createPage({ fieldsHandler, valuesHandler, putFails } = {}) {
  const formControls = [];   // collectGeneralValues/fillGeneralForm 的 [data-key] 查询结果
  const elements = new Map();
  const apiCalls = [];       // { method, url, body? } 全部 API 调用记录
  const targets = new Map([[1, "mac_workstation"], [2, "ios_mobile"], [3, "mac_gateway"]]);

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
      "btn.delete": "删除",
      "msg.saved": "已保存",
      "msg.updated": "已更新",
      "msg.deleted": "已删除",
      "msg.required": "必填",
      "profile.empty": "暂无配置方案",
      "profile.confirm_delete": "确认删除该配置方案？",
      "general.json_override": "自定义 JSON 覆盖",
      "general.json_hint": "输入 JSON 会覆盖上方表单中的同名字段。谨慎使用。",
      "general.forced_badge": "锁定",
      "general.tags_placeholder": "每行一个或逗号分隔",
      "general.json_error": "自定义 JSON 格式错误",
      "general.meta_missing": "字段元数据未加载，请重新打开此标签页",
      "general.meta_stale": "档案刚被修改，字段元数据已过期，请重新打开此标签页后再保存",
    },
    API: {
      async get(url) {
        apiCalls.push({ method: "GET", url });
        if (url.startsWith("/api/general/fields")) {
          if (fieldsHandler) return fieldsHandler(url, targets);
          const m = /profile_id=(\d+)/.exec(url);
          const target = targets.get(Number(m[1]));
          if (!target) throw new Error("404: 配置方案不存在");
          return META_FIXTURE[target];
        }
        if (url.startsWith("/api/general/values")) {
          if (valuesHandler) return valuesHandler(url, targets);
          const m = /profile_id=(\d+)/.exec(url);
          return { profile_id: Number(m[1]), profile_name: `p${m[1]}`, values: {} };
        }
        if (url.includes("server-ips")) return { ips: [] };
        return [];
      },
      async post() { return {}; },
      async put(url, body) {
        apiCalls.push({ method: "PUT", url, body });
        if (putFails && putFails(url)) throw new Error("422: 不支持的键");
        return {};
      },
      async del(url) { apiCalls.push({ method: "DEL", url }); return {}; },
    },
    showModal() {}, hideModal() {},
    confirmDialog: async () => true,
    toast: (msg, type) => toasts.push({ msg, type }),
    withLoading: async (_btn, fn) => fn(),
    escapeHtml: (s) => String(s),
    translateName: (s) => s,
    flashInput: () => {},
    setupRuleModals() {},
    showLoading() {}, hideLoading() {},
    console,
  };
  vm.createContext(sandbox);
  vm.runInContext(SOURCE + `
;globalThis.__hooks = {
  loadGeneralFields, renderGeneralTab, saveGeneralValues, renderGeneralForm,
  renderField, collectGeneralValues, fillGeneralForm, saveProfile, deleteProfile,
  saveMitm, invalidateGeneralMeta, generalMetaGen,
};`, sandbox, { filename: "profiles.js" });

  const reg = (id, initial) => {
    const el = makeElement(initial);
    elements.set(id, el);
    return el;
  };
  reg("profile-list");  // init() 的 loadData 落点
  const fieldsCalls = () =>
    apiCalls.filter(c => c.method === "GET" && c.url.startsWith("/api/general/fields"));
  return { sandbox, formControls, reg, toasts, apiCalls, fieldsCalls, targets };
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

/** 注册 profile 编辑弹窗元素（saveProfile 真实路径所需） */
function regProfileModal(page, id, name) {
  page.reg("profile-edit-id", { value: String(id) });
  page.reg("profile-name", { value: name });
  page.reg("profile-desc", { value: "" });
  page.reg("profile-default", { checked: false });
  page.reg("profile-locale", { value: "zh" });
  page.reg("profile-target", { value: "mac_workstation" });
  page.reg("profile-mode", { value: "scoped" });
}

const generalPuts = (page) =>
  page.apiCalls.filter(c => c.method === "PUT" && c.url.startsWith("/api/general/values"));

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

// ===== T2 renderGeneralTab（真实 open 路径）：完整 fixture 渲染 + 无缓存 =====

async function t2() {
  const page = createPage({
    valuesHandler: () => ({
      values: {
        "compatibility-mode": 1, "auto-suspend": true,
        "block-quic": "per-policy", "udp-policy-not-supported-behaviour": "REJECT",
        "loglevel": "warning",
      },
    }),
  });
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
  // 完整 fixture 渲染：iOS 22 个字段全部出现，forced 恰为 fixture 的 4 键
  assert.equal((html.match(/data-key=/g) || []).length, countFields(IOS_META),
    "整表必须渲染 fixture 的全部 iOS 字段");
  assert.equal((html.match(/data-forced="1"/g) || []).length, forcedKeysOf(IOS_META).size,
    "禁用数量必须等于 fixture iOS forced 字段数");
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

  // 无持久缓存：再次打开同一档案必须重新请求 fields
  await page.sandbox.__hooks.renderGeneralTab(2);
  assert.equal(page.fieldsCalls().length, 2, "无持久缓存：重复打开必须重新 GET fields");
  console.log("T2 renderGeneralTab：完整 fixture 渲染 + 强制值展示 + 无缓存 通过");
}

// ===== T3 collect（经 saveGeneralValues 真实路径）：forced 集合按目标隔离 =====

async function t3() {
  // --- T3a iOS（profile 2）：JSON 注入当前目标 forced 键被移除 + DOM 篡改免疫 ---
  const iosPage = createPage();
  regGeneralTab(iosPage, 2, JSON.stringify({
    "block-quic": "proxy",               // 公共强制键危险值
    "auto-suspend": false,               // iOS 平台强制键
    "compatibility-mode": 3,             // iOS 平台强制键
    "test-timeout": 9,                   // 普通 custom 键：应保留
    "some-future-key": "on",             // 表外未知键：保留交后端 422
  }));
  await iosPage.sandbox.__hooks.renderGeneralTab(2);
  // DOM 篡改：forced 控件不设 disabled、值改成危险值（stub 默认即无禁用属性）
  const blockQuic = control(iosPage.formControls, "block-quic", "select", { value: "proxy" });
  const autoSuspend = control(iosPage.formControls, "auto-suspend", "bool", { checked: false });
  control(iosPage.formControls, "compatibility-mode", "int", { value: "3" });
  control(iosPage.formControls, "wifi-assist", "bool", { checked: true });
  control(iosPage.formControls, "loglevel", "select", { value: "info" });

  await iosPage.sandbox.__hooks.saveGeneralValues(2, makeElement());

  // save 真实路径：render 一次 + save 前 fresh GET 一次（无缓存必重拉）
  assert.equal(iosPage.fieldsCalls().filter(c =>
    c.url === "/api/general/fields?profile_id=2").length, 2,
    "open 与 save 各拉一次（save 前必须重新取得当前 metadata）");
  const iosPut = generalPuts(iosPage)[0];
  assert.ok(iosPut, "iOS save 必须发出 PUT");
  assert.ok(!("block-quic" in iosPut.body), "forced select（值被篡改为 proxy）不得收集");
  assert.ok(!("auto-suspend" in iosPut.body), "JSON 注入的 iOS 强制键必须移除");
  assert.ok(!("compatibility-mode" in iosPut.body), "JSON 注入的 iOS 强制键必须移除");
  assert.equal(iosPut.body["wifi-assist"], true, "普通 bool 照常收集");
  assert.equal(iosPut.body["loglevel"], "info", "普通 select 照常收集");
  assert.equal(iosPut.body["test-timeout"], 9, "普通 custom 键保留在 payload 供后端校验");
  assert.equal(iosPut.body["some-future-key"], "on", "未知 custom 键保留（后端 422 拒绝）");
  // 判定依据是服务端 metadata 而非 DOM：元素属性不含禁用痕迹不影响排除集合
  assert.equal(blockQuic.dataset.forced, undefined);
  assert.equal(autoSuspend.disabled, undefined);

  // --- T3b workstation（profile 1）：iOS/gateway 强制键不串档 ---
  // 旧实现的全局 metadata（最后加载者胜）会把 iOS forced 集合带给 workstation；
  // 现在保存前 fresh GET 当前档案 metadata，forced 集合必然只含本目标键。
  const wsPage = createPage();
  regGeneralTab(wsPage, 1, JSON.stringify({
    "block-quic": "proxy",               // 公共强制键：workstation 同样 forced，必须移除
    "auto-suspend": true,                // iOS-only：对 workstation 非 forced → 保留交后端 422
    "compatibility-mode": 0,             // 同上
    "gateway-restricted-to-lan": false,  // gateway-only：同上
    "test-timeout": 5,
  }));
  await wsPage.sandbox.__hooks.renderGeneralTab(1);
  control(wsPage.formControls, "loglevel", "select", { value: "notify" });
  await wsPage.sandbox.__hooks.saveGeneralValues(1, makeElement());

  const wsPut = generalPuts(wsPage)[0];
  assert.ok(wsPut, "workstation save 必须发出 PUT");
  assert.equal(wsPut.url, "/api/general/values?profile_id=1");
  assert.ok(!("block-quic" in wsPut.body), "workstation 的公共 forced 键必须移除");
  assert.equal(wsPut.body["auto-suspend"], true,
    "iOS 强制键对 workstation 非 forced：不得误删（不串档）");
  assert.equal(wsPut.body["compatibility-mode"], 0, "iOS-only 键保留交后端 422");
  assert.equal(wsPut.body["gateway-restricted-to-lan"], false, "gateway-only 键保留交后端 422");
  assert.equal(wsPut.body["test-timeout"], 5, "普通 custom 键照常保留");
  assert.equal(wsPut.body["loglevel"], "notify", "表单普通键照常提交");
  console.log("T3 collect：iOS/workstation forced 集合隔离 + DOM/JSON 双免疫（save 真实路径）通过");
}

// ===== T4 并发：同窗口 inflight 去重；完成后重拉；失效后不复用旧 inflight =====

async function t4() {
  const page = createPage();

  // 同一调用窗口并发去重：两个并发调用共享一个请求
  const [a1, a2] = await Promise.all([
    page.sandbox.__hooks.loadGeneralFields(2),
    page.sandbox.__hooks.loadGeneralFields(2),
  ]);
  assert.equal(a1, a2, "并发调用必须复用同一响应");
  assert.equal(page.fieldsCalls().length, 1, "同一窗口并发加载必须 inflight 去重（恰好一次）");

  // 完成后再次调用：无持久缓存，必须重新请求
  await page.sandbox.__hooks.loadGeneralFields(2);
  assert.equal(page.fieldsCalls().length, 2, "完成后再次调用必须重新 GET（无持久缓存）");

  // 乱序：不同档案并发互不干扰，各自拿到各自目标的完整 metadata
  const p1 = page.sandbox.__hooks.loadGeneralFields(1);
  const p2 = page.sandbox.__hooks.loadGeneralFields(3);
  const [ws, gw] = await Promise.all([p1, p2]);
  assert.equal(countFields(ws), countFields(WS_META), "workstation 档案拿到 workstation metadata");
  assert.equal(countFields(gw), countFields(GW_META), "gateway 档案拿到 gateway metadata");

  // 在途请求期间档案失效：新调用不得复用旧 inflight（旧响应可能来自旧 target）
  let resolveOld;
  const slowPage = createPage({
    fieldsHandler(url) {
      const m = /profile_id=(\d+)/.exec(url);
      if (Number(m[1]) === 1 && slowPage.fieldsCalls().length === 1) {
        return new Promise((resolve) => { resolveOld = () => resolve(WS_META); });
      }
      const target = slowPage.targets.get(Number(m[1]));
      return META_FIXTURE[target];
    },
  });
  const oldPromise = slowPage.sandbox.__hooks.loadGeneralFields(1);
  // 模拟后端 PUT 已把该档案 target 改为 iOS，前端经失效路径得知档案更新
  slowPage.targets.set(1, "ios_mobile");
  slowPage.sandbox.__hooks.invalidateGeneralMeta(1);
  const newPromise = slowPage.sandbox.__hooks.loadGeneralFields(1);
  assert.equal(slowPage.fieldsCalls().length, 2, "失效后新调用必须发起新请求（不复用旧 inflight）");
  resolveOld();
  const [oldMeta, newMeta] = await Promise.all([oldPromise, newPromise]);
  assert.equal(oldMeta, WS_META, "旧 promise 仍兑现旧响应（不写共享状态，无覆盖面）");
  assert.equal(newMeta, IOS_META, "新调用必须拿到新 target 的 metadata");
  assert.ok(slowPage.sandbox.__hooks.generalMetaGen(1) >= 1, "失效必须推进 generation");
  console.log("T4 并发去重 + 完成后重拉 + 失效不复用旧 inflight 通过");
}

// ===== T5 fail closed：metadata 缺失 / 加载失败 / PUT 422 =====

async function t5() {
  // 未取得 metadata 的档案直接 collect：fail closed，不产出 payload
  const page = createPage();
  page.reg("custom-json-9", { value: "" });
  const blocked = page.sandbox.__hooks.collectGeneralValues(9);
  assert.equal(blocked, null, "metadata 缺失必须拒绝收集（fail closed）");
  assert.ok(page.toasts.some(t =>
    t.type === "error" && t.msg === "字段元数据未加载，请重新打开此标签页"),
    "fail closed 必须提示 metadata 缺失");
  assert.ok(!page.apiCalls.some(c => c.method === "PUT"), "fail closed 不得发出 PUT");

  // fields 加载失败：save 中止提交，只用错误提示，不用任何旧集合
  const failPage = createPage({
    fieldsHandler() { throw new Error("503: 服务暂不可用"); },
  });
  regGeneralTab(failPage, 3, "");
  await failPage.sandbox.__hooks.saveGeneralValues(3, makeElement());
  assert.ok(!generalPuts(failPage).length, "metadata 拉取失败必须中止提交");
  assert.ok(failPage.toasts.some(t => t.type === "error" && t.msg === "503: 服务暂不可用"),
    "拉取失败必须如实报错");

  // 后端并发变更导致 422：只报错，不重试不静默（fail closed）
  const rejectPage = createPage({
    putFails: (url) => url.startsWith("/api/general/values"),
  });
  regGeneralTab(rejectPage, 2, JSON.stringify({ "some-future-key": "x" }));
  control(rejectPage.formControls, "loglevel", "select", { value: "notify" });
  await rejectPage.sandbox.__hooks.saveGeneralValues(2, makeElement());
  assert.equal(generalPuts(rejectPage).length, 1, "PUT 422 不得重试");
  assert.ok(rejectPage.toasts.some(t => t.type === "error" && t.msg.includes("422")),
    "PUT 422 必须报错提示");
  assert.ok(!rejectPage.toasts.some(t => t.msg === "已保存"), "422 不得提示保存成功");
  console.log("T5 fail closed：metadata 缺失 / 拉取失败 / PUT 422 通过");
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

// ===== T7 档案生命周期：update/delete 失效 + 旧响应晚到不覆盖 =====

/** 经真实 saveProfile 路径触发档案更新失效（PUT /api/configs/profiles/{id} 成功） */
async function updateProfileViaUI(page, id) {
  regProfileModal(page, id, "改名");
  await page.sandbox.__hooks.saveProfile();
  const puts = page.apiCalls.filter(c =>
    c.method === "PUT" && c.url === `/api/configs/profiles/${id}`);
  assert.equal(puts.length, 1, "saveProfile 必须发出档案 PUT");
}

async function t7() {
  // --- T7a target 更新后 reload：使用新 forced 集合与字段集合 ---
  const pageA = createPage();
  regGeneralTab(pageA, 1);
  await pageA.sandbox.__hooks.renderGeneralTab(1);
  let html = pageA.sandbox.document.getElementById("groups-container-1").innerHTML;
  assert.equal((html.match(/data-key=/g) || []).length, countFields(WS_META),
    "初始 workstation 渲染完整 33 字段");
  assert.equal((html.match(/data-forced="1"/g) || []).length, forcedKeysOf(WS_META).size,
    "workstation 仅 2 个公共 forced");

  // 档案 PUT（可改 target；本页无 target 编辑器，模拟其他客户端已改）成功 → 失效
  await updateProfileViaUI(pageA, 1);
  pageA.targets.set(1, "ios_mobile");  // 模拟该 PUT 把 target 改为 iOS

  await pageA.sandbox.__hooks.renderGeneralTab(1);  // reload
  assert.equal(pageA.fieldsCalls().filter(c => c.url === "/api/general/fields?profile_id=1").length,
    2, "target 更新后 reload 必须重新 GET fields");
  html = pageA.sandbox.document.getElementById("groups-container-1").innerHTML;
  assert.equal((html.match(/data-key=/g) || []).length, countFields(IOS_META),
    "reload 后渲染 iOS 完整 22 字段（新集合生效）");
  assert.equal((html.match(/data-forced="1"/g) || []).length, forcedKeysOf(IOS_META).size,
    "reload 后禁用数等于 iOS forced 数（compatibility-mode/auto-suspend 新进强制集合）");
  assert.ok(html.includes("compatibility-mode"), "iOS 专属字段必须在场");
  assert.ok(!html.includes("http-api"), "Mac-only 字段不得再出现");
  console.log("T7a target 更新后 reload 使用新 forced 集合 通过");

  // --- T7b delete invalidates：删除后旧 render 响应不得渲染 ---
  let resolveDeleted;
  let firstFieldsCall = true;  // 仅首次（旧 render）延迟；删除后新请求直接 404
  const pageB = createPage({
    fieldsHandler(url) {
      const m = /profile_id=(\d+)/.exec(url);
      if (Number(m[1]) === 1 && firstFieldsCall) {
        firstFieldsCall = false;
        return new Promise((resolve) => { resolveDeleted = () => resolve(WS_META); });
      }
      const target = pageB.targets.get(Number(m[1]));
      if (!target) throw new Error("404: 配置方案不存在");
      return META_FIXTURE[target];
    },
  });
  regGeneralTab(pageB, 1);
  const renderPromise = pageB.sandbox.__hooks.renderGeneralTab(1);  // GET pending
  pageB.targets.delete(1);  // 后端档案即将不存在
  await pageB.sandbox.__hooks.deleteProfile(1, makeElement());      // DEL 成功 → 失效
  assert.equal(pageB.sandbox.__hooks.generalMetaGen(1), 1, "delete 必须推进 generation");
  resolveDeleted();                                                  // 旧响应晚到
  await renderPromise;
  assert.equal(pageB.sandbox.document.getElementById("groups-container-1").innerHTML, "",
    "删除后旧响应不得渲染表单");
  // 删除后新调用走新请求（并得到 404，而非复用旧 inflight 的旧响应）
  await assert.rejects(() => pageB.sandbox.__hooks.loadGeneralFields(1),
    { message: "404: 配置方案不存在" }, "删除后重新拉取必须得到 404（fail closed）");
  console.log("T7b delete invalidates：旧响应不渲染、新调用 404 通过");

  // --- T7c 旧响应晚到不覆盖新渲染 ---
  let resolveSlow;
  let fieldsSeq = 0;
  const page = createPage({
    fieldsHandler(url, targets) {
      const m = /profile_id=(\d+)/.exec(url);
      fieldsSeq += 1;
      if (Number(m[1]) === 1 && fieldsSeq === 1) {
        return new Promise((resolve) => { resolveSlow = () => resolve(WS_META); });
      }
      return META_FIXTURE[targets.get(Number(m[1]))];
    },
  });
  regGeneralTab(page, 1);
  const slowRender = page.sandbox.__hooks.renderGeneralTab(1);  // 旧 target 响应 pending
  await updateProfileViaUI(page, 1);                             // 档案更新 → 失效
  page.targets.set(1, "ios_mobile");                             // PUT 改了 target
  await page.sandbox.__hooks.renderGeneralTab(1);                // 新 render：新集合
  const container = page.sandbox.document.getElementById("groups-container-1");
  assert.equal((container.innerHTML.match(/data-key=/g) || []).length, countFields(IOS_META),
    "新渲染必须先落位 iOS 集合");
  resolveSlow();                                                 // 旧 workstation 响应晚到
  await slowRender;
  assert.equal((container.innerHTML.match(/data-key=/g) || []).length, countFields(IOS_META),
    "旧响应晚到不得覆盖新渲染（generation 守卫）");
  assert.ok(!container.innerHTML.includes("http-api"), "旧 Mac-only 字段不得回填");
  console.log("T7c 旧响应晚到不覆盖新渲染 通过");

  // --- T7d save 等待期间档案变更：中止提交（meta_stale，fail closed） ---
  let resolveSave;
  const delayedPage = createPage({
    fieldsHandler(url) {
      const m = /profile_id=(\d+)/.exec(url);
      const target = delayedPage.targets.get(Number(m[1]));
      return new Promise((resolve) => {
        resolveSave = () => resolve(META_FIXTURE[target]);
      });
    },
  });
  regGeneralTab(delayedPage, 3, JSON.stringify({ "test-timeout": 5 }));
  const savePromise = delayedPage.sandbox.__hooks.saveGeneralValues(3, makeElement());
  await updateProfileViaUI(delayedPage, 3);  // 等待期间档案 PUT 成功 → 失效
  resolveSave();                              // 旧窗口的 metadata 响应到达
  await savePromise;
  assert.ok(!generalPuts(delayedPage).length, "metadata 过期必须中止提交");
  assert.ok(delayedPage.toasts.some(t =>
    t.type === "error" && t.msg === "档案刚被修改，字段元数据已过期，请重新打开此标签页后再保存"),
    "必须提示 metadata 已过期");
  console.log("T7d save 等待期间档案变更中止提交 通过");

  // --- T7e save 重新拉当前 metadata：target 已变则用新 forced 集合 ---
  const pageE = createPage();
  regGeneralTab(pageE, 1, JSON.stringify({
    "auto-suspend": true,        // iOS-only：target 变更后属于 forced → 必须移除
    "compatibility-mode": 3,     // 同上
    "block-quic": "proxy",       // 公共 forced：始终移除
    "test-timeout": 5,           // 普通键：保留
  }));
  await pageE.sandbox.__hooks.renderGeneralTab(1);  // 旧 target（workstation）渲染
  await updateProfileViaUI(pageE, 1);
  pageE.targets.set(1, "ios_mobile");               // PUT 把 target 改为 iOS
  control(pageE.formControls, "loglevel", "select", { value: "warning" });
  await pageE.sandbox.__hooks.saveGeneralValues(1, makeElement());
  const put = generalPuts(pageE)[0];
  assert.ok(put, "save 必须发出 PUT");
  assert.ok(!("auto-suspend" in put.body), "target 已变 iOS：save 必须按新 forced 集合移除");
  assert.ok(!("compatibility-mode" in put.body), "target 已变 iOS：save 必须按新 forced 集合移除");
  assert.ok(!("block-quic" in put.body), "公共 forced 键始终移除");
  assert.equal(put.body["test-timeout"], 5, "普通 custom 键照常提交");
  assert.equal(put.body["loglevel"], "warning", "表单普通键照常提交");
  console.log("T7e save 重新拉当前 metadata（新 forced 集合）通过");

  // --- T7f MITM 等档案 PUT 同样失效（同一 invalidate 代码路径）---
  const pageF = createPage();
  regGeneralTab(pageF, 2);
  await pageF.sandbox.__hooks.renderGeneralTab(2);
  assert.equal(pageF.sandbox.__hooks.generalMetaGen(2), 0);
  // saveMitm 真实路径：注册 MITM 表单元素
  pageF.reg("mitm-skip-2", { checked: false });
  pageF.reg("mitm-tcp-2", { checked: false });
  pageF.reg("mitm-h2-2", { checked: false });
  pageF.reg("mitm-passphrase-2", { value: "" });
  pageF.reg("mitm-cap12-2", { value: "" });
  pageF.reg("mitm-hostname-2", { value: "" });
  await pageF.sandbox.__hooks.saveMitm(2);
  assert.equal(pageF.sandbox.__hooks.generalMetaGen(2), 1,
    "任何档案 PUT 成功（含 MITM）都必须推进 generation");
  console.log("T7f MITM 档案 PUT 同样失效 通过");
}

// ===== T8 General values 乱序：旧 values 晚到不污染新表单 =====

async function t8() {
  // --- T8a 旧 iOS values 晚于新 Mac 表单：generation 守卫 ---
  let resolveIosValues;
  let valuesSeq = 0;
  const page = createPage({
    valuesHandler(url) {
      const m = /profile_id=(\d+)/.exec(url);
      valuesSeq += 1;
      if (Number(m[1]) === 1 && valuesSeq === 1) {
        return new Promise((resolve) => {
          resolveIosValues = () => resolve({
            values: { "loglevel": "notify", "auto-suspend": true, "compatibility-mode": 3 },
          });
        });
      }
      return { values: { "loglevel": "warning" } };
    },
  });
  regGeneralTab(page, 1);
  const iosRender = page.sandbox.__hooks.renderGeneralTab(1);  // 旧 iOS values pending

  // 档案更新（target iOS → Mac）推进 generation，再完整重渲染新表单
  await updateProfileViaUI(page, 1);
  page.targets.set(1, "mac_workstation");
  await page.sandbox.__hooks.renderGeneralTab(1);

  // 新 Mac 表单控件落位（真实浏览器由 innerHTML 解析；stub 手动注册）
  page.formControls.length = 0;
  const loglevel = control(page.formControls, "loglevel", "select", { value: "" });
  const httpApi = control(page.formControls, "http-api", "text", { value: "" });
  assert.equal(loglevel.value, "", "前提：新表单初始为空");
  // 用 Mac values（立即到达）填充新表单：走真实 fillGeneralForm 路径
  await page.sandbox.__hooks.fillGeneralForm(1, { "loglevel": "warning" });
  assert.equal(loglevel.value, "warning", "前提：Mac values 已填充新表单");

  resolveIosValues();   // 旧 iOS values 晚到（loglevel=notify + iOS 专属键）
  await iosRender;
  assert.equal(loglevel.value, "warning", "旧 iOS values 不得污染新 Mac 表单（loglevel）");
  assert.equal(httpApi.value, "", "旧 iOS values 不含 Mac-only 键，保持为空");
  assert.equal(loglevel.value !== "notify", true, "notify 是旧 iOS 值，不得回填");
  console.log("T8a 旧 iOS values 晚于新 Mac 表单不污染 通过");

  // --- T8b values 挂起期间档案删除：generation 不一致不填充 ---
  let resolveDeletedValues;
  const pageB = createPage({
    valuesHandler() {
      return new Promise((resolve) => {
        resolveDeletedValues = () => resolve({ values: { "loglevel": "notify" } });
      });
    },
  });
  regGeneralTab(pageB, 1);
  const renderB = pageB.sandbox.__hooks.renderGeneralTab(1);
  await new Promise((r) => setImmediate(r));   // 等 metadata 阶段走完，values 已挂起
  const lvlB = control(pageB.formControls, "loglevel", "select", { value: "" });
  await pageB.sandbox.__hooks.deleteProfile(1, makeElement());   // 删除 → 失效
  resolveDeletedValues();                                        // 旧 values 晚到
  await renderB;
  assert.equal(lvlB.value, "", "档案删除后旧 values 不得填充");
  console.log("T8b values 挂起期间档案删除不填充 通过");

  // --- T8c values 挂起期间 tab 已卸载（容器消失）不填充 ---
  let resolveUnmountedValues;
  const pageC = createPage({
    valuesHandler() {
      return new Promise((resolve) => {
        resolveUnmountedValues = () => resolve({ values: { "loglevel": "notify" } });
      });
    },
  });
  regGeneralTab(pageC, 1);
  const renderC = pageC.sandbox.__hooks.renderGeneralTab(1);
  await new Promise((r) => setImmediate(r));
  const lvlC = control(pageC.formControls, "loglevel", "select", { value: "" });
  pageC.sandbox.document.getElementById = () => null;      // 模拟容器已卸载
  resolveUnmountedValues();
  await renderC;
  assert.equal(lvlC.value, "", "tab 已卸载后旧 values 不得填充");
  console.log("T8c values 挂起期间 tab 卸载不填充 通过");
}

(async () => {
  await t2();
  await t3();
  await t4();
  await t5();
  await t7();
  await t8();
  console.log("JS 行为测试：forced 渲染禁用/徽标 + 完整 fixture 元数据 + 无持久缓存"
    + " + 档案生命周期失效（update/delete/旧响应晚到/save 重拉）+ values 乱序守卫"
    + " + fail closed 全部通过");
})().catch((e) => { console.error(e); process.exit(1); });
