/* app.js — 前端单页逻辑（无框架）。 */
'use strict';

const LOT_STATUSES = ['未触发', '计划买入', '已买入', '达到 +5%', '达到 +10%', '部分卖出', '已卖出'];

const LOT_BADGE = {
  '未触发': ['lot-none', '未触发'],
  '计划买入': ['lot-plan', '计划'],
  '已买入': ['lot-bought', '持有'],
  '达到 +5%': ['lot-tp5', '+5%'],
  '达到 +10%': ['lot-tp10', '+10%'],
  '部分卖出': ['lot-part', '部分卖'],
  '已卖出': ['lot-sold', '已卖'],
};

const state = {
  stocks: [],
  tags: [],
  meta: { stock_types: [], lot_statuses: LOT_STATUSES },
  type: 'all',
  tag: null,
  quick: new Set(),
  search: '',
  sortKey: 'distance_abs',
  sortDir: 'asc',
  openStockId: null,
  editingId: null,
  autoRefresh: true,
};

const $ = (sel) => document.querySelector(sel);
const tbody = $('#tbody');
const modal = $('#modal');
const overlay = $('#modal-overlay');
const toastEl = $('#toast');

/* ------------------------------------------------------------------ */
/* 工具函数                                                             */
/* ------------------------------------------------------------------ */
async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `请求失败 (${res.status})`);
  return data;
}

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

function inputVal(v) { return (v === null || v === undefined) ? '' : String(v); }

function money(v) {
  if (v === null || v === undefined || v === '') return '—';
  return Number(v).toFixed(2);
}
// 止盈目标：最多 3 位小数，末位为 0 时退到 2 位（9.975 / 10.45 / 11.00）
function fmtTarget(v) {
  if (v === null || v === undefined) return '—';
  const n = Number(v);
  const s3 = n.toFixed(3);
  return s3.endsWith('0') ? n.toFixed(2) : s3;
}
function signedPct(v) {
  if (v === null || v === undefined) return '—';
  const n = Number(v);
  return (n > 0 ? '+' : '') + n.toFixed(2) + '%';
}
function todayStr() {
  const d = new Date();
  const m = String(d.getMonth() + 1).padStart(2, '0');
  const day = String(d.getDate()).padStart(2, '0');
  return d.getFullYear() + '-' + m + '-' + day;
}
function pnlText(v) {
  if (v === null || v === undefined) return '—';
  return (Number(v) > 0 ? '+' : '') + Number(v).toFixed(2);
}
function pnlClass(v) {
  if (v === null || v === undefined || Number(v) === 0) return '';
  return Number(v) > 0 ? 'pnl-pos' : 'pnl-neg';
}

let toastTimer = null;
function toast(msg, isErr = false) {
  toastEl.textContent = msg;
  toastEl.className = 'toast' + (isErr ? ' err' : '');
  toastEl.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { toastEl.hidden = true; }, 2400);
}

/* ------------------------------------------------------------------ */
/* 数据加载                                                             */
/* ------------------------------------------------------------------ */
async function loadAll() {
  try {
    const [stocks, tags, meta] = await Promise.all([
      api('/api/stocks'),
      api('/api/tags'),
      api('/api/meta'),
    ]);
    state.stocks = stocks;
    state.tags = tags.map(t => t.name);
    state.meta = meta;
    renderTags();
    renderTable();
  } catch (e) {
    toast(e.message, true);
  }
}

/* ------------------------------------------------------------------ */
/* 实时行情                                                             */
/* ------------------------------------------------------------------ */
function updateQuoteStatus(trading) {
  const el = $('#quote-status');
  el.textContent = trading ? '交易中' : '休市';
  el.classList.toggle('trading', !!trading);
}

async function refreshQuotes(force = false, quiet = false) {
  const btn = $('#btn-quote');
  try {
    btn.disabled = true;
    const data = await api('/api/quotes/refresh', {
      method: 'POST', body: JSON.stringify({ force }),
    });
    state.stocks = data.stocks;
    updateQuoteStatus(data.trading);
    renderTable();
    if (data.skipped) {
      if (!quiet) toast('非交易时段，未拉取行情');
    } else if (data.fetched === 0 && data.total > 0) {
      toast('行情获取失败，请检查网络', true);
    } else if (!quiet || data.updated > 0) {
      toast(`行情已更新（${data.updated} 只变动）`);
    }
  } catch (e) {
    if (!quiet) toast(e.message, true);
  } finally {
    btn.disabled = false;
  }
}

