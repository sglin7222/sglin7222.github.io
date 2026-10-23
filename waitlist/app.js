/* =====================================================================
 * 海大餐廳候位系統 前端共用程式（顧客端 index.html 與員工端 staff.html 共用）
 *
 * 架構：GitHub Pages（這些靜態網頁） ──fetch──► Google Apps Script 網頁應用程式（Code.gs）
 *                                               ├─ 自己寫的 Queue（JSON 陣列存在 PropertiesService，不用試算表、不用資料庫）
 *                                               └─ MailApp = 用你的 Gmail 寄候位通知
 *
 * 部署好 Apps Script（見作業說明）之後，把網址貼到下面的 API_URL。
 * API_URL 留空時為「示範模式」：資料只存在這台瀏覽器的 localStorage，
 * 可以先測試畫面與流程，但不會跨裝置、也不會真的寄信。
 * ===================================================================== */
const API_URL = "";          // 例如 "https://script.google.com/macros/s/AKfycb.../exec"
const RESTAURANT = "海大餐廳";
const DEMO_STAFF_KEY = "staff";   // 示範模式的員工密碼（正式模式的密碼寫在 Code.gs 的 STAFF_KEY）

const ACTIVE = ["waiting", "notified"];
const STATUS_TEXT = { waiting: "等候中", notified: "已通知，請至櫃台報到", seated: "已入座", cancelled: "已取消" };

/* ---------- 示範模式：前端自己用 localStorage 跑一個一樣的 Queue（和 Code.gs 的邏輯相同） ---------- */
const Local = {
  load() { try { return JSON.parse(localStorage.getItem("waitlist_demo") || "[]"); } catch (e) { return []; } },
  save(rows) { try { localStorage.setItem("waitlist_demo", JSON.stringify(rows)); } catch (e) {} },
  now() {
    const d = new Date(), z = n => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${z(d.getMonth() + 1)}-${z(d.getDate())} ${z(d.getHours())}:${z(d.getMinutes())}:${z(d.getSeconds())}`;
  },
  handle(action, p) {
    const rows = this.load();
    const active = rows.filter(r => ACTIVE.includes(r.status));
    const ahead = id => active.filter(r => Number(r.id) < Number(id)).length;
    const find = () => rows.find(r => String(r.id) === String(p.id));
    const staff = () => p.key === DEMO_STAFF_KEY;
    switch (action) {
      case "count": return { waiting: active.length };
      case "status": { const r = find(); if (!r) return { error: "找不到這個候位號碼" }; return { ...r, ahead: ahead(r.id), waiting: active.length }; }
      case "join": {
        if (!p.name || !p.email || String(p.email).indexOf("@") < 0) return { error: "請填寫姓名與正確的 E-mail" };
        const id = rows.length ? Math.max(...rows.map(r => Number(r.id))) + 1 : 1;
        rows.push({ id, name: p.name, gender: p.gender || "其他", email: p.email, party_size: Number(p.party_size) || 1,
                    status: "waiting", created_at: this.now(), notified_at: "", closed_at: "" });
        this.save(rows);
        return { id, ahead: ahead(id) };
      }
      case "leave": { const r = find(); if (r && ACTIVE.includes(r.status)) { r.status = "cancelled"; r.closed_at = this.now(); this.save(rows); } return { ok: true }; }
      case "queue": {
        if (!staff()) return { error: "unauthorized" };
        const today = this.now().slice(0, 10);
        return { queue: active, seated: rows.filter(r => r.status === "seated" && String(r.closed_at).startsWith(today)).length,
                 cancelled: rows.filter(r => r.status === "cancelled" && String(r.closed_at).startsWith(today)).length };
      }
      case "notify": {
        if (!staff()) return { error: "unauthorized" };
        const r = find(); if (!r) return { error: "找不到這個候位號碼" };
        r.status = "notified"; r.notified_at = this.now(); this.save(rows);
        return { ok: true, detail: "示範模式：狀態已改為已通知，但沒有真的寄信（設定 API_URL 後才會透過 Gmail 寄出）" };
      }
      case "seat": case "cancel": {
        if (!staff()) return { error: "unauthorized" };
        const r = find(); if (!r) return { error: "找不到這個候位號碼" };
        r.status = action === "seat" ? "seated" : "cancelled"; r.closed_at = this.now(); this.save(rows);
        return { ok: true };
      }
    }
    return { error: "unknown action " + action };
  }
};

/* ---------- 呼叫後端 ---------- */
const POST_ACTIONS = ["join", "leave", "notify", "seat", "cancel"];

async function api(action, params = {}) {
  if (!API_URL) return Local.handle(action, params);
  let res;
  if (POST_ACTIONS.includes(action)) {
    // 不設定 Content-Type，瀏覽器會用 text/plain，才不會觸發 Apps Script 不支援的 CORS preflight
    res = await fetch(API_URL, { method: "POST", body: JSON.stringify({ action, ...params }) });
  } else {
    res = await fetch(API_URL + "?" + new URLSearchParams({ action, ...params }));
  }
  if (!res.ok) throw new Error("HTTP " + res.status);
  return res.json();
}

function isDemo() { return !API_URL; }

function showDemoBanner() {
  if (!isDemo()) return;
  const el = document.getElementById("demo-banner");
  if (el) el.hidden = false;
}

function esc(s) { return String(s == null ? "" : s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }
