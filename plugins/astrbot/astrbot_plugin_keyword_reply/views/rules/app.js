const bridge = window.AstrBotPluginView;
const listEl = document.getElementById("ruleList");
const editorEl = document.getElementById("editor");
const previewEl = document.getElementById("preview");
const stateEl = document.getElementById("saveState");
const saveButton = document.getElementById("save");
const searchEl = document.getElementById("ruleSearch");
const deleteDialog = document.getElementById("deleteDialog");

let rules = [];
let selected = -1;
let dirty = false;
let saving = false;
let uploading = 0;
let filter = "all";
let query = "";
let advancedOpen = false;
let deleteTarget = null;

document.documentElement.dataset.theme =
  new URLSearchParams(location.search).get("theme") === "dark" ? "dark" : "light";

const esc = value => String(value ?? "").replace(/[&<>"']/g, ch =>
  ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[ch]));
const option = (value, current, label) =>
  `<option value="${value}" ${current === value ? "selected" : ""}>${label}</option>`;
const check = (label, name, checked) =>
  `<label class="check"><input type="checkbox" data-check="${name}" ${checked ? "checked" : ""}>${label}</label>`;

function newRule() {
  return {
    id: "", group_id: "", topic_id: "", pattern: "", mode: "contains",
    targets: ["text"], ignore_case: true, ignore_forward: false, enabled: true,
    templates: [{kind: "text", text: ""}], response_mode: "random",
    reply_to_source: true, delete_source: false, cooldown_seconds: 0, priority: 0
  };
}

function setStatus(message, type = "") {
  stateEl.textContent = message;
  stateEl.className = `save-state ${type}`;
}

function updateSaveButton() {
  saveButton.disabled = !dirty || saving || uploading > 0;
  saveButton.textContent = saving ? "正在保存…" : "保存更改";
  editorEl.inert = saving;
  listEl.inert = saving;
  document.getElementById("add").disabled = saving;
  document.getElementById("importRules").disabled = saving;
}

function changed() {
  dirty = true;
  setStatus("有未保存的更改", "dirty");
  updateSaveButton();
  renderList();
  renderPreview();
}

function visibleIndexes() {
  const needle = query.trim().toLocaleLowerCase();
  return rules.flatMap((rule, index) => {
    if (filter === "enabled" && !rule.enabled) return [];
    if (filter === "disabled" && rule.enabled) return [];
    if (needle && !`${rule.pattern} ${rule.group_id}`.toLocaleLowerCase().includes(needle)) return [];
    return [index];
  });
}

function renderList() {
  document.getElementById("count").textContent = rules.length;
  const visible = visibleIndexes();
  listEl.innerHTML = visible.length ? visible.map(index => {
    const rule = rules[index];
    return `<button type="button" class="rule-item ${index === selected ? "active" : ""} ${rule.enabled ? "" : "off"}" data-index="${index}" aria-current="${index === selected ? "true" : "false"}">
      <span class="rule-name">${esc(rule.pattern?.trim() || "未命名规则")}</span>
      <span class="rule-meta"><span class="state-dot" aria-hidden="true"></span><span class="group">${esc(rule.group_id || "待填写群 ID")}</span><span>·</span><span>${rule.enabled ? "已启用" : "已停用"}</span></span>
    </button>`;
  }).join("") : `<p class="list-empty">${rules.length ? "没有符合条件的规则。试试其他关键词或筛选条件。" : "还没有规则。点击“新建”开始。"}</p>`;
  listEl.querySelectorAll("[data-index]").forEach(button => button.addEventListener("click", () => {
    selected = Number(button.dataset.index);
    renderList();
    renderEditor();
    renderPreview();
  }));
}

