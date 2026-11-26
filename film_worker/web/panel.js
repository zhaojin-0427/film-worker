const form = document.querySelector("#form");
const notice = document.querySelector("#notice");
let csrf = "";

function message(text, error = false) {
  notice.textContent = text;
  notice.dataset.error = error;
}

async function api(path, body) {
  const init = body === undefined ? {} : {
    method: "POST",
    headers: {"Content-Type": "application/json", "X-Panel-Key": csrf},
    body: JSON.stringify(body)
  };
  const res = await fetch("/api/" + path, init);
  const data = await res.json();
  if (!res.ok) throw new Error(data.error ?? ("HTTP " + res.status));
  return data;
}

const stateNames = {connected: "服务已连接", stopped: "未运行", connecting: "正在连接", network_unavailable: "网络不可用", configuration_error: "配置需处理"};
const jobKinds = {prepare: "首制", revise: "返修", finalize: "出片"};
const jobStates = {
  queued: "排队中", running: "运行中", review: "待审核", done: "已交付",
  succeeded: "已提交", active: "运行中", paused: "已暂停", failed: "失败",
  interrupted: "已中断", stopped: "已停止", deleted: "已删除",
  awaiting_manual: "待手动交付", verifying: "云端校验中",
};
const phaseNames = {
  input: "准备素材", author: "Codex 制作", revise: "Codex 返修", draft: "逐镜自检",
  verify: "校验审核版", render: "审核渲染", finalRender: "成片渲染", package: "对比与打包",
  delivery: "编码与打包", review: "本地复核", upload: "上传提交",
};
const workflows = {
  prepare: ["input", "author", "draft", "render", "package", "review", "upload"],
  revise: ["input", "revise", "render", "package", "review", "upload"],
  finalize: ["input", "verify", "finalRender", "delivery", "review", "upload"],
};
const steps = {
  received: ["input", "确认工单"], recover: ["input", "恢复工单"],
  download: ["input", "下载输入文件"], prepare_source: ["input", "抽取编号源帧"],
  prepare_draft: ["input", "生成镜头参考"], waiting_for_codex: ["author", "等待 Codex 槽位"],
  codex_author: ["author", "分析原片与制作 Blender 工程"],
  codex_resume: ["author", "接续原 Codex 会话"],
  repair_engineering: ["author", "修复 Blender 工程"], codex_repair: ["author", "修复 Blender 工程"],
  waiting_for_draft_blender: ["draft", "等待 Blender 槽位"],
  draft_compile: ["draft", "编译自检工程"], draft_review: ["draft", "准备全片自检渲染"],
  encode_draft: ["draft", "编码首版自检视频"], verify_draft: ["draft", "核验自检视频时序"],
  codex_self_review: ["draft", "Codex 逐镜对照与修正"],
  preview_visual_review: ["draft", "试渲并送审低清关键帧"],
  preview_auditor: ["draft", "独立审核员检查低清关键帧"],
  codex_auditor: ["review", "独立审核员核对送审画面"],
  codex_visual_repair: ["author", "制作员按独立审核意见返修"],
  validate_scene: ["render", "核验 Blender 工程"], waiting_for_blender: ["render", "等待 Blender 槽位"],
  compare_review: ["package", "生成逐帧对比图"], package_review: ["package", "打包审核产物"],
  verify_review: ["verify", "核对审核帧签名"], encode_white: ["delivery", "编码白模成片"],
  encode_comparison: ["delivery", "编码对比成片"], verify_technical: ["delivery", "核验成片规格"],
  package_delivery: ["delivery", "打包交付产物"], reuse_outputs: ["review", "复用已核验的本地产物"],
  prepare_local_review: ["review", "生成本地复核样张"], awaiting_local_review: ["review", "等待本地确认"],
  waiting_for_upload: ["upload", "等待上传槽位"], commit_result: ["upload", "提交结果并等待回执"],
  package_oss: ["upload", "生成 OSS 交付包"], oss_upload: ["upload", "上传 OSS 交付包"],
};
const artifacts = {
  source_timeline: "源片时间线", scene_bundle: "Blender 工程包", shot_metadata: "镜头信息",
  review_bundle: "审核对比包", version_manifest: "版本清单", production_report: "制作记录",
  white_video: "白模成片", comparison_video: "对比成片", delivery_bundle: "交付包",
};
const activities = {
  reconnecting: "模型服务重连", tool: "Codex 调用本机工具",
  tool_result: "最近一次工具调用已返回", model_output: "Codex 已输出日志",
};
const milestoneNames = {
  shot_plan: "已保存镜头表", compiled: "已保存编译结果",
  draft_saved: "已生成自检视频文件", draft_checked: "已写入自检视频验证记录",
};
const recoveryLabels = {
  reconnecting: "模型服务重连", backoff: "等待自动接回", resuming: "正在接回原会话",
  checking_lease: "正在确认工单租约", restored: "已恢复制作", waiting: "等待处理",
  timed_out: "人工接回等待超时",
};
const recoveryReasons = {
  connection: "模型连接中断", timeout: "模型请求超时", rate_limit: "模型服务限流",
  auth: "模型鉴权失败", missing_session: "找不到可接回的原会话",
};
const requestedReconnects = new Set();
const progressSamples = new Map();
const timeFormat = new Intl.DateTimeFormat("zh-CN", {hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false});
let reviewKey = "";

