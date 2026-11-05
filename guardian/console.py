"""Local monitoring console: snapshots, evidence details and timeline.

The public app never registers these routes; the console is for the local
operator on 127.0.0.1.  The AI can neither reach nor modify any of it.
"""
import hmac
import os
from datetime import datetime, timezone

from .agent import AgentCoordinator
from .storage import Storage


def _investigation_summary(row: dict | None) -> dict | None:
    if not row:
        return None
    return {key: row.get(key) for key in
            ("id", "event_id", "device_id", "kind", "status", "ai_status",
             "model", "created_at", "started_at", "completed_at", "error")}


def event_summary(storage: Storage, event: dict) -> dict:
    notifications = storage.notifications_for(event["id"])
    return {
        **event,
        "investigation": _investigation_summary(
            storage.investigation_for_event(event["id"])),
        "notification": notifications[-1] if notifications else None,
        "notification_count": len(notifications),
    }


def snapshot(storage: Storage, agent: AgentCoordinator, device_id: str = "*") -> dict:
    server_time = datetime.now(timezone.utc).isoformat()
    if device_id == "*":
        events = [event_summary(storage, event)
                  for event in storage.recent_events(limit=50)]
        return {
            "type": "console_snapshot",
            "server_time": server_time,
            "devices": [agent.status(item) for item in storage.all_device_ids()],
            "events": events,
            "investigations": [_investigation_summary(item)
                               for item in storage.recent_investigations(limit=20)],
            "notifications": storage.recent_notifications(limit=20),
        }
    status = agent.status(device_id)
    return {
        **status,
        "type": "live_status",
        "server_time": server_time,
        "events": [event_summary(storage, event)
                   for event in storage.events(device_id, limit=20)],
        "investigations": [_investigation_summary(item)
                           for item in storage.recent_investigations(
                               limit=10, device_id=device_id)],
        "notifications": storage.recent_notifications(limit=10, device_id=device_id),
    }


def timeline(storage: Storage, event: dict) -> list[dict]:
    items = [{
        "at": event["opened_at"], "type": "rule_event_opened",
        "actor": "rule_engine", "title": "规则引擎创建事件",
        "detail": event["kind"],
    }]
    investigation = storage.investigation_for_event(event["id"])
    if investigation:
        if investigation.get("created_at"):
            items.append({"at": investigation["created_at"], "type": "investigation_queued",
                          "actor": "agent", "title": "Agent 调查入队",
                          "detail": investigation.get("status")})
        if investigation.get("started_at"):
            items.append({"at": investigation["started_at"], "type": "investigation_started",
                          "actor": "agent", "title": "Agent 开始调查",
                          "detail": investigation.get("ai_status")})
        if investigation.get("completed_at"):
            items.append({"at": investigation["completed_at"], "type": "investigation_completed",
                          "actor": "agent", "title": "Agent 调查完成",
                          "detail": f"ai_status={investigation.get('ai_status')}"})
    for notification in storage.notifications_for(event["id"]):
        items.append({"at": notification["created_at"], "type": "notification_created",
                      "actor": "notification",
                      "title": "告警通知创建",
                      "detail": notification["status"]})
        if notification.get("updated_at") and notification["updated_at"] != notification["created_at"]:
            items.append({"at": notification["updated_at"], "type": "notification_updated",
                          "actor": "notification",
                          "title": "告警通知状态更新",
                          "detail": notification["status"]})
    if event.get("acknowledged_at"):
        items.append({"at": event["acknowledged_at"], "type": "human_acknowledged",
                      "actor": event.get("acknowledged_by") or "human",
                      "title": "人工确认",
                      "detail": "人工操作，AI 未参与"})
    if event.get("closed_at"):
        items.append({"at": event["closed_at"], "type": "rule_event_resolved",
                      "actor": "rule_engine", "title": "规则引擎解除事件",
                      "detail": "恢复由规则判断，不等于人工确认或身体恢复"})
    return sorted(items, key=lambda item: item["at"])


def event_detail(storage: Storage, agent: AgentCoordinator, event_id: str) -> dict | None:
    event = storage.get_event(event_id)
    if not event:
        return None
    return {
        "event": event,
        "device": agent.status(event["device_id"]),
        "investigation": storage.investigation_for_event(event_id),
        "notifications": storage.notifications_for(event_id),
        "timeline": timeline(storage, event),
    }


def acknowledge(storage: Storage, event_id: str, confirmed_by: str) -> dict | None:
    return storage.acknowledge(event_id, actor=confirmed_by)


def console_token_ok(supplied: str | None) -> bool:
    expected = os.getenv("GUARDIAN_CONSOLE_TOKEN", "").strip()
    if not expected:
        return True
    return bool(supplied) and hmac.compare_digest(supplied, expected)


CONSOLE_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Guardian Angel 监控台</title>
<style>
:root{
  --bg:#0b0f14; --panel:#121820; --panel-2:#0f151c; --line:#22303d;
  --ink:#e6edf3; --muted:#8b98a5; --faint:#5d6b78;
  --good:#3fb950; --warn:#d29922; --serious:#f0883e; --crit:#f85149;
  --info:#58a6ff; --neutral:#8b98a5;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Hiragino Sans GB",sans-serif}
header{display:flex;align-items:center;justify-content:space-between;gap:16px;
  padding:16px 24px;border-bottom:1px solid var(--line);position:sticky;top:0;
  background:rgba(11,15,20,.94);backdrop-filter:blur(8px);z-index:5}
h1{font-size:18px;margin:0;font-weight:650;letter-spacing:.01em}
.sub{color:var(--muted);font-size:12px;margin-top:2px}
.conn{display:inline-flex;align-items:center;gap:8px;font-size:12px;color:var(--muted)}
.dot{width:8px;height:8px;border-radius:50%;background:var(--neutral)}
.dot.on{background:var(--good)}
.dot.off{background:var(--crit)}
main{max-width:1440px;margin:0 auto;padding:20px 24px 60px}
.stats{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:12px;margin-bottom:16px}
.stat{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.stat .num{font-size:30px;line-height:1.1;font-variant-numeric:tabular-nums;font-weight:650}
.stat .label{color:var(--muted);font-size:12px;margin-top:6px}
.panel-head{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:10px}
.panel-head h2{margin:0}
.system-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px}
.model-chip{border:1px solid var(--line);border-radius:8px;padding:9px 11px;background:var(--panel-2)}
.model-chip .name{font-size:12px;color:var(--muted);letter-spacing:.04em}
.model-chip .state{font-size:14px;font-weight:650;margin-top:3px}
.model-chip.ok{border-color:rgba(63,185,80,.35)}
.model-chip.bad{border-color:rgba(248,81,73,.4)}
.device-card{width:100%;text-align:left;background:var(--panel-2);border:1px solid var(--line);
  border-radius:10px;padding:12px;color:var(--ink);font:inherit;margin-bottom:10px;cursor:pointer}
.device-card:hover{border-color:#34475a}
.device-card.selected{border-color:var(--info);box-shadow:0 0 0 1px rgba(88,166,255,.25) inset}
.device-card .device-top{display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap}
.device-card .device-id{font-weight:650;font-size:14px}
.metric-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;margin-top:10px}
.metric{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:8px 10px}
.metric .metric-label{font-size:10.5px;letter-spacing:.06em;color:var(--muted);text-transform:uppercase}
.metric .metric-value{font-size:20px;font-weight:650;font-variant-numeric:tabular-nums;margin-top:2px}
.metric .metric-note{font-size:10.5px;color:var(--faint);margin-top:1px}
.source-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:10px}
.source-row{display:flex;align-items:center;justify-content:space-between;gap:8px;
  border:1px solid var(--line);border-radius:7px;padding:6px 9px;font-size:11.5px;color:var(--muted)}