function renderPreview() {
  const rule = rules[selected];
  if (!rule) {
    previewEl.innerHTML = `<div class="chat-bar"><span class="chat-avatar">♧</span>Telegram 群聊</div><p class="preview-empty">选中一条规则后，这里会显示群消息和机器人回复。</p>`;
    return;
  }
  const templates = rule.response_mode === "all" ? rule.templates : rule.templates.slice(0, 1);
  const messages = templates.map(template => {
    let body;
    if (template.kind === "text") body = esc(template.text?.trim() || "（填写回复文字后显示）");
    else body = `📎 ${esc(template.filename || "已上传媒体")}${template.caption ? "\n" + esc(template.caption) : ""}`;
    return `<div class="message bot"><span class="sender">机器人</span>${body}</div>`;
  }).join("");
  previewEl.innerHTML = `<div class="chat-bar"><span class="chat-avatar">♧</span>${esc(rule.group_id || "Telegram 群聊")}</div>
    <div class="message user">${esc(rule.pattern?.trim() || "触发消息")}</div>
    ${messages}
    ${rule.response_mode === "random" && rule.templates.length > 1 ? `<p class="hint">随机回复示例：1 / ${rule.templates.length}</p>` : ""}`;
}

function selectRule(index) {
  selected = index;
  renderList();
  renderEditor();
  renderPreview();
}