function element(tag, css, value) {
  const node = document.createElement(tag);
  if (css) node.className = css;
  if (value != null) node.textContent = value;
  return node;
}

function elapsed(seconds) {
  const whole = Math.max(0, Math.floor(seconds));
  if (whole >= 3600) return `${Math.floor(whole / 3600)} 小时 ${Math.floor(whole / 60) % 60} 分`;
  if (whole >= 60) return `${Math.floor(whole / 60)} 分 ${whole % 60} 秒`;
  return `${whole} 秒`;
}

function describeStage(stage, kind) {
  const render = /^render_(draft|review|final)_([a-z0-9]+)$/.exec(stage);
  if (render) {
    const [, purpose, backend] = render;
    const phase = {draft: "draft", review: "render", final: "finalRender"}[purpose];
    return {phase, name: "Blender 逐帧渲染", backend};
  }
  if (stage.startsWith("upload_")) {
    const role = stage.slice("upload_".length);
    return {phase: "upload", name: `上传${artifacts[role] ?? "产物"}`};
  }
  let [phase, name] = steps[stage] ?? ["", stage];
  if (phase === "author" && kind === "revise") phase = "revise";
  if (phase === "draft" && kind === "revise") phase = "revise";
  if (phase === "render" && kind === "finalize") phase = "finalRender";
  if (stage === "codex_author" && kind === "revise") name = "按反馈调整 Blender 工程";
  return {phase, name};
}

function showPhases(kind, step) {
  const flow = element("ol", "job-flow");
  flow.setAttribute("aria-label", "工单阶段");
  const phases = workflows[kind] ?? workflows.prepare;
  const current = phases.indexOf(step.phase);
  phases.forEach((phase, index) => {
    const node = element("li", "job-step");
    const status = index === current ? "active" : index < current ? "done" : "pending";
    node.dataset.state = status;
    const dot = element("span", "job-node", {active: "●", done: "✓", pending: ""}[status]);
    dot.setAttribute("aria-hidden", "true");
    if (status === "active") node.setAttribute("aria-current", "step");
    const name = phase === step.phase && step.backend && phase === "render"
      ? `${step.backend === "cpu" ? "CPU" : "GPU"} 审核渲染` : phaseNames[phase];
    const label = current < 0 ? "待确认" : {active: "进行中", done: "已完成", pending: "待开始"}[status];
    node.append(dot, element("span", "job-step-name", name), element("span", "job-step-status", label));
    flow.append(node);
  });
  return flow;
}