/* ------------------------------------------------------------------ */
/* 筛选 / 排序                                                          */
/* ------------------------------------------------------------------ */
function filtered() {
  let list = state.stocks;
  if (state.type !== 'all') list = list.filter(s => s.stock_type === state.type);
  if (state.quick.has('triggered')) {
    list = list.filter(s => s.stock_type === 'trade' && s.distance_pct != null && s.distance_pct <= 0);
  }
  if (state.quick.has('holding')) list = list.filter(s => s.filled_lots > 0);
  if (state.quick.has('tp')) list = list.filter(s => s.has_tp);
  if (state.tag) list = list.filter(s => (s.tags || []).includes(state.tag));
  const q = state.search.trim().toLowerCase();
  if (q) list = list.filter(s => s.code.toLowerCase().includes(q) || s.name.toLowerCase().includes(q));
  return list;
}

function sortValue(s, key) {
  switch (key) {
    case 'code': return s.code;
    case 'name': return s.name;
    case 'current_price': return (s.current_price == null) ? null : s.current_price;
    case 'watch_price': return (s.watch_price == null) ? null : s.watch_price;
    case 'distance_pct': return s.distance_pct;
    case 'distance_abs': return (s.distance_pct == null) ? null : Math.abs(s.distance_pct);
    case 'unrealized_pnl': return (s.unrealized_pnl == null) ? null : s.unrealized_pnl;
    case 'realized_pnl': return (s.realized_pnl == null) ? null : s.realized_pnl;
    default: return null;
  }
}

function sorted() {
  const key = state.sortKey;
  const dir = state.sortDir === 'asc' ? 1 : -1;
  return filtered().slice().sort((a, b) => {
    const va = sortValue(a, key), vb = sortValue(b, key);
    const na = (va === null || va === undefined);
    const nb = (vb === null || vb === undefined);
    if (na && nb) return 0;
    if (na) return 1;
    if (nb) return -1;
    if (typeof va === 'string') return va.localeCompare(vb, 'zh') * dir;
    return (va - vb) * dir;
  });
}

/* ------------------------------------------------------------------ */
/* 表格渲染                                                             */
/* ------------------------------------------------------------------ */
function distClass(d) {
  if (d === null || d === undefined) return 'above';
  if (d < 0) return 'below';
  if (d <= 5) return 'near';
  return 'above';
}

function lotCell(l) {
  if (!l) return '<span class="lot-badge lot-none">—</span>';
  const [cls, label] = LOT_BADGE[l.status] || ['lot-none', esc(l.status)];
  let html = `<span class="lot-badge ${cls}">${label}</span>`;
  if (l.filled && l.actual_price != null) {
    html += `<div class="lot-line">买 ${money(l.actual_price)}</div>`;
    if (l.target_5 != null) html += `<div class="lot-line sell">卖 ${fmtTarget(l.target_5)}</div>`;
  } else {
    html += `<div class="lot-line">计划 ${money(l.plan_price)}</div>`;
  }
  return html;
}

function renderTable() {
  const rows = sorted();
  tbody.innerHTML = rows.map((s) => {
    const isTrade = s.stock_type === 'trade';
    const typeBadge = isTrade
      ? '<span class="badge type-trade">交易型</span>'
      : '<span class="badge type-hold">长持型</span>';
    const tags = (s.tags || []).map(t => `<span class="tag-pill">${esc(t)}</span>`).join('');
    const tier0 = s.tiers[0] ? money(s.tiers[0].value) : '—';
    const tier1 = s.tiers[1] ? money(s.tiers[1].value) : '—';
    const tier2 = s.tiers[2] ? money(s.tiers[2].value) : '—';
    const dist = s.distance_pct == null ? '—' : signedPct(s.distance_pct);
    const dcls = distClass(s.distance_pct);
    const up5 = (isTrade && s.up_5 != null) ? money(s.up_5) : '—';
    const up10 = (isTrade && s.up_10 != null) ? money(s.up_10) : '—';

    const dash = '<span class="lot-badge lot-none">—</span>';
    const lotCells = isTrade
      ? [1, 2, 3].map(t => lotCell((s.lots || []).find(l => l.tranche === t)))
      : [dash, dash, dash];

    return `<tr data-id="${s.id}">
      <td><span class="code">${esc(s.code)}</span></td>
      <td><span class="name">${esc(s.name)}</span></td>
      <td>${typeBadge}</td>
      <td>${tags || '<span class="tag-pill" style="opacity:.5">—</span>'}</td>
      <td class="num">${money(s.current_price)}</td>
      <td class="num">${tier0}</td>
      <td class="num">${isTrade ? `<span class="val-down">${tier1}</span>` : '—'}</td>
      <td class="num">${isTrade ? `<span class="val-down">${tier2}</span>` : '—'}</td>
      <td class="num">${up5 !== '—' ? `<span class="val-up">${up5}</span>` : '—'}</td>
      <td class="num">${up10 !== '—' ? `<span class="val-up">${up10}</span>` : '—'}</td>
      <td class="num"><span class="dist ${dcls}">${dist}</span></td>
      <td class="num">${isTrade ? `<span class="${pnlClass(s.unrealized_pnl)}">${pnlText(s.unrealized_pnl)}</span>` : '—'}</td>
      <td class="num">${isTrade ? `<span class="${pnlClass(s.realized_pnl)}">${pnlText(s.realized_pnl)}</span>` : '—'}</td>
      <td>${lotCells[0]}</td>
      <td>${lotCells[1]}</td>
      <td>${lotCells[2]}</td>
      <td class="notes" title="${esc(s.notes)}">${esc(s.notes)}</td>
    </tr>`;
  }).join('');

  $('#empty').hidden = rows.length > 0;
  renderSummary();

  // 表头排序高亮
  document.querySelectorAll('thead th.sortable').forEach(th => {
    th.classList.toggle('sorted', th.dataset.sort === state.sortKey);
  });
}

function renderSummary() {
  let realized = 0, unrealized = 0;
  state.stocks.forEach(s => {
    realized += (s.realized_pnl || 0);
    unrealized += (s.unrealized_pnl || 0);
  });
  const ru = $('#sum-unrealized');
  const rr = $('#sum-realized');
  if (ru) { ru.textContent = pnlText(unrealized); ru.className = pnlClass(unrealized); }
  if (rr) { rr.textContent = pnlText(realized); rr.className = pnlClass(realized); }
}

function renderTags() {
  const wrap = $('#tag-chips');
  wrap.innerHTML = state.tags.map(t =>
    `<button class="chip ${state.tag === t ? 'active' : ''}" data-tag="${esc(t)}">${esc(t)}</button>`
  ).join('');
}

/* ------------------------------------------------------------------ */
/* 详情弹窗                                                             */
/* ------------------------------------------------------------------ */
function openDetail(id) {
  const s = state.stocks.find(x => x.id === id);
  if (!s) return;
  state.openStockId = id;
  modal.innerHTML = detailHTML(s);
  overlay.hidden = false;
  if (s.stock_type === 'trade') {
    (s.lots || []).forEach(l => loadSellRecords(l.id));
  }
}