function renderEditor() {
  const rule = rules[selected];
  if (!rule) {
    editorEl.innerHTML = `<div class="empty-state"><div class="empty-symbol" aria-hidden="true">✳</div><h2>还没有选中规则</h2><p>新建一条规则，设置触发内容和自动回复。</p></div>`;
    return;
  }
  editorEl.innerHTML = `
    <div class="editor-head">
      <div><span class="eyebrow">当前规则</span><h2 id="ruleTitle">${esc(rule.pattern?.trim() || "未命名规则")}</h2><p>匹配成功后，机器人将发送下方设置的回复。</p></div>
      <div class="editor-actions"><button id="duplicateRule" class="button button-quiet" type="button">复制</button><button id="removeRule" class="button button-text-danger" type="button">删除</button></div>
    </div>
    <section class="rule-section">
      <div class="section-title"><div><h3>触发消息</h3><p>先指定群，再填写成员发送的关键词。</p></div><label class="switch"><input type="checkbox" data-check="enabled" ${rule.enabled ? "checked" : ""}><span class="switch-track" aria-hidden="true"></span>启用规则</label></div>
      <div class="form-grid">
        <div class="field"><label for="groupId">Telegram 群 ID</label><input id="groupId" data-field="group_id" inputmode="numeric" maxlength="32" value="${esc(rule.group_id)}" placeholder="-1001234567890"><p class="hint">在群里发送 /sid 可查看。</p></div>
        <div class="field"><label for="topicId">话题 ID <span class="hint">可选</span></label><input id="topicId" data-field="topic_id" inputmode="numeric" maxlength="32" value="${esc(rule.topic_id)}" placeholder="留空表示整个群"></div>
        <div class="field full"><label for="pattern">关键词或正则表达式</label><input id="pattern" data-field="pattern" maxlength="256" value="${esc(rule.pattern)}" placeholder="例如：烧饼论坛插件"></div>
        <div class="field"><label for="mode">匹配方式</label><select id="mode" data-field="mode">${option("contains", rule.mode, "包含关键词")}${option("exact", rule.mode, "完全相同")}${option("regex", rule.mode, "正则表达式")}</select></div>
        <div class="field full target-field" id="matchTargets"><span class="label">匹配范围</span><div class="check-row">${check("消息文字 / 媒体说明", "target:text", rule.targets.includes("text"))}${check("文件名", "target:filename", rule.targets.includes("filename"))}${check("贴纸 Emoji", "target:sticker_emoji", rule.targets.includes("sticker_emoji"))}</div><p class="hint">可多选；任意一项命中就会触发回复，至少保留一项。</p></div>
      </div>
    </section>
    <section class="rule-section">
      <div class="section-title"><div><h3>发送回复</h3><p>可以设置多条文字或媒体回复。</p></div></div>
      <div id="templates" class="template-list"></div>
      <div class="template-actions"><button id="addTemplate" class="button button-small" type="button">＋ 添加一条回复</button><p class="hint">支持图片、GIF、视频、音频和 PDF，单个文件不超过 20 MB。</p></div>
      <div class="form-grid" style="margin-top:20px">
        <div class="field"><label for="responseMode">多条回复如何发送</label><select id="responseMode" data-field="response_mode">${option("random", rule.response_mode, "随机发送一条")}${option("all", rule.response_mode, "按顺序发送全部")}</select></div>
        <div class="field"><label for="cooldown">再次触发间隔（秒）</label><input id="cooldown" type="number" min="0" max="86400" data-field="cooldown_seconds" value="${esc(rule.cooldown_seconds)}"><p class="hint">0 表示不限制。</p></div>
      </div>
    </section>
    <details class="rule-section advanced" ${advancedOpen ? "open" : ""}><summary>高级设置</summary><div class="advanced-content">
      <div class="form-grid"><div class="field"><label for="priority">优先级</label><input id="priority" type="number" min="-100" max="100" data-field="priority" value="${esc(rule.priority)}"><p class="hint">多条规则同时命中时，数字大的先执行。</p></div></div>
      <p class="subheading">匹配选项</p><div class="check-row">${check("忽略英文大小写", "ignore_case", rule.ignore_case)}${check("忽略转发消息", "ignore_forward", rule.ignore_forward)}</div>
      <p class="subheading">发送动作</p><div class="check-row">${check("引用触发消息", "reply_to_source", rule.reply_to_source)}${check("删除触发消息", "delete_source", rule.delete_source)}</div>
      <p class="hint">删除消息需要机器人在群里拥有删除权限。</p>
    </div></details>`;

  editorEl.querySelector(".advanced").addEventListener("toggle", event => {
    advancedOpen = event.target.open;
  });
  editorEl.querySelector("#removeRule").addEventListener("click", () => {
    deleteTarget = rule;
    deleteDialog.returnValue = "";
    document.getElementById("deleteDescription").textContent =
      `“${rule.pattern || "未命名规则"}”将从列表移除。点击“保存更改”后才会生效。`;
    deleteDialog.showModal();
  });
  editorEl.querySelector("#duplicateRule").addEventListener("click", () => {
    if (rules.length >= 500) return setStatus("最多支持 500 条规则", "error");
    const copy = structuredClone(rule);
    copy.id = "";
    copy.pattern = "";
    rules.push(copy);
    query = "";
    filter = "all";
    searchEl.value = "";
    syncFilterButtons();
    selectRule(rules.length - 1);
    changed();
    editorEl.querySelector("#pattern").focus();
  });
  editorEl.querySelectorAll("[data-field]").forEach(input => {
    const update = () => {
      const key = input.dataset.field;
      rule[key] = ["priority", "cooldown_seconds"].includes(key) ?
        (input.value === "" ? NaN : Number(input.value)) : input.value;
      clearFieldError(input);
      if (key === "pattern") document.getElementById("ruleTitle").textContent = rule.pattern.trim() || "未命名规则";
      changed();
    };
    input.addEventListener(input.tagName === "SELECT" ? "change" : "input", update);
  });
  editorEl.querySelectorAll("[data-check]").forEach(input => input.addEventListener("change", () => {
    const key = input.dataset.check;
    if (key.startsWith("target:")) {
      const target = key.slice(7);
      if (!input.checked && rule.targets.length === 1 && rule.targets[0] === target) {
        input.checked = true;
        setStatus("匹配范围至少保留一项", "error");
        return;
      }
      rule.targets = input.checked ? [...new Set([...rule.targets, target])] : rule.targets.filter(value => value !== target);
      editorEl.querySelector("#matchTargets .field-error")?.remove();
    } else rule[key] = input.checked;
    changed();
    if (key === "enabled" && !visibleIndexes().includes(selected)) {
      selected = visibleIndexes()[0] ?? -1;
      selectRule(selected);
    }
  }));
  editorEl.querySelector("#addTemplate").addEventListener("click", () => {
    if (rule.templates.length >= 20) return setStatus("一条规则最多有 20 条回复", "error");
    rule.templates.push({kind: "text", text: ""});
    changed();
    renderTemplates();
    editorEl.querySelector(`[data-text="${rule.templates.length - 1}"]`)?.focus();
  });
  renderTemplates();
}

function clearFieldError(input) {
  input.classList.remove("invalid");
  input.closest(".field")?.querySelector(".field-error")?.remove();
}

function renderTemplates() {
  const rule = rules[selected];
  const root = document.getElementById("templates");
  if (!rule || !root) return;
  root.innerHTML = rule.templates.map((template, index) => `<div class="template">
    <div class="template-head"><div><strong>回复 ${index + 1}</strong><span class="template-meta">${template.kind === "text" ? "文字" : esc(template.filename || "媒体")}</span></div><button class="button button-text-danger" type="button" data-remove="${index}" aria-label="移除回复 ${index + 1}">移除</button></div>
    ${template.kind === "text" ?
      `<div class="field"><label for="text-${index}">回复文字</label><textarea id="text-${index}" data-text="${index}" maxlength="4096" placeholder="输入机器人要发送的内容">${esc(template.text)}</textarea></div>` :
      `<div class="field"><label for="caption-${index}">媒体说明</label><textarea id="caption-${index}" data-caption="${index}" maxlength="1024" placeholder="可留空">${esc(template.caption)}</textarea></div>`}
    <div class="template-actions"><label class="upload-label">${template.kind === "text" ? "上传媒体替换文字" : "更换媒体"}<input type="file" data-upload="${index}" accept=".jpg,.jpeg,.png,.webp,.gif,.mp4,.mp3,.ogg,.wav,.pdf"></label></div>
  </div>`).join("");

  root.querySelectorAll("[data-remove]").forEach(button => button.addEventListener("click", () => {
    if (rule.templates.length <= 1) return setStatus("至少保留一条回复", "error");
    rule.templates.splice(Number(button.dataset.remove), 1);
    changed();
    renderTemplates();
  }));
  root.querySelectorAll("[data-text]").forEach(input => input.addEventListener("input", () => {
    rule.templates[Number(input.dataset.text)].text = input.value;
    clearFieldError(input);
    changed();
  }));
  root.querySelectorAll("[data-caption]").forEach(input => input.addEventListener("input", () => {
    rule.templates[Number(input.dataset.caption)].caption = input.value;
    clearFieldError(input);
    changed();
  }));
  root.querySelectorAll("[data-upload]").forEach(input => input.addEventListener("change", async () => {
    const file = input.files?.[0];
    if (!file) return;
    if (file.size === 0 || file.size > 20 * 1024 * 1024) {
      input.value = "";
      return setStatus("文件大小需在 1 字节到 20 MB 之间", "error");
    }
    const index = Number(input.dataset.upload);
    const originalTemplate = rule.templates[index];
    uploading++;
    updateSaveButton();
    setStatus("正在上传媒体…");
    try {
      const result = await bridge.upload("media/upload", file);
      const currentIndex = rule.templates.indexOf(originalTemplate);
      if (currentIndex < 0) {
        setStatus("媒体已上传，但对应回复已被移除", "error");
        return;
      }
      rule.templates[currentIndex] = {
        kind: result.kind, media_id: result.media_id, filename: result.filename,
        caption: originalTemplate.caption || ""
      };
      changed();
      if (rules[selected] === rule) renderTemplates();
      setStatus("媒体已上传，记得保存更改", "dirty");
    } catch (error) {
      setStatus(`上传失败：${error.message}`, "error");
    } finally {
      uploading--;
      updateSaveButton();
      input.value = "";
    }
  }));
}

