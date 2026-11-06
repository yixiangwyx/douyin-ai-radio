/* app.js — 登录页 / 控制台 / 管理后台共用的最小工具（无框架） */
const $ = (id) => document.getElementById(id);

function esc(x) {
  return String(x == null ? '' : x).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}

async function api(path, body) {
  const opt = body ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) } : {};
  return fetch(path, opt);
}

async function postJSON(path, body) {
  const r = await api(path, body);
  let d = {};
  try { d = await r.json(); } catch (e) {}
  if (!r.ok) {
    if (r.status === 401 || (d && d.need_login)) { gotoLogin(d.error); throw new Error(d.error || '请先登录'); }
    if (r.status === 410 && d && d.need_start) { openStartWizard(d.error); throw new Error(d.error); }
    throw new Error(d.error || ('HTTP ' + r.status));
  }
  return d;
}

let toastTimer = null;
function toast(msg, isErr) {
  const el = $('toast');
  if (!el) { console.log(msg); return; }
  el.textContent = msg;
  el.className = 'toast show' + (isErr ? ' err' : '');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.className = 'toast'; }, 3800);
}

const inflight = new Set();
/* 所有按钮统一走这个封装：防重复点 + 处理中变灰 + 明确的成功/失败提示 */
async function act(btnId, label, fn) {
  const key = btnId || ('anon:' + label);
  if (inflight.has(key)) { toast(label + ' 正在处理中…'); return; }
  inflight.add(key);
  const el = btnId ? $(btnId) : null;
  const old = el ? el.textContent : '';
  if (el) { el.disabled = true; el.classList.add('busy'); el.textContent = '处理中…'; }
  try {
    const msg = await fn();
    toast(msg || (label + '完成'));
  } catch (e) {
    toast(label + '失败：' + (e && e.message ? e.message : e), true);
  } finally {
    inflight.delete(key);
    if (el) { el.disabled = false; el.classList.remove('busy'); el.textContent = old; }
  }
}

function gotoLogin(msg) {
  if (location.pathname !== '/') location.href = '/' + (msg ? ('?msg=' + encodeURIComponent(msg)) : '');
}

/* 页面关闭不主动停：刷新页面也会触发 pagehide，停掉会打断正常直播。
   真正的停止机制：① 服务端空闲回收（没有请求 = 页面关了，--idle 秒后停）② 用户点「退出」或「立即停止我这个电台」。 */
