/**
 * 海大餐廳候位系統 後端（Google Apps Script）
 * ------------------------------------------------------------
 * Queue 自己寫：候位資料是一個 JSON 陣列，存在 Apps Script 內建的
 * PropertiesService（不用試算表、不用資料庫）。通知信用 MailApp 從自己的 Gmail 寄出。
 *
 * 部署方式（詳見作業說明）：
 *   1. 到 https://script.google.com → 新專案，把這個檔案全部貼到 Code.gs，改掉 STAFF_KEY
 *   2. 部署 → 新增部署作業 → 類型「網頁應用程式」
 *      執行身分：我　／　誰可以存取：所有人
 *   3. 複製「網頁應用程式網址」，貼到前端 app.js 的 API_URL
 */
const STAFF_KEY  = "staff";        // 員工密碼，請改成自己的
const RESTAURANT = "海大餐廳";
const ACTIVE     = ["waiting", "notified"];
const MAX_QUEUE  = 50;             // 屬性儲存區一個值最多 9KB，約可放 50 組

/* ============================================================
 * 1. 自己寫的 Queue（先進先出）
 *    內容：[{id, name, gender, email, party_size, status, created_at, notified_at}, ...]
 *    依加入順序排列，越前面越早候位。
 * ============================================================ */
const store = PropertiesService.getScriptProperties();

const Queue = {
  load: function () { return JSON.parse(store.getProperty("queue") || "[]"); },
  save: function (q) { store.setProperty("queue", JSON.stringify(q)); },

  /** 加入候位：產生號碼，放到隊尾 */
  enqueue: function (item) {
    const q = this.load();
    if (q.length >= MAX_QUEUE) throw new Error("候位已滿，請稍後再試");
    const id = Number(store.getProperty("next_id") || "1");
    store.setProperty("next_id", String(id + 1));
    item.id = id;
    q.push(item);
    this.save(q);
    return item;
  },

  /** 找某號碼 */
  find: function (id) { return this.load().find(function (t) { return String(t.id) === String(id); }) || null; },

  /** 前面還有幾組 = 排在自己前面的筆數 */
  positionOf: function (id) {
    const q = this.load();
    for (let i = 0; i < q.length; i++) if (String(q[i].id) === String(id)) return i;
    return -1;
  },

  /** 更新某號碼的欄位 */
  update: function (id, fields) {
    const q = this.load();
    const t = q.find(function (t) { return String(t.id) === String(id); });
    if (!t) return null;
    Object.assign(t, fields);
    this.save(q);
    return t;
  },

  /** 移出隊伍（入座或取消），並記到當日統計 */
  remove: function (id, reason) {
    const q = this.load();
    const idx = q.findIndex(function (t) { return String(t.id) === String(id); });
    if (idx < 0) return null;
    const t = q.splice(idx, 1)[0];
    this.save(q);
    const key = "stats:" + today_();
    const s = JSON.parse(store.getProperty(key) || '{"seated":0,"cancelled":0}');
    s[reason] = (s[reason] || 0) + 1;
    store.setProperty(key, JSON.stringify(s));
    t.status = reason; t.closed_at = now_();
    return t;
  },

  stats: function () { return JSON.parse(store.getProperty("stats:" + today_()) || '{"seated":0,"cancelled":0}'); }
};

/* 事件紀錄（最近 40 筆），給報告當「實際結果」用：?action=log&key=員工密碼 */
function log_(event, detail) {
  const L = JSON.parse(store.getProperty("log") || "[]");
  L.push(now_() + " | " + event + " | " + detail);
  while (L.length > 40) L.shift();
  store.setProperty("log", JSON.stringify(L));
}

/* ============================================================
 * 2. HTTP 進入點與動作分派
 * ============================================================ */
function doGet(e) {
  const p = (e && e.parameter) || {};
  return out_(handle_(p.action || "count", p));
}

function doPost(e) {
  let p = {};
  try { p = JSON.parse(e.postData.contents); } catch (err) { p = (e && e.parameter) || {}; }
  return out_(handle_(p.action, p));
}

function out_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}