function showMilestones(detail, extra, now) {
  if (extra.milestones?.length) {
    const list = element("ul", "job-milestones");
    for (const milestone of extra.milestones) {
      const entry = element("li", "job-milestone");
      const time = element("time", "", timeFormat.format(new Date(milestone.at)));
      time.dateTime = milestone.at;
      entry.append(time, element("span", "", milestoneNames[milestone.step] ?? milestone.step));
      list.append(entry);
    }
    detail.append(list);
  }
  if (extra.log_updated_at) {
    const last = Date.parse(extra.log_updated_at);
    const seconds = Math.max(0, (now - last) / 1000);
    const label = seconds >= 120
      ? `已 ${elapsed(seconds)} 没有新日志`
      : `最近日志 ${timeFormat.format(new Date(last))}`;
    const status = activities[extra.activity];
    const message = element("p", "job-log-status", status ? `${label} · ${status}` : label);
    message.dataset.quiet = seconds >= 120;
    detail.append(message);
  }
}

function showCounts(detail, job, step, extra, sample, now) {
  const total = Number(job.total);
  if (!(total > 0)) return;
  const done = Math.max(0, Math.min(total, Number(job.completed) || 0));
  const percent = done === total ? 100 : Math.floor(done * 100 / total);
  const transfer = job.stage.startsWith("upload_") || ["package_oss", "oss_upload"].includes(job.stage);
  const amount = transfer
    ? `${(done / 1048576).toFixed(1)} / ${(total / 1048576).toFixed(1)} MiB`
    : `${done.toLocaleString("zh-CN")} / ${total.toLocaleString("zh-CN")} 帧`;
  const header = element("div", "job-counts");
  header.append(element("strong", "", amount), element("span", "", `${percent}%`));
  const progress = element("progress", "job-meter");
  progress.setAttribute("aria-label", step.name);
  progress.max = total;
  progress.value = done;
  detail.append(header, progress);
  if (!step.backend) return;

  const stats = element("dl", "job-stats");
  const fields = [["渲染设备", step.backend === "cpu" ? "CPU" : `GPU · ${step.backend === "optix" ? "OptiX" : step.backend.toUpperCase()}`]];
  if (extra.render_size) fields.push(["输出尺寸", extra.render_size.join(" × ")]);
  const seconds = (now - sample.since) / 1000;
  const frames = done - sample.done;
  if (done < total && frames >= 10 && seconds >= 15 && now - sample.lastAdvance < 30000) {
    fields.push(["本阶段预计剩余", `约 ${elapsed((total - done) * seconds / frames)}`]);
  }
  fields.forEach(([name, value]) => {
    const group = element("div");
    group.append(element("dt", "", name), element("dd", "", value));
    stats.append(group);
  });
  detail.append(stats);
}

function showRecovery(detail, job, extra) {
  const recovery = extra.recovery;
  if (!recovery || !recoveryLabels[recovery.state]) return;
  const row = element("div", "job-recovery");
  row.dataset.state = recovery.state;
  const status = element("div");
  status.append(element("strong", "", recoveryLabels[recovery.state]));
  const facts = [recoveryReasons[recovery.reason], `自动接回 ${recovery.used} / ${recovery.limit}`];
  if (recovery.last_activity) facts.push(`最近有效活动 ${timeFormat.format(new Date(recovery.last_activity))}`);
  if (recovery.next_retry_at) facts.push(`下次接回 ${timeFormat.format(new Date(recovery.next_retry_at))}`);
  if (recovery.manual_deadline) facts.push(`处理截止 ${timeFormat.format(new Date(recovery.manual_deadline))}`);
  status.append(element("p", "", facts.filter(Boolean).join(" · ")));
  row.append(status);
  if (recovery.wait_id) {
    const button = element("button", "", "接回原会话");
    button.type = "button";
    button.disabled = !csrf || requestedReconnects.has(recovery.wait_id);
    button.onclick = async () => {
      requestedReconnects.add(recovery.wait_id);
      button.disabled = true;
      try {
        await api("codex-reconnect", {work_id: job.work_id, attempt: job.attempt, wait_id: recovery.wait_id});
        message("已请求接回原 Codex 会话");
      } catch (error) {
        requestedReconnects.delete(recovery.wait_id);
        button.disabled = false;
        message(error.message, true);
      }
    };
    row.append(button);
  }
  detail.append(row);
}

