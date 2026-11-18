"use strict";

const $ = (id) => document.getElementById(id);
const environments = ["A", "B"];
const steps = ["初始失败", "候选解释", "选择探针", "执行两个动作", "反例淘汰", "新会话盲测"];
const axisName = (axis) => ({version: "版本", currency: "币种"}[axis] || axis || "未记录");
const statusName = (status) => ({
  pending: "待验证", active: "已启用", rejected: "已淘汰", superseded: "已替换", rolled_back: "已回滚"
}[status] || status);
const page = {run: null, env: "A", step: 5, eventKey: null, comparisons: {}, busy: false};
const current = () => page.run.environments[page.env];

function element(tag, className, ...children) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  node.append(...children);
  return node;
}
const paragraph = (...children) => element("p", "", ...children);
const mono = (value) => element("code", "", String(value ?? "未记录"));
const note = (value) => element("p", "note", value);
function button(label, onClick, className = "") {
  const node = element("button", className, label);
  node.type = "button";
  node.addEventListener("click", onClick);
  return node;
}
function record(entries) {
  return element("dl", "record", ...entries.map(([label, val]) =>
    element("div", "pair", element("dt", "", label), element("dd", "", val ?? "未记录"))));
}
function field(name, content) {
  const node = content instanceof Node ? content : mono(content);
  node.dataset.field = name;
  return node;
}
function grade(verdict) {
  const state = verdict === "PASS" ? "pass" : verdict === "FAIL" ? "fail" : "";
  return field("verdict", element("span", "grade " + state, verdict ?? "未评分"));
}
function orderText(row) {
  return [row.order_id, row.version + "/" + row.currency, "输入 " + row.amount].join(" · ");
}
function mapping(rule) {
  return element("div", "mapping", ...Object.entries(rule?.mapping || {}).map(
    ([value, action]) => mono(value + " → " + action)));
}
function metric(label, name, value) {
  return element("div", "", element("span", "result-label", label),
    field(name, element("span", "money", String(value ?? "未记录"))));
}
function eventLabel(row) { return "事件 " + row.event_id; }
function showStep(step, key = null, focus = false) {
  page.step = step;
  page.eventKey = key;
  renderInvestigation();
  if (focus) $("step-detail").focus();
}
function gotoEvent(env, key) {
  page.env = env;
  const run = current();
  const row = [...run.initial, ...run.attempts].find((r) => r.event_key === key);
  if (!row) throw new Error("找不到该事件的执行记录");
  showStep(run.initial.includes(row) ? 0 : 3, key, true);
  renderComparison();
}
function eventLink(env, row, label = eventLabel(row)) {
  return button(label, () => gotoEvent(env, row.event_key), "link-btn");
}
function counterLinks(env, run, axis) {
  return (run.result.counterexample_keys[axis] || []).map((key) => {
    const row = [...run.initial, ...run.attempts].find((r) => r.event_key === key);
    if (!row) return note("反例执行缺少记录");
    const link = eventLink(env, row, "按" + axisName(axis) + "的反例 · " + eventLabel(row) + " ↗");
    link.dataset.counterexample = key;
    return link;
  });
}
function summary(env, run) {
  const winner = run.plan.candidates.find((c) => c.axis === run.result.active);
  const label = "环境 " + env;
  const card = element("article", "environment");
  card.dataset.summary = env;
  card.setAttribute("aria-label", label);
  const title = element("h3", "", label + (winner ? " · 按" + axisName(winner.axis) + "导入" : " · 精确匹配"));
  card.append(element("div", "env-head", element("span", "env-letter", env),
    element("div", "", title, mapping(winner))));
  const blind = run.blind;
  card.append(element("div", "env-result", metric("盲测实际入库（分）", "actual", blind.actual),
    metric("评分器期望（分）", "target", blind.target), grade(blind.verdict)));
  const links = Object.keys(run.result.counterexample_keys).flatMap((axis) => counterLinks(env, run, axis));
  card.append(element("div", "env-foot", element("span", "", "动作 ", field("action", blind.action)), ...links));
  return card;
}
function renderOverview() {
  const groups = environments.map((env) => {
    const run = page.run.environments[env];
    const seeds = [...new Set(run.initial.map(orderText))].join("；");
    return [["初始订单", seeds], ["探针", orderText(run.plan.order)], ["盲测", orderText(run.blind)]];
  });
  const shared = JSON.stringify(groups[0]) === JSON.stringify(groups[1]);
  const strip = (group, prefix) => element("div", "input-strip", ...group.map(([label, val]) =>
    element("div", "", element("strong", "", prefix + label), mono(val))));
  $("inputs").replaceChildren(...(shared ? [strip(groups[0], "")] :
    groups.map((group, i) => strip(group, "环境 " + environments[i] + " · "))));
  $("summaries").replaceChildren(...environments.map((env) => summary(env, page.run.environments[env])));
}
function selectedExecution() {
  const run = current();
  if (page.step === 5) return run.blind;
  const rows = page.step === 0 ? run.initial : page.step === 3 ? run.attempts : [];
  return rows.find((row) => row.event_key === page.eventKey) || rows[0];
}
function executionDetail(run, row) {
  const box = element("div");
  box.id = "execution-detail";
  if (row.event_key) box.dataset.eventKey = row.event_key;
  const entries = [["订单", mono(orderText(row))]];
  if (page.step === 3) {
    const predictions = run.plan.candidates.filter((c) => c.mapping[row[c.axis]] === row.action);
    for (const c of predictions) entries.push(["按" + axisName(c.axis) + "预测", mono(c.mapping[row[c.axis]])]);
  }
  entries.push(["实际动作", field("action", row.action)], ["入库金额", field("actual", row.actual)],
    ["评分器期望", field("target", row.target)], ["独立评分", grade(row.verdict)]);
  box.append(record(entries));
  return box;
}
function renderDetail(run, row) {
  const area = $("step-detail");
  area.replaceChildren(element("p", "context", "环境 " + page.env + " / " + steps[page.step]));
  if (row) {
    area.append(element("h3", "", row.order_id + " · " +
      (page.step === 5 ? "只读会话" : eventLabel(row))));
    if (page.step !== 5) {
      const choices = page.step === 0 ? run.initial : run.attempts;
      area.append(element("div", "select-list", ...choices.map((r) => {
        const select = button(eventLabel(r) + " · " + r.verdict, () => showStep(page.step, r.event_key));
        select.setAttribute("aria-pressed", String(row === r));
        return select;
      })));
    }
    area.append(executionDetail(run, row));
    if (page.step === 5) {
      area.append(button("查看禁用修订对照 ↓", () => $("compare-button").focus(), "link-btn"));
    }
    return;
  }
  if (page.step === 2) {
    area.append(element("h3", "", "在哪个组合上，两条规则会分歧？"),
      record([["选中的订单", mono(orderText(run.plan.order))]]));
    for (const alt of run.plan.alternatives) {
      const title = alt.version + "/" + alt.currency + " · " + alt.disagreement + " 对预测分歧";
      area.append(element("div", "candidate", element("h4", "", title),
        ...Object.entries(alt.predictions).map(([axis, action]) =>
          paragraph("按" + axisName(axis) + "预测 ", mono(action)))));
    }
    area.append(note("选取预测分歧最多的未执行组合。并列时按版本、币种升序选择。"));
    return;
  }
  const resolved = page.step === 4;
  area.append(element("h3", "", resolved ? "哪条规则经得起探针检验？" : "两条规则都能解释初始订单"));
  for (const c of run.plan.candidates) {
    const revision = run.memory.revisions.find((r) => r.axis === c.axis &&
      (r.id === run.result.revision_id || run.result.counterexamples[c.axis]?.length));
    const status = resolved ? statusName(revision?.status) || "未记录" : "待验证";
    const block = element("div", "candidate", element("h4", "", "按" + axisName(c.axis) + " · " + status), mapping(c));
    if (resolved) block.append(...counterLinks(page.env, run, c.axis));
    area.append(block);
  }
}
function layer(title, ...content) {
  return element("div", "layer", element("h4", "", title), element("div", "layer-body", ...content));
}
function hitDescription(hit) {
  if (!hit) return "检索结果未记录";
  if (hit.revision_id) return "命中修订 #" + hit.revision_id + " · " + axisName(hit.axis) + " " + hit.value;
  if (hit.skill_id) return "精确命中技能 #" + hit.skill_id;
  return "未命中记忆";
}
function renderMemory(run, row) {
  const area = $("memory");
  const db = run.memory;
  const hit = row?.retrieved;
  const eventKeys = new Set([row?.event_key, hit?.event_key].filter(Boolean));
  const events = db.events.filter((e) => eventKeys.has(e.event_key));
  const observations = db.observations.filter((o) => eventKeys.has(o.event_key));
  const skills = db.skills.filter((s) => observations.some((o) => o.id === s.observation_id));
  area.replaceChildren(element("h3", "", "四层记忆"));
  const work = row ? [
    paragraph(mono(page.run.project), " / ", mono(orderText(row)), " → ", mono(row.action)),
    paragraph(hitDescription(hit)),
    paragraph("会话 ", mono(row.session))
  ] : [note("此步骤没有单次执行的决策快照。")];
  area.append(layer("工作记忆", ...work));
  const episodeNodes = events.map((e) => paragraph(e.event_key === row?.event_key ? "本次执行 " : "检索来源 ",
    eventLink(page.env, {event_id: e.id, event_key: e.event_key}),
    " · ", mono(e.order_id + " " + e.version + "/" + e.currency), " ", grade(e.verdict)));
  if (row && !row.event_key) episodeNodes.unshift(note("只读盲测未新增情景记录。"));
  area.append(layer("情景记忆", ...(episodeNodes.length ? episodeNodes : [note("此步骤无关联事件。")])));
  area.append(layer("语义观察", ...(observations.length ? observations.map((o) =>
    paragraph("观察 #" + o.id + " ← 事件 " + o.event_id + " · ", o.observation)) : [note("无关联观察记录。")])));
  const entries = skills.map((s) => paragraph("技能 #" + s.id + " ← 观察 #" + s.observation_id + " · ", mono(s.action)));
  const planning = !row;
  const revisions = db.revisions.filter((r) => planning || r.id === hit?.revision_id ||
    db.revision_evidence.some((v) => v.revision_id === r.id && eventKeys.has(v.event_key)));
  for (const revision of revisions) {
    const details = element("details", "", element("summary", "", "修订 #" + revision.id +
      " · 按" + axisName(revision.axis) + " · " + statusName(revision.status)));
    details.dataset.revisionId = revision.id;
    for (const rule of db.revision_rules.filter((r) => r.revision_id === revision.id)) {
      const skill = db.skills.find((s) => s.id === rule.skill_id);
      const observation = db.observations.find((o) => o.id === skill?.observation_id);
      const source = paragraph(mono(rule.value), " → 技能 #" + rule.skill_id);
      source.dataset.skillId = rule.skill_id;
      if (observation) source.append(" ← 观察 #" + observation.id + " ← ", eventLink(page.env, observation));
      details.append(source);
    }
    for (const ev of db.revision_evidence.filter((e) => e.revision_id === revision.id)) {
      details.append(paragraph(ev.role === "support" ? "支持 " : "反例 ", eventLink(page.env, ev)));
    }
    entries.push(details);
  }
  area.append(layer("技能与修订", ...(entries.length ? entries : [note("无关联技能或修订记录。")])));
  area.append(note("修订状态取自本次实验的冻结库。"));
}
function renderInvestigation() {
  $("environment-picker").replaceChildren(...environments.map((env) => {
    const pick = button("环境 " + env, () => {
      page.env = env;
      showStep(page.step);
      renderComparison();
    });
    pick.dataset.environment = env;
    pick.setAttribute("aria-pressed", String(env === page.env));
    return pick;
  }));
  $("timeline").replaceChildren(...steps.map((name, number) => {
    const item = button("", () => showStep(number));
    item.append(element("span", "step-dot"), name);
    if (page.step === number) item.setAttribute("aria-current", "step");
    return item;
  }));
  const row = selectedExecution();
  renderDetail(current(), row);
  renderMemory(current(), row);
}