function detailHTML(s) {
  const isTrade = s.stock_type === 'trade';
  const typeBadge = isTrade ? '<span class="badge type-trade">交易型</span>' : '<span class="badge type-hold">长持型</span>';
  const tags = (s.tags || []).map(t => `<span class="tag-pill">${esc(t)}</span>`).join('');

  const tierRow = isTrade && s.tiers.length
    ? `<div class="tier-row">${s.tiers.map((t, i) =>
        `<div class="tier"><div class="k">${t.label}</div><div class="v${i === 0 ? '' : ' val-down'}">${money(t.value)}</div></div>`
      ).join('')}
        <div class="tier"><div class="k">+5%</div><div class="v val-up">${money(s.up_5)}</div></div>
        <div class="tier"><div class="k">+10%</div><div class="v val-up">${money(s.up_10)}</div></div>
      </div>`
    : '';

  const lotsSection = isTrade
    ? `<div class="section-title">三笔买入计划（每笔独立止盈）</div>
       <div class="lot-grid">${(s.lots || []).map(l => lotCardHTML(l, s.current_price)).join('')}</div>`
    : `<div class="section-title">备注</div>
       <div class="hold-note">${s.notes ? esc(s.notes) : '暂无备注。长持型股票不生成三笔买入计划。'}</div>`;

  return `
  <div class="modal-head">
    <div>
      <h2>${esc(s.name)}</h2>
      <div class="code">${esc(s.code)} ${typeBadge} ${tags}</div>
    </div>
    <div style="display:flex;gap:8px;align-items:center;">
      <button class="btn btn-sm" data-action="edit-stock">编辑</button>
      <button class="modal-close" data-action="close" title="关闭">×</button>
    </div>
  </div>

  <div class="meta-note" style="margin-top:14px;">
    当前价
    <input type="number" step="0.01" id="cur-price" value="${inputVal(s.current_price)}" style="width:110px;padding:5px 8px;border:1px solid var(--line);border-radius:7px;">
    <button class="btn btn-sm" data-action="save-current">更新当前价</button>
  </div>

  <div class="kpis">
    <div class="kpi"><div class="k">当前价</div><div class="v">${money(s.current_price)}</div></div>
    <div class="kpi"><div class="k">观察基准价</div><div class="v">${money(s.watch_price)}</div></div>
    <div class="kpi"><div class="k">相对观察价</div><div class="v">${signedPct(s.distance_pct)}</div></div>
    <div class="kpi"><div class="k">已成交笔数</div><div class="v">${isTrade ? s.filled_lots + ' / 3' : '—'}</div></div>
  </div>

  ${isTrade ? `<div class="pnl-summary">
    <div class="pnl-item"><span class="k">浮盈浮亏（未实现）</span><span class="v ${pnlClass(s.unrealized_pnl)}">${pnlText(s.unrealized_pnl)}</span></div>
    <div class="pnl-item"><span class="k">累计盈利（已实现）</span><span class="v ${pnlClass(s.realized_pnl)}">${pnlText(s.realized_pnl)}</span></div>
  </div>` : ''}

  ${tierRow}
  ${isTrade && s.notes ? `<div class="meta-note">备注：${esc(s.notes)}</div>` : ''}
  ${lotsSection}

  <div class="form-actions" style="margin-top:20px;">
    <button class="btn btn-danger btn-sm" data-action="delete-stock">删除该股票</button>
  </div>`;
}

function lotCardHTML(l, currentPrice) {
  const names = ['', '第一笔', '第二笔', '第三笔'];
  const title = names[l.tranche] || ('第' + l.tranche + '笔');
  const [cls, label] = LOT_BADGE[l.status] || ['lot-none', esc(l.status)];

  const spread = (currentPrice != null && l.actual_price != null) ? (currentPrice - l.actual_price) : null;
  const filledInfo = l.filled
    ? `<div class="filled-info">
        <div class="row"><span class="k">买入价</span><span class="v">${money(l.actual_price)}</span></div>
        <div class="row"><span class="k">现价</span><span class="v">${money(currentPrice)}</span></div>
        <div class="row"><span class="k">价差</span><span class="v ${pnlClass(spread)}">${pnlText(spread)}</span></div>
        <div class="row"><span class="k">数量</span><span class="v">${l.quantity} 手</span></div>
        <div class="row"><span class="k">剩余</span><span class="v">${l.remaining} 手</span></div>
        <div class="row"><span class="k">浮盈</span><span class="v ${pnlClass(l.unrealized_pnl)}">${pnlText(l.unrealized_pnl)}</span></div>
        <div class="row hl"><span class="k">卖出目标(+5%)</span><span class="v">${fmtTarget(l.target_5)}</span></div>
      </div>`
    : `<div class="meta-note" style="margin-top:8px;">尚未成交</div>`;

  const pnlInfo = (l.realized_pnl != null && l.realized_pnl !== 0)
    ? `<div class="filled-info" style="margin-top:8px;">
        <div class="row"><span class="k">累计盈利(已实现)</span><span class="v ${pnlClass(l.realized_pnl)}">${pnlText(l.realized_pnl)}</span></div>
      </div>`
    : '';

  const statusOptions = state.meta.lot_statuses.map(st =>
    `<option value="${esc(st)}" ${st === l.status ? 'selected' : ''}>${esc(st)}</option>`
  ).join('');

  return `<div class="lot-card" data-card="${l.id}">
    <div class="lot-title">【${title}】<span class="lot-badge ${cls}">${label}</span></div>
    <div class="plan">计划买入 ${money(l.plan_price)}</div>
    ${filledInfo}
    ${pnlInfo}
    <div class="lot-form" style="margin-top:12px;">
      <input type="number" step="0.01" class="lot-price" data-lot="${l.id}" placeholder="成交价" value="${inputVal(l.actual_price)}">
      <input type="number" step="1" class="lot-qty" data-lot="${l.id}" placeholder="数量(手)" value="${inputVal(l.quantity)}">
      <input type="date" class="lot-date" data-lot="${l.id}" value="${l.buy_date || todayStr()}">
      <select class="lot-status" data-lot="${l.id}">${statusOptions}</select>
      <div class="span2">
        <button class="btn btn-sm" data-action="save-lot" data-lot="${l.id}">保存该笔</button>
        <button class="btn-link" data-action="clear-lot" data-lot="${l.id}">清空成交</button>
      </div>
    </div>
    <div class="section-title" style="margin-top:14px;">卖出记录</div>
    <div data-sells="${l.id}"></div>
    <div class="lot-form" style="margin-top:8px;">
      <input type="number" step="0.01" class="sell-price" data-lot="${l.id}" placeholder="卖出价">
      <input type="number" step="1" class="sell-qty" data-lot="${l.id}" placeholder="数量(手)">
      <input type="date" class="sell-date" data-lot="${l.id}" value="${todayStr()}">
      <div class="span2">
        <button class="btn btn-sm" data-action="add-sell" data-lot="${l.id}">＋ 记录卖出</button>
      </div>
    </div>
  </div>`;
}

async function loadSellRecords(lotId) {
  const wrap = modal.querySelector(`[data-sells="${lotId}"]`);
  if (!wrap) return;
  try {
    const recs = await api(`/api/lots/${lotId}/sells`);
    wrap.innerHTML = recs.length
      ? recs.map((r) => {
          const profit = (r.sell_price != null && r.buy_price != null && r.quantity)
            ? (r.sell_price - r.buy_price) * r.quantity * 100 : null;
          return `<div class="sell-item">
            <span>${esc(r.sell_date || '')} · 卖 <span class="price">${money(r.sell_price)}</span> × ${r.quantity ?? '—'} 手
              ${r.buy_price != null ? ` · 成本 ${money(r.buy_price)}` : ''}
              ${profit != null ? ` · <span class="${pnlClass(profit)}">${pnlText(profit)}</span>` : ''}
            </span>
            <button class="btn-link" data-action="del-sell" data-sell="${r.id}">删除</button>
          </div>`;
        }).join('')
      : '<div class="meta-note" style="margin:0;">暂无卖出记录。</div>';
  } catch (e) { /* ignore */ }
}

/* ------------------------------------------------------------------ */
/* 新增 / 编辑表单                                                      */
/* ------------------------------------------------------------------ */
function openEdit(id) {
  const s = id ? state.stocks.find(x => x.id === id) : null;
  state.editingId = id;
  modal.innerHTML = formHTML(s);
  overlay.hidden = false;
  bindFormAutocomplete();
}