function showJobs(state) {
  const list = document.querySelector("#jobs");
  const now = Date.now();
  const visible = new Set();
  const rows = state.active.map(job => {
    const key = `${job.work_id}/${job.attempt}`;
    visible.add(key);
    const recent = (state.history ?? []).find(item => item.work_id === job.work_id && item.attempt === job.attempt);
    const kind = job.kind ?? recent?.kind ?? "prepare";
    const step = describeStage(job.stage, kind);
    const extra = job.details ?? {};
    const started = Date.parse(extra.started_at);
    let sample = progressSamples.get(key);
    if (!sample || sample.stage !== job.stage || sample.started !== extra.started_at || job.completed < sample.lastDone) {
      sample = {stage: job.stage, started: extra.started_at, since: now, done: job.completed,
                lastDone: job.completed, lastAdvance: now};
      progressSamples.set(key, sample);
    }
    if (job.completed > sample.lastDone) sample.lastAdvance = now;
    sample.lastDone = job.completed;

    const row = element("li", "job-card");
    const heading = element("div", "job-header");
    const identity = element("div", "job-identity");
    const jobId = job.job_id ?? recent?.job_id;
    const title = element("strong", "", jobId ? `工单 ${jobId}` : job.work_id);
    title.title = job.work_id;
    identity.append(title, element("span", "job-muted", `${jobKinds[kind] ?? kind} · 第 ${job.attempt} 次尝试`));
    heading.append(identity, element("span", "job-phase", phaseNames[step.phase] ?? "当前进度"));
    const detail = element("div", "job-detail");
    const detailHead = element("div", "job-detail-head");
    const names = element("div");
    names.append(element("h3", "", step.name));
    detailHead.append(names, element("span", "job-muted", Number.isFinite(started)
      ? `本步骤已用 ${elapsed((now - started) / 1000)}`
      : `页面已跟踪 ${elapsed((now - sample.since) / 1000)}`));
    detail.append(detailHead);
    if (extra.visual_review) {
      const {ready, total} = extra.visual_review;
      detail.append(element("p", "job-log-status", `已交给独立审核员 ${ready} / ${total} 张重点对比帧`));
    }
    showRecovery(detail, job, extra);
    showMilestones(detail, extra, now);
    showCounts(detail, job, step, extra, sample, now);
    row.append(heading, showPhases(kind, step), detail);
    if (job.stage === "awaiting_local_review") {
      row.append(element("p", "job-review-note", "审核产物就绪后，需本地确认才能上传。"));
    }
    return row;
  });
  for (const key of progressSamples.keys()) {
    if (!visible.has(key)) progressSamples.delete(key);
  }
  const waitingIds = new Set(state.active.map(job => job.details?.recovery?.wait_id));
  for (const id of requestedReconnects) {
    if (!waitingIds.has(id)) requestedReconnects.delete(id);
  }
  list.replaceChildren(...(rows.length ? rows : [element("li", "job-empty", "当前没有运行中的工单")]));
}