.source-row .dot{flex:none}
.trend-legend{display:flex;gap:14px;flex-wrap:wrap;font-size:11.5px;color:var(--muted);margin-bottom:8px}
.trend-legend .swatch{display:inline-block;width:14px;height:3px;border-radius:2px;vertical-align:middle;margin-right:5px}
.trend-wrap{background:var(--panel-2);border:1px solid var(--line);border-radius:8px;padding:8px}
.trend-svg{display:block;width:100%;height:210px}
.trend-empty{color:var(--faint);font-size:13px;padding:18px 4px}
.evidence-table td{font-size:11.5px}
.badge-mode{font-size:10.5px;letter-spacing:.04em}
@media (max-width:1100px){.metric-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}
.layout{display:grid;grid-template-columns:380px minmax(0,1fr);gap:16px;align-items:start}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin-bottom:16px}
.panel h2{font-size:12px;letter-spacing:.09em;text-transform:uppercase;color:var(--muted);
  margin:0 0 10px;font-weight:600}
.events{max-height:72vh;overflow:auto;display:flex;flex-direction:column;gap:8px}
.event{width:100%;text-align:left;background:var(--panel-2);border:1px solid var(--line);
  border-radius:8px;padding:10px 12px;color:var(--ink);cursor:pointer;font:inherit}
.event:hover{border-color:#34475a}
.event.selected{border-color:var(--info);box-shadow:0 0 0 1px rgba(88,166,255,.25) inset}
.event-top{display:flex;justify-content:space-between;align-items:center;gap:8px}
.event-kind{font-weight:600}
.event-meta{color:var(--muted);font-size:12px;margin-top:4px;display:flex;flex-wrap:wrap;gap:8px}
.pill{display:inline-flex;align-items:center;gap:6px;border:1px solid var(--line);
  border-radius:999px;padding:1px 8px;font-size:12px;color:var(--muted);white-space:nowrap}
.pill::before{content:"●";font-size:9px;line-height:1}
.pill.good{color:var(--good);border-color:rgba(63,185,80,.35)}
.pill.warn{color:var(--warn);border-color:rgba(210,153,34,.35)}
.pill.serious{color:var(--serious);border-color:rgba(240,136,62,.35)}
.pill.crit{color:var(--crit);border-color:rgba(248,81,73,.35)}
.pill.info{color:var(--info);border-color:rgba(88,166,255,.35)}
.detail-head{display:flex;justify-content:space-between;gap:12px;align-items:flex-start;
  border-bottom:1px solid var(--line);padding-bottom:12px;margin-bottom:12px}
.detail-head h3{margin:0 0 4px;font-size:16px}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12px;color:var(--muted)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:10px 18px}
.kv{display:flex;flex-direction:column;gap:2px}
.kv .k{color:var(--muted);font-size:11px;letter-spacing:.04em}
.kv .v{font-size:13px}
ul.plain{margin:0;padding-left:18px}
ul.plain li{margin:3px 0}
.tag{display:inline-block;font-size:11px;color:var(--muted);border:1px solid var(--line);
  border-radius:4px;padding:0 6px;margin-left:6px}
.note{color:var(--muted);font-size:12px}
.warnbox{border-left:3px solid var(--warn);background:rgba(210,153,34,.08);
  padding:8px 10px;border-radius:0 6px 6px 0;color:var(--muted);font-size:12px;margin:8px 0}
.critbox{border-left:3px solid var(--crit);background:rgba(248,81,73,.08);
  padding:8px 10px;border-radius:0 6px 6px 0;color:var(--muted);font-size:12px;margin:8px 0}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:600;font-size:11px;letter-spacing:.05em;text-transform:uppercase}
td.num{font-variant-numeric:tabular-nums}
pre{margin:0;white-space:pre-wrap;word-break:break-word;font-size:11.5px;color:var(--muted)}
.timeline{list-style:none;margin:0;padding:0}
.timeline li{position:relative;padding:0 0 12px 18px;border-left:1px solid var(--line);margin-left:4px}
.timeline li:last-child{padding-bottom:0}
.timeline li::before{content:"";position:absolute;left:-4px;top:5px;width:7px;height:7px;
  border-radius:50%;background:var(--faint)}