function handle_(action, p) {
  const lock = LockService.getScriptLock();
  lock.waitLock(10000);                       // 多人同時取號時避免號碼重複
  try {
    switch (action) {
      case "count":  return { waiting: Queue.load().length };
      case "status": return status_(p);
      case "join":   return join_(p);
      case "leave":  return leave_(p);
      case "queue":  return staff_(p, queue_);
      case "notify": return staff_(p, notify_);
      case "seat":   return staff_(p, function (t) { Queue.remove(t.id, "seated");    log_("入座", t.id + " 號"); return { ok: true }; });
      case "cancel": return staff_(p, function (t) { Queue.remove(t.id, "cancelled"); log_("員工取消", t.id + " 號"); return { ok: true }; });
      case "log":    return staff_(p, function () { return { log: JSON.parse(store.getProperty("log") || "[]") }; });
      case "reset":  return staff_(p, function () { store.deleteAllProperties(); return { ok: true }; });
      default:       return { error: "unknown action " + action };
    }
  } catch (err) {
    return { error: String(err.message || err) };
  } finally {
    lock.releaseLock();
  }
}

/* ============================================================
 * 3. 顧客端
 * ============================================================ */
function status_(p) {
  const t = Queue.find(p.id);
  if (!t) return { error: "找不到這個候位號碼（可能已入座或取消）" };
  return { id: t.id, name: t.name, status: t.status, party_size: t.party_size, created_at: t.created_at,
           ahead: Queue.positionOf(t.id), waiting: Queue.load().length };
}

function join_(p) {
  if (!p.name || !p.email || String(p.email).indexOf("@") < 0) return { error: "請填寫姓名與正確的 E-mail" };
  const t = Queue.enqueue({
    name: String(p.name).trim(), gender: p.gender || "其他", email: String(p.email).trim(),
    party_size: Number(p.party_size) || 1, status: "waiting", created_at: now_(), notified_at: ""
  });
  log_("候位", t.id + " 號 " + t.name + "（" + t.gender + "，" + t.party_size + " 位）");
  return { id: t.id, ahead: Queue.positionOf(t.id) };
}

function leave_(p) {
  const t = Queue.remove(p.id, "cancelled");
  if (t) log_("顧客取消", t.id + " 號");
  return { ok: true };
}

/* ============================================================
 * 4. 員工端
 * ============================================================ */
function staff_(p, fn) {
  if (p.key !== STAFF_KEY) return { error: "unauthorized" };
  if (fn.length === 0) return fn();           // queue / log / reset 不需要 id
  const t = Queue.find(p.id);
  if (!t) return { error: "找不到這個候位號碼" };
  return fn(t);
}

function queue_() {
  const s = Queue.stats();
  return { queue: Queue.load(), seated: s.seated, cancelled: s.cancelled };
}

function notify_(t) {
  const honor = { "男": "先生", "女": "小姐" }[t.gender] || "";
  const body =
    t.name + " " + honor + " 您好：\n\n" +
    "您在 " + RESTAURANT + " 的候位號碼 " + t.id + " 號（" + t.party_size + " 位）已經輪到，\n" +
    "請於 5 分鐘內至櫃台報到，逾時將視同放棄。\n\n" +
    "候位時間：" + t.created_at + "\n通知時間：" + now_() + "\n\n" + RESTAURANT + " 敬上";
  MailApp.sendEmail({
    to: t.email,
    subject: "【" + RESTAURANT + "】候位通知：" + t.id + " 號請準備入座",
    body: body,
    name: RESTAURANT
  });
  Queue.update(t.id, { status: "notified", notified_at: now_() });
  log_("通知", t.id + " 號 " + t.name + " → " + t.email);
  return { ok: true, detail: "通知信已寄到 " + t.email + "（今日剩餘寄信額度 " + MailApp.getRemainingDailyQuota() + " 封）" };
}

/* ---------- 小工具 ---------- */
function now_()   { return Utilities.formatDate(new Date(), Session.getScriptTimeZone(), "yyyy-MM-dd HH:mm:ss"); }
function today_() { return now_().slice(0, 10); }