function showLocalReviews(reviews) {
  const key = JSON.stringify(reviews);
  if (key === reviewKey) return;
  reviewKey = key;
  const area = document.querySelector("#local-review");
  area.hidden = reviews.length === 0;
  area.replaceChildren(...reviews.map(review => {
    const row = document.createElement("div");
    row.className = "review-row";
    const label = document.createElement("strong");
    label.textContent = `工单 ${review.job_id} · 本地复核`;
    row.append(label);
    const samples = element("div", "actions review-samples");
    for (const sample of review.samples) {
      const frame = sample.split(/[\\/]/).pop();
      const link = document.createElement("a");
      link.href = "/api/local-review/sample?" + new URLSearchParams({work: review.work_id, attempt: review.attempt, frame});
      link.target = "_blank";
      link.rel = "noopener";
      link.textContent = `第 ${parseInt(frame, 10)} 帧`;
      samples.append(link);
    }
    row.append(samples);
    const controls = element("div", "actions");
    for (const [decision, label] of [["approve", "确认并上传"], ["reject", "退回产物"]]) {
      const button = document.createElement("button");
      button.type = "button";
      if (decision === "approve") button.className = "primary";
      button.disabled = !csrf;
      button.textContent = label;
      button.onclick = async () => {
        try {
          const {work_id, attempt, outputs_sha256} = review;
          await api("local-review", {work_id, attempt, outputs_sha256, decision});
          row.querySelectorAll("button").forEach(control => { control.disabled = true; });
          message("复核结果已保存");
        } catch (error) {
          message(error.message, true);
        }
      };
      controls.append(button);
    }
    row.append(controls);
    return row;
  }));
}

async function refresh() {
  try {
    const state = await api("status");
    const badge = document.querySelector("#connection");
    badge.textContent = stateNames[state.connection] ?? state.connection;
    badge.title = state.detail ?? "";
    badge.dataset.online = state.connection === "connected";
    const release = state.release;
    document.querySelector("#update-status").textContent = release ?
      `${release.version} · ${release.commit.slice(0, 12) || "本地版本"} · ${state.update_status}` : "";
    document.querySelector('[data-control="rollback"]').disabled = !state.managed || !state.rollback_available;
    document.querySelector('[data-control="update"]').disabled = !state.managed;
    document.querySelector("#capacity").textContent = state.detail || (state.slots
      ? (state.draining ? "节点已暂停领单" : "节点正在接收工单") : "节点待启动");
    document.querySelector("#slot-status").textContent = state.slots ? `${state.active.length} / ${state.slots}` : "—";
    document.querySelector("#disk-status").textContent = state.free_gib == null ? "—" : `${state.free_gib} GiB`;
    document.querySelector("#blender-status").textContent = state.versions?.blender || "—";
    const queue = document.querySelector("#queue-status");
    queue.textContent = `${state.active.length} 条工单`;
    queue.dataset.active = state.active.length > 0;
    showJobs(state);
    const waiting = state.active.some(job => job.stage === "awaiting_local_review");
    showLocalReviews(waiting ? (await api("local-review")).reviews : []);
    const recent = (state.history ?? []).map(item => {
      const line = element("li", "history-row");
      const identity = element("div", "history-identity");
      identity.append(element("strong", "", `工单 ${item.job_id}`),
                      element("span", "", `${jobKinds[item.kind] ?? item.kind} · 第 ${item.attempt} 次尝试`));
      const status = item.oss_delivery || ["failed", "interrupted"].includes(item.status)
        ? item.status : item.job_state ?? item.status;
      const label = element("span", "history-status", item.oss_delivery && status === "succeeded"
        ? (item.kind === "finalize" ? "已交付" : "已送审") : jobStates[status] ?? status);
      label.dataset.state = status;
      if (item.message) identity.append(element("span", "submission-path", item.message));
      const delivery = (item.delivery_versions ?? []).join("、");
      if (delivery) identity.append(element("span", "", `交付版 ${delivery}`));
      if (item.submission_path) identity.append(element("code", "submission-path", item.submission_path));
      line.append(identity, label);
      return line;
    });
    document.querySelector("#history").replaceChildren(...recent);
  } catch (error) {
    const badge = document.querySelector("#connection");
    badge.textContent = "工作台暂不可用";
    badge.dataset.online = false;
    badge.title = error.message;
  }
}

async function openPanel() {
  try {
    const data = await api("config");
    csrf = data.csrf;
    if (typeof data.oss_configured === "boolean") {
      document.querySelector("#oss-settings").hidden = false;
      showOssKeyStatus(data.oss_configured);
    }
    for (const [key, value] of Object.entries(data.config)) {
      const field = form.elements.namedItem(key);
      if (field?.type === "checkbox") field.checked = value;
      else if (field) field.value = value;
    }
    document.querySelector("#address").textContent = data.config.server || "本机控制面板";
  } catch (error) {
    message(error.message, true);
  }
  await refresh();
  window.setInterval(refresh, 2500);
}

