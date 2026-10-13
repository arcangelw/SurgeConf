/** 订阅管理页（由 subscriptions.html 内联 JS 迁出） */

async function loadList() {
  try {
    const subs = await API.get("/api/subscriptions");
    const tbody = document.getElementById("sub-list");
    const empty = document.getElementById("empty-state");

    if (subs.length === 0) {
      tbody.innerHTML = "";
      empty.style.display = "block";
      return;
    }
    empty.style.display = "none";

    tbody.innerHTML = subs.map(s => `
      <tr>
        <td>
          <div style="display:flex;align-items:center;gap:8px;">
            <span style="width:10px;height:10px;border-radius:50%;background:${s.color};flex-shrink:0;"></span>
            <strong>${escapeHtml(s.name)}</strong>
          </div>
        </td>
        <td>
          ${s.tag ? `<span class="badge" style="background:${s.color}22;color:${s.color};border:1px solid ${s.color}44;">${escapeHtml(s.tag)}</span>` : '<span style="color:var(--text2);">-</span>'}
        </td>
        <td style="max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">
          <span title="${escapeHtml(s.url)}" style="font-size:12px;color:var(--text2);">${escapeHtml(s.url)}</span>
        </td>
        <td><span class="badge ${s.enabled ? 'badge-success' : 'badge-danger'}">${s.enabled ? I18N["btn.enable"] : I18N["btn.disable"]}</span></td>
        <td style="color:var(--text2);font-size:12px;">${s.last_update || I18N["sub.not_synced"]}</td>
        <td>
          <div style="display:flex;gap:6px;">
            <button class="btn btn-sm btn-outline" onclick="syncOne(${s.id}, this)">${I18N["btn.sync"]}</button>
            <button class="btn btn-sm btn-outline" onclick='editSub(${JSON.stringify(s)})'>${I18N["btn.edit"]}</button>
            <button class="btn btn-sm btn-danger" onclick="deleteSub(${s.id}, '${escapeHtml(s.name)}')">${I18N["btn.delete"]}</button>
          </div>
        </td>
      </tr>
    `).join("");
    compactActions();
  } catch (e) {
    toast(e.message, "error");
  }
}

function showAddModal() {
  document.getElementById("modal-title").textContent = I18N["btn.add"];
  document.getElementById("edit-id").value = "";
  document.getElementById("sub-name").value = "";
  document.getElementById("sub-tag").value = "";
  document.getElementById("sub-color").value = "#6c5ce7";
  document.getElementById("sub-url").value = "";
  document.getElementById("sub-enabled").checked = true;
  document.getElementById("sub-auto-update").checked = true;
  document.getElementById("sub-interval").value = 86400;
  showModal("sub-modal");
}

function editSub(s) {
  document.getElementById("modal-title").textContent = I18N["btn.edit"];
  document.getElementById("edit-id").value = s.id;
  document.getElementById("sub-name").value = s.name;
  document.getElementById("sub-tag").value = s.tag || "";
  document.getElementById("sub-color").value = s.color || "#6c5ce7";
  document.getElementById("sub-url").value = s.url;
  document.getElementById("sub-enabled").checked = s.enabled;
  showModal("sub-modal");
}

async function saveSubscription() {
  const id = document.getElementById("edit-id").value;
  const nameEl = document.getElementById("sub-name");
  const urlEl = document.getElementById("sub-url");
  if (!nameEl.value.trim()) {
    toast(I18N["msg.required"], "error"); flashInput(nameEl); return;
  }
  if (!urlEl.value.trim()) {
    toast(I18N["msg.required"], "error"); flashInput(urlEl); return;
  }
  const data = {
    name: nameEl.value.trim(),
    tag: document.getElementById("sub-tag").value || null,
    color: document.getElementById("sub-color").value,
    url: urlEl.value.trim(),
    enabled: document.getElementById("sub-enabled").checked,
    auto_update: document.getElementById("sub-auto-update").checked,
    update_interval: parseInt(document.getElementById("sub-interval").value) || 86400,
  };

  try {
    if (id) {
      await API.put(`/api/subscriptions/${id}`, data);
      toast(I18N["msg.updated"]);
    } else {
      await API.post("/api/subscriptions", data);
      toast(I18N["msg.added"]);
    }
    hideModal("sub-modal");
    loadList();
  } catch (e) { toast(e.message, "error"); }
}

async function syncOne(id, btn) {
  await withLoading(btn, async () => {
    try {
      showLoading();
      const result = await API.post(`/api/subscriptions/${id}/sync`);
      toast(I18N["sub.sync_done"].replace("{0}", result.subscription).replace("{1}", result.node_count));
      loadList();
    } catch (e) { toast(e.message, "error"); }
    finally { hideLoading(); }
  });
}

async function syncAll(btn) {
  await withLoading(btn, async () => {
    try {
      showLoading();
      const result = await API.post("/api/subscriptions/sync-all");
      toast(I18N["sub.sync_all_done"].replace("{0}", result.results.length));
      loadList();
    } catch (e) { toast(e.message, "error"); }
    finally { hideLoading(); }
  });
}

async function deleteSub(id, name) {
  if (!(await confirmDialog(I18N["sub.confirm_delete"].replace("{0}", name), { danger: true }))) return;
  try {
    await API.del(`/api/subscriptions/${id}`);
    toast(I18N["msg.deleted"]);
    loadList();
  } catch (e) { toast(e.message, "error"); }
}

loadList();