function validateRule(rule) {
  if (typeof rule.group_id !== "string" || !/^-?\d{1,31}$/.test(rule.group_id)) return ["请输入有效的 Telegram 数字群 ID", "#groupId"];
  if (typeof rule.topic_id !== "string" || (rule.topic_id && !/^\d{1,32}$/.test(rule.topic_id))) return ["话题 ID 只能包含数字", "#topicId"];
  if (typeof rule.pattern !== "string" || !rule.pattern.trim() || rule.pattern.trim().length > 256) return ["请输入不超过 256 字的关键词", "#pattern"];
  if (!["contains", "exact", "regex"].includes(rule.mode)) return ["匹配方式无效", "#mode"];
  if (!Array.isArray(rule.targets) || !rule.targets.length || rule.targets.some(t => !["text", "filename", "sticker_emoji"].includes(t)))
    return ["至少选择一种匹配范围", "#matchTargets"];
  if (!Array.isArray(rule.templates) || !rule.templates.length || rule.templates.length > 20) return ["请设置 1 到 20 条回复", "#templates"];
  for (let index = 0; index < rule.templates.length; index++) {
    const template = rule.templates[index];
    if (!template || typeof template !== "object") return [`回复 ${index + 1} 格式无效`, "#templates"];
    if (template.kind === "text") {
      if (typeof template.text !== "string" || !template.text.trim() || template.text.trim().length > 4096)
        return [`回复 ${index + 1} 需要文字内容（最多 4096 字）`, `[data-text="${index}"]`];
    } else if (!["photo", "video", "animation", "audio", "voice", "document"].includes(template.kind) ||
      !/^[a-f0-9]{32}$/.test(template.media_id || "") || typeof template.caption !== "string" || template.caption.length > 1024) {
      return [`回复 ${index + 1} 的媒体内容无效`, `[data-caption="${index}"]`];
    }
  }
  if (!["random", "all"].includes(rule.response_mode)) return ["发送方式无效", "#responseMode"];
  if (!Number.isInteger(rule.cooldown_seconds) || rule.cooldown_seconds < 0 || rule.cooldown_seconds > 86400)
    return ["触发间隔需在 0 到 86400 秒之间", "#cooldown"];
  if (!Number.isInteger(rule.priority) || rule.priority < -100 || rule.priority > 100)
    return ["优先级需在 -100 到 100 之间", "#priority"];
  return null;
}

function showValidationError(message, selector) {
  setStatus(message, "error");
  const input = editorEl.querySelector(selector);
  if (selector === "#priority") {
    const details = editorEl.querySelector(".advanced");
    details.open = true;
    advancedOpen = true;
  }
  if (input && input.matches("input, textarea, select")) {
    input.classList.add("invalid");
    input.closest(".field")?.insertAdjacentHTML("beforeend", `<p class="field-error">${esc(message)}</p>`);
  } else if (input?.classList.contains("field")) {
    input.insertAdjacentHTML("beforeend", `<p class="field-error">${esc(message)}</p>`);
  }
  input?.focus();
  input?.scrollIntoView({block: "center", behavior: "smooth"});
}

function syncFilterButtons() {
  document.querySelectorAll("[data-filter]").forEach(button => {
    const active = button.dataset.filter === filter;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  });
}