form.addEventListener("submit", async event => {
  event.preventDefault();
  try {
    const config = Object.fromEntries(new FormData(form));
    const token = config.token;
    delete config.token;
    const id = config.oss_key_id.trim();
    const secret = config.oss_key_secret.trim();
    delete config.oss_key_id;
    delete config.oss_key_secret;
    if (Boolean(id) !== Boolean(secret)) throw new Error("请同时填写 OSS AccessKey ID 和 Secret");
    const oss_keys = id ? {id, secret} : undefined;
    config.auto_update = form.elements.namedItem("auto_update").checked;
    config.dual_role_review = form.elements.namedItem("dual_role_review").checked;
    for (const field of form.querySelectorAll("input[type=number]")) config[field.name] = Number(field.value);
    await api("config", {config, token, oss_keys});
    form.elements.namedItem("token").value = "";
    form.elements.namedItem("oss_key_id").value = "";
    form.elements.namedItem("oss_key_secret").value = "";
    if (oss_keys) showOssKeyStatus(true);
    document.querySelector("#address").textContent = config.server || "本机控制面板";
    message("配置已保存");
  } catch (error) {
    message(error.message, true);
  }
});

function showOssKeyStatus(configured) {
  document.querySelector("#oss-key-status").textContent = configured ?
    "已保存在系统凭据库，留空沿用。" : "自动交付需要配置。手动交付由服务端为设备指定。";
}

const navigation = document.querySelectorAll(".sidebar nav a");
function showLocation() {
  const hash = location.hash || "#workbench";
  for (const link of navigation) {
    if (link.hash === hash) link.setAttribute("aria-current", "location");
    else link.removeAttribute("aria-current");
  }
}
window.addEventListener("hashchange", showLocation);
showLocation();

document.querySelectorAll("[data-control]").forEach(button => {
  button.addEventListener("click", async () => {
    button.disabled = true;
    try {
      const state = await api(button.dataset.control, {});
      message(state.versions ? Object.entries(state.versions).map(([k, v]) => `${k} ${v}`).join(" · ") : "操作已提交");
      await refresh();
    } catch (error) {
      message(error.message, true);
    } finally {
      button.disabled = false;
    }
  });
});
document.querySelector("#test-model").addEventListener("click", async event => {
  const button = event.currentTarget;
  button.disabled = true;
  message("正在测试本机 Codex 登录与模型");
  try {
    const response = await api("model-test", {});
    const fields = Object.values(response.observed).filter(Boolean).join(" · ");
    message(fields ? "模型测试通过 · " + fields : "调用已通过，CLI 未提供实际模型标识");
  } catch (error) {
    message(error.message, true);
  } finally {
    button.disabled = false;
  }
});

const loginStatus = document.querySelector("#login-status");
const loginToggle = document.querySelector("#login-toggle");
let loginEnabled = false;

function showLogin(data) {
  loginEnabled = data.enabled;
  loginStatus.textContent = loginEnabled ? "已启用 · 下次登录后后台运行" : "未启用";
  loginToggle.textContent = loginEnabled ? "关闭登录启动" : "启用登录启动";
}

async function loadLogin() {
  try {
    const status = await api("status");
    const data = await api("login");
    showLogin(data);
    loginToggle.disabled = !status.managed || !csrf;
  } catch (error) {
    loginStatus.textContent = error.message;
  }
}

loginToggle.addEventListener("click", async () => {
  loginToggle.disabled = true;
  try {
    showLogin(await api("login", {enabled: !loginEnabled}));
  } catch (error) {
    loginStatus.textContent = error.message;
  } finally {
    loginToggle.disabled = false;
  }
});

openPanel().then(loadLogin);