.timeline .t-top{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.timeline .t-title{font-weight:600;font-size:13px}
.timeline .t-detail{color:var(--muted);font-size:12px;margin-top:2px}
.ack{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:10px 0 4px}
input{background:var(--panel-2);border:1px solid var(--line);border-radius:6px;
  color:var(--ink);padding:7px 9px;font:inherit;font-size:13px;min-width:150px}
button.action{background:var(--info);color:#06121f;border:0;border-radius:6px;
  padding:8px 12px;font:inherit;font-weight:650;cursor:pointer}
button.action:hover{filter:brightness(1.08)}
button.action:disabled{opacity:.5;cursor:default}
.empty{color:var(--faint);font-size:13px;padding:12px 0}
.toast{position:fixed;left:50%;bottom:24px;transform:translateX(-50%);
  background:#18231d;border:1px solid rgba(63,185,80,.4);color:var(--good);
  padding:10px 14px;border-radius:8px;font-size:13px;z-index:20;display:none}
.toast.err{background:#241517;border-color:rgba(248,81,73,.45);color:var(--crit)}
@media (max-width:900px){
  .stats{grid-template-columns:repeat(2,minmax(0,1fr))}
  .layout{grid-template-columns:1fr}
  .events{max-height:40vh}
}
</style>
</head>
<body>
<header>
  <div>
    <h1>知微 SenseMind v0.7 监测控制台</h1>
    <div class="sub">实验性安全辅助原型 · 规则引擎负责告警，AI 只做只读调查 · 非医疗器械</div>
  </div>
  <div class="conn"><span id="conn-dot" class="dot"></span><span id="conn-text">连接中…</span></div>
</header>
<main>
  <div class="stats">
    <div class="stat"><div id="stat-devices" class="num">0</div><div class="label">设备总数</div></div>
    <div class="stat"><div id="stat-active" class="num">0</div><div class="label">活动事件</div></div>
    <div class="stat"><div id="stat-investigating" class="num">0</div><div class="label">Agent 调查中</div></div>
    <div class="stat"><div id="stat-pending" class="num">0</div><div class="label">待发送告警</div></div>
    <div class="stat"><div id="stat-unacked" class="num">0</div><div class="label">待人工确认</div></div>
  </div>
  <section class="panel" id="runtime-health">
    <div class="panel-head">
      <h2>系统健康与模型状态</h2>
      <span id="system-updated" class="mono">读取中…</span>
    </div>
    <div id="system-health"><div class="empty">正在读取 /health、/v1/runtime/status、/v1/ml/status…</div></div>
  </section>
  <div class="layout">
    <div>
      <section class="panel">
        <h2>设备状态</h2>
        <div id="devices"></div>
      </section>
      <section class="panel">
        <h2>事件</h2>
        <div id="events" class="events"></div>
      </section>
    </div>
    <div>
      <section class="panel">
        <div class="panel-head">
          <h2>实时趋势</h2>
          <span id="trend-mode" class="pill neutral badge-mode">未选择设备</span>
        </div>
        <div id="trend"><div class="trend-empty">选择左侧设备查看心率、血氧与运动趋势。</div></div>
      </section>
      <section class="panel">
        <h2>证据链与事件闭环</h2>
        <div id="detail"><div class="empty">选择左侧事件查看调查过程。</div></div>
      </section>
    </div>
  </div>
</main>
<div id="toast" class="toast"></div>
<script>
const STATUS_TEXT = {
  active:"活动", resolved:"已解除", pending:"待发送", sent:"已发送", failed:"失败",
  suppressed_duplicate:"已去重", queued:"排队中", running:"调查中", completed:"已完成",
  not_configured:"模型未配置", timeout:"模型超时", network_error:"网络错误",
  tool_call_rejected:"工具调用被拒", invalid_response:"模型返回无效", analysis_failed:"分析失败",
  applied:"已应用"
};
const STATUS_TONE = {
  active:"warn", pending:"warn", queued:"info", running:"info",
  resolved:"good", sent:"good", completed:"good",
  failed:"crit", timeout:"crit", network_error:"crit", tool_call_rejected:"crit",
  invalid_response:"crit", analysis_failed:"crit", suppressed_duplicate:"neutral",
  not_configured:"neutral"
};
const KIND_TEXT = {
  high_heart_rate:"心率偏高（实验规则）", low_spo2:"血氧偏低（实验规则）",
  concurrent_signals:"多信号并发（实验规则）", unusual_motion:"明显运动（实验规则）",
  data_quality:"数据质量不足（实验规则）"
};
const state = {snapshot:null, selected:null, detail:null, detailSig:null, ws:null,
  device:null, history:[], historyDevice:null, historyFetchedAt:0,
  evidence:[], investigationDetail:null, system:null};

const EVIDENCE_SOURCES = [
  "ppg", "heart_rate_estimate", "signal_quality", "activity", "imu_raw",
  "radar_status", "breathing_rate", "device_online", "historical_event", "device_status"
];
const EVIDENCE_LABEL = {
  ppg:"原始 PPG", heart_rate_estimate:"心率估计", signal_quality:"信号质量",
  activity:"活动", imu_raw:"IMU 原始", radar_status:"雷达状态",
  breathing_rate:"呼吸率", device_online:"设备在线", historical_event:"历史事件",
  device_status:"设备状态"
};

const $ = (selector, root=document) => root.querySelector(selector);
function node(tag, className, text){
  const element = document.createElement(tag);
  if(className) element.className = className;
  if(text !== undefined && text !== null) element.textContent = String(text);
  return element;
}
function pill(status){
  const element = node("span", "pill " + (STATUS_TONE[status] || "neutral"),
                       STATUS_TEXT[status] || status || "未知");
  return element;
}
function tonePill(label, tone){
  return node("span", "pill " + (tone || "neutral"), label);
}
function fmtTime(value){
  if(!value) return "—";
  const date = new Date(value);
  return isNaN(date.getTime()) ? String(value) : date.toLocaleString("zh-CN", {hour12:false});
}
function fmtValue(value){ return value === null || value === undefined ? "—" : value; }
function kv(key, value){
  const wrap = node("div", "kv");
  wrap.append(node("span", "k", key), node("span", "v", value));
  return wrap;
}
function list(items, emptyText){
  if(!items || !items.length) return node("div", "empty", emptyText || "无");
  const ul = node("ul", "plain");
  for(const item of items){
    const li = node("li");
    li.textContent = typeof item === "string" ? item : (item.text || JSON.stringify(item));
    if(item && item.evidence){
      const tag = node("span", "tag", "证据");
      li.append(tag);
    }
    ul.append(li);
  }
  return ul;
}
function panelSection(title, content, subtitle){
  const section = node("div");
  const heading = node("h2", null, title);
  section.append(heading);
  if(subtitle) section.append(node("div", "note", subtitle));
  section.append(content);
  return section;
}
function toast(message, isError){
  const element = $("#toast");
  element.textContent = message;
  element.className = "toast" + (isError ? " err" : "");
  element.style.display = "block";
  clearTimeout(element._timer);
  element._timer = setTimeout(() => { element.style.display = "none"; }, 3200);
}

function fmtNum(value, digits=0){
  if(value === null || value === undefined || Number.isNaN(Number(value))) return "—";
  const n = Number(value);
  return digits ? n.toFixed(digits) : String(Math.round(n));
}
function ageText(seconds){
  if(seconds === null || seconds === undefined) return "无数据";
  if(seconds < 60) return Math.round(seconds) + " 秒前";
  if(seconds < 3600) return Math.round(seconds / 60) + " 分钟前";
  return Math.round(seconds / 3600) + " 小时前";
}
function modeLabel(mode){
  return mode === "live" ? "Live Mode · 硬件来源"
    : mode === "replay" ? "Replay Mode · 历史/模拟" : "模式未知";
}
function modeTone(mode){ return mode === "live" ? "good" : mode === "replay" ? "info" : "neutral"; }
function sourceState(device, kind){
  const latest = device.latest || {};
  const source = latest.source || "unknown";
  if(kind === "nrf"){
    if(source === "esp32") return {tone:"neutral", text:"无 nRF 数据"};
    if(source === "simulator") return {tone:"neutral", text:"回放/模拟 · 无硬件连接证据"};
    if(source === "stm32" || source === "ring")
      return {tone:"warn", text:"有遥测（source=" + source + "），未确认真机", dot:"warn"};
    return {tone:"neutral", text:"无数据 / 未连接", dot:"off"};
  }
  if(source === "esp32") return {tone:"warn", text:"有遥测（source=esp32），未做硬件验收", dot:"warn"};
  return {tone:"neutral", text:"无数据 / 未连接", dot:"off"};
}
function motionState(device){
  const model = device.model_output && device.model_output.result;
  const activity = model && model.activity;
  if(activity && activity.available){
    return {value: activity.label || "—",
            note: "置信度 " + fmtNum((activity.confidence || 0) * 100, 1) + "%"};
  }
  const latest = device.latest;
  if(latest && [latest.acc_x, latest.acc_y, latest.acc_z].every(v => v !== null && v !== undefined)){
    const magnitude = Math.sqrt(latest.acc_x ** 2 + latest.acc_y ** 2 + latest.acc_z ** 2);
    return {value: magnitude.toFixed(2) + " g", note: "加速度幅值，不是活动分类"};
  }
  return {value:"—", note:"无模型或 IMU 数据"};
}
function renderStats(snapshot){
  const events = snapshot.events || [];
  const devices = snapshot.devices || [];
  const active = events.filter(event => event.status === "active");
  const investigating = (snapshot.investigations || [])
    .filter(item => item.status === "queued" || item.status === "running");
  const pending = (snapshot.notifications || []).filter(item => item.status === "pending");
  $("#stat-devices").textContent = devices.length;
  $("#stat-active").textContent = active.length;
  $("#stat-investigating").textContent = investigating.length;
  $("#stat-pending").textContent = pending.length;
  $("#stat-unacked").textContent = active.filter(event => !event.acknowledged).length;
}

function renderDevices(snapshot){
  const box = $("#devices");
  box.replaceChildren();
  const devices = snapshot.devices || [];
  if(!devices.length){ box.append(node("div", "empty", "还没有设备数据。")); return; }
  for(const device of devices){
    const card = node("button", "device-card" + (state.device === device.device_id ? " selected" : ""));
    card.type = "button";
    const top = node("div", "device-top");
    top.append(node("span", "device-id", device.device_id));
    top.append(tonePill(modeLabel(device.data_mode), modeTone(device.data_mode)));
    top.append(tonePill(device.stale ? "数据过期" : "数据新鲜",
                        device.stale ? "crit" : "good"));
    const latest = device.latest || {};
    const meta = node("div", "event-meta");
    if(device.latest){
      meta.append(node("span", null, "来源 " + fmtValue(latest.source) + " / " + fmtValue(latest.dataset)));
      meta.append(node("span", null, "最近数据 " + ageText(device.age_seconds)));
      meta.append(node("span", null, "质量 " + fmtValue(latest.signal_quality)));
      if(latest.device_status && latest.device_status.sampling_status){
        meta.append(node("span", null, "采样 " + latest.device_status.sampling_status));
      } else {
        meta.append(node("span", null, "无硬件连接证据"));
      }
    } else {
      meta.append(node("span", null, "没有遥测数据"));
    }
    const motion = motionState(device);
    const metrics = node("div", "metric-grid");
    const metric = (label, value, note) => {
      const item = node("div", "metric");
      item.append(node("div", "metric-label", label),
                  node("div", "metric-value", value),
                  node("div", "metric-note", note));
      return item;
    };
    metrics.append(
      metric("心率 bpm", fmtValue(latest.heart_rate),
             latest.reading_valid ? "读数有效" : "无有效读数"),
      metric("SpO₂ %", fmtValue(latest.spo2),
             latest.spo2 === null || latest.spo2 === undefined ? "未提供" : "原型估算"),
      metric("呼吸", "—", "未接入雷达原始数据"),
      metric("运动", motion.value, motion.note)
    );
    const sources = node("div", "source-grid");
    for(const [kind, label] of [["nrf", "nRF52840 / 穿戴"], ["esp32", "ESP32 / 雷达"]]){
      const status = sourceState(device, kind);
      const row = node("div", "source-row");
      const left = node("span", null);
      const dot = node("span", "dot " + (status.dot || ""));
      left.append(dot, document.createTextNode(" " + label));
      row.append(left, node("span", null, status.text));
      sources.append(row);
    }
    const note = node("div", "note",
      "H1 硬件 API 未合入 main；以上是遥测来源推断，不是 nRF/ESP32 真机连接证明。");
    card.append(top, meta, metrics, sources, note);
    card.addEventListener("click", () => selectDevice(device.device_id));
    box.append(card);
  }
}

function selectDevice(deviceId){
  state.device = deviceId;
  state.history = [];
  state.historyDevice = null;
  state.historyFetchedAt = 0;
  if(state.snapshot) renderDevices(state.snapshot);
  loadHistory(deviceId, true);
}

async function loadHistory(deviceId, force=false){
  const now = Date.now();
  if(!force && state.historyDevice === deviceId && now - state.historyFetchedAt < 5000) return;
  try {
    const response = await fetch("/v1/history?device_id=" + encodeURIComponent(deviceId) + "&limit=180");
    if(!response.ok) throw new Error("history failed");
    const rows = await response.json();
    if(state.device !== deviceId) return;
    state.history = Array.isArray(rows) ? rows : [];
    state.historyDevice = deviceId;
    state.historyFetchedAt = Date.now();
  } catch(error){
    if(state.device !== deviceId) return;
    state.history = [];
    state.historyDevice = deviceId;
  }
  renderTrend();
}

function svgElement(tag, attrs={}){
  const element = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for(const [key, value] of Object.entries(attrs)) element.setAttribute(key, value);
  return element;
}

function sparkline(title, samples, key, color, unit, options={}){
  const wrap = node("div");
  wrap.append(node("div", "mono", title + " · " + unit));
  const values = samples
    .map(sample => ({at: sample.timestamp, value: sample[key]}))
    .filter(item => typeof item.value === "number" && Number.isFinite(item.value));
  if(values.length < 2){
    wrap.append(node("div", "trend-empty", "样本不足，无法绘制趋势。"));
    return wrap;
  }
  const width = 620, height = 150;
  const pad = {left: 44, right: 14, top: 12, bottom: 20};
  const svg = svgElement("svg", {viewBox: "0 0 " + width + " " + height,
    class:"trend-svg", role:"img", "aria-label":title});
  let min = options.min === undefined ? Math.min(...values.map(v => v.value)) : options.min;
  let max = options.max === undefined ? Math.max(...values.map(v => v.value)) : options.max;
  if(max - min < 1e-6){ max += 1; min -= 1; }
  const span = max - min;
  const x = index => pad.left + (index / (values.length - 1)) * (width - pad.left - pad.right);
  const y = value => pad.top + (1 - (value - min) / span) * (height - pad.top - pad.bottom);
  for(let i = 0; i <= 3; i++){
    const gy = pad.top + (i / 3) * (height - pad.top - pad.bottom);
    svg.append(svgElement("line", {x1:pad.left, y1:gy, x2:width-pad.right, y2:gy,
      stroke:"#22303d", "stroke-width":"1"}));
    const label = svgElement("text", {x:6, y:gy + 4, fill:"#8b98a5", "font-size":"10"});
    label.textContent = (max - (i / 3) * span).toFixed(span < 10 ? 1 : 0);
    svg.append(label);
  }
  const points = values.map((item, index) => x(index) + "," + y(item.value)).join(" ");
  svg.append(svgElement("polyline", {points, fill:"none", stroke:color,
    "stroke-width":"2", "stroke-linejoin":"round", "stroke-linecap":"round"}));
  const last = values[values.length - 1];
  const lastDot = svgElement("circle", {cx:x(values.length - 1), cy:y(last.value),
    r:"3.5", fill:color});
  svg.append(lastDot);
  const footer = node("div", "mono",
    "样本 " + values.length + " · 最新 " + last.value.toFixed(1) + " " + unit +
    " · " + fmtTime(last.at));
  wrap.append(svg, footer);
  return wrap;
}

function renderTrend(){
  const box = $("#trend");
  const badge = $("#trend-mode");
  box.replaceChildren();
  if(!state.device){
    badge.className = "pill neutral badge-mode";
    badge.textContent = "未选择设备";
    box.append(node("div", "trend-empty", "选择左侧设备查看趋势。"));
    return;
  }
  const device = ((state.snapshot && state.snapshot.devices) || [])
    .find(item => item.device_id === state.device);
  badge.className = "pill " + modeTone(device && device.data_mode) + " badge-mode";
  badge.textContent = modeLabel(device && device.data_mode);
  if(!state.history.length){
    box.append(node("div", "trend-empty",
      "设备 " + state.device + " 暂无历史样本；趋势只在有遥测记录时绘制。"));
    return;
  }
  const legend = node("div", "trend-legend");
  for(const [label, color] of [["心率", "#f85149"], ["SpO₂", "#58a6ff"], ["运动幅值", "#3fb950"]]){
    const item = node("span");
    const swatch = node("span", "swatch");
    swatch.style.background = color;
    item.append(swatch, document.createTextNode(label));
    legend.append(item);
  }
  box.append(legend);
  const charts = node("div", "trend-wrap");
  charts.append(
    sparkline("心率", state.history, "heart_rate", "#f85149", "bpm", {min:0, max:200}),
    sparkline("血氧", state.history, "spo2", "#58a6ff", "%", {min:80, max:100})
  );
  const motionSamples = state.history.map(sample => {
    if([sample.acc_x, sample.acc_y, sample.acc_z].some(v => v === null || v === undefined)) return sample;
    return {...sample, acc_magnitude:
      Math.sqrt(sample.acc_x ** 2 + sample.acc_y ** 2 + sample.acc_z ** 2)};
  });
  charts.append(sparkline("运动幅值", motionSamples, "acc_magnitude", "#3fb950", "g"));
  box.append(charts);
}

function eventSignature(event){
  const investigation = event.investigation || {};
  return [event.status, event.acknowledged, event.notification_status,
          investigation.status, investigation.ai_status].join("|");
}

function renderEvents(snapshot){
  const box = $("#events");
  box.replaceChildren();
  const events = snapshot.events || [];
  if(!events.length){ box.append(node("div", "empty", "还没有规则事件。")); return; }
  for(const event of events){
    const button = node("button", "event" + (state.selected === event.id ? " selected" : ""));
    button.type = "button";
    const top = node("div", "event-top");
    top.append(node("span", "event-kind", KIND_TEXT[event.kind] || event.kind));
    top.append(pill(event.status));
    const meta = node("div", "event-meta");
    meta.append(node("span", null, fmtTime(event.opened_at)));
    meta.append(node("span", null, event.acknowledged ? "已人工确认" : "未确认"));
    meta.append(pill(event.notification_status || "pending"));
    if(event.investigation){
      meta.append(pill(event.investigation.status));
      if(event.investigation.ai_status && event.investigation.ai_status !== "pending"){
        meta.append(node("span", null, "AI " + (STATUS_TEXT[event.investigation.ai_status] || event.investigation.ai_status)));
      }
    } else {
      meta.append(node("span", null, "无调查记录"));
    }
    button.append(top, meta);
    button.addEventListener("click", () => {
      state.selected = event.id;
      state.detailSig = null;
      renderEvents(state.snapshot);
      loadDetail(event.id);
    });
    box.append(button);
  }
}

function renderDetail(){
  const box = $("#detail");
  box.replaceChildren();
  if(!state.detail){
    box.append(node("div", "empty", "选择左侧事件查看调查过程。"));
    return;
  }
  const detail = state.detail;
  const event = detail.event;
  const investigation = detail.investigation;
  const report = investigation && investigation.report;

  const head = node("div", "detail-head");
  const titleBox = node("div");
  titleBox.append(node("h3", null, KIND_TEXT[event.kind] || event.kind));
  titleBox.append(node("div", "mono", "event_id " + event.id));
  const badges = node("div");
  badges.append(pill(event.status), document.createTextNode(" "), pill(event.notification_status || "pending"));
  head.append(titleBox, badges);
  box.append(head);

  const overview = node("div", "grid");
  overview.append(
    kv("设备", event.device_id),
    kv("事件时间", fmtTime(event.opened_at)),
    kv("事件状态", event.status === "active" ? "活动（规则判断）" : "已解除（规则判断）"),
    kv("人工确认", event.acknowledged
        ? (event.acknowledged_by || "人工") + " · " + fmtTime(event.acknowledged_at)
        : "尚未确认"),
    kv("调查状态", investigation ? (STATUS_TEXT[investigation.status] || investigation.status) : "无"),
    kv("AI 状态", investigation ? (STATUS_TEXT[investigation.ai_status] || investigation.ai_status) : "无"),
    kv("模型", investigation && investigation.model ? investigation.model : "未配置"),
    kv("通知状态", STATUS_TEXT[event.notification_status] || event.notification_status || "—"),
    kv("报告生成时间", report && report.generated_at ? fmtTime(report.generated_at) : "—")
  );
  box.append(panelSection("事件概览", overview));

  const ack = node("div", "ack");
  const operator = node("input");
  operator.id = "operator";
  operator.placeholder = "确认人姓名";
  operator.value = sessionStorage.getItem("ga_operator") || "local_operator";
  const token = node("input");
  token.id = "console-token";
  token.type = "password";
  token.placeholder = "控制台令牌（如已配置）";
  token.value = sessionStorage.getItem("ga_token") || "";
  const ackButton = node("button", "action", event.acknowledged ? "已确认（查看记录）" : "人工确认事件");
  ackButton.type = "button";
  ackButton.disabled = Boolean(event.acknowledged);
  ackButton.addEventListener("click", () => acknowledge(event.id));
  ack.append(operator, token, ackButton);
  box.append(panelSection("人工确认", ack,
    "只有本机控制台可以确认；AI 工具集中没有确认或取消事件的写操作。"));

  const evidencePanel = renderEvidence();
  if(evidencePanel) box.append(evidencePanel);
  const investigationPanel = renderInvestigationDetail();
  if(investigationPanel) box.append(investigationPanel);

  if(!investigation){
    box.append(node("div", "empty", "Agent 尚未为这个事件创建调查记录。"));
  } else if(!report){
    if(investigation.status === "failed"){
      box.append(node("div", "critbox",
        "Agent 调查失败：" + (investigation.error || "未知错误") +
        "。基础告警仍然保留，可稍后重试调查。"));
    } else {
      box.append(node("div", "warnbox",
        "Agent 正在调查（" + (STATUS_TEXT[investigation.status] || investigation.status) + "）…"));
    }
  } else {
    if(report.ai_error){
      box.append(node("div", "warnbox",
        "模型分析失败：'" + report.ai_error + "'，以下为本地确定性摘要；基础告警不受影响。"));
    }
    const quality = report.data_quality || {};
    const qualityBox = node("div", "grid");
    const counts = quality.quality_counts || {};
    qualityBox.append(
      kv("窗口样本", fmtValue(quality.sample_count)),
      kv("good / poor / unknown / invalid",
         [counts.good||0, counts.poor||0, counts.unknown||0, counts.invalid||0].join(" / ")),
      kv("最新数据年龄", quality.age_seconds === null || quality.age_seconds === undefined
          ? "—" : Math.round(quality.age_seconds) + " 秒"),
      kv("数据过期 stale", quality.stale ? "是（不能视为正常）" : "否"),
      kv("时钟偏差 clock_skew", quality.clock_skew ? "是" : "否"),
      kv("来源", (report.sources || []).join(", ") || "—")
    );
    box.append(panelSection("数据质量（调查时快照）", qualityBox,
      "以下数据质量是报告生成时的快照；左侧设备状态是实时值。质量不足、设备离线或时间异常时，系统只记录不确定性，不会解释为安全。"));

    if(report.model_evidence){
      const model = report.model_evidence;
      if(model.available){
        const modelBox = node("div", "grid");
        modelBox.append(
          kv("活动（实验模型）", fmtValue(model.activity_label)),
          kv("活动置信度", fmtValue(model.activity_confidence)),
          kv("信号可靠性", fmtValue(model.signal_reliability)),
          kv("异常评分", fmtValue(model.anomaly_score)),
          kv("模型版本", fmtValue(model.model_version)),
          kv("数据集 / 参与者", (fmtValue(model.dataset)) + " / " + (fmtValue(model.participant_id))),
          kv("模型窗口", fmtTime(model.window_start) + " → " + fmtTime(model.window_end))
        );
        box.append(panelSection("模型辅助证据（实验性）", modelBox,
          "模型只提供辅助证据，不覆盖规则引擎告警；不能用于诊断或急救。"));
      } else {
        box.append(panelSection("模型辅助证据（实验性）",
          node("div", "empty", "当前设备没有可用的模型推理结果：" +
               (model.reason || "unknown"))));
      }
    }

    box.append(panelSection("可验证的传感器事实", list(report.facts, "没有可用事实。"),
      "以下内容由工具实际读取的数据确定，不是模型措辞。"));
    box.append(panelSection("规则引擎判断", list(report.rule_findings, "无"),
      "规则阈值是演示数字，不是医学判断。"));
    const analysisIsAi = report.analysis_source === "ai";
    box.append(panelSection(
      analysisIsAi ? "AI 推测" : "本地确定性分析（模型未参与）",
      list(report.ai_inferences, analysisIsAi ? "AI 未提供额外推测。" : "没有额外分析。"),
      analysisIsAi
        ? "AI 只基于上述工具结果解释，未人工验证，不能当作事实。"
        : "模型未完成分析，以下结论由本地规则/统计生成；仍不能当作事实或诊断。"));
    if(report.ai_narrative){
      const narrative = node("div", "note", report.ai_narrative);
      box.append(panelSection("AI 说明", narrative));
    }
    box.append(panelSection("尚未确定的信息", list(report.uncertainties, "无")));
    box.append(panelSection("建议采取的行动", list(report.recommended_actions, "无"),
      "保守建议；不构成诊断、用药或急救指导。"));

    if(report.key_history && report.key_history.length){
      const table = node("table");
      const headRow = node("tr");
      for(const label of ["时间", "来源", "心率", "血氧", "质量", "有效"]){
        headRow.append(node("th", null, label));
      }
      table.append(headRow);
      for(const sample of report.key_history){
        const row = node("tr");
        row.append(node("td", null, fmtTime(sample.timestamp)));
        row.append(node("td", null, sample.source || "—"));
        row.append(node("td", "num", fmtValue(sample.heart_rate)));
        row.append(node("td", "num", fmtValue(sample.spo2)));
        row.append(node("td", null, sample.signal_quality || "—"));
        row.append(node("td", null, sample.reading_valid ? "是" : "否"));
        table.append(row);
      }
      box.append(panelSection("关键历史数据", table));
    }

    if(report.tool_calls && report.tool_calls.length){
      const table = node("table");
      const headRow = node("tr");
      for(const label of ["角色", "工具", "参数", "耗时", "结果摘要"]){
        headRow.append(node("th", null, label));
      }
      table.append(headRow);
      for(const call of report.tool_calls){
        const row = node("tr");
        row.append(node("td", null, call.actor === "agent" ? "AI 选择" : "系统补充"));
        row.append(node("td", "mono", call.tool));
        row.append(node("td", null, JSON.stringify(call.arguments || {})));
        row.append(node("td", "num", (call.duration_ms || 0) + " ms"));
        const cell = node("td");
        const pre = node("pre", null, JSON.stringify(call.result, null, 2));
        cell.append(pre);
        row.append(cell);
        table.append(row);
      }
      box.append(panelSection("Agent 工具调用记录", table,
        "这些是实际执行过的只读查询；AI 不能修改设备、事件或告警。"));
    }

    if(detail.notifications && detail.notifications.length){
      const table = node("table");
      const headRow = node("tr");
      for(const label of ["创建时间", "渠道", "状态", "尝试", "错误"]){
        headRow.append(node("th", null, label));
      }
      table.append(headRow);
      for(const notification of detail.notifications){
        const row = node("tr");
        row.append(node("td", null, fmtTime(notification.created_at)));
        row.append(node("td", null, notification.channel));
        const statusCell = node("td");
        statusCell.append(pill(notification.status));
        row.append(statusCell);
        row.append(node("td", "num", notification.attempts));
        row.append(node("td", null, notification.error || "—"));
        table.append(row);
      }
      box.append(panelSection("告警通知状态", table,
        "待发送会在控制台连接后变为已发送；超时未送达会记录为失败。"));
    }

    if(report.limitations && report.limitations.length){
      box.append(panelSection("系统限制", list(report.limitations)));
    }
  }

  if(detail.timeline && detail.timeline.length){
    const timeline = node("ul", "timeline");
    for(const item of detail.timeline){
      const li = node("li");
      const top = node("div", "t-top");
      top.append(node("span", "t-title", item.title));
      top.append(node("span", "mono", fmtTime(item.at)));
      top.append(node("span", "tag", item.actor));
      li.append(top);
      if(item.detail) li.append(node("div", "t-detail", item.detail));
      timeline.append(li);
    }
    box.append(panelSection("事件时间线", timeline,
      "规则触发/恢复、Agent 调查、通知和人工确认分别记录，不互相替代。"));
  }
}

async function loadDetail(eventId){
  try {
    const response = await fetch("/v1/console/events/" + encodeURIComponent(eventId));
    if(!response.ok){
      state.detail = null; state.evidence = []; state.investigationDetail = null;
      renderDetail(); return;
    }
    state.detail = await response.json();
    const investigation = state.detail.investigation;
    const evidencePromise = fetch("/v1/events/" + encodeURIComponent(eventId) + "/evidence")
      .catch(() => null);
    const investigationPromise = investigation && investigation.id
      ? fetch("/v1/investigations/" + encodeURIComponent(investigation.id)).catch(() => null)
      : Promise.resolve(null);
    const [evidenceResponse, investigationResponse] = await Promise.all(
      [evidencePromise, investigationPromise]);
    state.evidence = [];
    if(evidenceResponse && evidenceResponse.ok){
      const payload = await evidenceResponse.json().catch(() => null);
      state.evidence = payload && Array.isArray(payload.evidence) ? payload.evidence : [];
    }
    state.investigationDetail = null;
    if(investigationResponse && investigationResponse.ok){
      state.investigationDetail = await investigationResponse.json().catch(() => null);
    }
    const summary = (state.snapshot && state.snapshot.events || [])
      .find(event => event.id === eventId);
    state.detailSig = summary ? eventSignature(summary) : null;
    renderDetail();
  } catch(error){
    state.detail = null; state.evidence = []; state.investigationDetail = null;
    renderDetail();
  }
}

function renderEvidence(){
  const box = node("div");
  const evidence = state.evidence || [];
  const expected = new Set(evidence.map(item => item.source_type));
  const missing = EVIDENCE_SOURCES.filter(source => !expected.has(source));
  const modes = [...new Set(evidence.map(item => item.data_mode))];
  const syntheticCount = evidence.filter(item => item.synthetic).length;
  const summary = node("div", "event-meta");
  summary.append(node("span", null, "证据 " + evidence.length + " 条"));
  for(const mode of modes){
    summary.append(tonePill(mode === "live" ? "Live" : mode === "replay" ? "Replay" : "模式未知",
                            mode === "live" ? "good" : mode === "replay" ? "info" : "neutral"));
  }
  if(syntheticCount) summary.append(tonePill("synthetic " + syntheticCount, "warn"));
  box.append(summary);
  if(evidence.length){
    const table = node("table", "evidence-table");
    const headRow = node("tr");
    for(const label of ["来源", "值", "单位", "质量", "模式", "时间", "模型 / 时钟"]){
      headRow.append(node("th", null, label));
    }
    table.append(headRow);
    const ordered = [...evidence].sort((a, b) =>
      String(a.source_type).localeCompare(String(b.source_type)) ||
      String(a.timestamp || "").localeCompare(String(b.timestamp || "")));
    for(const item of ordered){
      const row = node("tr");
      row.append(node("td", null, EVIDENCE_LABEL[item.source_type] || item.source_type));
      const value = item.value === null || item.value === undefined ? "—"
        : typeof item.value === "object" ? JSON.stringify(item.value) : String(item.value);
      row.append(node("td", null, value.length > 80 ? value.slice(0, 80) + "…" : value));
      row.append(node("td", null, item.unit || "—"));
      row.append(node("td", null, item.quality_status || "—"));
      row.append(node("td", null, item.data_mode || "—"));
      row.append(node("td", null, fmtTime(item.timestamp)));
      row.append(node("td", null,
        (item.model_version || "—") + " / " +
        (item.clock_uncertainty_ms === null || item.clock_uncertainty_ms === undefined
          ? "时钟未知" : item.clock_uncertainty_ms + " ms")));
      table.append(row);
    }
    box.append(table);
  } else {
    box.append(node("div", "empty", "该事件暂无证据记录。"));
  }
  if(missing.length){
    box.append(node("div", "note", "未提供的来源：" +
      missing.map(source => EVIDENCE_LABEL[source] || source).join("、") +
      "。缺失不会被解释为正常。"));
  }
  return panelSection("Evidence Engine 证据详情", box,
    "证据由服务端调查链生成；客户端只读取，不能伪造或修改调查结论。");
}

function renderInvestigationDetail(){
  const detail = state.investigationDetail;
  if(!detail) return null;
  const box = node("div");
  const transitions = detail.transitions || [];
  const overview = node("div", "grid");
  overview.append(
    kv("调查状态", STATUS_TEXT[detail.status] || detail.status || "—"),
    kv("AI 状态", STATUS_TEXT[detail.ai_status] || detail.ai_status || "—"),
    kv("证据数量", fmtValue(detail.evidence_count)),
    kv("尝试 / 上限", fmtValue(detail.attempts) + " / " + fmtValue(detail.max_attempts)),
    kv("重复事件", detail.duplicate_of_event_id || "无"),
    kv("最近错误", detail.last_error_code || "无"),
    kv("Token 用量", detail.usage && detail.usage.total_tokens !== undefined
      ? fmtValue(detail.usage.total_tokens) : "—"),
    kv("截止时间", fmtTime(detail.deadline_at))
  );
  box.append(overview);
  if(transitions.length){
    const table = node("table");
    const headRow = node("tr");
    for(const label of ["时间", "从", "到", "原因"]){
      headRow.append(node("th", null, label));
    }
    table.append(headRow);
    for(const item of transitions){
      const row = node("tr");
      row.append(node("td", null, fmtTime(item.created_at)));
      row.append(node("td", null, item.from_status || "—"));
      row.append(node("td", null, item.to_status || "—"));
      row.append(node("td", null, item.reason || "—"));
      table.append(row);
    }
    box.append(table);
  }
  return panelSection("Agent 2.0 调查状态与状态转换", box,
    "状态转换由服务端记录；客户端不能自行标记调查完成。");
}

async function acknowledge(eventId){
  const confirmedBy = $("#operator").value.trim() || "local_operator";
  const token = $("#console-token").value.trim();
  sessionStorage.setItem("ga_operator", confirmedBy);
  sessionStorage.setItem("ga_token", token);
  const headers = {"Content-Type": "application/json"};
  if(token) headers["X-Console-Token"] = token;
  try {
    const response = await fetch("/v1/events/" + encodeURIComponent(eventId) + "/ack", {
      method: "POST", headers, body: JSON.stringify({confirmed_by: confirmedBy})
    });
    const body = await response.json().catch(() => ({}));
    if(!response.ok){
      toast(body.message || body.detail || ("确认失败 " + response.status), true);
      return;
    }
    toast("已由 " + body.acknowledged_by + " 完成人工确认");
    await loadDetail(eventId);
  } catch(error){
    toast("确认请求失败，请检查本地服务。", true);
  }
}

function renderAll(){
  if(!state.snapshot) return;
  if(!state.selected){
    const events = state.snapshot.events || [];
    if(events.length){ state.selected = events[0].id; state.detailSig = null; }
  }
  const devices = state.snapshot.devices || [];
  if(!state.device && devices.length) state.device = devices[0].device_id;
  if(state.device && !devices.some(device => device.device_id === state.device)){
    state.device = devices.length ? devices[0].device_id : null;
    state.history = []; state.historyDevice = null;
  }
  renderStats(state.snapshot);
  renderDevices(state.snapshot);
  renderEvents(state.snapshot);
  if(state.device) loadHistory(state.device);
  renderTrend();
  if(state.selected){
    const summary = (state.snapshot.events || []).find(event => event.id === state.selected);
    if(summary && (!state.detail || eventSignature(summary) !== state.detailSig)){
      loadDetail(state.selected);
    }
  }
}

function setConnection(connected){
  const dot = $("#conn-dot");
  dot.className = "dot " + (connected ? "on" : "off");
  $("#conn-text").textContent = connected ? "控制台连接正常" : "连接断开，正在重连…";
}

function connect(){
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  const ws = new WebSocket(protocol + "//" + location.host + "/ws/live?device_id=*");
  state.ws = ws;
  ws.addEventListener("open", () => setConnection(true));
  ws.addEventListener("message", event => {
    try { state.snapshot = JSON.parse(event.data); renderAll(); }
    catch(error){ /* ignore malformed frame */ }
  });
  ws.addEventListener("close", () => {
    setConnection(false);
    setTimeout(connect, 2000);
  });
  ws.addEventListener("error", () => ws.close());
}

function renderSystem(){
  const box = $("#system-health");
  if(!box) return;
  box.replaceChildren();
  const system = state.system;
  if(!system){
    box.append(node("div", "empty", "运行状态查询失败：检查本地服务与网络。"));
    return;
  }
  const models = (system.runtime && system.runtime.models && system.runtime.models.models) || {};
  const grid = node("div", "system-grid");
  for(const [name, model] of Object.entries(models)){
    const chip = node("div", "model-chip " + (model.available ? "ok" : "bad"));
    chip.append(node("div", "name", "模型 · " + name),
                node("div", "state", model.available ? "可用" : "不可用"),
                node("div", "note", model.version || model.error || "—"));
    grid.append(chip);
  }
  const health = system.health || {};
  const runtime = system.runtime || {};
  const assistant = runtime.assistant || {};
  const hardwareChip = node("div", "model-chip " + (runtime.hardware_tested ? "ok" : "bad"));
  hardwareChip.append(
    node("div", "name", "硬件验收"),
    node("div", "state", runtime.hardware_tested ? "有真机证据" : "无硬件连接证据"),
    node("div", "note", "H1 未接入 main；nRF52840 / ESP32 实机均未验收")
  );
  grid.append(hardwareChip);
  const assistantChip = node("div", "model-chip " + (assistant.enabled ? "ok" : "bad"));
  assistantChip.append(
    node("div", "name", "DeepSeek Agent"),
    node("div", "state", assistant.enabled ? "已配置" : "未配置 / 本地降级"),
    node("div", "note", assistant.enabled ? (assistant.model || "模型名未知") + " · 连通性以调用结果为准" : "不会发起外部模型调用")
  );
  grid.append(assistantChip);
  box.append(grid);
  const details = node("div", "grid");
  details.append(
    kv("服务版本", health.version || "—"),
    kv("APK 契约", health.apk_contract || "—"),
    kv("真实设备规则", health.real_device_rules_enabled ? "已开启" : "关闭（默认）"),
    kv("调查异步 / 并发", health.investigations
      ? "异步；max_tokens " + fmtValue(health.investigations.max_llm_tokens) : "—"),
    kv("模式", (runtime.modes || []).join(" / ") || "unknown"),
    kv("说明", runtime.note || "—")
  );
  box.append(details);
  const updated = $("#system-updated");
  if(updated) updated.textContent = "更新于 " + new Date().toLocaleTimeString("zh-CN", {hour12:false});
}

async function loadSystem(){
  try {
    const [healthResponse, runtimeResponse, mlResponse] = await Promise.all([
      fetch("/health").catch(() => null),
      fetch("/v1/runtime/status").catch(() => null),
      fetch("/v1/ml/status").catch(() => null)
    ]);
    state.system = {
      health: healthResponse && healthResponse.ok ? await healthResponse.json() : null,
      runtime: runtimeResponse && runtimeResponse.ok ? await runtimeResponse.json() : null,
      ml: mlResponse && mlResponse.ok ? await mlResponse.json() : null
    };
    const runtime = state.system.runtime;
    if(runtime && runtime.models === undefined && state.system.ml){
      runtime.models = state.system.ml;
    }
  } catch(error){
    state.system = null;
  }
  renderSystem();
}
async function bootstrap(){
  loadSystem(); setInterval(loadSystem, 5000);
  try {
    const response = await fetch("/v1/console/snapshot?device_id=*");
    if(response.ok){ state.snapshot = await response.json(); renderAll(); }
  } catch(error){ /* the WebSocket will retry */ }
  connect();
}

bootstrap();
</script>
</body>
</html>
"""