function branchCard(which, result) {
  const row = result[which];
  const card = element("article", "branch");
  card.dataset.branch = which;
  card.append(element("h3", "", which === "enabled" ? "保留当前修订" : "禁用当前修订", grade(row.verdict)));
  const restored = result.previous_revision_id == null ? "Step 2 精确匹配" :
    "父修订 #" + result.previous_revision_id;
  if (which === "disabled") card.append(note("回滚恢复：" + restored));
  const policy = row.policy === "default" ? "未命中精确技能，使用默认策略 " + row.action :
    hitDescription(row.retrieved);
  const entries = [["本次决策依据", policy]];
  if (row.retrieved?.event_key) {
    entries.push(["记忆来源", eventLink(result.environment, row.retrieved,
      "技能 #" + row.retrieved.skill_id + " ← 观察 #" + row.retrieved.observation_id +
      " ← " + eventLabel(row.retrieved))]);
  }
  entries.push(["实际动作", field("action", row.action)], ["入库金额", field("actual", row.actual)],
    ["评分器期望", field("target", row.target)], ["只读会话", mono(row.session)]);
  card.append(record(entries));
  return card;
}
function renderComparison() {
  const env = page.env;
  $("compare-button").disabled = page.busy;
  $("compare-context").textContent = "环境 " + env + " · " + orderText(current().blind) +
    "。两份冻结库副本，各启动一次只读会话。";
  $("compare-status").textContent = page.comparing === env ? "正在启动只读会话并评分…" : "";
  const area = $("comparison-results");
  area.replaceChildren();
  const result = page.comparisons[env];
  if (!result) return;
  if (page.comparing !== env) {
    const {enabled, disabled} = result;
    $("compare-status").textContent = enabled.actual === disabled.actual && enabled.verdict === disabled.verdict
      ? "两支入库金额相同，均为 " + enabled.actual + "，评分均为 " + enabled.verdict + "。"
      : "禁用后入库金额由 " + enabled.actual + " 变为 " + disabled.actual +
        "，评分由 " + enabled.verdict + " 变为 " + disabled.verdict + "。";
  }
  const checks = element("details", "digest", element("summary", "",
    result.sha_before === result.sha_after ? "冻结库 SHA-256 · 前后一致" : "冻结库 SHA-256 · 校验失败"),
    record([["运行前", mono(result.sha_before)], ["运行后", mono(result.sha_after)]]));
  checks.id = "sha-check";
  area.append(element("div", "branch-grid", branchCard("enabled", result), branchCard("disabled", result)), checks);
}
async function api(path, method = "GET") {
  const response = await fetch(path, {method, cache: "no-store"});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "服务返回 " + response.status);
  return data;
}
function notice(message = "") {
  $("notice").textContent = message;
  $("notice").hidden = !message;
}
function setBusy(busy) {
  page.busy = busy;
  $("run-picker").disabled = busy || !$("run-picker").options.length;
  $("create-run").disabled = busy;
  $("compare-button").disabled = busy || !page.run;
}
function displayRun(run) {
  page.run = run;
  page.env = "A";
  page.step = 5;
  page.eventKey = null;
  page.comparisons = {};
  const url = new URL(location.href);
  url.searchParams.set("run", run.id);
  history.replaceState(null, "", url);
  $("run-picker").value = run.id;
  $("run-meta").textContent = "项目 " + run.project + "　/　" +
    new Date(run.created_at).toLocaleString("zh-CN", {hour12: false});
  $("workspace").hidden = false;
  renderOverview();
  renderInvestigation();
  renderComparison();
}
function runOption(id) {
  // Timestamp and short suffix keep experiments from the same day distinguishable.
  const stamp = id.match(/^(\d{4})(\d\d)(\d\d)T(\d\d)(\d\d)(\d\d)/);
  let label = id;
  if (stamp) {
    const [, year, month, day, hour, minute, second] = stamp;
    const time = new Date(year + "-" + month + "-" + day + "T" + hour + ":" + minute + ":" + second + "Z");
    label = time.toLocaleString("zh-CN", {hour12: false}) + " · " + id.slice(-5);
  }
  const option = element("option", "", label);
  option.value = id;
  return option;
}
async function openSelected(id) {
  return displayRun(await api("/api/runs/" + encodeURIComponent(id)));
}
async function changeRun(create) {
  setBusy(true);
  notice(create ? "正在两个环境中运行实验并冻结记忆库…" : "正在读取已保存的实验…");
  try {
    if (create) {
      const run = await api("/api/runs", "POST");
      $("run-picker").prepend(runOption(run.id));
      displayRun(run);
    } else {
      await openSelected($("run-picker").value);
    }
    notice();
  } catch (error) {
    if (page.run) $("run-picker").value = page.run.id;
    notice("打开实验失败：" + error.message);
  } finally { setBusy(false); }
}
async function compare() {
  if (page.busy) return;
  const env = page.env;
  page.comparing = env;
  setBusy(true);
  renderComparison();
  notice();
  try {
    page.comparisons[env] = await api("/api/runs/" + encodeURIComponent(page.run.id) + "/" + env + "/compare", "POST");
  } catch (error) {
    delete page.comparisons[env];
    notice("对照失败：" + error.message);
  } finally {
    page.comparing = null;
    setBusy(false);
    renderComparison();
  }
}
async function initialize() {
  setBusy(true);
  try {
    const {runs} = await api("/api/runs");
    $("run-picker").replaceChildren(...runs.map(runOption));
    const id = new URL(location.href).searchParams.get("run") || runs[0];
    if (!id) { notice("尚无已保存的实验。请点击「新建实验」。"); return; }
    await openSelected(id);
  } catch (error) {
    notice("读取回放失败：" + error.message);
  } finally { setBusy(false); }
}
$("run-picker").addEventListener("change", () => changeRun(false));
$("create-run").addEventListener("click", () => changeRun(true));
$("compare-button").addEventListener("click", compare);
initialize();
