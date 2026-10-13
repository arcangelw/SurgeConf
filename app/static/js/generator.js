/** 生成配置页：预览/下载/订阅链接（由 generator.html 内联 JS 迁出） */

const SERVER_PORT = location.port || (location.protocol === "https:" ? "443" : "80");

function getLocaleParam() {
  const val = document.getElementById("locale-select").value;
  return val ? `&locale=${val}` : "";
}

function getProfileParam() {
  const val = document.getElementById("profile-select").value;
  return val ? `&profile_id=${val}` : "";
}

async function loadServerIps() {
  const select = document.getElementById("ip-select");
  try {
    const data = await API.get("/api/configs/server-ips");
    const ips = data.ips || [];
    const loOpt = document.createElement("option");
    loOpt.value = "127.0.0.1";
    loOpt.textContent = I18N["generator.localhost"];
    select.appendChild(loOpt);
    ips.forEach(ip => {
      const opt = document.createElement("option");
      opt.value = ip;
      opt.textContent = I18N["generator.lan_ip"].replace("{0}", ip);
      select.appendChild(opt);
    });
    if (ips.length > 0) select.value = ips[0];
  } catch (e) {
    const opt = document.createElement("option");
    opt.value = location.hostname;
    opt.textContent = location.hostname;
    select.appendChild(opt);
  }
  updateSubscribeUrl();
}

function buildBaseUrl(ip) {
  const port = SERVER_PORT === "80" || SERVER_PORT === "443" ? "" : `:${SERVER_PORT}`;
  return `${location.protocol}//${ip}${port}`;
}

function updateSubscribeUrl() {
  const ip = document.getElementById("ip-select").value;
  const base = buildBaseUrl(ip);
  let url = `${base}/api/configs/subscribe?`;
  const profileId = document.getElementById("profile-select").value;
  const locale = document.getElementById("locale-select").value;
  if (profileId) url += `profile_id=${profileId}&`;
  if (locale) url += `locale=${locale}&`;
  document.getElementById("subscribe-url").value = url.replace(/[?&]$/, "");
}

async function loadProfiles() {
  try {
    const profiles = await API.get("/api/configs/profiles");
    const select = document.getElementById("profile-select");
    profiles.forEach(p => {
      const opt = document.createElement("option");
      opt.value = p.id;
      opt.textContent = `${p.name}${p.is_default ? ` ${I18N["msg.default"]}` : ''}`;
      select.appendChild(opt);
    });
  } catch (e) { /* ignore */ }
}

document.getElementById("profile-select").addEventListener("change", updateSubscribeUrl);
document.getElementById("ip-select").addEventListener("change", updateSubscribeUrl);
document.getElementById("locale-select").addEventListener("change", updateSubscribeUrl);

async function previewConfig() {
  const params = new URLSearchParams();
  const profileId = document.getElementById("profile-select").value;
  const locale = document.getElementById("locale-select").value;
  if (profileId) params.set("profile_id", profileId);
  if (locale) params.set("locale", locale);
  const url = "/api/configs/preview?" + params.toString();
  try {
    showLoading();
    const result = await API.get(url);
    const sizeLabel = `${(result.size / 1024).toFixed(1)} KB`;
    document.getElementById("config-size").textContent = sizeLabel;
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

function downloadConfig() {
  const filename = document.getElementById("filename-input").value.trim() || "Surge.conf";
  let url = "/api/configs/generate?filename=" + encodeURIComponent(filename);
  url += getProfileParam() + getLocaleParam();
  window.open(url, "_blank");
}

function copySubscribeUrl() {
  const input = document.getElementById("subscribe-url");
  input.select();
  input.setSelectionRange(0, 99999);
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

Promise.all([loadProfiles(), loadServerIps()]);