function formHTML(s) {
  const isEdit = !!s;
  const tags = (s ? (s.tags || []) : []).join('，');
  const tagChips = state.tags.map(t =>
    `<button class="chip" type="button" data-tag-add="${esc(t)}">${esc(t)}</button>`
  ).join('');

  return `
  <div class="modal-head">
    <h2>${isEdit ? '编辑股票' : '添加股票'}</h2>
    <button class="modal-close" data-action="close">×</button>
  </div>
  <div class="form" id="stock-form">
    <div class="field"><label>股票代码 *</label><input name="code" value="${esc(s ? s.code : '')}" placeholder="如 600741"></div>
    <div class="field"><label>股票名称 *</label><div class="input-wrap"><input name="name" value="${esc(s ? s.name : '')}" placeholder="如 华域汽车（输入名称可搜索）"></div></div>
    <div class="field"><label>类型</label>
      <select name="stock_type">
        ${state.meta.stock_types.map(t =>
          `<option value="${t.value}" ${s && s.stock_type === t.value ? 'selected' : ''}>${t.label}</option>`
        ).join('')}
      </select>
    </div>
    <div class="field"><label>观察基准价</label><input name="watch_price" type="number" step="0.01" value="${inputVal(s ? s.watch_price : '')}" placeholder="如 15.00"></div>
    <div class="field"><label>当前价</label><input name="current_price" type="number" step="0.01" value="${inputVal(s ? s.current_price : '')}"></div>
    <div class="field"><label>标签（逗号分隔）</label><input name="tags" value="${esc(tags)}" placeholder="烟蒂，观察">
      <div style="margin-top:6px;">${tagChips}</div>
    </div>
    <div class="field full"><label>备注</label><textarea name="notes">${esc(s ? s.notes : '')}</textarea></div>
    <div class="form-actions">
      ${isEdit ? '<button class="btn btn-danger" data-action="delete-stock" style="margin-right:auto;">删除</button>' : ''}
      <button class="btn" data-action="close">取消</button>
      <button class="btn btn-primary" data-action="save-stock">保存</button>
    </div>
  </div>`;
}

/* 代码 / 名称自动补全 */
let searchTimer = null;

function bindFormAutocomplete() {
  const codeEl = modal.querySelector('input[name="code"]');
  const nameEl = modal.querySelector('input[name="name"]');
  const priceEl = modal.querySelector('input[name="current_price"]');
  if (!codeEl || !nameEl) return;

  // 代码失焦 → 自动带出名称 + 当前价
  codeEl.addEventListener('blur', async () => {
    const code = codeEl.value.trim();
    if (!/^\d{6}$/.test(code)) return;
    try {
      const info = await api('/api/quote/lookup?code=' + encodeURIComponent(code));
      if (!nameEl.value.trim()) nameEl.value = info.name;
      if (priceEl && !priceEl.value && info.price != null) priceEl.value = info.price;
      toast(`已带出：${info.name}${info.price != null ? ' @ ' + info.price : ''}`);
    } catch (e) { /* 查不到则静默 */ }
  });

  // 名称输入 → 搜索建议
  nameEl.addEventListener('input', () => {
    clearTimeout(searchTimer);
    const q = nameEl.value.trim();
    if (!q) { hideSuggest(); return; }
    searchTimer = setTimeout(() => doSearch(q), 250);
  });
  nameEl.addEventListener('blur', () => setTimeout(hideSuggest, 150));
}

async function doSearch(q) {
  try {
    const list = await api('/api/quote/search?q=' + encodeURIComponent(q));
    showSuggest(list);
  } catch (e) {
    hideSuggest();
  }
}

function showSuggest(list) {
  hideSuggest();
  if (!list || !list.length) return;
  const nameEl = modal.querySelector('input[name="name"]');
  if (!nameEl) return;
  const wrap = document.createElement('div');
  wrap.className = 'suggest';
  list.slice(0, 8).forEach((item) => {
    const row = document.createElement('div');
    row.className = 'suggest-item';
    row.innerHTML = `<span class="code">${esc(item.code)}</span><span>${esc(item.name)}</span>`;
    row.addEventListener('mousedown', (e) => {
      e.preventDefault();
      selectSuggestion(item);
    });
    wrap.appendChild(row);
  });
  nameEl.parentElement.appendChild(wrap);
}

function selectSuggestion(item) {
  const codeEl = modal.querySelector('input[name="code"]');
  const nameEl = modal.querySelector('input[name="name"]');
  const priceEl = modal.querySelector('input[name="current_price"]');
  codeEl.value = item.code;
  nameEl.value = item.name;
  hideSuggest();
  if (priceEl && !priceEl.value) {
    api('/api/quote/lookup?code=' + encodeURIComponent(item.code))
      .then((info) => { if (info.price != null && !priceEl.value) priceEl.value = info.price; })
      .catch(() => {});
  }
}

function hideSuggest() {
  modal.querySelectorAll('.suggest').forEach((el) => el.remove());
}

function readStockForm() {
  const f = $('#stock-form');
  const get = (n) => f.querySelector(`[name="${n}"]`).value;
  const tags = get('tags').split(/[,，]/).map(t => t.trim()).filter(Boolean);
  return {
    code: get('code').trim(),
    name: get('name').trim(),
    stock_type: get('stock_type'),
    watch_price: get('watch_price') === '' ? null : parseFloat(get('watch_price')),
    current_price: get('current_price') === '' ? null : parseFloat(get('current_price')),
    tags,
    notes: get('notes'),
  };
}

/* ------------------------------------------------------------------ */
/* 事件处理                                                             */
/* ------------------------------------------------------------------ */
async function handleAction(action, el) {
  try {
    if (action === 'close') {
      overlay.hidden = true; state.openStockId = null; state.editingId = null; return;
    }
    if (action === 'edit-stock') { openEdit(state.openStockId); return; }
    if (action === 'delete-stock') {
      const id = state.openStockId;
      const stock = state.stocks.find(x => x.id === id);
      const code = stock ? stock.code : '';
      const label = stock ? `${stock.name} (${stock.code})` : '该股票';
      if (!confirm(`确定删除「${label}」及其全部买入/卖出记录？此操作不可恢复。`)) return;
      const typed = prompt(`此操作不可恢复。请输入股票代码「${code}」以确认删除：`);
      if (typed !== code) { toast('已取消（代码不匹配）'); return; }
      await api(`/api/stocks/${id}`, { method: 'DELETE' });
      overlay.hidden = true;
      await loadAll();
      toast('已删除');
      return;
    }
    if (action === 'save-stock') {
      const data = readStockForm();
      if (!data.code || !data.name) { toast('代码和名称不能为空', true); return; }
      if (state.editingId) {
        await api(`/api/stocks/${state.editingId}`, { method: 'PUT', body: JSON.stringify(data) });
        toast('已保存');
      } else {
        await api('/api/stocks', { method: 'POST', body: JSON.stringify(data) });
        toast('已添加');
      }
      await loadAll();
      const targetId = state.editingId || state.stocks[state.stocks.length - 1].id;
      state.editingId = null;
      openDetail(targetId);
      return;
    }
    if (action === 'save-current') {
      const v = $('#cur-price').value;
      await api(`/api/stocks/${state.openStockId}`, {
        method: 'PUT', body: JSON.stringify({ current_price: v === '' ? null : parseFloat(v) }),
      });
      await loadAll();
      openDetail(state.openStockId);
      toast('当前价已更新');
      return;
    }
    if (action === 'save-lot') {
      const id = el.dataset.lot;
      const payload = {
        actual_price: readNum(modal, `.lot-price[data-lot="${id}"]`),
        quantity: readNum(modal, `.lot-qty[data-lot="${id}"]`, true),
        buy_date: modal.querySelector(`.lot-date[data-lot="${id}"]`).value || null,
        status: modal.querySelector(`.lot-status[data-lot="${id}"]`).value,
      };
      await api(`/api/lots/${id}`, { method: 'PUT', body: JSON.stringify(payload) });
      await loadAll();
      openDetail(state.openStockId);
      toast('该笔已保存');
      return;
    }
    if (action === 'clear-lot') {
      const id = el.dataset.lot;
      if (!confirm('清空该笔的成交信息（不影响计划价）？')) return;
      await api(`/api/lots/${id}`, {
        method: 'PUT',
        body: JSON.stringify({ actual_price: null, quantity: null, buy_date: null, status: '未触发' }),
      });
      await loadAll();
      openDetail(state.openStockId);
      toast('已清空成交');
      return;
    }
    if (action === 'add-sell') {
      const id = el.dataset.lot;
      const price = readNum(modal, `.sell-price[data-lot="${id}"]`);
      if (price == null) { toast('请填写卖出价', true); return; }
      await api(`/api/lots/${id}/sells`, {
        method: 'POST',
        body: JSON.stringify({
          sell_price: price,
          quantity: readNum(modal, `.sell-qty[data-lot="${id}"]`, true),
          sell_date: modal.querySelector(`.sell-date[data-lot="${id}"]`).value || null,
        }),
      });
      await loadAll();
      openDetail(state.openStockId);
      toast('卖出记录已添加');
      return;
    }
    if (action === 'del-sell') {
      if (!confirm('删除这条卖出记录？')) return;
      await api(`/api/sells/${el.dataset.sell}`, { method: 'DELETE' });
      await loadAll();
      openDetail(state.openStockId);
      return;
    }
  } catch (e) {
    toast(e.message, true);
  }
}

