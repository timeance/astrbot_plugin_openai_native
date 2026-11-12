/* SPDX-License-Identifier: Apache-2.0 */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const own = (obj, key) => Object.prototype.hasOwnProperty.call(obj || {}, key);
  const clone = (value) => JSON.parse(JSON.stringify(value));
  const get = (obj, path) => path.split(".").reduce((value, key) => value?.[key], obj);
  const set = (obj, path, value) => {
    const parts = path.split(".");
    let target = obj;
    parts.slice(0, -1).forEach((key) => {
      if (!own(target, key)) target[key] = {};
      target = target[key];
    });
    target[parts[parts.length - 1]] = value;
  };
  const canonical = (value) => {
    if (Array.isArray(value)) return value.map(canonical);
    if (value && typeof value === "object") {
      return Object.fromEntries(Object.keys(value).sort().map((key) => [key, canonical(value[key])]));
    }
    return value;
  };
  const equal = (a, b) => JSON.stringify(canonical(a)) === JSON.stringify(canonical(b));
  const element = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = String(text);
    return node;
  };
  const option = (value, label) => {
    const node = element("option", "", label);
    node.value = value;
    return node;
  };

  const state = {
    bridge: null, context: null, snapshot: null, providers: [], diagnostics: null,
    loaded: false, loading: false, saving: false, refreshing: false, resetting: false,
    conflict: false, currentTab: "overview", theme: "host", summary: null,
    tasks: { offset: 0, limit: 20, total: 0, sequence: 0 },
    gallery: { offset: 0, limit: 12, total: 0, sequence: 0 }, previewSequence: 0
  };
  const controls = new Map();
  const sourceControls = new Map();
  const secretControls = new Map();
  const fields = [];
  const field = (path, label, type, group, extra = {}) => {
    const entry = { path, label, type, group, ...extra };
    fields.push(entry);
    return entry;
  };
  const services = [["auto", "自动识别"], ["openai", "OpenAI"], ["newapi", "NewAPI"], ["sub2api", "Sub2API"], ["unknown", "未知 / 不识别"]];
  const groups = [
    ["diagnostics", "overview", "诊断日志", "默认关闭。保存后立即生效，按总量上限滚动清理最旧日志；关闭后保留已有文件。不保存聊天正文、图片内容或密钥。"],
    ["search-connection", "search", "搜索连接", "使用当前会话、固定供应商，或独立的手动 API。"],
    ["search-bounds", "search", "触发与查询", "按需搜索由主代理判断是否需要；明确触发仅响应用户明确的搜索请求。"],
    ["search-display", "search", "结果摘要与来源信息", "控制提供给模型的搜索内容；最终回复由模型生成。"],
    ["search-quota", "search", "搜索额度与并发", "命令与自然语言共用每日额度。填写 0 表示不限，搜索按次计。"],
    ["search-advanced", "search", "搜索连接参数", ""],
    ["image-connection", "image", "图片连接", "搜索与图片独立。图片模型不会自动使用聊天模型。"],
    ["image-prompt", "image", "任务提示与绘图预设", "按名称保存绘图提示词，任务受理回执可单独设置。"],
    ["image-output", "image", "输出约束", ""],
    ["gif", "image", "原生 GIF", "默认关闭。需上游原生 GIF 支持；不会在本地拼接静态帧。"],
    ["image-quota", "image", "生图每日额度", "命令与自然语言共用每日额度。填写 0 表示不限，图片按张计。"],
    ["image-schedule", "image", "生图并发与队列", ""],
    ["storage", "image", "参考图、结果文件与记录保存", "文件大小以字节填写，字段下显示换算。任务记录保留天数不得短于结果文件。"],
    ["image-advanced", "image", "图片连接参数", ""],
    ["quota", "advanced", "共用额度规则", "每日边界按所选时区计算。"],
    ["schedule", "advanced", "共用请求频率", ""],
    ["network", "advanced", "下载与网络", "私有下载主机仅在确实需要时放行，按主机填写。"]
  ];

  for (const name of ["search", "image"]) {
    const group = name + "-connection";
    field(name + ".enabled", name === "search" ? "启用网页搜索" : "启用生图与编辑", "boolean", group);
    field(name + ".mode", "供应商来源", "source", group);
    field(name + ".provider_id", "供应商 ID", "source-id", group);
    field(name + ".base_url", "API 基础地址", "text", group, {
      when: () => mode(name) === "manual", placeholder: "https://api.openai.com/v1",
      hint: "仅手动 API 使用；支持完整环境变量引用。"
    });
    field(name + ".keys", "替换 API 密钥", "secret", group, { when: () => mode(name) === "manual" });
    field(name + ".model", name === "image" ? "图片模型 ID" : "搜索模型 ID", "text", group, {
      hint: name === "image" ? "必须显式填写上游实际可用的图片模型，未填写仍可保存。" : "留空继承所选供应商模型；手动 API 需填写。",
      placeholder: name === "image" ? "尚未配置图片模型" : "留空继承供应商模型"
    });
    field(name + ".protocol", "接口协议", "select", group, {
      options: name === "search" ? [["auto", "自动"], ["responses", "Responses 网页搜索"], ["alpha", "Alpha 网页搜索"]]
        : [["auto", "自动（Images）"], ["images", "Images API"], ["responses", "Responses 生图"]],
      hint: name === "image" ? "GIF 需要 Images API；Responses 生图还需要宿主模型。" : "入口品牌不能保证渠道支持该协议。"
    });
    field(name + ".host_model", "Responses 宿主模型", "text", name === "image" ? group : name + "-advanced", {
      when: () => controlValue(name + ".protocol") === "responses",
      hint: "留空时可继承所选供应商的聊天模型；手动 API 需明确配置。"
    });
    field(name + ".service_type", "入口服务类型", "select", name + "-advanced", { options: services, hint: "只覆盖服务识别；不会证明账号或渠道支持某项能力。" });
    field(name + ".proxy", "代理地址", "text", name + "-advanced", { hint: "留空继承供应商设置；支持完整环境变量引用。", placeholder: "留空继承" });
    field(name + ".timeout", "请求超时", "number", name + "-advanced", { min: 0, integer: false, unit: "秒", hint: "0 表示继承供应商超时；无可继承值时使用服务默认值。" });
    field(name + ".headers", "附加请求头", "json", name + "-advanced", { advanced: true, hint: "JSON 对象。认证头由连接管理，不在此覆盖；优先使用环境变量引用。", stringValues: true });
    field(name + ".extra_body", "附加请求体", "json", name + "-advanced", { advanced: true, hint: "JSON 对象；服务端仅使用对应协议允许的参数。" });
    field(name + ".paths", "接口路径覆盖", "json", name + "-advanced", { advanced: true, hint: "JSON 对象，值为本地接口路径，例如 /responses；不得包含完整 URL、查询或路径跳转。", stringValues: true });
  }
  field("search.trigger_policy", "搜索触发策略", "select", "search-bounds", {
    options: [["on_demand", "按需搜索"], ["explicit", "仅明确请求搜索"]]
  });
  field("search.max_queries", "每次最多查询", "number", "search-bounds", { min: 1, max: 4, unit: "条" });
  field("search.default_results", "默认搜索结果", "number", "search-bounds", { min: 1, max: 10, unit: "条" });
  field("search.max_results", "最多搜索结果", "number", "search-bounds", { min: 1, max: 10, unit: "条" });
  field("limits.search_calls_per_message", "后续最多补查", "number", "search-bounds", {
    min: 0, displayOffset: -1, unit: "次", hint: "不含首次搜索；填写 1 表示每条消息最多搜索 2 次。"
  });
  field("search.max_tool_calls", "单次上游工具调用上限", "number", "search-bounds", { min: 1, unit: "次", hint: "限制一次搜索请求中的上游工具调用。" });
  field("search.snippet_chars", "每条结果摘要上限", "number", "search-display", { min: 1, unit: "字符" });
  field("search.response_chars", "搜索回应总预算", "number", "search-display", { min: 100, unit: "字符" });
  field("search.show_citations", "向模型提供引用标记", "boolean", "search-display", { hint: "仅控制工具数据，不能强制模型最终回复的格式。" });
  field("search.show_links", "向模型提供来源链接", "boolean", "search-display", { hint: "仅控制工具数据，不能强制模型最终回复包含链接。" });
  field("search.include_raw", "包含上游原始搜索内容", "boolean", "search-display", { hint: "原始内容可能较长，仅在确实需要时开启。" });
  field("image.acceptance_template", "任务受理回执模板", "text", "image-prompt", {
    wide: true, hint: "可使用 {kind} 和 {task_id}；不能为空，最多 300 字符。"
  });
  field("image.presets", "绘图预设（JSON）", "json", "image-prompt", {
    stringValues: true, hint: "名称到提示词模板的 JSON 对象，例如 {\"水彩\": \"柔和水彩风格\"}；最多 50 项。"
  });
  field("image.max_outputs", "每次最多输出", "number", "image-output", { min: 1, max: 4, unit: "张" });
  field("image.max_references", "最多参考图片", "number", "image-output", { min: 1, unit: "张" });
  field("image.formats", "允许的输出格式", "list", "image-output", { hint: "png、jpeg、webp、gif；用逗号或换行分隔。GIF 另需开启下方开关。", allowed: ["png", "jpeg", "webp", "gif"] });
  field("image.sizes", "允许的尺寸", "list", "image-output", { hint: "填写 auto 或正整数宽x高，用逗号或换行分隔；留空不额外限制。", placeholder: "留空不限制" });
  field("image.default_format", "默认输出格式", "select", "image-output", { options: [["png", "PNG"], ["jpeg", "JPEG"], ["webp", "WebP"], ["gif", "GIF"]] });
  field("image.default_size", "默认尺寸", "text", "image-output", { placeholder: "auto", hint: "填写 auto 或正整数宽x高，例如 1024x1024；需在像素上限内。" });
  field("image.max_pixels", "单帧像素上限", "number", "image-output", { min: 1, unit: "像素" });
  field("image.max_frames_pixels", "全部帧总像素上限", "number", "image-output", { min: 1, unit: "像素" });
  field("gif.enabled", "允许原生 GIF", "boolean", "gif", { hint: "启用后仍需账号与渠道实测支持。" });
  field("gif.frame_count", "默认帧数", "number", "gif", { min: 2, max: 16, unit: "帧" });
  field("gif.frame_duration_ms", "每帧时长", "number", "gif", { min: 20, max: 2000, unit: "毫秒" });
  field("limits.timezone", "每日重置时区", "text", "quota", { placeholder: "UTC", hint: "IANA 时区，例如 Asia/Shanghai、Asia/Hong_Kong 或 UTC。" });
  for (const [prefix, title, unit] of [["search", "搜索", "次/日"], ["image", "图片", "张/日"]]) {
    for (const [scope, label] of [["user", "用户"], ["group", "群组"], ["private", "私聊"]]) {
      field("limits." + scope + "_" + prefix, label + title + "额度", "number", prefix + "-quota", { min: 0, unit, hint: "0 表示不限。" });
    }
  }
  field("limits.admin_exempt", "管理员免除每日额度", "boolean", "quota", { hint: "仅影响额度，不代表上游免费。" });
  for (const [key, label, unit] of [
    ["search_concurrency", "搜索并发", "个"], ["image_concurrency", "图片并发", "个"],
    ["queue_size", "图片队列容量", "个任务"], ["user_image_concurrency", "单用户图片并发", "个"]
  ]) field("limits." + key, label, "number", key === "search_concurrency" ? "search-quota" : "image-schedule", { min: 1, unit });
  field("limits.min_interval_seconds", "用户请求最小间隔", "number", "schedule", { min: 0, integer: false, unit: "秒" });
  for (const [key, label, unit] of [
    ["reference_ttl_seconds", "近期参考图缓存", "秒"], ["artifact_days", "结果文件保留", "天"],
    ["task_days", "任务记录保留", "天"], ["max_input_bytes", "单个输入文件上限", "字节"],
    ["max_output_bytes", "单个输出文件上限", "字节"], ["max_total_bytes", "结果总存储上限", "字节"]
  ]) field("storage." + key, label, "number", "storage", { min: 1, unit, bytes: key.endsWith("_bytes") });
  field("network.connect_timeout", "连接超时", "number", "network", { min: 0, integer: false, unit: "秒" });
  field("network.download_timeout", "下载超时", "number", "network", { min: 0, integer: false, unit: "秒" });
  field("network.download_attempts", "下载尝试次数", "number", "network", { min: 1, unit: "次", hint: "只重试结果下载，不重新生成图片。" });
  field("network.allow_private_download_hosts", "允许下载的私有主机", "list", "network", { hint: "用逗号或换行分隔；默认留空。", wide: true });
  field("diagnostics.enabled", "保存诊断日志", "boolean", "diagnostics", {
    hint: "开启后记录命令匹配、工具筛选和任务状态，便于排查未触发原因。"
  });
  field("diagnostics.max_mb", "诊断日志总量上限", "number", "diagnostics", {
    min: 1, max: 10000, unit: "MB", hint: "默认 100 MB，包含当前文件与轮转备份。缩小上限时会清理最旧日志。"
  });
  const fieldByPath = new Map(fields.map((entry) => [entry.path, entry]));

  function controlValue(path) { return controls.get(path)?.input.value || ""; }
  function mode(name) {
    const value = sourceControls.get(name)?.input.value || "current";
    return value === "manual" ? "manual" : value === "current" ? "current" : "provider";
  }
  function safeText(value, max = 260) {
    if (value === null || value === undefined || typeof value === "object") return "";
    let text = String(value).replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f]/g, "");
    text = text.replace(/\b(?:https?|socks5h?):\/\/[^\s<>"')\]]+/gi, "[地址已隐藏]")
      .replace(/\bBearer\s+\S+/gi, "Bearer [已隐藏]")
      .replace(/\bsk-[A-Za-z0-9_-]+/g, "[密钥已隐藏]")
      .replace(/((?:api[_-]?key|authorization|token|password)\s*[:=]\s*)[^\s,;}]+/gi, "$1[已隐藏]");
    for (const { input } of secretControls.values()) {
      for (const secret of input.value.split(/\r?\n/).filter((item) => item.length > 3)) text = text.split(secret).join("[已隐藏]");
    }
    return text.slice(0, max);
  }
  function showNotice(message, kind = "") {
    $("notice").textContent = message;
    $("notice").className = "notice" + (kind ? " " + kind : "");
  }
  function showTab(name, focus = false) {
    state.currentTab = name;
    document.querySelectorAll("[data-tab]").forEach((node) => {
      const selected = node.dataset.tab === name;
      node.setAttribute("aria-selected", String(selected));
      node.tabIndex = selected ? 0 : -1;
      if (selected && focus) node.focus();
    });
    document.querySelectorAll(".tab-panel").forEach((node) => { node.hidden = node.id !== "panel-" + name; });
    updateSaveBar();
    if (name === "tasks") loadTasks();
    if (name === "gallery") loadGallery();
  }
  function focusField(path) {
    const entry = fieldByPath.get(path);
    const group = groups.find((item) => item[0] === entry?.group);
    if (group) showTab(group[1]);
    const control = controls.get(path) || sourceControls.get(path.split(".")[0]);
    if (!control) return;
    const details = control.wrapper.closest("details");
    if (details) details.open = true;
    control.input.focus();
  }

  const replaceable = (path) => /^(search|image)\.(base_url|proxy|headers|extra_body|paths)$/.test(path);
  const clearedValue = (entry) => entry.type === "json" ? {} : entry.type === "secret" ? [] : "";

  function addField(entry, grid) {
    if (entry.type === "source-id") return;
    const wrapper = element("div", "field" + (entry.wide || entry.type === "json" || entry.type === "secret" ? " wide" : ""));
    const id = "field-" + entry.path.replaceAll(".", "-");
    const label = element("label", "field-label", entry.label);
    label.htmlFor = id;
    if (entry.unit) label.append(element("span", "unit", "（" + entry.unit + "）"));
    wrapper.append(label);
    let input;
    if (entry.type === "source" || entry.type === "select") {
      input = element("select");
      if (entry.type === "select") entry.options.forEach(([value, title]) => input.append(option(value, title)));
    } else if (["json", "list", "secret"].includes(entry.type)) {
      input = element("textarea", entry.type === "json" ? "json" : "");
      input.rows = entry.type === "json" ? 4 : 2;
      input.spellcheck = false;
      if (entry.type === "secret") {
        input.autocomplete = "new-password";
        input.classList.add("secret-input");
        input.placeholder = "留空保持现有密钥；更换时每行一项，可用 env:KEY";
      }
    } else {
      input = element("input");
      input.type = entry.type === "boolean" ? "checkbox" : entry.type === "number" ? "number" : "text";
      if (entry.type === "number") {
        if (entry.min !== undefined) input.min = entry.min;
        if (entry.max !== undefined) input.max = entry.max;
        input.step = entry.integer === false ? "any" : "1";
        input.inputMode = entry.integer === false ? "decimal" : "numeric";
      }
    }
    input.id = id;
    input.name = entry.path;
    if (entry.placeholder) input.placeholder = entry.placeholder;
    if (entry.type === "boolean") {
      wrapper.replaceChildren();
      const check = element("label", "check-label");
      check.htmlFor = id;
      check.append(input, element("span", "", entry.label));
      wrapper.append(check);
    } else wrapper.append(input);
    const hint = element("span", "hint", entry.hint || "");
    hint.id = id + "-hint";
    hint.hidden = !entry.hint && !entry.bytes && entry.type !== "secret";
    const error = element("div", "field-error");
    error.id = id + "-error";
    error.hidden = true;
    input.setAttribute("aria-describedby", hint.id + " " + error.id);
    wrapper.append(hint, error);
    const control = { input, wrapper, error, hint, entry };
    if (entry.type === "source") {
      sourceControls.set(entry.path.split(".")[0], control);
      input.addEventListener("change", changed);
    } else if (entry.type === "secret") {
      const actions = element("div", "secret-actions");
      const status = element("span", "hint", "未读取密钥状态");
      const checkLabel = element("label", "check-label");
      const clear = element("input");
      clear.type = "checkbox";
      clear.id = id + "-clear";
      checkLabel.htmlFor = clear.id;
      checkLabel.append(clear, element("span", "", "保存时清除现有密钥"));
      actions.append(status, checkLabel);
      wrapper.append(actions);
      secretControls.set(entry.path, { ...control, clear, status });
      input.addEventListener("input", () => {
        if (input.value) clear.checked = false;
        changed();
      });
      clear.addEventListener("change", () => {
        if (clear.checked) input.value = "";
        input.disabled = clear.checked;
        changed();
      });
    } else {
      controls.set(entry.path, control);
      if (replaceable(entry.path)) {
        const actions = element("div", "secret-actions");
        actions.hidden = true;
        const status = element("span", "hint");
        const clearLabel = element("label", "check-label");
        const clear = element("input");
        clear.type = "checkbox";
        clear.id = id + "-clear";
        clearLabel.htmlFor = clear.id;
        clearLabel.append(clear, element("span", "", "保存时清除此字段"));
        actions.append(status, clearLabel);
        wrapper.append(actions);
        Object.assign(control, { actions, status, clear, redacted: false });
        clear.addEventListener("change", () => {
          if (clear.checked) input.value = "";
          input.disabled = clear.checked;
          changed();
        });
        input.addEventListener("input", () => {
          if (input.value) clear.checked = false;
        });
      }
      input.addEventListener(entry.type === "boolean" || entry.type === "select" ? "change" : "input", changed);
    }
    grid.append(wrapper);
  }
  function buildFields() {
    for (const [id, tab, title, description] of groups) {
      const section = element("section", "surface");
      section.append(element("h3", "", title));
      if (description) section.append(element("p", "hint", description));
      const entries = fields.filter((entry) => entry.group === id);
      const basic = element("div", "field-grid");
      basic.style.marginTop = "14px";
      entries.filter((entry) => !entry.advanced).forEach((entry) => addField(entry, basic));
      section.append(basic);
      const advanced = entries.filter((entry) => entry.advanced);
      if (advanced.length) {
        const details = element("details", "details-block");
        details.append(element("summary", "", "请求头、附加参数与路径 · JSON"));
        const grid = element("div", "field-grid");
        advanced.forEach((entry) => addField(entry, grid));
        details.append(grid);
        section.append(details);
      }
      $(tab + "-fields").append(section);
    }
  }
  function refreshSources() {
    for (const [name, { input }] of sourceControls) {
      const selected = input.value || (state.snapshot ? sourceValue(state.snapshot.values, name) : "current");
      input.replaceChildren(option("current", "当前会话供应商"), option("manual", "手动 API"));
      for (const provider of state.providers) {
        const value = "provider:" + provider.id;
        input.append(option(value, safeText(provider.label || provider.id, 100) + (provider.model ? " · " + safeText(provider.model, 100) : "")));
      }
      if (selected.startsWith("provider:") && !Array.from(input.options).some((item) => item.value === selected)) {
        input.append(option(selected, "未加载的供应商 · " + safeText(selected.slice(9), 100)));
      }
      input.value = selected;
    }
  }
  function sourceValue(values, name) {
    const value = get(values, name + ".mode");
    return value === "provider" ? "provider:" + (get(values, name + ".provider_id") || "") : value === "manual" ? "manual" : "current";
  }
  function writeControl(control, value) {
    const { entry, input } = control;
    if (entry.type === "boolean") input.checked = value === true;
    else if (entry.type === "json") input.value = typeof value === "string" ? value : JSON.stringify(value ?? {}, null, 2);
    else if (entry.type === "list") input.value = Array.isArray(value) ? value.join("\n") : typeof value === "string" ? value : "";
    else {
      const normalized = entry.group.endsWith("quota") && value === -1 ? 0 : value;
      const displayed = entry.type === "number" && normalized !== null && normalized !== undefined && entry.displayOffset
        ? Number(normalized) + entry.displayOffset : normalized;
      const text = displayed === null || displayed === undefined ? "" : String(displayed);
      if (entry.type === "select" && !Array.from(input.options).some((item) => item.value === text)) input.append(option(text, text || "未选择"));
      input.value = text;
    }
  }
  function validSnapshot(snapshot) {
    return snapshot && snapshot.values && typeof snapshot.values === "object" && !Array.isArray(snapshot.values) && snapshot.revision !== undefined && snapshot.revision !== null;
  }
  function acceptSnapshot(snapshot, preserve = false) {
    if (!validSnapshot(snapshot)) throw new Error("invalid_settings_response");
    const pending = preserve && state.loaded ? collect() : null;
    const dirty = new Set(pending?.dirty || []);
    const sourceDrafts = new Map(Array.from(sourceControls, ([name, { input }]) => [name, input.value]));
    // Secrets never become form defaults, even if an older backend returns them accidentally.
    const clean = clone(snapshot);
    for (const values of [clean.values, clean.active_values]) {
      if (values && typeof values === "object") {
        for (const name of ["search", "image"]) if (values[name]) delete values[name].keys;
      }
    }
    state.snapshot = clean;
    state.loaded = true;
    for (const [path, control] of controls) {
      if (replaceable(path)) {
        control.redacted = clean.fields?.[path]?.secret_update === true || own(clean.secrets, path);
        control.actions.hidden = !control.redacted;
        const secret = clean.secrets?.[path];
        control.status.textContent = secret?.configured ? "已配置，内容不回显；留空保持" : "当前为空；留空保持";
        control.input.placeholder = control.redacted ? "留空保持现有值；仅在明确更换时输入" : control.entry.placeholder || "";
        if (!dirty.has(path)) {
          control.clear.checked = false;
          if (control.redacted) control.input.value = "";
          else writeControl(control, get(clean.values, path));
        }
        control.input.disabled = control.clear.checked;
      } else if (!dirty.has(path)) writeControl(control, get(clean.values, path));
    }
    for (const [name, control] of sourceControls) {
      control.input.value = "";
      refreshSources();
      const keepSource = dirty.has(name + ".mode") || dirty.has(name + ".provider_id");
      const value = keepSource ? sourceDrafts.get(name) : sourceValue(clean.values, name);
      if (!Array.from(control.input.options).some((item) => item.value === value)) control.input.append(option(value, "未加载的供应商 · " + safeText(value.slice(9), 100)));
      control.input.value = value;
    }
    for (const [path, control] of secretControls) {
      if (!preserve) { control.input.value = ""; control.clear.checked = false; }
      control.input.disabled = control.clear.checked;
      const secret = clean.secrets?.[path];
      control.status.textContent = secret?.configured ? "已配置" + (Number.isInteger(secret.count) ? " · " + secret.count + " 项" : "") + "，内容不回显" : "尚未配置手动密钥";
      control.hint.textContent = "留空保持现有值；每行一项。勾选清除会在保存时移除全部手动密钥。";
      control.hint.hidden = false;
    }
    updateConditions();
    renderRuntime();
    updateDraft();
  }

  function parseField(entry, input) {
    if (entry.type === "boolean") return { value: input.checked };
    const raw = input.value.trim();
    if (entry.type === "number") {
      if (!raw || input.validity?.badInput) return { error: "请填写有效数字。" };
      const displayValue = Number(raw);
      if (!Number.isFinite(displayValue)) return { error: "请填写有限数字。" };
      if (entry.integer !== false && !Number.isSafeInteger(displayValue)) return { error: "请填写有效整数。" };
      if (entry.min !== undefined && displayValue < entry.min) return { error: "不能小于 " + entry.min + "。" };
      if (entry.max !== undefined && displayValue > entry.max) return { error: "不能大于 " + entry.max + "。" };
      const value = displayValue - (entry.displayOffset || 0);
      const limits = state.snapshot?.fields?.[entry.path];
      if (typeof limits?.min === "number" && value < limits.min) return { error: "不能小于 " + limits.min + "。" };
      if (typeof limits?.max === "number" && value > limits.max) return { error: "不能大于 " + limits.max + "。" };
      return { value };
    }
    if (entry.type === "json") {
      try {
        const value = JSON.parse(raw);
        if (!value || Array.isArray(value) || typeof value !== "object") return { error: "请填写 JSON 对象，例如 {}。" };
        if (entry.stringValues && Object.values(value).some((item) => typeof item !== "string")) return { error: "此对象的每个值都需为字符串。" };
        if (entry.path === "image.presets" && (Object.keys(value).length > 50 || Object.entries(value).some(([name, prompt]) =>
          !name.trim() || name.length > 40 || /[\[\]\r\n]/.test(name) || !prompt.trim() || prompt.length > 8000))) return { error: "预设最多 50 项；名称需为 1–40 字符，提示词需为 1–8000 字符。" };
        if (entry.path.endsWith(".paths") && Object.values(value).some((item) => /[?#\\]/.test(item) || /(^|\/)\.\.?($|\/)/.test(item) || /^[a-z][a-z0-9+.-]*:/i.test(item) || item.startsWith("//"))) return { error: "路径需为本地接口路径，不能带 URL、查询或路径跳转。" };
        return { value };
      } catch { return { error: "JSON 格式错误，请检查引号、逗号与括号。" }; }
    }
    if (entry.type === "list") {
      const value = raw.split(/[\r\n,]+/).map((item) => item.trim()).filter(Boolean);
      if (entry.allowed && value.some((item) => !entry.allowed.includes(item))) return { error: "只允许 " + entry.allowed.join("、") + "。" };
      return { value };
    }
    if (entry.path === "limits.timezone" && raw) {
      try { new Intl.DateTimeFormat("zh-CN", { timeZone: raw }).format(); }
      catch { return { error: "请填写有效 IANA 时区，例如 Asia/Shanghai。" }; }
    }
    if (entry.path === "image.acceptance_template") {
      const literal = raw.replaceAll("{{", "").replaceAll("}}", "").replaceAll("{kind}", "").replaceAll("{task_id}", "");
      if (!raw || raw.length > 300 || /[{}]/.test(literal)) return { error: "请填写 1–300 字符的模板，仅使用 {kind}、{task_id} 占位符。" };
    }
    return { value: raw };
  }
  function collect() {
    const values = clone(state.snapshot?.values || {});
    const errors = new Map();
    const dirty = [];
    const patch = {};
    const secretUpdates = {};
    for (const [path, control] of controls) {
      if (control.redacted && control.clear?.checked) {
        const value = clearedValue(control.entry);
        dirty.push(path); secretUpdates[path] = value; set(values, path, value); continue;
      }
      if (control.redacted && !control.input.value.trim()) continue;
      const parsed = parseField(control.entry, control.input);
      if (parsed.error) { errors.set(path, parsed.error); dirty.push(path); continue; }
      set(values, path, parsed.value);
      let baseline = get(state.snapshot?.values, path);
      if (control.entry.type === "json" && typeof baseline === "string") {
        try { baseline = JSON.parse(baseline); } catch { /* Retain legacy text until explicitly replaced. */ }
      }
      if (control.redacted || !equal(parsed.value, baseline)) {
        dirty.push(path);
        if (replaceable(path)) secretUpdates[path] = parsed.value;
        else set(patch, path, parsed.value);
      }
    }
    for (const [name, { input }] of sourceControls) {
      const selected = input.value;
      const selectedMode = mode(name);
      const providerId = selectedMode === "provider" ? selected.slice(9) : get(state.snapshot?.values, name + ".provider_id") || "";
      for (const [key, value] of [["mode", selectedMode], ["provider_id", providerId]]) {
        const path = name + "." + key;
        set(values, path, value);
        if (!equal(value, get(state.snapshot?.values, path))) { dirty.push(path); set(patch, path, value); }
      }
    }
    for (const [path, { input, clear }] of secretControls) {
      if (clear.checked) { dirty.push(path); secretUpdates[path] = []; }
      else if (input.value.trim()) {
        dirty.push(path);
        secretUpdates[path] = input.value.split(/\r?\n/).map((item) => item.trim()).filter(Boolean);
      }
    }
    const defaultResults = get(values, "search.default_results");
    if (defaultResults > get(values, "search.max_results")) errors.set("search.default_results", "默认结果数不能超过最多结果数。");
    if (get(values, "storage.task_days") < get(values, "storage.artifact_days")) errors.set("storage.task_days", "任务记录保留天数不能短于结果文件保留天数。");
    const sizeError = (size, frames = 1) => {
      if (size === "auto") return "";
      const match = /^([0-9]+)x([0-9]+)$/.exec(size);
      if (!match) return "尺寸需为 auto 或正整数宽x高，例如 1024x1024。";
      const width = Number(match[1]), height = Number(match[2]);
      if (!Number.isSafeInteger(width) || !Number.isSafeInteger(height) || width < 1 || height < 1) return "尺寸的宽和高必须为有效正整数。";
      const pixels = width * height;
      if (!Number.isSafeInteger(pixels) || pixels > get(values, "image.max_pixels")) return "尺寸像素数超过当前单帧像素上限。";
      if (frames > 1 && pixels * frames > get(values, "image.max_frames_pixels")) return "默认 GIF 尺寸与帧数的总像素超过当前全部帧上限。";
      return "";
    };
    const defaultSizeError = sizeError(get(values, "image.default_size"), get(values, "image.default_format") === "gif" ? get(values, "gif.frame_count") : 1);
    if (defaultSizeError) errors.set("image.default_size", defaultSizeError);
    for (const size of get(values, "image.sizes") || []) {
      const message = sizeError(size);
      if (message) { errors.set("image.sizes", message); break; }
    }
    return { values, errors, dirty, patch, secretUpdates };
  }
  function updateConditions() {
    for (const entry of fields) {
      const control = controls.get(entry.path) || secretControls.get(entry.path);
      if (control && entry.when) control.wrapper.hidden = !entry.when();
    }
    $("image-model-warning").hidden = Boolean(controlValue("image.model").trim());
    for (const control of controls.values()) {
      if (control.entry.bytes) {
        const value = Number(control.input.value);
        control.hint.textContent = Number.isFinite(value) && value > 0 ? "约 " + (value / (1024 * 1024)).toLocaleString("zh-CN", { maximumFractionDigits: 2, minimumFractionDigits: 2 }) + " MiB" : "1 MiB = 1,048,576 字节";
        control.hint.hidden = false;
      }
    }
  }
  function changed() {
    updateConditions();
    updateDraft();
  }
  function updateDraft() {
    if (!state.loaded) return;
    const result = collect();
    const dirtySet = new Set(result.dirty);
    for (const [path, control] of controls) {
      const message = result.errors.get(path);
      control.input.setAttribute("aria-invalid", String(Boolean(message)));
      control.error.textContent = message || "";
      control.error.hidden = !message;
      control.wrapper.classList.toggle("field-dirty", dirtySet.has(path));
    }
    for (const [path, control] of secretControls) control.wrapper.classList.toggle("field-dirty", dirtySet.has(path));
    for (const [name, control] of sourceControls) control.wrapper.classList.toggle("field-dirty", dirtySet.has(name + ".mode") || dirtySet.has(name + ".provider_id"));
    const count = result.dirty.length;
    $("draft-state").textContent = state.saving ? "正在保存…" : count ? "尚未保存 · " + count + " 项变更" : "草稿与已保存配置一致";
    $("save").disabled = !count || state.saving || state.loading || state.conflict;
    $("save").textContent = state.saving ? "正在保存…" : "保存配置";
    $("save-hint").textContent = result.errors.size ? "存在 " + result.errors.size + " 项输入错误，请修正后保存。" : "诊断日志保存后立即生效；其他配置请在插件管理中手动重载。";
    const summary = $("validation-summary");
    if (summary.dataset.revealed === "true") renderValidation(result.errors);
    updateSaveBar(count);
  }
  function updateSaveBar(dirty) {
    const count = dirty ?? (state.loaded ? collect().dirty.length : 0);
    $("save-bar").hidden = !["search", "image", "advanced"].includes(state.currentTab) && !count;
  }
  function renderValidation(errors) {
    const summary = $("validation-summary");
    summary.replaceChildren();
    summary.hidden = !errors.size;
    if (!errors.size) return;
    summary.append(element("strong", "", "请先修正以下输入，草稿已保留："));
    const list = element("ul");
    for (const [path, message] of errors) {
      const item = element("li");
      const link = element("button", "", (fieldByPath.get(path)?.label || path) + "：" + message);
      link.type = "button";
      link.addEventListener("click", () => focusField(path));
      item.append(link); list.append(item);
    }
    summary.append(list);
  }
  function renderRuntime() {
    const snapshot = state.snapshot;
    if (!snapshot) return;
    const changed = Array.isArray(snapshot.changed_paths) ? snapshot.changed_paths : [];
    $("revision-label").textContent = "版本 · " + safeText(snapshot.revision, 16);
    $("runtime-state").textContent = snapshot.needs_reload ? "已保存配置与运行中配置不同，请手动重载插件。" : "已保存配置与当前运行配置一致。";
    const container = $("runtime-diff");
    container.replaceChildren();
    if (!changed.length) {
      if (snapshot.needs_reload) container.append(element("p", "hint", "服务标记为待重载；差异明细暂不可用。"));
      return;
    }
    const table = element("table");
    const head = element("thead");
    const headRow = element("tr");
    ["参数", "运行中", "已保存（重载后使用）"].forEach((title) => headRow.append(element("th", "", title)));
    head.append(headRow); table.append(head);
    const body = element("tbody");
    for (const path of changed) {
      if (typeof path !== "string") continue;
      const row = element("tr");
      const entry = fieldByPath.get(path);
      row.append(element("td", "", entry ? entry.label + " · " + path.split(".")[0] : safeText(path, 100)));
      const sensitive = /(?:keys|headers|proxy|base_url|extra_body|paths)/.test(path);
      for (const source of [snapshot.active_values, snapshot.values]) {
        const value = get(source, path);
        const text = sensitive ? "已隐藏 · 仅显示变更状态" : value === undefined ? "未提供" : typeof value === "boolean" ? (value ? "开启" : "关闭") : Array.isArray(value) ? value.map((item) => safeText(item, 60)).join("、") || "空列表" : safeText(value, 100) || "留空";
        row.append(element("td", "", text));
      }
      body.append(row);
    }
    table.append(body);
    const scroll = element("div", "table-scroll");
    scroll.append(table); container.append(scroll);
  }

  const statuses = {
    available: ["实测通过", "good"], auth_blocked: ["认证或权限受限", "bad"],
    started: ["已开始", ""], error: ["执行错误", "bad"],
    verified: ["已验证", "good"], supported: ["实测通过", "good"], passed: ["实测通过", "good"],
    unverified: ["尚未实测", ""], unknown: ["尚未实测", ""],
    unsupported: ["明确不支持", "bad"], temporary_failure: ["临时失败", "warn"],
    unavailable: ["不可用", "warn"], failed: ["失败", "bad"], success: ["成功", "good"],
    disabled: ["已关闭", ""], configuration: ["配置未就绪", "warn"],
    awaiting_session: ["等待会话验证", "warn"], queued: ["已排队", ""],
    running: ["执行中", ""], completed: ["已完成", "good"], partial: ["部分完成", "warn"],
    cancelled: ["已取消", ""], needs_reconciliation: ["待人工核对", "warn"],
    succeeded: ["成功", "good"], receiving: ["接收结果中", ""],
    cancel_requested: ["取消中", "warn"], result_unavailable: ["结果不可用", "warn"]
  };
  function badge(text, kind = "") { return element("span", "badge" + (kind ? " " + kind : ""), text); }
  function statusBadge(value) {
    const item = statuses[value];
    return badge(item?.[0] || safeText(value || "未提供", 60), item?.[1] || "");
  }
  function stateLine(label, value) {
    const line = element("div", "state-line");
    line.append(element("span", "", label), element("span", "", value));
    return line;
  }
  function capabilityLabel(name) {
    if (name === "search") return "网页搜索";
    return name.replace(/^generate:/, "生成 · ").replace(/^edit:/, "编辑 · ");
  }
  function renderReadiness() {
    const container = $("readiness-grid");
    container.replaceChildren();
    const features = state.diagnostics?.features || {};
    for (const [name, title] of [["search", "网页搜索"], ["image", "生图与 GIF"]]) {
      const feature = features[name];
      const card = element("section", "surface readiness-card " + name + "-card");
      const heading = element("div", "card-title");
      heading.append(element("h3", "", title));
      if (!feature) heading.append(badge("状态未读取"));
      else if (feature.reason_code === "awaiting_session") heading.append(badge("等待会话验证", "warn"));
      else heading.append(badge(feature.configuration_ready === true ? "配置就绪" : "配置未就绪", feature.configuration_ready ? "good" : "warn"));
      card.append(heading);
      const metrics = element("div", "feature-metrics");
      const summary = state.summary?.[name];
      for (const [key, label] of [["total", "保留任务"], ["active", "进行中"], ["succeeded", "成功"], ["failed", "失败"]]) {
        const metric = element("div");
        metric.append(element("strong", "", summary ? String(summary[key] ?? 0) : "—"), element("span", "", label));
        metrics.append(metric);
      }
      card.append(metrics);
      const lines = element("div", "state-lines");
      const selectedMode = feature?.mode || get(state.snapshot?.active_values, name + ".mode");
      lines.append(stateLine("连接来源", selectedMode === "current" ? "当前会话" : selectedMode === "manual" ? "手动 API" : selectedMode === "provider" ? "固定供应商" : "未提供"));
      lines.append(stateLine("配置检查", !feature ? "尚未读取" : feature.reason_code === "awaiting_session" ? "等待会话验证" : feature.configuration_ready === true ? "已就绪" : "尚未就绪"));
      lines.append(stateLine("账号实测", feature?.tested === true ? "有实测记录（见下方能力）" : "尚未实测"));
      lines.append(stateLine("运行可用", !feature ? "尚未读取" : feature.reason_code === "awaiting_session" ? "等待会话验证" : feature.ready === true ? "可尝试调用" : "尚未就绪"));
      if (feature?.provider_id) lines.append(stateLine("供应商", safeText(feature.provider_id, 100)));
      if (feature?.model) lines.append(stateLine(name === "image" ? "图片模型" : "搜索模型", safeText(feature.model, 100)));
      if (feature?.protocol) lines.append(stateLine("运行协议", safeText(feature.protocol, 50)));
      if (feature?.host_model) lines.append(stateLine("宿主模型", safeText(feature.host_model, 100)));
      if (feature?.observed_at) lines.append(stateLine("记录时间", formatTime(feature.observed_at)));
      card.append(lines);
      if (feature?.reason) card.append(element("p", "readiness-reason", safeText(feature.reason)));
      if (feature?.formats?.length) card.append(element("p", "hint", "可尝试格式：" + feature.formats.map((value) => safeText(value, 20)).join("、")));
      if (feature?.actions?.length) card.append(element("p", "hint", "可尝试操作：" + feature.actions.map((value) => value === "generate" ? "生成" : value === "edit" ? "编辑" : safeText(value, 30)).join("、")));
      if (feature?.error) {
        const details = element("details", "record-details");
        details.append(element("summary", "", "配置检查错误"));
        const dl = element("dl"); appendError(dl, feature.error); details.append(dl); card.append(details);
      }
      else if (!feature) card.append(element("p", "readiness-reason", "运行诊断暂不可用。可重试刷新，不影响已填写的草稿。"));
      const capabilities = feature?.capabilities;
      if (capabilities && typeof capabilities === "object" && !Array.isArray(capabilities)) {
        const list = element("div", "capabilities");
        for (const [key, value] of Object.entries(capabilities)) {
          if (!value || typeof value !== "object") continue;
          const item = statuses[value.status];
          const chip = badge(safeText(capabilityLabel(key), 60) + "：" + (item?.[0] || safeText(value.status || "尚未实测", 40)), item?.[1] || "");
          if (value.reason) chip.title = safeText(value.reason);
          list.append(chip);
          if (value.error) {
            const details = element("details", "record-details");
            details.append(element("summary", "", safeText(capabilityLabel(key), 60) + " · 错误详情"));
            const dl = element("dl");
            appendError(dl, value.error);
            details.append(dl); list.append(details);
          }
        }
        card.append(list);
      }
      const actions = element("div", "feature-actions");
      const settings = element("button", "button", name === "search" ? "搜索设置" : "生图设置");
      settings.type = "button"; settings.addEventListener("click", () => showTab(name, true));
      const tasks = element("button", "button small", "查看任务");
      tasks.type = "button";
      tasks.addEventListener("click", () => { $("task-kind").value = name; state.tasks.offset = 0; showTab("tasks", true); });
      actions.append(settings, tasks);
      if (name === "image") {
        const gallery = element("button", "button small", "打开图库");
        gallery.type = "button"; gallery.addEventListener("click", () => showTab("gallery", true));
        actions.append(gallery);
      }
      card.append(actions);
      container.append(card);
    }
  }
  function formatTime(value) {
    if (value === null || value === undefined || value === "") return "未提供";
    const date = new Date(typeof value === "number" ? value * (value < 100000000000 ? 1000 : 1) : value);
    return Number.isNaN(date.getTime()) ? safeText(value, 40) : date.toLocaleString("zh-CN", { hour12: false });
  }
  function appendDetail(dl, title, value) {
    if (value === undefined || value === null || value === "") return;
    const text = Array.isArray(value) ? value.map((item) => safeText(item, 80)).filter(Boolean).join("、") : safeText(value, 600);
    if (!text) return;
    dl.append(element("dt", "", title), element("dd", "", text));
  }
  function appendError(dl, error) {
    if (!error || typeof error !== "object") { appendDetail(dl, "错误", error); return; }
    for (const [key, title] of [
      ["category", "错误类别"], ["status_code", "HTTP 状态"], ["upstream_type", "error.type"],
      ["upstream_code", "error.code"], ["message", "error.message"], ["request_id", "请求 ID"]
    ]) appendDetail(dl, title, error[key]);
  }
  function renderLogging() {
    const container = $("diagnostic-logging-status");
    container.replaceChildren();
    const logging = state.diagnostics?.logging;
    if (!logging) {
      container.textContent = "尚未读取日志保存状态，请刷新诊断。";
      return;
    }
    container.append(badge(logging.error ? "日志保存异常" : logging.enabled ? "正在保存诊断日志" : "日志保存未开启", logging.error ? "bad" : logging.enabled ? "good" : ""));
    const dl = element("dl");
    const runtime = state.diagnostics?.runtime || {};
    appendDetail(dl, "AstrBot 版本", runtime.astrbot_version);
    appendDetail(dl, "插件版本", runtime.plugin_version);
    appendDetail(dl, "代码指纹", runtime.bridge_sha256);
    appendDetail(dl, "已保存 / 上限", (Number(logging.total_bytes || 0) / 1024 / 1024).toFixed(2) + " MB / " + logging.max_mb + " MB");
    appendDetail(dl, "保存目录", logging.directory);
    appendDetail(dl, "日志文件", (logging.files || []).map((file) => file.name + " · " + (Number(file.bytes || 0) / 1024 / 1024).toFixed(2) + " MB"));
    appendDetail(dl, "已注册命令", (state.diagnostics?.registered_commands || []).map((item) => item.command + " · " + item.handler));
    appendDetail(dl, "保存异常", logging.error);
    container.append(dl);
  }
  function renderRecent() {
    const container = $("recent-records");
    container.className = "";
    container.replaceChildren();
    const records = Array.isArray(state.diagnostics?.recent) ? state.diagnostics.recent.slice(0, 30) : [];
    if (!records.length) {
      container.className = "empty-state";
      container.textContent = "暂无当前运行周期的调用记录；在目标会话调用后刷新。";
      return;
    }
    const table = element("table");
    const head = element("thead");
    const row = element("tr");
    ["时间", "阶段 / 工具", "结果", "详情"].forEach((title) => row.append(element("th", "", title)));
    head.append(row); table.append(head);
    const body = element("tbody");
    for (const record of records) {
      if (!record || typeof record !== "object") continue;
      const tr = element("tr");
      tr.append(element("td", "", formatTime(record.time)));
      const stages = { request: "模型请求", hook: "工具筛选", execution: "工具执行", tool_execution: "工具执行", tool: "工具执行", result: "执行结果", readiness: "就绪检查", message: "消息与命令匹配", reference_cache: "参考图读取", runtime: "运行版本" };
      tr.append(element("td", "", safeText(record.tool || stages[record.kind] || record.kind || "调用记录", 100)));
      const outcome = element("td");
      if (record.state || record.status) outcome.append(statusBadge(record.state || record.status));
      else if (record.error) outcome.append(badge("执行错误", "bad"));
      else if (record.hidden && Object.keys(record.hidden).length) outcome.append(badge("存在隐藏工具", "warn"));
      else outcome.append(badge("已记录"));
      tr.append(outcome);
      const detailsCell = element("td");
      const details = element("details", "record-details");
      details.append(element("summary", "", "展开"));
      const dl = element("dl");
      appendDetail(dl, "关联 ID", record.correlation_id);
      appendDetail(dl, "消息跟踪 ID", record.trace_id);
      appendDetail(dl, "AstrBot 版本", record.astrbot_version);
      appendDetail(dl, "插件版本", record.plugin_version);
      appendDetail(dl, "代码指纹", record.bridge_sha256);
      appendDetail(dl, "命令前缀", record.command_hint);
      appendDetail(dl, "分隔符类型", record.command_boundary);
      appendDetail(dl, "分隔符编码", record.separator_codepoint);
      appendDetail(dl, "正文长度", record.message_length);
      if (record.kind === "message") {
        appendDetail(dl, "唤醒 / 命令条件", record.is_at_or_wake_command ? "已满足" : "未满足");
        appendDetail(dl, "当前图片数", record.image_count);
        appendDetail(dl, "包含引用", record.has_reply ? "是" : "否");
      }
      appendDetail(dl, "已匹配处理器", record.activated_handlers);
      appendDetail(dl, "进入前工具", record.tools_before);
      appendDetail(dl, "筛选后工具", record.tools_after);
      appendDetail(dl, "执行工具", record.tool);
      appendDetail(dl, "任务 ID", record.task_id);
      appendDetail(dl, "操作 ID", record.operation_id);
      const connection = record.connection;
      if (connection && typeof connection === "object") {
        for (const [key, title] of [["provider_id", "供应商"], ["model", "模型"], ["host_model", "宿主模型"], ["protocol", "协议"], ["service_type", "入口类型"]]) appendDetail(dl, title, connection[key]);
      }
      if (record.hidden && typeof record.hidden === "object") {
        for (const [tool, reason] of Object.entries(record.hidden)) {
          appendDetail(dl, "隐藏 · " + safeText(tool, 60), typeof reason === "object" && reason ? [reason.code, reason.message].filter(Boolean).map((item) => safeText(item)).join(" · ") : reason);
        }
      }
      appendError(dl, record.error);
      if (!dl.childNodes.length) appendDetail(dl, "说明", "没有更多详情。");
      details.append(dl); detailsCell.append(details); tr.append(detailsCell); body.append(tr);
    }
    table.append(body);
    const scroll = element("div", "table-scroll"); scroll.append(table); container.append(scroll);
  }
  function renderConflicts() {
    const container = $("command-conflicts");
    container.replaceChildren();
    const conflicts = state.diagnostics?.command_conflicts;
    if (!Array.isArray(conflicts) || !conflicts.length) return;
    const notice = element("div", "notice warning", "检测到固定命令可能冲突，请使用 /onative image 作为明确入口。");
    const list = element("ul");
    for (const item of conflicts) {
      const text = typeof item === "string" ? item : item?.command || item?.name || item?.message;
      if (text) list.append(element("li", "", safeText(text, 160)));
    }
    if (list.childNodes.length) notice.append(list);
    container.append(notice);
  }
  async function refreshSummary() {
    try {
      const result = await request("POST", "tasks/query", { offset: 0, limit: 1, kind: "all", status: "all" });
      if (!result?.summary) throw new Error("invalid_summary");
      state.summary = result.summary;
      $("summary-status").textContent = "任务统计已更新，含命令与自然语言调用。";
      renderReadiness();
    } catch {
      $("summary-status").textContent = "任务统计读取失败，可点击刷新概览重试。已有统计保留。";
    }
  }
  function pageControls(name, loading = false) {
    const page = state[name];
    $(name + "-prev").disabled = loading || page.offset === 0;
    $(name + "-next").disabled = loading || page.offset + page.limit >= page.total;
    $(name + "-page").textContent = "第 " + (Math.floor(page.offset / page.limit) + 1) + " / " + Math.max(1, Math.ceil(page.total / page.limit)) + " 页 · 共 " + page.total + (name === "tasks" ? " 个任务" : " 张图片");
  }
  function renderTasks(items) {
    const container = $("task-list");
    container.replaceChildren();
    container.className = "surface";
    if (!items.length) {
      container.append(element("p", "empty-state", $("task-kind").value !== "all" || $("task-status").value !== "all"
        ? "没有符合筛选条件的任务，可切换功能或状态。" : "暂无任务。在聊天中使用搜索、生图命令，或通过自然语言调用后，点击刷新。"));
      return;
    }
    const scroll = element("div", "table-scroll");
    const table = element("table", "task-table");
    const head = element("thead"), header = element("tr");
    for (const title of ["时间", "功能 / 任务", "状态", "结果", "内容与详情"]) header.append(element("th", "", title));
    head.append(header); table.append(head);
    const body = element("tbody");
    const delivery = { none: "无需发送", pending: "等待发送", sent: "已发送", failed: "发送失败", sending: "发送中" };
    for (const item of items) {
      const row = element("tr");
      row.append(element("td", "time-cell", formatTime(item.created)));
      const identity = element("td");
      identity.append(element("span", "", item.kind === "search" ? "网页搜索" : "生图与 GIF"), element("code", "task-id", safeText(item.id, 80)));
      const status = element("td"); status.append(statusBadge(item.status));
      const result = element("td");
      result.append(element("span", "", "完成 " + (item.actual ?? 0) + " / " + (item.requested ?? 0)));
      if (item.kind === "image") result.append(element("span", "hint", delivery[item.delivery_status] || safeText(item.delivery_status, 40)));
      const detail = element("td");
      const prompt = element("p", "prompt-clamp", safeText(item.prompt, 1000) || "未保留内容摘要");
      detail.append(prompt);
      const more = element("details", "record-details");
      more.append(element("summary", "", "展开详情"));
      const dl = element("dl");
      appendDetail(dl, "任务 ID", item.id); appendDetail(dl, "更新时间", formatTime(item.updated));
      appendDetail(dl, "内容", item.prompt);
      appendError(dl, item.error);
      more.append(dl); detail.append(more);
      row.append(identity, status, result, detail); body.append(row);
    }
    table.append(body); scroll.append(table); container.append(scroll);
  }
  async function loadTasks(offset = state.tasks.offset) {
    if (!state.bridge) return;
    const page = state.tasks, sequence = ++page.sequence;
    pageControls("tasks", true);
    $("refresh-tasks").disabled = true;
    $("task-list").setAttribute("aria-busy", "true");
    $("tasks-status").textContent = "正在读取任务…";
    try {
      const result = await request("POST", "tasks/query", { offset, limit: page.limit, kind: $("task-kind").value, status: $("task-status").value });
      if (sequence !== page.sequence) return;
      if (!Array.isArray(result?.items) || !Number.isInteger(result.total)) throw new Error("invalid_tasks");
      page.offset = result.offset; page.total = result.total;
      if (page.offset >= page.total && page.offset > 0) { loadTasks(0); return; }
      renderTasks(result.items);
      if (result.summary) { state.summary = result.summary; renderReadiness(); }
      $("tasks-status").textContent = "已更新于 " + new Date().toLocaleTimeString("zh-CN", { hour12: false });
    } catch {
      if (sequence === page.sequence) $("tasks-status").textContent = "任务读取失败，保留上次列表。请点击刷新任务重试。";
    } finally {
      if (sequence === page.sequence) {
        pageControls("tasks"); $("refresh-tasks").disabled = false;
        $("task-list").setAttribute("aria-busy", "false");
      }
    }
  }
  function validPreview(result) {
    return typeof result?.data_url === "string" && /^data:image\/(?:png|jpeg|webp|gif);base64,[A-Za-z0-9+/=]+$/.test(result.data_url);
  }
  async function openPreview(item, trigger) {
    const sequence = ++state.previewSequence;
    const dialog = $("image-preview"), image = $("preview-image");
    image.hidden = true; image.removeAttribute("src");
    image.alt = safeText(item.prompt, 240) || "生成图片";
    $("preview-title").textContent = item.frame_count > 1 ? "GIF 预览" : "图片预览";
    $("preview-prompt").textContent = safeText(item.prompt, 1000);
    $("preview-status").textContent = "正在读取图片…";
    if (!dialog.open) dialog.showModal();
    dialog.addEventListener("close", () => trigger?.focus(), { once: true });
    try {
      const result = await request("POST", "gallery/preview", { task_id: item.task_id, artifact_id: item.artifact_id, thumbnail: false });
      if (sequence !== state.previewSequence || !dialog.open) return;
      if (!validPreview(result)) throw new Error("invalid_preview");
      image.src = result.data_url; image.hidden = false;
      $("preview-status").textContent = (result.first_frame ? "动态图片首帧预览 · " : "") + safeText(item.task_id, 80);
    } catch {
      if (sequence === state.previewSequence) $("preview-status").textContent = "图片读取失败，文件可能已清理。关闭后可刷新图库并重试。";
    }
  }
  function renderGallery(items, sequence) {
    const container = $("gallery-grid");
    container.replaceChildren();
    if (!items.length) {
      container.append(element("p", "empty-state", "暂无生成图片。生图任务完成并保存结果后，将在这里展示。"));
      return;
    }
    const pending = [];
    for (const item of items) {
      const card = element("article", "gallery-card");
      const thumb = element("button", "gallery-thumb");
      thumb.type = "button";
      thumb.disabled = !item.available;
      thumb.setAttribute("aria-label", "预览图片 " + safeText(item.prompt || item.task_id, 120));
      thumb.append(element("span", "hint", item.available ? "正在读取预览…" : "文件已清理或不可用"));
      thumb.addEventListener("click", () => openPreview(item, thumb));
      const body = element("div", "gallery-body");
      const meta = element("div", "gallery-meta");
      meta.append(badge(item.frame_count > 1 ? "GIF · " + item.frame_count + " 帧" : String(item.mime || "图片").replace("image/", "").toUpperCase()), element("span", "meta", (item.width || "—") + " × " + (item.height || "—")));
      body.append(meta, element("p", "prompt-clamp", safeText(item.prompt, 500) || "未保留提示词"), element("span", "hint", formatTime(item.created)), element("code", "task-id", safeText(item.task_id, 80)));
      card.append(thumb, body); container.append(card);
      if (item.available) pending.push({ item, thumb });
    }
    // Keep preview reads bounded, and ignore responses for a page the user has left.
    const worker = async () => {
      while (pending.length && sequence === state.gallery.sequence && state.currentTab === "gallery") {
        const { item, thumb } = pending.shift();
        try {
          const result = await request("POST", "gallery/preview", { task_id: item.task_id, artifact_id: item.artifact_id, thumbnail: true });
          if (sequence !== state.gallery.sequence || !thumb.isConnected) return;
          if (!validPreview(result)) throw new Error("invalid_preview");
          const image = element("img"); image.src = result.data_url; image.alt = ""; image.loading = "lazy";
          thumb.replaceChildren(image);
        } catch {
          if (thumb.isConnected) thumb.replaceChildren(element("span", "hint", "预览未加载，点击重试"));
        }
      }
    };
    Promise.allSettled([worker(), worker(), worker()]);
  }
  async function loadGallery(offset = state.gallery.offset) {
    if (!state.bridge) return;
    const page = state.gallery, sequence = ++page.sequence;
    pageControls("gallery", true); $("refresh-gallery").disabled = true;
    $("gallery-grid").setAttribute("aria-busy", "true");
    $("gallery-status").textContent = "正在读取图库…";
    try {
      const result = await request("POST", "gallery/query", { offset, limit: page.limit });
      if (sequence !== page.sequence) return;
      if (!Array.isArray(result?.items) || !Number.isInteger(result.total)) throw new Error("invalid_gallery");
      page.offset = result.offset; page.total = result.total;
      if (page.offset >= page.total && page.offset > 0) { loadGallery(0); return; }
      renderGallery(result.items, sequence);
      $("gallery-status").textContent = "已读取 " + result.total + " 张图片记录。缩略图仅预览首帧，点击可查看完整结果。";
    } catch {
      if (sequence === page.sequence) $("gallery-status").textContent = "图库读取失败，保留上次列表。请点击刷新图库重试。";
    } finally {
      if (sequence === page.sequence) {
        pageControls("gallery"); $("refresh-gallery").disabled = false;
        $("gallery-grid").setAttribute("aria-busy", "false");
      }
    }
  }
  async function request(method, endpoint, body) {
    if (!state.bridge) throw new Error("bridge_missing");
    let result = await state.bridge[method === "POST" ? "apiPost" : "apiGet"]("openai-native/" + endpoint, ...(method === "POST" ? [body] : []));
    if (result?.status === "error" || result?.ok === false) {
      const error = new Error(typeof result.message === "string" ? result.message : "request_failed");
      error.status = result.status_code || result.code || result.error?.code;
      throw error;
    }
    if (result?.data && typeof result.data === "object" && (result.status === "ok" || result.status === "success" || result.ok === true)) result = result.data;
    return result;
  }
  function isConflict(error) {
    return error?.status === 409 || error?.status === "conflict" || /409|revision|conflict|版本|冲突/i.test(error?.message || "");
  }
  async function refreshDiagnostics() {
    if (!state.bridge || state.refreshing) return;
    state.refreshing = true;
    $("refresh-diagnostics").disabled = true;
    $("diagnostic-status").textContent = "正在读取运行诊断…";
    try {
      const result = await request("GET", "diagnostics");
      if (!result || typeof result !== "object" || !result.features) throw new Error("invalid_diagnostics");
      state.diagnostics = result;
      renderReadiness(); renderLogging(); renderRecent(); renderConflicts();
      $("diagnostic-status").textContent = "诊断更新于 " + new Date().toLocaleTimeString("zh-CN", { hour12: false }) + (result.notice ? " · " + safeText(result.notice) : "");
    } catch {
      $("diagnostic-status").textContent = "诊断读取失败；保留上次记录，当前表单草稿不受影响。可再次刷新。";
      renderReadiness();
    } finally {
      state.refreshing = false;
      $("refresh-diagnostics").disabled = false;
    }
  }
  async function refreshProviders() {
    try {
      const result = await request("GET", "providers");
      state.providers = Array.isArray(result?.providers) ? result.providers.filter((provider) => typeof provider.id === "string" && provider.id) : [];
      refreshSources();
    } catch {
      for (const { hint } of sourceControls.values()) { hint.hidden = false; hint.textContent = "供应商列表未加载；现有绑定保持不变。重新连接可再读取。"; }
    }
  }
  async function loadSettings(preserve = false) {
    if (state.loading || state.saving) return;
    state.loading = true; $("editor").disabled = true; $("save").disabled = true;
    $("retry-load").hidden = true;
    $("refresh-settings").disabled = true;
    try {
      const result = await request("GET", "settings");
      acceptSnapshot(result, preserve);
      state.conflict = false;
      $("refresh-settings").hidden = true;
      $("connection-state").textContent = "已连接 AstrBot";
      $("connection-state").className = "badge good";
      showNotice(preserve ? "已读取最新配置并保留草稿。请核对未保存项，再点击保存；此操作没有提交修改。" : "配置已加载。诊断日志保存后立即生效；其他配置需手动重载。", preserve ? "warning" : "");
    } catch {
      showNotice("配置读取失败，已有草稿保持不变。请检查宿主连接与插件接口后重试。", "error");
      $("retry-load").hidden = false;
      $("connection-state").textContent = "配置未同步";
      $("connection-state").className = "badge warn";
    } finally {
      state.loading = false;
      $("editor").disabled = !state.loaded;
      $("refresh-settings").disabled = false;
      updateDraft();
    }
  }
  async function save(event) {
    event.preventDefault();
    if (!state.loaded || state.saving || state.loading || state.conflict) return;
    const result = collect();
    $("validation-summary").dataset.revealed = "true";
    renderValidation(result.errors);
    if (result.errors.size) {
      showNotice("输入校验未通过，未提交保存。所有草稿均已保留。", "error");
      $("validation-summary").focus(); return;
    }
    if (!result.dirty.length) return;
    const body = { revision: state.snapshot.revision, patch: result.patch };
    if (Object.keys(result.secretUpdates).length) body.secret_updates = result.secretUpdates;
    state.saving = true; $("editor").disabled = true; $("refresh-settings").disabled = true;
    updateDraft();
    let persisted = false;
    try {
      let response = await request("POST", "settings", body);
      persisted = true;
      if (!validSnapshot(response)) response = await request("GET", "settings");
      acceptSnapshot(response);
      state.conflict = false;
      $("refresh-settings").hidden = true;
      const diagnosticSaved = own(body.patch, "diagnostics");
      showNotice(response.needs_reload
        ? "配置已保存。" + (diagnosticSaved ? "诊断日志设置已生效；其他改动" : "改动") + "请在插件管理中手动重载后生效。"
        : diagnosticSaved ? "诊断日志设置已保存并生效。" : "配置已保存。当前运行配置与已保存配置一致。", "success");
      if (diagnosticSaved) await refreshDiagnostics();
    } catch (error) {
      state.conflict = isConflict(error) || persisted;
      $("refresh-settings").hidden = false;
      showNotice(persisted ? "保存请求已成功，但最新状态未能读取。草稿已保留；先读取最新配置核对，避免重复提交。" : isConflict(error)
        ? "配置版本冲突，未覆盖其他修改。全部草稿已保留；请读取最新配置并核对后再次保存。"
        : "保存失败，全部草稿已保留。请检查接口或输入；可读取最新配置核对后重试。", "error");
    } finally {
      state.saving = false; $("editor").disabled = false; $("refresh-settings").disabled = false;
      updateDraft();
    }
  }
  async function resetCapabilities() {
    if (state.resetting || !state.bridge) return;
    state.resetting = true; $("reset-capabilities").disabled = true;
    try {
      await request("POST", "capabilities/reset", {});
      showNotice("能力记录已重置，未发起付费请求。表单草稿保持不变；后续实际调用会重新记录能力。", "success");
      await refreshDiagnostics();
    } catch { showNotice("能力记录重置失败。未改动配置或表单草稿，可稍后重试。", "error"); }
    finally { state.resetting = false; $("reset-capabilities").disabled = false; }
  }
  function buildCommands() {
    const commands = [
      ["/onative status", "配置与能力状态 · 不发起付费模型请求"],
      ["/onative probe search confirm", "网页搜索探测 · 可能计费"],
      ["/onative probe generate:png confirm", "静态生图探测 · 可能计费"],
      ["/onative probe edit:png confirm", "参考图编辑探测 · 可能计费，需提供参考图"],
      ["/onative probe generate:gif confirm", "原生 GIF 探测 · 可能计费，需先开启 GIF"]
    ];
    for (const [command, hint] of commands) {
      const row = element("div", "command-row");
      const content = element("div");
      content.append(element("code", "", command), element("span", "hint", hint));
      const button = element("button", "button small", "复制");
      button.type = "button";
      button.setAttribute("aria-label", "复制命令 " + command);
      button.addEventListener("click", async () => {
        try {
          if (navigator.clipboard?.writeText) await navigator.clipboard.writeText(command);
          else {
            const selection = window.getSelection();
            const range = document.createRange();
            range.selectNodeContents(content.firstChild);
            selection.removeAllRanges(); selection.addRange(range);
            if (!document.execCommand("copy")) throw new Error("copy_failed");
          }
          button.textContent = "已复制";
          setTimeout(() => { button.textContent = "复制"; }, 1800);
        } catch {
          button.textContent = "请手动复制";
          showNotice("当前宿主不允许自动复制，请选中命令手动复制；页面不会执行测试命令。", "warning");
        }
      });
      row.append(content, button); $("test-commands").append(row);
    }
  }
  function applyTheme() {
    let dark = state.context?.isDark;
    if (typeof dark !== "boolean") dark = window.matchMedia?.("(prefers-color-scheme: dark)").matches ?? true;
    document.documentElement.dataset.theme = state.theme === "host" ? (dark ? "dark" : "light") : state.theme;
    $("theme").value = state.theme;
  }
  async function start() {
    buildFields(); refreshSources(); buildCommands(); renderReadiness();
    try {
      const stored = localStorage.getItem("openai-native.dashboard.theme");
      if (["host", "dark", "light"].includes(stored)) state.theme = stored;
    } catch { /* A restricted iframe may not expose storage. */ }
    applyTheme();
    $("theme").addEventListener("change", () => {
      state.theme = $("theme").value;
      try { localStorage.setItem("openai-native.dashboard.theme", state.theme); } catch { /* Theme still works for this visit. */ }
      applyTheme();
    });
    const media = window.matchMedia?.("(prefers-color-scheme: dark)");
    media?.addEventListener?.("change", applyTheme);
    document.querySelectorAll("[data-tab]").forEach((button) => {
      button.addEventListener("click", () => showTab(button.dataset.tab));
      button.addEventListener("keydown", (event) => {
        const tabs = Array.from(document.querySelectorAll("[data-tab]"));
        let index = tabs.indexOf(button);
        if (event.key === "ArrowRight") index = (index + 1) % tabs.length;
        else if (event.key === "ArrowLeft") index = (index - 1 + tabs.length) % tabs.length;
        else if (event.key === "Home") index = 0;
        else if (event.key === "End") index = tabs.length - 1;
        else return;
        event.preventDefault(); showTab(tabs[index].dataset.tab, true);
      });
    });
    $("settings-form").addEventListener("submit", save);
    $("refresh-diagnostics").addEventListener("click", () => Promise.allSettled([refreshDiagnostics(), refreshSummary()]));
    $("refresh-tasks").addEventListener("click", () => loadTasks());
    for (const id of ["task-kind", "task-status"]) $(id).addEventListener("change", () => loadTasks(0));
    $("refresh-gallery").addEventListener("click", () => loadGallery());
    for (const name of ["tasks", "gallery"]) {
      for (const [suffix, delta] of [["prev", -1], ["next", 1]]) {
        $(name + "-" + suffix).addEventListener("click", () => {
          const page = state[name], offset = Math.max(0, page.offset + delta * page.limit);
          if (name === "tasks") loadTasks(offset); else loadGallery(offset);
        });
      }
    }
    $("close-preview").addEventListener("click", () => $("image-preview").close());
    $("image-preview").addEventListener("close", () => {
      ++state.previewSequence;
      $("preview-image").removeAttribute("src");
      $("preview-image").hidden = true;
    });
    $("reset-capabilities").addEventListener("click", resetCapabilities);
    $("refresh-settings").addEventListener("click", () => loadSettings(true));
    $("retry-load").addEventListener("click", async () => {
      if (!state.bridge) { await connect(); return; }
      await Promise.allSettled([loadSettings(state.loaded), refreshProviders(), refreshDiagnostics(), refreshSummary()]);
    });
    window.addEventListener("beforeunload", (event) => {
      if (state.loaded && collect().dirty.length) { event.preventDefault(); event.returnValue = ""; }
    });
    await connect();
  }
  async function connect() {
    state.bridge = window.AstrBotPluginView || window.AstrBotPluginPage;
    if (!state.bridge) {
      $("connection-state").textContent = "未连接宿主";
      showNotice("请从 AstrBot 的插件页面打开此页。未找到宿主 bridge SDK，配置暂不可读取或保存。", "warning");
      $("retry-load").hidden = false; return;
    }
    try {
      state.context = state.bridge.getContext?.() || null;
      state.bridge.onContext?.((context) => { state.context = context; applyTheme(); });
      applyTheme();
      let timer;
      try {
        await Promise.race([
          typeof state.bridge.ready === "function" ? state.bridge.ready() : state.bridge.ready,
          new Promise((_, reject) => { timer = setTimeout(() => reject(new Error("bridge_timeout")), 12000); })
        ]);
      } finally { clearTimeout(timer); }
      state.context = state.bridge.getContext?.() || state.context;
      applyTheme();
      await Promise.allSettled([loadSettings(state.loaded), refreshProviders(), refreshDiagnostics(), refreshSummary()]);
    } catch {
      $("connection-state").textContent = "宿主连接未就绪";
      showNotice("等待 AstrBot 宿主响应超时，草稿保持不变。请重新连接或从插件页面重新打开。", "error");
      $("retry-load").hidden = false;
    }
  }
  start().catch(() => {
    showNotice("页面初始化失败，请重新打开插件页面。未提交任何配置。", "error");
  });
})();