document.getElementById("add").addEventListener("click", () => {
  if (rules.length >= 500) return setStatus("最多支持 500 条规则", "error");
  rules.push(newRule());
  query = "";
  filter = "all";
  searchEl.value = "";
  syncFilterButtons();
  selectRule(rules.length - 1);
  changed();
  editorEl.querySelector("#groupId").focus();
});
searchEl.addEventListener("input", () => {
  query = searchEl.value;
  const visible = visibleIndexes();
  if (!visible.includes(selected)) selected = visible[0] ?? -1;
  renderList();
  renderEditor();
  renderPreview();
});
document.querySelectorAll("[data-filter]").forEach(button => button.addEventListener("click", () => {
  filter = button.dataset.filter;
  syncFilterButtons();
  const visible = visibleIndexes();
  if (!visible.includes(selected)) selected = visible[0] ?? -1;
  renderList();
  renderEditor();
  renderPreview();
}));
deleteDialog.addEventListener("close", () => {
  if (deleteDialog.returnValue !== "delete" || !deleteTarget) {
    deleteTarget = null;
    return;
  }
  const index = rules.indexOf(deleteTarget);
  deleteTarget = null;
  if (index < 0) return;
  rules.splice(index, 1);
  selected = Math.min(index, rules.length - 1);
  const visible = visibleIndexes();
  if (!visible.includes(selected)) selected = visible[0] ?? -1;
  selectRule(selected);
  changed();
});
document.getElementById("importRules").addEventListener("click", () => document.getElementById("importFile").click());
document.getElementById("importFile").addEventListener("change", async event => {
  const file = event.target.files?.[0];
  if (!file) return;
  try {
    if (file.size > 256 * 1024) throw new Error("规则文件不能超过 256 KB");
    const parsed = JSON.parse(await file.text());
    const incoming = Array.isArray(parsed) ? parsed : parsed?.rules;
    if (!Array.isArray(incoming)) throw new Error("文件需要是规则数组，或包含 rules 数组的对象");
    const additions = [];
    for (const [index, raw] of incoming.entries()) {
      if (!raw || typeof raw !== "object" || Array.isArray(raw)) throw new Error(`第 ${index + 1} 条规则格式无效`);
      const candidate = {...newRule(), ...raw};
      const error = validateRule(candidate);
      if (error) throw new Error(`第 ${index + 1} 条规则：${error[0]}`);
      const duplicate = [...rules, ...additions].some(existing =>
        (candidate.id && existing.id === candidate.id) ||
        (existing.group_id === candidate.group_id && existing.pattern === candidate.pattern && existing.mode === candidate.mode));
      if (duplicate) continue;
      if (rules.length + additions.length >= 500) throw new Error("最多支持 500 条规则");
      additions.push(candidate);
    }
    if (additions.length) {
      const first = rules.length;
      rules.push(...additions);
      query = "";
      filter = "all";
      searchEl.value = "";
      syncFilterButtons();
      selectRule(first);
      changed();
    }
    setStatus(`已导入 ${additions.length} 条规则${additions.length ? "，请检查并保存" : "（重复规则已跳过）"}`, additions.length ? "dirty" : "success");
  } catch (error) {
    setStatus(`导入失败：${error.message}`, "error");
  } finally {
    event.target.value = "";
  }
});
document.getElementById("exportRules").addEventListener("click", () => {
  const blob = new Blob([JSON.stringify({rules}, null, 2)], {type: "application/json"});
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = `astrbot-keyword-rules-${new Date().toISOString().slice(0, 10)}.json`;
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  setStatus(`已导出 ${rules.length} 条规则${dirty ? "（包含未保存更改）" : ""}`, dirty ? "dirty" : "success");
});

async function saveRules() {
  if (!dirty || saving || uploading) return;
  for (const [index, rule] of rules.entries()) {
    const error = validateRule(rule);
    if (error) {
      query = "";
      filter = "all";
      searchEl.value = "";
      syncFilterButtons();
      selectRule(index);
      showValidationError(...error);
      return;
    }
  }
  saving = true;
  updateSaveButton();
  setStatus("正在保存…");
  try {
    const result = await bridge.apiPost("rules/save", {rules});
    rules = result.rules;
    selected = Math.min(selected, rules.length - 1);
    dirty = false;
    renderList();
    renderEditor();
    renderPreview();
    setStatus("已保存", "success");
  } catch (error) {
    setStatus(`保存失败：${error.message}`, "error");
  } finally {
    saving = false;
    updateSaveButton();
  }
}
saveButton.addEventListener("click", saveRules);
document.addEventListener("keydown", event => {
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s") {
    event.preventDefault();
    saveRules();
  }
});
window.addEventListener("beforeunload", event => {
  if (dirty) { event.preventDefault(); event.returnValue = ""; }
});

renderList();
renderEditor();
renderPreview();
updateSaveButton();
try {
  await bridge.ready();
  const result = await bridge.apiGet("rules");
  rules = result.rules;
  selected = rules.length ? 0 : -1;
  renderList();
  renderEditor();
  renderPreview();
  setStatus("已同步", "success");
} catch (error) {
  setStatus(`载入规则失败：${error.message}`, "error");
}