function readNum(scope, sel, isInt = false) {
  const el = scope.querySelector(sel);
  if (!el || el.value === '') return null;
  const n = isInt ? parseInt(el.value, 10) : parseFloat(el.value);
  return Number.isFinite(n) ? n : null;
}

/* ------------------------------------------------------------------ */
/* 事件绑定                                                             */
/* ------------------------------------------------------------------ */
document.addEventListener('DOMContentLoaded', () => {
  loadAll();

  $('#btn-add').addEventListener('click', () => openEdit(null));

  $('#btn-quote').addEventListener('click', () => refreshQuotes(true, false));
  $('#auto-refresh').addEventListener('change', (e) => { state.autoRefresh = e.target.checked; });

  // 每 60 秒自动拉取一次；非交易时段由服务端跳过（不会联网）
  setInterval(() => { if (state.autoRefresh) refreshQuotes(false, true); }, 60000);

  // 页面加载后显示交易状态
  api('/api/quotes/status').then(s => updateQuoteStatus(s.trading)).catch(() => {});

  $('#search').addEventListener('input', (e) => { state.search = e.target.value; renderTable(); });

  $('#type-seg').addEventListener('click', (e) => {
    const btn = e.target.closest('.seg-btn');
    if (!btn) return;
    state.type = btn.dataset.type;
    document.querySelectorAll('#type-seg .seg-btn').forEach(b => b.classList.toggle('active', b === btn));
    renderTable();
  });

  $('#sort').addEventListener('change', (e) => { state.sortKey = e.target.value; renderTable(); });

  $('#btn-dir').addEventListener('click', () => {
    state.sortDir = state.sortDir === 'asc' ? 'desc' : 'asc';
    $('#btn-dir').textContent = state.sortDir === 'asc' ? '↑' : '↓';
    renderTable();
  });

  // 快筛 chips
  document.querySelectorAll('.chip[data-quick]').forEach(chip => {
    chip.addEventListener('click', () => {
      const q = chip.dataset.quick;
      if (state.quick.has(q)) state.quick.delete(q);
      else state.quick.add(q);
      chip.classList.toggle('active', state.quick.has(q));
      renderTable();
    });
  });

  // 标签 chips（事件委托，因为会重渲染）
  $('#tag-chips').addEventListener('click', (e) => {
    const chip = e.target.closest('.chip[data-tag]');
    if (!chip) return;
    state.tag = (state.tag === chip.dataset.tag) ? null : chip.dataset.tag;
    renderTags();
    renderTable();
  });

  // 表头排序
  document.querySelector('thead').addEventListener('click', (e) => {
    const th = e.target.closest('th.sortable');
    if (!th) return;
    const key = th.dataset.sort;
    if (state.sortKey === key) state.sortDir = state.sortDir === 'asc' ? 'desc' : 'asc';
    else { state.sortKey = key; state.sortDir = 'asc'; }
    $('#sort').value = key;
    $('#btn-dir').textContent = state.sortDir === 'asc' ? '↑' : '↓';
    renderTable();
  });

  // 行点击 → 详情
  tbody.addEventListener('click', (e) => {
    const tr = e.target.closest('tr[data-id]');
    if (tr) openDetail(Number(tr.dataset.id));
  });

  // 弹窗内点击（事件委托）
  modal.addEventListener('click', async (e) => {
    const tagAdd = e.target.closest('[data-tag-add]');
    if (tagAdd) {
      const input = modal.querySelector('input[name="tags"]');
      const cur = input.value.split(/[,，]/).map(t => t.trim()).filter(Boolean);
      if (!cur.includes(tagAdd.dataset.tagAdd)) cur.push(tagAdd.dataset.tagAdd);
      input.value = cur.join('，');
      return;
    }
    const btn = e.target.closest('[data-action]');
    if (btn) await handleAction(btn.dataset.action, btn);
  });

  // 点击遮罩关闭
  overlay.addEventListener('click', (e) => {
    if (e.target === overlay) { overlay.hidden = true; state.openStockId = null; state.editingId = null; }
  });
});
