/* 股票研究页：研究库 + 详情工作台。所有展示内容来自现有研究接口。 */
'use strict';

/* 原 8 类的 UI 语义已降级为「股票画像」（Style / Profile），不再代表最终评分模型。
   最终模型一律看路由结果里的 primary_model —— Router 是唯一权威。 */
const TYPE_LABELS = {
  growth: '成长', quality: '质量', value: '价值', dividend: '高股息',
  cigar_butt: '烟蒂', asset_value: '资产价值', cyclical: '周期', turnaround: '困境反转',
};
const UNROUTED = '__unrouted';   // 研究库筛选里「Router 之前分析过的旧数据」这一档
/* 路由状态的语气色。中文名由后端 ROUTE_STATUS_LABELS 下发（route_status_label），
   前端不复制一份，免得两边说辞不一致。 */
const ROUTE_STATUS_TONE = {
  CLEAR: 'green', HYBRID: 'blue', AMBIGUOUS: 'amber', FALLBACK: 'gray', INSUFFICIENT_DATA: 'gray',
};
const COMP_LABELS = {
  quality: '企业质量', valuation: '估值', balance: '资产负债表', cashflow: '现金流',
  shareholder: '股东回报', growth: '成长性', cyclical_position: '周期位置',
  asset_value: '资产价值', valuation_discount: '估值折价', liability_safety: '负债安全',
  cashflow_survival: '现金流存活能力', profit_survival: '盈利存活能力', dividend_quality: '股息质量',
};
/* 风险等级的**语气色**是纯 UI 概念，留在这里。
   等级 -> 中文名归后端（rules.RISK_SIGNAL_LABELS），随 /api/meta 的
   risk_signal_labels 下发，前端不再自己造一份说法。下面这份兜底只在
   meta 还没加载出来时用，内容与后端逐字一致，由
   tests/test_scoring_invariants.py 钉住——漂了测试就红。 */
const RISK_TONE = { GREEN: 'green', YELLOW: 'amber', ORANGE: 'orange', RED: 'red' };
const RISK_LABELS_FALLBACK = {
  GREEN: '未见风险', YELLOW: '轻度风险',
  ORANGE: '明显风险', RED: '严重风险',
};
function riskLabels() { return (state.meta && state.meta.risk_signal_labels) || RISK_LABELS_FALLBACK; }
/* 资产审计状态的中文名，同一条约定：后端 audit_job.AUDIT_STATUS_LABELS 下发，
   前端只留一份逐字一致的兜底（test_audit_gate.py 逐字比对两份表）。
   四档里只有 OK 是「能看分数」；其余三档一律挡死，理由是分数还没算过资产语义层
   ——那是另一套口径的数（见 research/engine.py 的 AUDIT_SUPPRESSED_FIELDS）。 */
const AUDIT_STATUS_LABELS_FALLBACK = {
  UNAUDITED: '未审计', RUNNING: '审计中', FAILED: '审计失败', OK: '已审计',
};
function auditLabels() { return (state.meta && state.meta.audit_status_labels) || AUDIT_STATUS_LABELS_FALLBACK; }
function auditLabel(s) { return auditLabels()[s.audit_status] || s.audit_status || '未审计'; }
/* 未通过审计的股票：列表里独立置顶、详情页整屏只显示状态。 */
function isGated(s) { return (s?.audit_status || 'UNAUDITED') !== 'OK'; }
function auditPending() { return state.stocks.some((s) => s.audit_status === 'RUNNING'); }
const TABS = ['概览', '评分细则', '财务趋势', '资产负债表', '资产审计', '猪行业数据', '现金流', '风险', '历史评分', '价格模拟'];
/* 「猪行业数据」这个 tab 只对 ``is_pig_company`` 为真的股票开放。判据**不在前端**：
   它由后端随列表 / 详情一起下发（research/engine.py 的 ``_pig_mark``，与「补充核心
   经营数据」那个弹窗同一把尺子）。前端若自己按行业名判一次，就会出现「弹窗问你
   这只新股票的三个数、页面上却没有这个 tab」——两把尺子迟早说两句话。
   ``TABS`` 本身不动（它是「有哪些页」的清单，不是「这只股票看得到哪些页」），
   过滤只发生在渲染的那一刻。 */
const PIG_TAB = '猪行业数据';
function pigTabVisible() { return !!(state.currentDetail && state.currentDetail.is_pig_company); }
/* 当前 tab 还看不看得见：URL 直达（``?tab=猪行业数据``）或**上一只股票留着这个 tab、
   这一只没有这个页**时回落到概览。回落在渲染**之前**做——否则这一页会白发一次
   ``pig-evidence`` 请求，而且页面上会先闪一下只有那几只股票才有的表。 */
function normalizeTab() {
  if (state.currentTab === PIG_TAB && !pigTabVisible()) state.currentTab = '概览';
  return state.currentTab;
}
const COLORS = ['#2563eb', '#13a36f', '#f59e0b', '#8b5cf6'];
const initialParams = new URLSearchParams(location.search);
const initialTab = TABS.includes(initialParams.get('tab')) ? initialParams.get('tab') : '概览';
const state = { stocks: [], meta: null, model: 'all', sort: 'total_score', busy: false, bulkBusy: false, currentCode: null,
  currentDetail: null, currentTab: initialTab, trendMetric: 'revenue', historyMetric: 'total', sim: null,
  assets: null, assetsState: null, assetsCode: null, assetsBusy: false, auditFilter: 'all', auditOpen: new Set(),
  pig: null, pigError: null, pigCode: null, pigBusy: false, pigFilter: 'all', pigOpen: new Set(), pigEvidence: {}, pigEvidenceBusy: new Set(),
  pigCore: null,
  scoreOpen: new Set(['growth']), historyLegacyOpen: false };
/* 排序标签：net_cash_ratio 排的是**资产语义层**的调整后净现金/市值
   （旧字段名只是个兼容别名），所以标签必须写明「调整后」，免得又变成
   同一个中文名对应两个不同口径。 */
const SORT_LABELS = { total_score: '总评分', value: '估值分', quality: '质量分', price: '当前价格', net_cash_ratio: '调整后净现金 / 市值', updated_at: '最近更新' };

const $ = (s) => document.querySelector(s);
const tbody = $('#tbody'), suggest = $('#suggest'), drawer = $('#drawer');
const drawerHead = $('#drawer-head'), drawerTabs = $('#drawer-tabs'), drawerBody = $('#drawer-body');
const toastEl = $('#toast');

async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { 'Content-Type': 'application/json' }, ...opts });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `请求失败（${res.status}）`);
  return data;
}
function esc(s) { return String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])); }
function absent(reason = '数据缺失') { return `<span class="missing-value">${reason}</span>`; }
function bad(v) { return v === null || v === undefined || v === '' || !Number.isFinite(Number(v)); }
function num(v, digits = 2, missing = '数据缺失') { return bad(v) ? absent(missing) : Number(v).toFixed(digits); }
function money(v) { return bad(v) ? absent() : Number(v).toFixed(2); }
function score(v) { return bad(v) ? absent('评分不足') : Number(v).toFixed(1); }
function ratio(v, digits = 2) { return bad(v) ? absent() : `${(Number(v) * 100).toFixed(digits)}%`; }
function percent(v, digits = 2) { return bad(v) ? absent() : `${Number(v).toFixed(digits)}%`; }
function multiple(v) { return bad(v) ? absent() : `${Number(v).toFixed(2)}x`; }
function amount(v) {
  if (bad(v)) return absent();
  const n = Number(v), a = Math.abs(n);
  if (a >= 1e8) return `${(n / 1e8).toFixed(2)}亿`;
  if (a >= 1e4) return `${(n / 1e4).toFixed(2)}万`;
  return n.toFixed(2);
}
function plainAmount(v) { return bad(v) ? '数据缺失' : amount(v); }
function riskBadge(level) {
  const key = String(level || '').toUpperCase();
  const tone = RISK_TONE[key] || 'gray';
  return `<span class="risk-chip ${tone}"><i></i>${riskLabel(level)}</span>`;
}
function riskLabel(level, fallback = '风险数据缺失') {
  return riskLabels()[String(level || '').toUpperCase()] || fallback;
}
function delta(v) {
  if (bad(v)) return '<span class="muted">无可比历史</span>';
  const n = Number(v), cls = n > 0 ? 'positive' : n < 0 ? 'negative' : 'neutral';
  return `<span class="delta ${cls}">${n > 0 ? '+' : ''}${n.toFixed(1)}</span>`;
}
function deltaScore(v) {
  if (bad(v)) return '<span class="muted">无可比</span>';
  const n = Number(v), cls = n > 0 ? 'positive' : n < 0 ? 'negative' : 'neutral';
  return `<span class="delta ${cls}">${n > 0 ? '+' : ''}${n.toFixed(1)}</span>`;
}
function text(v, missing = '数据缺失') { return v ? esc(v) : absent(missing); }
function period(v) { return v ? esc(String(v).slice(0, 10)) : '报告期缺失'; }
// 细则各指标的原始值单位由后端 rules.COMPONENT_UNITS 下发，前端不再按指标名猜。
// （同名「率」的 raw 形态并不统一：ROIC 是小数 0.0827，ROE 是百分数 11.79。）
const UNIT_FORMATTERS = {
  money: (v) => amount(v),      // 元 -> 亿 / 万
  multiple: (v) => multiple(v), // 倍数
  ratio: (v) => ratio(v),       // 小数比率 -> 百分数
  points: (v) => percent(v),    // 已是百分数
  count: (v) => num(v, 0),      // 年数 / 次数
  plain: (v) => num(v),         // 无量纲
};
function rawValue(c) {
  const v = c?.raw, unit = c?.unit;
  // 状态与文本类没有数值语义，bad() 会把它们判成缺失，得先短路
  if (unit === 'state' || unit === 'text') return (v === null || v === undefined || v === '') ? absent() : esc(String(v));
  if (bad(v)) return absent();
  const fmt = UNIT_FORMATTERS[unit];
  const cell = fmt ? fmt(v) : num(v);
  // 带明确状态的指标（利润反转 / 现金流改善）把状态一并标出
  return c.state ? `${cell} · ${esc(c.state)}` : cell;
}
let toastTimer;
function toast(message, error) {
  toastEl.textContent = message; toastEl.className = 'toast' + (error ? ' err' : '');
  toastEl.hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(() => { toastEl.hidden = true; }, 2600);
}

/* ---------- 研究库 ---------- */
async function loadMeta() {
  // 模型字典（顺序 + 中文名）、风险等级中文名、规则版本指纹。拿不到也不影响
  // 主流程，各调用点都有兜底。
  try { state.meta = await api('/api/meta'); } catch (e) { state.meta = null; }
  // 顶栏的规则版本从指纹里取，不再写死在 HTML 里——写死的那份在 rules.py
  // 升到 V1.2 之后还显示 V1.0，是纯粹在骗人。
  const el = document.getElementById('rule-version');
  if (el) el.textContent = state.meta?.rule?.rule_version || '规则版本未知';
}
async function loadList() {
  try { state.stocks = await api('/api/research/list'); renderLibrary(); }
  catch (e) { toast(e.message, true); }
}
function filtered() {
  if (state.model === 'all') return state.stocks;
  if (state.model === UNROUTED) return state.stocks.filter(isUnrouted);
  return state.stocks.filter((s) => !isUnrouted(s) && s.primary_model === state.model);
}
/* 「未路由」= Router 没有给出正式的主模型。两种情况都算：
   1) 压根没有路由结果（旧数据），primary_model 是空的；
   2) 有路由结果但没达到门槛，此时 primary_model 是兜底的 GENERAL_VALUE_V2、
      适配度是空的——**不写死模型名**，判据就是「有没有适配度」。
   这一档的语义不是「算不出来」，而是「没有哪套模型够格」；所以模板退回画像判型，
   界面必须如实说是兜底，不能把它当成一个正式模型展示。 */
function isUnrouted(s) { return !s.primary_model || bad(s.primary_fit); }
/* 适配度最高的候选。未路由时要靠它说清「差多少」，不然界面上只剩一句干巴巴的
   未路由。候选列表未必有序，所以取最大值而不是取第一个。 */
function topCandidate(route) {
  return ((route && route.candidates) || []).filter((c) => !bad(c.fit))
    .reduce((best, c) => (best === null || Number(c.fit) > Number(best.fit) ? c : best), null);
}
/* 路由展示：主模型 + 适配度。适配度回答的是「该不该用这套模型看它」，
   不是「它值多少分」，所以只跟在模型名后面，绝不和研究评分并列。 */
function routeText(s) {
  if (isUnrouted(s)) {
    return '<span class="route-none" title="没有模型达到路由门槛，评分模板按画像判型兜底">未路由</span>';
  }
  const fit = bad(s.primary_fit) ? '' : ` ${Number(s.primary_fit).toFixed(0)}%`;
  return `<span class="route-tag">${esc(s.primary_model_label || s.primary_model)}${fit}</span>`;
}
function routeStatusChip(s) {
  const status = String(s.route_status || '');
  const [tone, label] = [ROUTE_STATUS_TONE[status] || 'gray', s.route_status_label || status];
  if (!status) return '<span class="route-chip gray" title="该项目在 Router 上线前分析，没有路由结果">未路由（旧数据）</span>';
  return `<span class="route-chip ${tone}" title="${esc(s.router_version || '')}">${esc(label)}</span>`;
}
/* 模型字典来自 /api/meta（后端 router.model_catalog），前端不写死模型表，
   这样以后开启一个未启用模型只需改后端 enabled，chip 顺序与名字自动跟上。 */
function modelOrder() { return (state.meta?.models || []).map((m) => m.model); }
function modelLabel(id) {
  const hit = (state.meta?.models || []).find((m) => m.model === id);
  return hit ? hit.label : id;
}
/* 筛选 chip 按主模型生成。没有股票的模型不出现，免得左栏挤满永远筛不出东西的 chip。 */
function renderFilters() {
  const order = modelOrder();
  const seen = new Map();
  let unrouted = 0;
  state.stocks.forEach((s) => {
    // 未审计的股票不进任何模型档：它们的 primary_model 是空的，但把它们算进
    // 「未路由」是谎报——那一档的语义是「Router 没给出正式的主模型」（旧数据，
    // 或者哪套模型都没到门槛），而它们只是还没算。它们在置顶分组里有自己的说明。
    if (isGated(s)) return;
    if (isUnrouted(s)) { unrouted += 1; return; }
    if (!seen.has(s.primary_model)) seen.set(s.primary_model, { count: 0 });
    seen.get(s.primary_model).count += 1;
  });
  if (!seen.has(state.model) && state.model !== 'all' && state.model !== UNROUTED) state.model = 'all';
  const rank = (id) => { const i = order.indexOf(id); return i < 0 ? order.length : i; };
  const groups = [...seen.keys()].sort((a, b) => rank(a) - rank(b))
    .map((id) => ({ id, label: modelLabel(id), count: seen.get(id).count }));
  const chip = (value, label, count) => `<button class="filter-chip ${state.model === value ? 'active' : ''}" data-model="${esc(value)}">${esc(label)}<em>${count}</em></button>`;
  const html = [chip('all', '全部', state.stocks.length)]
    .concat(groups.map((g) => chip(g.id, g.label, g.count)));
  if (unrouted) html.push(chip(UNROUTED, '未路由', unrouted));
  $('#type-seg').innerHTML = html.join('');
}
function sortValue(s, key) {
  const v = s.valuation || {}, a = s.attr_scores || {};
  return ({ total_score: s.total_score, value: a.value?.score, quality: a.quality?.score,
    price: v.price,
    // 调整后净现金 / 市值：**只读后端给的 canonical 值**（engine.net_cash_sort_value
    // 一处判定，列表和详情页取的是同一个数）。前端不许再写 `?? v.net_cash_ratio`
    // 这种兜底链——旧名那份可能还是应付票据改口径之前的数（华域 0.158 vs 现算
    // 0.4027，差 2.5 倍），兜底会悄悄把它当成同一个量排进序列里。
    net_cash_ratio: s.adjusted_net_cash_to_mcap,
    updated_at: s.last_updated_at })[key];
}
/* 列表里是不是混了旧口径的值，要让人看得见。后端在 net_cash_source 里标了
   每一行的取数来源（canonical / legacy_alias / missing），前端只负责显示。 */
function hasLegacyNetCash(rows) {
  return rows.some((s) => s.net_cash_source === 'legacy_alias');
}
function sorted() {
  return filtered().slice().sort((a, b) => {
    const av = sortValue(a, state.sort), bv = sortValue(b, state.sort);
    if (av == null) return bv == null ? 0 : 1;
    if (bv == null) return -1;
    return bv > av ? 1 : bv < av ? -1 : 0;
  });
}
/* 未通过资产审计的股票**独立置顶分组**，且不参与筛选与排序：
   1) 它们没有分数——排进「总评分」序列里会变成一个看起来正常的名次；
   2) 若只从 sorted() 里滤掉而不置顶，用户得翻到底才知道有股票被挡着。
   置顶组直接从 state.stocks 取（不走 filtered()）：搜索框、模型筛选都不影响它，
   否则「被挡死的股票」会随着一次搜索从页面上消失。
   两边互斥是硬要求——同一只股票在置顶组和正常组各出现一次，是列表最像故障的样子。 */
function gatedRow(s) {
  const v = s.valuation || {}, active = s.code === state.currentCode ? ' active' : '';
  const label = auditLabel(s);
  const reason = s.audit_status === 'FAILED' && s.audit_error ? ` title="${esc(s.audit_error)}"` : '';
  return `<tr class="${active} is-gated" data-code="${esc(s.code)}">
    <td><div class="stock-name">${esc(s.name || '名称缺失')}</div><div class="stock-code stock-line">${esc(s.code)} · <span class="route-none">未审计，不参与评分</span></div></td>
    <td class="num">${money(v.price)}</td>
    <td class="num"><span class="audit-chip ${esc(s.audit_status || 'UNAUDITED')}"${reason}>${esc(label)}</span></td>
    <td class="num library-change">${absent('—')}</td>
    <td><span class="muted">—</span></td>
  </tr>`;
}
function renderLibrary() {
  const gated = state.stocks.filter(isGated);
  const rows = sorted().filter((s) => !isGated(s));
  renderFilters();
  $('#library-count').textContent = gated.length
    ? `${rows.length + gated.length} 只（${gated.length} 只待审计）` : `${rows.length} 只`;
  const head = gated.length ? `<tr class="group-row"><td colspan="5">待审计 · ${gated.length} 只<em>资产审计通过之前不显示分数（那会是没有资产口径的另一套数）</em></td></tr>` : '';
  tbody.innerHTML = head + gated.map(gatedRow).join('') + rows.map((s) => {
    const v = s.valuation || {}, active = s.code === state.currentCode ? ' active' : '';
    // 主记录里只有旧口径值时给名字挂个星号。不挂的话，这个列表看起来是一条
    // 同口径的排名，实际上混了两种年份的值——排序结果本身看不出这一点。
    const legacy = s.net_cash_source === 'legacy_alias'
      ? '<i class="legacy-mark" title="「调整后净现金 / 市值」仍是 V1.2 之前的旧口径值，重新分析后更新">*</i>' : '';
    return `<tr class="${active}" data-code="${esc(s.code)}">
      <td><div class="stock-name">${esc(s.name || '名称缺失')}${legacy}</div><div class="stock-code stock-line">${esc(s.code)} · ${routeText(s)}${cohortMark(s)}</div></td>
      <td class="num">${money(v.price)}</td>
      <td class="num"><strong class="library-score">${score(s.total_score)}</strong>${legacyRuleMark(s, '旧')}</td>
      <td class="num library-change">${deltaScore(s.delta_score)}</td>
      <td>${riskBadge(s.risk_level)}</td>
    </tr>`;
  }).join('');
  $('#empty').hidden = rows.length + gated.length > 0;
  const label = $('#sort-label'), stale = hasLegacyNetCash(rows);
  label.textContent = (SORT_LABELS[state.sort] || '总评分') + (stale ? ' *' : '');
  label.title = stale
    ? '带 * 的股票，其「调整后净现金 / 市值」在主记录里只有 V1.2 之前的旧口径值（可能还是应付票据改口径之前算的），重新分析后自动更新'
    : '';
  document.querySelectorAll('[data-sort-option]').forEach((option) => option.setAttribute('aria-selected', String(option.dataset.sortOption === state.sort)));
  scheduleAuditPoll();
}

let searchTimer;
$('#search').addEventListener('input', () => {
  clearTimeout(searchTimer);
  const q = $('#search').value.trim();
  if (!q) { suggest.hidden = true; return; }
  searchTimer = setTimeout(() => search(q), 260);
});
$('#search').addEventListener('blur', () => setTimeout(() => { suggest.hidden = true; }, 180));
async function search(q) {
  try {
    const list = await api('/api/research/search?q=' + encodeURIComponent(q));
    suggest.innerHTML = list.slice(0, 8).map((item) =>
      `<button class="s-item" data-code="${esc(item.code)}"><span>${esc(item.code)}</span>${esc(item.name)}</button>`).join('');
    suggest.hidden = !list.length;
  } catch (_) { suggest.hidden = true; }
}
suggest.addEventListener('mousedown', (e) => {
  const item = e.target.closest('.s-item');
  if (item) { e.preventDefault(); $('#search').value = item.dataset.code; suggest.hidden = true; analyze(item.dataset.code, false); }
});
$('#btn-analyze').addEventListener('click', () => {
  const q = $('#search').value.trim();
  if (!q) { toast('请输入股票代码或名称'); return; }
  analyze((q.match(/\d{6}/) || [q])[0], false);
});
async function analyze(code, forceFinancials) {
  if (state.busy) return;
  state.busy = true; $('#btn-analyze').disabled = true;
  try {
    toast(forceFinancials ? '正在重新分析财务数据…' : '正在刷新研究结果…');
    const res = await api('/api/research/analyze', { method: 'POST', body: JSON.stringify({ code, force_financials: forceFinancials }) });
    await loadList(); await openDetail(code, true);
    // 首次研究一只**组合成员**、而三项里有缺时，后端把补录载荷挂在这一条响应上。
    // **只有这一条路径会自动弹**：库里已经有这一行之后再点分析，后端不会再给
    // 这个键（见 server.py 的 _pig_core_prompt），所以「刷新 / 重新评分 /
    // 重启服务都不再弹」不需要前端记任何状态。「暂不填写」= 什么都不发，
    // 股票早已由上面这次 analyze 正常落库。批量刷新（refreshAllPrices）走的
    // 也是这个接口，但它刷的都是库里已有的股票，所以不会弹出一串窗口。
    if (res && res.pig_core_prompt) openPigCoreDialog(res.pig_core_prompt);
  } catch (e) { toast(e.message, true); }
  finally { state.busy = false; $('#btn-analyze').disabled = false; }
}

/* ---------- 全库刷新价格 ---------- */
/* 逐只重跑 analyze，也就是详情页那个「↻ 刷新价格」，一次一只。
   为什么不做成「只换价格数字」的轻量刷新：评分吃的是 price / 市值 / PE / PB
   （build_metrics 从东财行情一次取全），而腾讯那条批量接口只给 name/price——
   拿它刷一遍，库里会留下「新价格 + 旧市值」的混口径记录。
   为什么循环写在前端而不加批量端点：串行、逐只进度、门禁与 _persist 的复用全是现成的，
   服务端一行都不用动；ThreadingHTTPServer 每请求一个线程，跑的时候页面照样能点。 */
const REFRESH_ALL_LABEL = '↻ 刷新全部价格';
const REFRESH_ALL_EVERY = 5;     // 每 5 只重画一次列表；进度主要看按钮文字，列表少动几次
async function refreshAllPrices() {
  const btn = $('#btn-refresh-all');
  if (state.busy || state.bulkBusy) return;      // 串行：单只分析在跑就不插手
  const codes = state.stocks.map((s) => s.code);
  if (!codes.length) { toast('研究库为空'); return; }
  const before = new Map(state.stocks.map((s) => [s.code, (s.valuation || {}).price]));
  // 一并占住 analyze() 自己那道闸门：批量期间点单只不会插进来（analyze 一个字没改）
  state.bulkBusy = true; state.busy = true;
  btn.disabled = true; $('#btn-analyze').disabled = true;
  const failed = [];
  try {
    for (let i = 0; i < codes.length; i++) {
      btn.textContent = `刷新中 ${i + 1}/${codes.length}`;   // toast 2.6 秒就没了，撑不住十几秒
      try {
        await api('/api/research/analyze', { method: 'POST', body: JSON.stringify({ code: codes[i], force_financials: false }) });
      } catch (e) { failed.push(`${codes[i]}（${e.message}）`); }   // 一只失败不停下
      if ((i + 1) % REFRESH_ALL_EVERY === 0) await loadList();
    }
    await loadList();          // renderLibrary() 末尾会重排审计轮询
    await reloadDetail();      // 详情开着的那只跟着更新（没开详情它自己 return）
    const changed = state.stocks.filter((s) => before.get(s.code) !== (s.valuation || {}).price).length;
    if (failed.length) toast(`已刷新 ${codes.length - failed.length} 只，${failed.length} 只失败：${failed.join('、')}`, true);
    else toast(`已刷新 ${codes.length} 只（${changed} 只价格有变动）`);
  } finally {
    state.bulkBusy = false; state.busy = false;    // 不放闸门会永久卡住全站
    btn.disabled = false; btn.textContent = REFRESH_ALL_LABEL;
    $('#btn-analyze').disabled = false;
  }
}
$('#btn-refresh-all').addEventListener('click', refreshAllPrices);

/* ---------- 详情框架 ---------- */
async function openDetail(code, preserveTab = false) {
  state.currentCode = code;
  if (!preserveTab) state.currentTab = '概览';
  state.scoreOpen = new Set(['growth']);
  state.sim = null;
  state.auditOpen = new Set();
  state.historyLegacyOpen = false;
  try {
    state.currentDetail = await api('/api/research/detail?code=' + encodeURIComponent(code));
    drawer.hidden = false; $('#detail-empty').hidden = true;
    // 未通过资产审计：没有页签可点，整屏只有状态（用户明确要求：审计中详情页
    // 不能看，只显示状态）。后端那边这些字段本身也没发过来，这里是第二道。
    drawerTabs.hidden = isGated(state.currentDetail);
    renderDetailHead(); renderTabs(); renderDetailBody(); renderLibrary();
    history.replaceState(null, '', '/static/research.html?stock=' + encodeURIComponent(code));
  } catch (e) { toast(e.message, true); }
}
function closeDetail() {
  drawer.hidden = true; $('#detail-empty').hidden = false;
  state.currentCode = null; state.currentDetail = null; state.sim = null;
  clearAuditPoll();
  history.replaceState(null, '', '/static/research.html'); renderLibrary();
}
/* ---------- 审计状态（未通过审计时详情页的全部内容） ---------- */
const AUDIT_POLL_MS = 4000;
let auditTimer = null;
function clearAuditPoll() { clearTimeout(auditTimer); auditTimer = null; }
/* 只在**有股票正在审计**时轮询（自续的 setTimeout 链，不用 setInterval）：
   「未审计」「审计失败」不会自己变好，定时去问只是白跑。列表一重画就重新排一次，
   所以审计完成的下一刻列表自己就更新了，不需要用户手动刷新。 */
function scheduleAuditPoll() {
  clearAuditPoll();
  if (!auditPending() && !(state.currentDetail && state.currentDetail.audit_status === 'RUNNING')) return;
  auditTimer = setTimeout(async () => {
    auditTimer = null;
    await reloadDetail();
    await loadList();                    // 末尾会再排一次，链子自己接着走
  }, AUDIT_POLL_MS);
}
async function reloadDetail() {
  const code = state.currentCode;
  if (!code) return;
  let d;
  try { d = await api('/api/research/detail?code=' + encodeURIComponent(code)); } catch (_) { return; }
  if (state.currentCode !== code || !d) return;
  const wasGated = isGated(state.currentDetail);
  state.currentDetail = d;
  drawerTabs.hidden = isGated(d);
  if (wasGated && !isGated(d)) toast('资产审计已完成，已恢复完整研究结果');
  renderDetailHead(); renderTabs(); renderDetailBody();
}
function renderAuditStatePanel(s, opts = {}) {
  const status = (s && s.audit_status) || 'UNAUDITED';
  const label = (s && auditLabel(s)) || '未审计';
  const why = {
    UNAUDITED: '这只股票已经排进审计队列，还没有开始跑。',
    RUNNING: '正在解析它的定期报告：下载 PDF → 抽版面 → 按经济实质分类 → 计算资产指标。首次审计要几分钟。',
    FAILED: '审计没有跑出可用的口径。原因见下。',
    OK: '审计已完成，重新读取中…',
  }[status] || '';
  const err = s && s.audit_error
    ? `<p class="footnote warn">${esc(s.audit_error)}</p>` : '';
  // 按钮只挂 data 属性、由文档级那个委托监听统一接管：详情页的头部和正文两处
  // 都会渲染它，各自直接绑定的话同一次点击会被处理两遍（跑两遍审计）。
  const retry = status === 'RUNNING'
    ? '<button class="btn btn-sm" disabled>正在审计…</button>'
    : `<button class="btn btn-sm btn-primary app-button" data-audit-retry="${esc(opts.retry || 'run')}">重试资产审计</button>`;
  const body = `<div class="state-panel">
    <div class="state-hero"><span class="audit-chip ${esc(status)}">${esc(label)}</span>
      <div><b>资产审计尚未通过，本页不显示任何评分</b>
      <span>${esc(why)}</span></div></div>
    ${err}
    <p class="footnote">为什么宁可空着也不先给分：资产语义层没跑过时，评分分母里少掉整批资产类分量——实测同一价格下切换资产层，总分动 −11.55 ~ +38.85。那样的分数是另一套口径的数，与审计之后的分数不是同一个量，放在一起看会得出反向的结论。审计完成后这一页自动恢复。</p>
    <div class="state-actions">${retry}</div>
  </div>`;
  return section(opts.title || '资产审计', `${esc(label)}｜本页只显示状态，不显示分数与明细`, body, 'audit-card');
}
function renderGatedDetailHead(s) {
  const v = s.valuation || {};
  drawerHead.innerHTML = `<div class="detail-title">
    <div class="detail-ident"><h2>${esc(s.name || '名称缺失')}</h2><span>${esc(s.code)}</span><em class="detail-model none">${esc(auditLabel(s))}</em></div>
    <div class="quote-line"><strong>${money(v.price)}</strong><span>研究评分 <b>${absent('审计通过后才计算')}</b></span>
      <span class="updated">更新于 ${period(s.last_updated_at)}</span></div>
  </div>
  <div class="drawer-actions">${
    (s.audit_status || 'UNAUDITED') === 'RUNNING'
      ? '<button class="btn btn-sm" disabled>正在审计…</button>'
      : '<button class="btn btn-sm btn-primary app-button" data-audit-retry="run">重试资产审计</button>'
  }<button class="icon-btn" id="d-close" title="关闭详情">×</button></div>`;
  $('#d-close').addEventListener('click', closeDetail);
}
function renderDetailHead() {
  const s = state.currentDetail;
  if (isGated(s)) { renderGatedDetailHead(s); return; }
  const v = s.valuation || {};
  // 顶部只突出「主模型」这一套分类。原 system_type（画像）不再出现在这里——
  // 两套「类型」并列会让人以为它们是同一层东西（§23 / 用户明确要求）。
  const route = s.route || {};
  const chips = [];
  if (!isUnrouted(s)) {
    const fit = bad(s.primary_fit) ? '' : ` · 适配度 ${Number(s.primary_fit).toFixed(0)}%`;
    chips.push(`<em class="detail-model">${esc(s.primary_model_label || s.primary_model)}${fit}</em>`);
    if (s.secondary_model) {
      const sfit = bad(s.secondary_fit) ? '' : ` ${Number(s.secondary_fit).toFixed(0)}%`;
      chips.push(`<em class="detail-model secondary">次模型 ${esc(s.secondary_model_label || s.secondary_model)}${sfit}</em>`);
    }
  } else {
    // 未路由也要交代「差多少」：只写「未路由」的话，用户看不出是数据缺失、
    // 还是差一点点。适配度最高的那个候选一并报出来。
    const best = topCandidate(route);
    const bestFit = best ? ` · 最高适配度 ${Number(best.fit).toFixed(0)}%` : '';
    chips.push(`<em class="detail-model none">未路由${bestFit}</em>`);
  }
  // 组合标记排在模型 chip 之后：它回答「这家落在哪个已建证据的行业组合里」，与
  // 「用哪套模型看它」是两层。中文名同样只读 payload，前端不留第二份。
  if (s.cohort_label) chips.push(`<em class="detail-cohort">${esc(s.cohort_label)}</em>`);
  drawerHead.innerHTML = `<div class="detail-title">
    <div class="detail-ident"><h2>${esc(s.name || '名称缺失')}</h2><span>${esc(s.code)}</span>${chips.join('')}</div>
    <div class="quote-line"><strong>${money(v.price)}</strong><span>研究评分 <b>${score(s.total_score)}</b>${legacyRuleMark(s, '旧结果')}</span>${routeStatusChip(s)}${riskBadge(s.risk_level)}
      <span class="updated">数据覆盖率 ${bad(s.route_coverage) ? '—' : `${(Number(s.route_coverage) * 100).toFixed(0)}%`} · 更新于 ${period(s.last_updated_at)}</span></div>
  </div>
  <div class="drawer-actions"><button class="btn btn-sm" id="d-refresh-price">↻ 刷新价格</button><button class="btn btn-sm" id="d-refresh-fin">重新分析财务</button><button class="icon-btn" id="d-close" title="关闭详情">×</button></div>`;
  $('#d-refresh-price').addEventListener('click', () => analyze(state.currentCode, false));
  $('#d-refresh-fin').addEventListener('click', () => analyze(state.currentCode, true));
  $('#d-close').addEventListener('click', closeDetail);
}
function renderTabs() {
  normalizeTab();
  drawerTabs.innerHTML = TABS.filter((tab) => tab !== PIG_TAB || pigTabVisible()).map((tab) => `<button class="drawer-tab ${tab === state.currentTab ? 'active' : ''}" data-tab="${tab}">${tab}</button>`).join('');
}
function renderDetailBody() {
  normalizeTab();
  if (isGated(state.currentDetail)) {
    drawerBody.innerHTML = renderAuditStatePanel(state.currentDetail);
    drawerBody.scrollTop = 0;
    return;
  }
  drawerBody.innerHTML = renderTab();
  drawerBody.scrollTop = 0;
  if (state.currentTab === '价格模拟') setTimeout(() => { if (!state.sim) runDefaultSimulation(); }, 0);
  if (state.currentTab === '资产审计') setTimeout(() => { if (state.assetsCode !== state.currentCode) loadAssetAudit(); }, 0);
  if (state.currentTab === PIG_TAB) setTimeout(() => { if (state.pigCode !== state.currentCode) loadPigIndustry(); }, 0);
}
function renderTab() {
  const s = state.currentDetail;
  normalizeTab();
  switch (state.currentTab) {
    case '概览': return renderOverview(s);
    case '评分细则': return renderScoreDetails(s);
    case '财务趋势': return renderFinancialTrends(s);
    case '资产负债表': return renderBalance(s);
    case '资产审计': return renderAssetAudit(s);
    case PIG_TAB: return renderPigIndustry(s);
    case '现金流': return renderCashflow(s);
    case '风险': return renderRisk(s);
    case '历史评分': return renderHistory(s);
    case '价格模拟': return renderSimulation(s);
    default: return '';
  }
}

/* ---------- 通用组件 ---------- */
function section(title, subtitle, content, cardClass = '') {
  return `<section class="research-card app-card ${cardClass}"><div class="card-head"><div><h3>${title}</h3>${subtitle ? `<p>${subtitle}</p>` : ''}</div></div>${content}</section>`;
}
function metricCard(label, value, sub = '', note = '') {
  return `<div class="metric-card app-card"${note ? ` title="${esc(note)}"` : ''}><div class="metric-label">${label}</div><strong>${value}</strong>${sub ? `<small>${sub}</small>` : ''}</div>`;
}
/* 一组带标题的卡片。清算口径的四个指标分两组摆，因为它们的减项不是一回事：
   资产价值组减的是零/全部负债，资本结构组减的是有息负债。挤在一行里没法写
   公式，而这一页此前的问题正是「数值按全部负债算，文案写扣有息负债」。 */
function metricGroup(title, hint = '', cards = [], columns = 2) {
  return `<h4 class="subsection-title">${esc(title)}${hint ? ` <span class="muted">（${esc(hint)}）</span>` : ''}</h4><div class="metric-grid group-${columns}">${cards.join('')}</div>`;
}
/* 取值来源：证据层要能看出哪个数不是财报直接披露的 */
const ORIGIN_LABELS = {
  reported: '财报披露值', derived: '由披露值计算', inferred_zero: '依 Provider 的 null 语义推断为 0',
  fallback: '备用来源', estimated: '估计值', missing: '缺失',
};
function originNote(m) {
  const label = ORIGIN_LABELS[m?.value_origin];
  return label ? `取值来源：${label}` : '';
}
/* 概览统一口径卡片：值 + 报告期/口径后缀，不同口径不挤在同一格 */
function ovCard(label, m, fmt) {
  const ok = m && !bad(m.value);
  return metricCard(label, ok ? fmt(m.value) : absent(), ok ? (m.basis || '') : '', originNote(m || {}));
}
/* 时点类报告期标签，与后端 series.point_label 保持一致 */
function basisLabel(date) {
  const match = String(date || '').match(/^(\d{4})-(\d{2})/);
  if (!match) return '';
  return `${match[1]}${{ '03': 'Q1', '06': 'H1', '09': 'Q3', '12': 'FY' }[match[2]] || ''}`;
}
function bar(value, max = 100, tone = 'blue') {
  if (bad(value) || bad(max) || Number(max) <= 0) return '<span class="bar-missing">数据缺失</span>';
  return `<span class="score-bar ${tone}"><i style="width:${Math.max(0, Math.min(100, Number(value) / Number(max) * 100))}%"></i></span>`;
}
function svgChart(series, opts = {}) {
  const clean = series.map((s) => ({ ...s, values: (s.values || []).map((v) => bad(v) ? null : Number(v)) })).filter((s) => s.values.some((v) => v !== null));
  if (!clean.length) return '<div class="chart-empty">历史数据不足，暂无法绘制趋势图。</div>';
  const points = Math.max(...clean.map((s) => s.values.length));
  if (points < 2) return '<div class="chart-empty">至少需要两个报告期，才能展示趋势图。</div>';
  const all = clean.flatMap((s) => s.values).filter((v) => v !== null);
  let min = Math.min(...all), max = Math.max(...all);
  if (min === max) { min -= Math.abs(min || 1) * .15; max += Math.abs(max || 1) * .15; }
  const width = Number(opts.width) || 1200, height = Number(opts.height) || 360;
  const left = opts.left || (width <= 640 ? 54 : 72), right = width <= 640 ? 20 : 28;
  const top = 24, bottom = 48, w = width - left - right, h = height - top - bottom;
  const x = (i) => left + (points === 1 ? w / 2 : i * w / (points - 1));
  const y = (v) => top + (max - v) * h / (max - min);
  const grid = Array.from({ length: 5 }, (_, i) => { const yy = top + i * h / 4; const val = max - i * (max - min) / 4; return `<path d="M${left} ${yy}H${width - right}" class="chart-grid"/><text x="2" y="${yy + 4}" class="axis-label">${opts.formatY ? opts.formatY(val) : val.toFixed(1)}</text>`; }).join('');
  const labels = (opts.labels || []).slice(-points);
  const xlabels = labels.map((label, i) => `<text x="${x(i)}" y="${height - 8}" text-anchor="middle" class="axis-label">${esc(String(label).slice(0, 8))}</text>`).join('');
  const lines = clean.map((s, n) => {
    const coords = s.values.map((v, i) => v === null ? null : `${x(i)},${y(v)}`).filter(Boolean).join(' ');
    const color = s.color || COLORS[n % COLORS.length];
    const dots = s.values.map((v, i) => v === null ? '' : `<circle cx="${x(i)}" cy="${y(v)}" r="3.5" fill="${color}"/>`).join('');
    return `<polyline points="${coords}" fill="none" stroke="${color}" stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round"/>${dots}`;
  }).join('');
  const legend = clean.map((s, i) => `<span><i style="background:${s.color || COLORS[i % COLORS.length]}"></i>${esc(s.name)}</span>`).join('');
  return `<div class="chart-legend">${legend}</div><svg class="line-chart" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}" preserveAspectRatio="xMidYMid meet" role="img" aria-label="${esc(opts.label || '趋势图')}">${grid}${lines}${xlabels}</svg>`;
}
function trendSeries(s, key) {
  const series = s.financial_series?.indicators?.[key] || [];
  if (series.length) return series;
  const fallback = s.financial?.[key] || [];
  const report = String(s.latest_report_period || '');
  const match = report.match(/^(\d{4})-(\d{2})-\d{2}$/);
  if (!match) return fallback.map((value) => ({ date: '', value }));
  const latestAnnualYear = Number(match[1]) - (match[2] === '12' ? 0 : 1);
  return fallback.map((value, i) => ({ date: `${latestAnnualYear - fallback.length + i + 1}-12-31`, value }));
}
function dateLabels(series) {
  return series.map((p) => {
    const date = String(p.date || '');
    const match = date.match(/^(\d{4})-(\d{2})/);
    if (!match) return '报告期缺失';
    return match[2] === '06' ? `${match[1]}H1` : match[1];
  });
}
function yoy(values) { return values.map((v, i) => i && !bad(v) && !bad(values[i - 1]) && Number(values[i - 1]) !== 0 ? (Number(v) / Number(values[i - 1]) - 1) * 100 : null); }

/* ---------- 模型路由（MODEL_ROUTER_V1） ---------- */
/* 这一块只讲「该用哪套模型看它」。它与下面的「最终投资评分结构」是两件事：
   适配度（fit）不是投资价值，画像分也不是。 */
function renderRouteCard(s) {
  const route = s.route;
  const status = String(s.route_status || '');
  const fit = (v) => (bad(v) ? absent('无适配度') : `${Number(v).toFixed(1)} / 100`);
  const r2 = (v) => (bad(v) ? '数据缺失' : `${(Number(v) * 100).toFixed(0)}%`);
  // 未路由时不许把兜底模型的 id 当主模型展示：那一格写「未路由」，并把适配度
  // 最高的候选报出来——用户要看到的是「哪套模型最接近、差多少」，而不是一个
  // 看起来像正式结论的「通用价值 V2」。
  const unrouted = isUnrouted(s);
  const noRoute = !s.primary_model;      // 连路由结果都没有（Router 上线之前的旧数据）
  const best = topCandidate(route);
  const gate = num(route && route.thresholds && route.thresholds.fit_entry, 65);
  const bestLine = best ? `最高：${esc(best.label)} ${Number(best.fit).toFixed(1)} / 100` : '';
  const cards = [
    metricCard('主模型', unrouted ? absent('未路由') : esc(s.primary_model_label || s.primary_model),
      noRoute ? '该项目没有路由结果' : unrouted ? bestLine : esc(s.primary_model)),
    metricCard('主模型适配度', unrouted ? absent('未达门槛') : fit(s.primary_fit),
      noRoute ? '该项目没有路由结果'
        : unrouted ? `没有模型达到 ${gate} 分，评分模板按画像判型兜底`
          : bad(s.primary_fit) ? '兜底模型没有适配度' : '≥ 65 才允许正式路由'),
    metricCard('次模型', s.secondary_model ? esc(s.secondary_model_label || s.secondary_model) : '无', s.secondary_model ? fit(s.secondary_fit) : '没有第二个模型形成竞争'),
    metricCard('路由状态', s.route_status_label ? esc(s.route_status_label) : absent('未路由'), status || ''),
    metricCard('数据覆盖率', r2(s.route_coverage), '路由输入的有效权重占比'),
    metricCard('路由版本', text(s.router_version, '版本缺失'), ''),
  ].join('');
  if (!route) {
    return section('模型路由', '该项目在 Router 上线前分析，没有路由结果；重新分析即可生成',
      `<div class="metric-grid">${cards}</div><div class="empty-inline">旧数据保持原样，不补算、不回填。</div>`, 'route-card');
  }

  const cands = route.candidates || [];
  const candRows = cands.map((c) => {
    const role = c.role === 'primary' ? '<em class="route-role primary">主模型</em>'
      : c.role === 'secondary' ? '<em class="route-role secondary">次模型</em>' : '';
    const state = c.enabled ? '<span class="route-on">已启用</span>'
      : `<span class="route-off">未启用（${esc(c.implementation_status)}）</span>`;
    return `<div class="route-cand-row${c.enabled ? '' : ' is-off'}">
      <span>${esc(c.label)}${role}</span><span>${fit(c.fit)}</span><span>${r2(c.coverage)}</span><span>${state}</span></div>`;
  }).join('') || '<div class="empty-inline">没有模型算得出适配度。</div>';
  const th = route.thresholds || {};
  const candsBlock = `<h4 class="subsection-title">候选模型适配度</h4>
    <div class="route-cand-head"><span>模型</span><span>适配度</span><span>覆盖率</span><span>状态</span></div>
    <div class="route-cands">${candRows}</div>
    <p class="route-note">阈值：适配度 ≥ ${num(th.fit_entry, 0)} 才允许正式路由；次席 ≥ ${num(th.secondary, 0)} 且差距 &lt; ${num(th.hybrid_gap, 0)} 判为混合；覆盖率低于 ${r2(th.min_coverage)} 不输出路由。</p>`;

  const reasons = (route.reasons || []).map((r) => `<li>${esc(r)}</li>`).join('');
  const reasonBlock = reasons ? `<h4 class="subsection-title">路由理由</h4><ul class="route-reasons">${reasons}</ul>` : '';

  return section('模型路由', '回答「该用哪套模型看它」。适配度衡量适配程度，不是投资价值',
    `<div class="metric-grid">${cards}</div>${candsBlock}${reasonBlock}`, 'route-card');
}

/* 「周期位置」的 4 个子分量。模板分量默认不序列化子明细，所以这是后端专门补进
   主记录的那一份（见 engine._with_cyclical_breakdown）；渲染完全照抄「评分细则」
   的 .score-row 四格，单位表在后端 rules.COMPONENT_UNITS 里，前端不按指标名猜
   量纲，也**不写死**子分量名与满分——那两个都从 components 自己取。

   「行业盈利状态」的单位是 text，rawValue 会原样输出后端算好的
   后端算好的「期间 + 档位」串——档位由评分层算、这里只显示，前端**不复制**
   那张阈值表。 */
function cyclicalSubRows(components) {
  return components.map((c) => {
    // 口径脚注：分位类的样本到底有多长（几个月、起止、当期值是哪一天）。后端算好
    // 下发，前端不留第二份；没有 sample 的行一律不显示脚注——「数据缺失」和
    // 「算出来了但样本短」在页面上必须长得不一样。
    const foot = c.sample ? `<p class="footnote">${esc(c.sample)}</p>` : '';
    if (c.missing || bad(c.score)) return `<div class="score-row missing"><span>${esc(c.name)}</span><span>${absent('数据缺失')}</span><span class="status-text">未纳入评分</span><strong>${absent('不适用')}</strong></div>${foot}`;
    const progress = bad(c.max) || !c.max ? 0 : Math.max(0, Math.min(100, Number(c.score) / Number(c.max) * 100));
    return `<div class="score-row" title="${esc(c.reason || c.note || '')}"><span>${esc(c.name)}</span><span>${rawValue(c)}</span><span>${bar(progress, 100)}<small>${progress.toFixed(0)}%</small></span><strong>${Number(c.score).toFixed(1)} / ${c.max}</strong></div>${foot}`;
  }).join('');
}

/* 「已建证据的行业组合」标记（见后端 industry_margin.COHORTS）。中文名一律读
   payload 的 cohort_label，前端不留第二份——照 route_status_label 的先例。 */
function cohortMark(s) {
  return s.cohort_label ? `<span class="cohort-chip">${esc(s.cohort_label)}</span>` : '';
}

/* 行业盈利状态证据卡的内容。只报**实测事实**：期间 / 加权毛利率 / 覆盖 / 集中度 /
   逐家页码 / 谁没纳入。档位与得分不在这里——那是评分层的决定，写在「周期位置」
   的展开行里。依据是 industry_margin.to_dict() 刻意不含 score / band。 */
function industryMarginBlock(im) {
  const members = im.members || [], failures = im.failures || {};
  const top = bad(im.top_weight) ? null : Number(im.top_weight);
  const topName = members.length ? (members[0].name || members[0].stock_code) : '';
  const concentrated = top !== null && top > 0.8 && topName;
  const rows = members.map((m) => `<tr><td>${esc(m.name || m.stock_code)}</td><td>${esc(m.stock_code)}</td><td>${period(m.period_end)}</td><td>${amount(m.revenue)}</td><td>${bad(m.margin) ? absent() : percent(m.margin)}</td><td>${bad(m.source_page) ? absent() : `p.${num(m.source_page, 0)}`}</td></tr>`).join('');
  // 98% 来自一家不能叫「全行业」——集中度超阈值必须点名，否则这张卡就在替一家
  // 公司冒充行业。阈值与后端 CONCENTRATION_WARN 同值，只用于「说不说这句」。
  const coverSub = bad(im.coverage) ? '占组合比例缺失'
    : `占组合 ${(Number(im.coverage) * 100).toFixed(0)}%${concentrated ? ` · 约等于${esc(topName)}一家` : ''}`;
  const cards = [
    metricCard('报告期', text(im.period, '期间缺失'), im.period_end ? `截至 ${period(im.period_end)}` : ''),
    metricCard('组合加权毛利率', bad(im.weighted_margin) ? absent('证据不足') : percent(im.weighted_margin), '按分产品收入加权，不是简单平均'),
    metricCard('覆盖', members.length ? `${members.length} 家` : absent('无'), coverSub),
  ].join('');
  const table = rows
    ? `<div class="table-scroll"><table class="data-table"><thead><tr><th>公司</th><th>代码</th><th>报告期</th><th>分产品收入</th><th>毛利率</th><th>页码</th></tr></thead><tbody>${rows}</tbody></table></div>`
    : '<div class="empty-inline">这一期没有任何一家有可用证据。</div>';
  const failLine = Object.entries(failures).length
    ? `<p class="footnote">未纳入这一期：${Object.entries(failures).map(([c, why]) => `${esc(c)}（${esc(why)}）`).join('；')}</p>` : '';
  return `<div class="metric-grid group-3">${cards}</div>
    <h4 class="subsection-title">逐家口径（各公司年报「生猪」分产品，不含范围更宽的「猪产业」）</h4>${table}${failLine}
    <p class="footnote">毛利率由 (分产品收入 − 成本) / 收入 现算，与表内印的百分比可能有微小差异；这类科目属已披露但未经审计意见逐项覆盖，所以卡片保留页码供回查。</p>`;
}

/* ---------- 概览 ---------- */
function renderOverview(s) {
  const v = s.valuation || {}, attr = s.attr_scores || {}, cat = s.category_scores || {}, risk = s.risk || {};
  const totalScore = bad(s.total_score) ? null : Math.max(0, Math.min(100, Number(s.total_score)));
  const comps = (cat.components || []).filter((c) => !c.missing && !bad(c.score));
  const contribution = comps.map((c) => {
    const row = `<div class="contrib-row"><span>${esc(COMP_LABELS[c.key] || c.key)}</span>${bar(c.score)}<strong>${score(c.score)}</strong><small>× ${(Number(c.weight || 0) * 100).toFixed(0)}%</small><b>${(Number(c.score) * Number(c.weight || 0)).toFixed(1)}</b></div>`;
    // 「周期位置」就地展开出它的 4 个子分量。模板分量默认**不序列化子明细**，所以
    // 只有后端往主记录补过这一层才可展开：非周期模板、以及还没被重写过的旧记录
    // 一律走上面那一行原样，不给一个点开是空的箭头。
    if (c.key !== 'cyclical_position' || !Array.isArray(c.components) || !c.components.length) return row;
    const open = state.scoreOpen.has('cyclical_position');
    // 提示行里的名字与满分从 components 自己取，不在 JS 里写死某一格的名字和
    // 满分——那样 rules 一改名或改满分，这里就开始说谎。
    const hint = c.components.map((x) => `${esc(x.name)} ${bad(x.max) ? '—' : x.max}`).join(' / ');
    return `<section class="score-accordion contrib-group${open ? ' is-open' : ''}">${row}` +
      `<button class="score-accordion-head contrib-toggle" type="button" data-score-toggle="cyclical_position" aria-expanded="${open}">` +
      `<span>周期位置 · ${c.components.length} 个子分量</span><em>${hint}</em><b></b><i aria-hidden="true">⌄</i></button>` +
      (open ? `<div class="score-accordion-content">${cyclicalSubRows(c.components)}</div>` : '') + '</section>';
  }).join('') || '<div class="empty-inline">评分分量数据不足。</div>';
  // 模板跟着主模型走（router 的 primary_model 是唯一入口），所以这里必须写明
  // 模板是谁定的：正式路由定的，还是未路由时按画像判型兜底的。不写明的话，
  // 用户看到「采用周期价值型模板」会以为这就是 Router 的结论。
  const templateSource = cat.template_source === 'route'
    ? `主模型 ${esc(modelLabel(cat.template_model) || cat.template_model || '')}`
    : '未路由，按画像判型兜底';
  const structure = section('最终投资评分结构', `采用 ${esc(cat.template || '当前')} 模板（${templateSource}）；贡献值为分项得分 × 有效权重`,
    `<div class="score-summary"><div class="score-orbit${totalScore === null ? ' is-missing' : ''}" style="--score-progress:${totalScore === null ? 0 : (totalScore * 3.6).toFixed(2)}deg"><strong>${score(s.total_score)}</strong><span>/ 100</span></div><div class="score-context"><b>${text(cat.template, '模板缺失')}</b><span>指标覆盖率 ${bad(cat.completeness) ? '数据缺失' : (Number(cat.completeness) * 100).toFixed(0) + '%'}</span><span>评分变化 ${delta(s.delta_score)}</span></div></div><div class="contrib-head"><span>模块</span><span>状态 / 得分</span><span>得分</span><span>权重</span><span>贡献</span></div><div class="contrib-list">${contribution}</div>`);
  // 行业盈利状态证据卡：后端**只在已建证据的行业**写 financial.industry_margin，
  // 所以没有建过证据的 23 只连卡片都不出现，不存在一张写着「无数据」的空卡。
  const im = (s.financial || {}).industry_margin;
  const industryCard = im ? section('行业盈利状态',
    '行业级证据，只回答「行业在周期的哪个位置」；公司维度的成本优势（完全成本、头均利润）仍缺口径，装不进这一格',
    industryMarginBlock(im)) : '';
  // 「画像」这一块的语义已经降到「路由的输入」：8 个属性分送给 Router 算适配度，
  // 它们自己不再回答「算哪套模型」——所以**不再有「主画像」那一格**。留着它，
  // 页面上就会同时出现两个互相竞争的「类型」：一个说它是周期股，一个说它算通用
  // 价值，用户没法判断哪个才是结论。算哪套模型只看左边的模型路由。
  const portrait = section('股票画像', '8 个属性分是路由的输入；算哪套模型看「模型路由」，值多少看「最终投资评分结构」',
    `<div class="portrait-meta"><div><span>行业</span><b>${text(s.industry, '行业数据缺失')}</b></div><div><span>市值</span><b>${amount(v.total_market_cap)}</b></div><div><span>风险等级</span>${riskBadge(s.risk_level)}</div></div>
     <div class="attribute-grid">${Object.keys(TYPE_LABELS).map((key) => `<div><span>${TYPE_LABELS[key]}</span><b>${attr[key] ? score(attr[key].score) : absent('数据不足')}</b>${attr[key] ? bar(attr[key].score) : ''}</div>`).join('')}</div>`);
  // 统一口径：流量/盈利类走 TTM（真实滚动 12 个月），时点类走最新一期资产负债表。
  // 每格都把口径与报告期显示出来，不再让不同口径共用一个格子。
  const o = v.overview || {};
  const trendLast = (key) => { const arr = trendSeries(s, key); return arr.length ? arr[arr.length - 1] : null; };
  const revLast = trendLast('revenue'), npLast = trendLast('net_profit');
  const metrics = [
    ovCard('PE(TTM)', o.pe_ttm, multiple),
    ovCard('ROE(TTM)', o.roe_ttm, ratio),
    ovCard('ROIC(TTM)', o.roic_ttm, ratio),
    ovCard('毛利率(TTM)', o.gross_margin_ttm, percent),
    ovCard('FCF 收益率(TTM)', o.fcf_yield_ttm, ratio),
    ovCard('资产负债率', o.debt_asset_ratio, percent),
    // 这两格是**一级财务科目口径**（类现金 − 报表有息负债），与评分/排序/烟蒂
    // 用的「调整后净现金/市值」是两个不同的数（华域 0.16 vs 0.40，方向都可能相反）。
    // 所以名字里必须写死「报表口径」，不许再和「调整后净现金」共用中文名。
    // 旧键兜底：V1.2 之前的快照只有 net_cash / net_cash_to_mcap。
    ovCard('报表口径净现金', o.reported_net_cash ?? o.net_cash, amount),
    ovCard('报表口径净现金 / 市值', o.reported_net_cash_to_mcap ?? o.net_cash_to_mcap, ratio),
    ovCard('PB 对应净资产', o.book_value, amount),
    metricCard('PB', multiple(v.pb)),
    metricCard('营收', amount(revLast?.value), revLast ? basisLabel(revLast.date) : ''),
    metricCard('净利润', amount(npLast?.value), npLast ? basisLabel(npLast.date) : ''),
  ];
  const metricBlock = section('关键指标', '流量/盈利类为 TTM（滚动 12 个月），时点类为最新一期资产负债表；每格标注口径与报告期',
    `<div class="metric-grid">${metrics.join('')}</div>`);
  const riskBlock = section('风险速览', '风险算法结果仅供研究参考', risk.flags?.length
    ? `<div class="risk-preview">${risk.flags.map((f) => `<div class="risk-item"><span class="risk-dot ${esc(f.level)}"></span><div><b>${esc(f.type)}</b><p>${esc(f.detail || '未提供说明')}</p></div><em>${riskLabel(f.level, '需关注')}</em></div>`).join('')}</div>`
    : `<div class="risk-ok"><b>✓ ${riskBadge(risk.level)}</b><span>当前规则未识别出结构化风险信号；这不代表不存在投资风险。</span></div>`);
  const context = section('研究数据状态', '展示当前研究结果的来源范围，不以空白替代缺失信息', `<div class="research-status-grid">${metricCard('最新报告期', period(s.latest_report_period))}${metricCard('指标覆盖率', bad(cat.completeness) ? absent() : `${(Number(cat.completeness) * 100).toFixed(0)}%`)}${metricCard('规则版本', text(s.rule_version, '版本缺失') + legacyRuleMark(s, ' · 旧实验结果'))}${metricCard('最近更新', period(s.last_updated_at))}</div>`, 'overview-context');
  return `<div class="overview-layout"><div class="overview-primary">${renderRouteCard(s)}${structure}${industryCard}${metricBlock}</div><div class="overview-secondary">${portrait}${riskBlock}</div></div>${context}`;
}
function lastValue(series) { return series.length ? series[series.length - 1].value : null; }

/* ---------- 评分细则 ---------- */
function renderScoreDetails(s) {
  const attrs = s.attr_scores || {};
  const list = Object.keys(TYPE_LABELS).map((key) => {
    const item = attrs[key]; if (!item) return '';
    const open = state.scoreOpen.has(key);
    const coverage = bad(item.completeness) ? '覆盖率数据缺失' : `覆盖率 ${(Number(item.completeness) * 100).toFixed(0)}%`;
    const rows = (item.components || []).map((c) => {
      if (c.missing || bad(c.score)) return `<div class="score-row missing"><span>${esc(c.name)}</span><span>${absent('数据缺失')}</span><span class="status-text">未纳入评分</span><strong>${absent('不适用')}</strong></div>`;
      const progress = bad(c.max) || !c.max ? 0 : Math.max(0, Math.min(100, Number(c.score) / Number(c.max) * 100));
      return `<div class="score-row" title="${esc(c.note || '')}"><span>${esc(c.name)}</span><span>${rawValue(c)}</span><span>${bar(progress, 100)}<small>${progress.toFixed(0)}%</small></span><strong>${Number(c.score).toFixed(1)} / ${c.max}</strong></div>`;
    }).join('') || '<div class="empty-inline">该模块暂无可展示的评分项。</div>';
    return `<section class="score-accordion ${open ? 'is-open' : ''}"><button class="score-accordion-head" type="button" data-score-toggle="${key}" aria-expanded="${open}"><span>${TYPE_LABELS[key]}</span><em>${coverage}</em><b>${score(item.score)}</b><i aria-hidden="true">⌄</i></button>${open ? `<div class="score-accordion-content"><div class="score-meta"><span>原始得分 <b>${bad(item.earned) ? '数据缺失' : Number(item.earned).toFixed(1)} / ${bad(item.max_available) ? '数据缺失' : Number(item.max_available).toFixed(0)}</b></span><span>归一化得分 <b>${score(item.score)} / 100</b></span></div><div class="score-table-head"><span>指标名称</span><span>原始值</span><span>进度 / 状态</span><span>得分（满分）</span></div><div class="score-rows">${rows}</div></div>` : ''}</section>`;
  }).join('');
  return section('评分细则', '这 8 个模块是风格画像分（Style / Profile），回答「它是什么类型」；最终评分模型看「概览」里的模型路由', list);
}

/* ---------- 财务趋势 ---------- */
const TREND_BUTTONS = [['revenue', '营收'], ['net_profit', '净利润'], ['deduct_profit', '扣非净利润'], ['roe', 'ROE'], ['gross_margin', '毛利率']];
function renderFinancialTrends(s) {
  const active = state.trendMetric, primary = trendSeries(s, active);
  const title = TREND_BUTTONS.find((x) => x[0] === active)?.[1] || '营收';
  const values = primary.map((p) => active === 'revenue' || active.includes('profit') ? Number(p.value) / 1e8 : p.value);
  const chart = svgChart([{ name: title + (active === 'revenue' || active.includes('profit') ? '（亿元）' : '（%）'), values }], { labels: dateLabels(primary), label: title + '趋势' });
  const revenue = trendSeries(s, 'revenue'), profit = trendSeries(s, 'net_profit'), gross = trendSeries(s, 'gross_margin');
  const years = dateLabels(revenue); const revValues = revenue.map((p) => p.value), profitValues = profit.map((p) => p.value);
  const table = years.length ? `<div class="table-scroll"><table class="data-table"><thead><tr><th>年度</th><th>营收</th><th>同比</th><th>净利润</th><th>同比</th><th>毛利率</th></tr></thead><tbody>${years.map((year, i) => `<tr><td>${year}</td><td>${amount(revValues[i])}</td><td>${yoy(revValues)[i] == null ? '历史不足' : percent(yoy(revValues)[i])}</td><td>${amount(profitValues[i])}</td><td>${yoy(profitValues)[i] == null ? '历史不足' : percent(yoy(profitValues)[i])}</td><td>${percent(gross[i]?.value)}</td></tr>`).join('')}</tbody></table></div>` : '<div class="empty-inline">年度财务序列尚未获取。</div>';
  return section('财务趋势', '年度数据；横轴仅显示真实报告期或由年报序列推导的年度', `<div class="metric-switch segmented-control">${TREND_BUTTONS.map(([key, label]) => `<button class="${key === active ? 'active' : ''}" data-trend="${key}">${label}</button>`).join('')}</div><div class="chart-panel">${chart}</div><h4 class="subsection-title">核心财务摘要</h4>${table}`, 'chart-card');
}

/* ---------- 资产负债表 ---------- */
const ASSETS = [['货币资金', 'monetary_funds'], ['金融资产', 'trading_finasset'], ['应收账款', 'accounts_receivable'], ['存货', 'inventory'], ['固定资产', 'fixed_asset'], ['在建工程', 'construction_in_progress']];
function renderBalance(s) {
  const f = s.financial || {}, bal = f.balance || {}, totalAssets = bal.total_assets;
  const items = ASSETS.map(([name, key]) => ({ name, value: bal[key] })).filter((x) => !bad(x.value));
  const known = items.reduce((sum, x) => sum + Number(x.value), 0);
  if (!bad(totalAssets) && Number(totalAssets) > known) items.push({ name: '其他资产', value: Number(totalAssets) - known });
  const donut = renderDonut(items, totalAssets);
  const model = f.asset_model || {}, breakdown = model.breakdown || [];
  const detailRows = breakdown.map((row) => {
    const ratioAsset = !bad(row.value) && !bad(totalAssets) && Number(totalAssets) !== 0 ? ratio(Number(row.value) / Number(totalAssets)) : absent();
    return `<tr><td>${esc(row.name)}</td><td>${amount(row.value)}</td><td>${ratioAsset}</td><td>当前接口未提供</td><td>${bad(row.rate) ? absent() : percent(Number(row.rate) * 100, 0)}</td><td>${amount(row.discounted)}</td></tr>`;
  }).join('') || '<tr><td colspan="6" class="table-empty">资产明细数据缺失</td></tr>';
  const totals = `<div class="balance-totals">${metricCard('总资产', amount(totalAssets))}${metricCard('总负债', amount(bal.total_liabilities))}${metricCard('净资产', amount(bal.total_equity))}</div>`;
  // 四个清算口径的指标**必须各自成组、各自写明公式**。以前这里只有两个卡片，
  // 而且「保守清算价值」那一格的减项被写成了有息负债——数是扣全部负债的，
  // 说明却写扣有息负债，两个都错。现在四格分开：资产价值组减的是零/全部负债，
  // 资本结构组减的才是有息负债。
  const debt = model.interest_bearing_debt, liabs = model.total_liabilities;
  const debtOK = model.interest_debt_valid !== false;
  const valueGroup = metricGroup('资产价值', '折价后资产合计分别减什么', [
    metricCard('保守资产价值', amount(model.gross_conservative_asset_value), '折价后资产合计，未减负债'),
    metricCard('保守清算价值', amount(model.adjusted_liquidation_value), `折价后资产合计 − 全部负债 ${amount(liabs)}`),
  ]);
  // 「调整后净现金 / 市值」用 s.adjusted_net_cash_to_mcap —— 与列表排序**同一个
  // 字段、同一份主记录**（后端 engine.net_cash_sort_value 一处判定）。这里绝不
  // 现算 model.adjusted_net_cash / 市值：那会变成第二个取数口径，而这一页已经
  // 因为「同一个名字两个数」出过一次事。
  const ncrCard = metricCard('调整后净现金 / 市值', ratio(s.adjusted_net_cash_to_mcap),
    s.net_cash_source === 'legacy_alias' ? '仍是 V1.2 之前的旧口径值，重新分析后更新' : '资产语义层 AdjustedNetCash ÷ 市值');
  // 闸门关闭时副标题留空：写「− 有息负债 数据缺失」比不写更让人费解，原因由
  // 下面那行警告统一交代。
  const capitalGroup = metricGroup('资本结构', '减项全部是有息负债', debtOK ? [
    metricCard('扣有息负债后资产价值', amount(model.net_interest_bearing_asset_value), `折价后资产合计 − 有息负债 ${amount(debt)}`),
    metricCard('调整后净现金', amount(model.adjusted_net_cash), `类现金 ${amount(model.near_cash)} − 有息负债 ${amount(debt)}`),
    ncrCard,
  ] : [
    metricCard('扣有息负债后资产价值', amount(model.net_interest_bearing_asset_value)),
    metricCard('调整后净现金', amount(model.adjusted_net_cash), `类现金 ${amount(model.near_cash)}`),
    ncrCard,
  ], 3);
  // 闸门关闭时上面四格里有三格是空的。不说明原因的话，看起来像数据源没取到。
  const gate = model.interest_debt_valid === false
    ? `<p class="footnote warn">负债未与报表「负债合计」对平：${esc(model.liability_status || '—')}。依赖有息负债的指标（扣有息负债后资产价值、调整后净现金、净现金占市值、利息覆盖倍数）一律不输出，而不是给一个偏高的数。</p>` : '';
  return section('资产负债表', '资产结构与保守资产价值模型；同比数据由数据源后续补充时才会显示', `<div class="balance-top"><div class="asset-donut">${donut}</div><div class="asset-legend">${items.length ? items.map((x, i) => `<div><i style="background:${COLORS[i % COLORS.length]}"></i><span>${x.name}</span><b>${amount(x.value)}</b><em>${!bad(totalAssets) && Number(totalAssets) ? ratio(Number(x.value) / Number(totalAssets)) : '占比数据缺失'}</em></div>`).join('') : '<div class="empty-inline">资产结构数据缺失</div>'}</div></div>${totals}${valueGroup}${capitalGroup}${gate}<h4 class="subsection-title">资产明细与折价假设</h4><div class="table-scroll"><table class="data-table"><thead><tr><th>项目</th><th>金额</th><th>占总资产</th><th>同比</th><th>折价率</th><th>保守价值</th></tr></thead><tbody>${detailRows}</tbody></table></div><p class="footnote">保守价值为模型中的折价假设结果，不代表可实现的真实清算价值。</p>`);
}
function renderDonut(items, total) {
  if (!items.length || bad(total) || Number(total) <= 0) return '<div class="chart-empty">资产结构数据缺失</div>';
  const r = 62, c = 2 * Math.PI * r, known = items.reduce((sum, x) => sum + Number(x.value), 0);
  let offset = 0;
  const paths = items.map((x, i) => { const length = Math.max(0, Math.min(c, Number(x.value) / Number(total) * c)); const path = `<circle cx="85" cy="85" r="${r}" fill="none" stroke="${COLORS[i % COLORS.length]}" stroke-width="22" stroke-dasharray="${length} ${c - length}" stroke-dashoffset="${-offset}" transform="rotate(-90 85 85)"/>`; offset += length; return path; }).join('');
  return `<svg viewBox="0 0 170 170" class="donut-chart">${paths}<text x="85" y="78" text-anchor="middle" class="donut-label">总资产</text><text x="85" y="100" text-anchor="middle" class="donut-value">${esc(plainAmount(total))}</text></svg>`;
}

/* ---------- 资产审计（§21） ----------
   九个字段一项不少：指标 / 原始值 / 调整值 / 经济分类 / 折扣 / 来源 / 期间 /
   证据 / 置信度。少一个，审计就退化成「相信系统吧」——而这一层的全部意义
   就是让人能自己判断「这一笔资产到底是什么」。 */
const AUDIT_FILTERS = [
  ['all', '全部'], ['cash', '现金及类现金'], ['unknown', '未分类'],
  ['conflict', '合计对不上'], ['llm', 'LLM 分类'],
];
const CASH_CLASSES = new Set(['CASH', 'BANK_DEPOSIT', 'TERM_DEPOSIT', 'NEGOTIABLE_CD',
  'STRUCTURED_DEPOSIT', 'RESTRICTED_CASH', 'LOW_RISK_FINANCIAL_ASSET', 'MARKETABLE_SECURITY']);
const SCENARIO_LABELS = { CONSERVATIVE: '保守', BASE: '中性', OPTIMISTIC: '乐观' };

// LLM 给了判定、却没有附注原文——那个判定不是读出来的，是照着科目名猜的。
// 现役快照里华域「其他流动资产」的 evidence 写着「一年内到期的发放贷款及
// 垫款」，而华域是汽车零部件公司，根本没有这个科目；同一份空输入在历史上
// 既回过 RECEIVABLE_NORMAL 也回过 OTHER_UNKNOWN。这类判定不可复现，标出来。
function ungrounded(r) {
  return r && r.method === 'llm' && !String(r.source_text || '').trim();
}
function auditRows(a) {
  const f = state.auditFilter;
  return a.rows.filter((r) => {
    if (f === 'cash') return CASH_CLASSES.has(r.economic_class);
    if (f === 'unknown') return r.economic_class === 'OTHER_UNKNOWN';
    if (f === 'conflict') return r.conflict;
    if (f === 'llm') return r.method === 'llm';
    return true;
  });
}
function scenarioCells(map, fmt) {
  return a3(map).map((v) => `<td class="num">${fmt(v)}</td>`).join('');
}
function a3(map) { return ['CONSERVATIVE', 'BASE', 'OPTIMISTIC'].map((k) => (map || {})[k]); }

function renderAssetAuditState(st) {
  // 兜底路径。门禁下这个页签根本打不开（详情页整屏就是状态面板），能走到这儿只
  // 有一种情况：状态说审计通过了、库里却取不出快照（快照正在被重跑换掉的那一
  // 瞬）。给的是同一块状态面板，只是「重试」换成带 refresh=1 重新审计这一只。
  // 两种入参形状都要认：接口的 {status, error} 与股票记录的 {audit_status, ...}。
  const src = st || {};
  return renderAuditStatePanel(
    { audit_status: src.status || src.audit_status, audit_error: src.error || src.audit_error },
    { retry: 'refresh' });
}
function renderAssetAudit(s) {
  if (state.assetsBusy) return section('资产审计', '正在读取资产语义层快照', '<div class="chart-empty">正在读取已落库的资产语义快照…</div>');
  const a = state.assets;
  // 没有 payload 就是这只股票还没有通过审计（接口给的就是这个形状：状态 +
  // 可选的 payload）。不再现跑一次审计——那会卡住整条请求好几分钟。
  if (!a) return renderAssetAuditState(state.assetsState || s);

  const tiers = a.cash_tiers || {}, net = a.net_cash || {}, prof = a.asset_value_profile || {}, sc = prof.scenarios || {};
  const mc = a.market_cap;
  // 有息负债闸门。审计页读的是快照 JSON 原文，而**老快照**（没有对平字段的那
  // 些）里净现金和有息负债都是当年那套漏了整段非流动负债的代码算出来的——那
  // 个数看起来完全正常，只是偏高。所以这里也要跟着闸，不能只闸读侧。
  const recon = (prof.liabilities || {}).reconciliation;
  const debtOK = recon === 'OK';
  const head = `<div class="metric-grid">${
    metricCard('类现金资产', amount(tiers.NearCash), bad(mc) ? '' : `占市值 ${ratio(Number(tiers.NearCash) / Number(mc))}`)
  }${metricCard('调整后净现金', amount(debtOK ? net.AdjustedNetCash : null), debtOK ? `类现金 − 有息负债 ${amount(net.TotalInterestBearingDebt)}` : '有息负债未对平，不输出')
  }${metricCard('有息负债', amount(debtOK ? net.TotalInterestBearingDebt : null), '按合并报表口径')
  }${metricCard('受限资金', amount(tiers.RestrictedCash), a.restricted_cash ? '已从类现金中扣除' : '未披露受限金额')
  }</div>`;
  const gateNote = debtOK ? '' : `<p class="footnote warn">负债未与报表「负债合计」对平：${esc((prof.liabilities || {}).status || '这份快照里没有负债对平信息')}。依赖有息负债的指标（调整后净现金、净现金占市值、利息覆盖倍数、扣有息负债后资产价值）一律不输出。</p>`;

  // 口径的三件事：报告期、金额量纲、量级校验。缺了它们，一个错 10⁶ 倍的资产
  // 总计在页面上看不出任何异常——招商银行半年报的报表通篇没写金额单位，
  // 13,785,280 当元读还是当百万元读都是一串正常的数字。报告期还可能不是最新
  // 一期（最新一期是扫描件时回溯到上一期，中国建筑就是这样），那更是必须写明：
  // 份额、市值与同一批股票的横向比较全都建立在这个时点上。
  const unit = String(prof.amount_unit || '').trim();
  const unitNote = String(prof.amount_note || '').trim();
  const periodNote = String(a.report_period_note || '').trim();
  const gate = prof.amount_gate || {};
  // 「快照里没有闸门记录」与「闸门跑了但没比对对象」是两回事，不能合成一句。
  // 前者是这份快照生成于量级校验上线之前（2026-09-24 之前落库的那 15 只），
  // 它们**都有市值**——替它们印「无市值，未校验」就是页面在编事实。
  const gateRecorded = typeof gate.checked === 'boolean';
  const gateOk = gateRecorded && gate.checked === true && gate.valid !== false;
  const gateBad = gateRecorded && gate.checked === true && gate.valid === false;
  const basisBits = [];
  if (a.report_period) basisBits.push(`口径基于 ${a.report_period}`);
  basisBits.push(unit ? `金额按「${unit}」折算成元` : '金额按元计');
  basisBits.push(gateOk
    ? `量级校验：资产总计 ÷ 市值 = ${bad(gate.ratio) ? '—' : Number(gate.ratio).toFixed(2)}`
    : gateBad ? '量级校验未通过'
      : gateRecorded ? '量级校验：无市值，未校验'
        : '量级校验：这份快照没记（生成于该校验之前）');
  const basisNote = `<p class="footnote">${basisBits.map(esc).join(' · ')}${unitNote ? `｜${esc(unitNote)}` : ''}</p>`
    + (gateBad ? `<p class="footnote warn">资产总计与市值不在同一量级：${esc(gate.reason || '金额单位可能识别错')}。这份口径不可信，别按它看资产价值。</p>` : '')
    + (periodNote ? `<p class="footnote warn">${esc(periodNote)}</p>` : '');

  const tierRows = [['PureCash', '纯净现金', '库存现金 + 银行存款'], ['NearCash', '类现金', '+ 定期存款 + 可转让大额存单'],
    ['LiquidFinancialAssets', '高流动性金融资产', '+ 结构性存款 + 低风险理财 + 交易性金融资产']];
  const tierTable = `<div class="table-scroll"><table class="data-table"><thead><tr><th>层级</th><th>含义</th><th class="num">金额</th><th class="num">占总资产</th><th class="num">占市值</th></tr></thead><tbody>${
    tierRows.map(([k, label, hint]) => `<tr><td>${label}</td><td class="muted">${hint}</td><td class="num"><b>${amount(tiers[k])}</b></td><td class="num">${!bad(a.total_assets) && Number(a.total_assets) ? ratio(Number(tiers[k]) / Number(a.total_assets)) : absent()}</td><td class="num">${bad(mc) ? absent() : ratio(Number(tiers[k]) / Number(mc))}</td></tr>`).join('')
  }</tbody></table></div>`;

  // 三档情景各给四列，四个清算口径指标一个不缺。原来的说明列写「已扣除有息
  // 负债」而那一格渲染的是扣**全部负债**的 ``v.liquidation_value``——文案和
  // 数值对不上，减值那一列的键 ``prof.total_debt`` 在画像里根本不存在，恒显示
  // 「数据缺失」。现在减项单独成列写明是全部负债。
  const liqTable = `<div class="table-scroll"><table class="data-table"><thead><tr><th>情景</th><th class="num">折价后资产</th><th class="num">保守清算价值</th><th class="num">扣有息负债后资产价值</th><th class="num">清算价值 ÷ 市值</th><th>减项</th></tr></thead><tbody>${
    ['CONSERVATIVE', 'BASE', 'OPTIMISTIC'].map((k) => { const v = sc[k] || {}; return `<tr><td>${SCENARIO_LABELS[k]}</td><td class="num">${amount(v.gross_adjusted_assets)}</td><td class="num"><b>${amount(v.liquidation_value)}</b></td><td class="num">${amount(v.net_interest_bearing_asset_value)}</td><td class="num">${bad(v.liquidation_to_market_cap) ? absent() : ratio(v.liquidation_to_market_cap)}</td><td class="muted">全部负债 ${amount(prof.total_liabilities)}</td></tr>`; }).join('')
  }</tbody></table></div>
  <p class="footnote">保守清算价值 = 折价后资产 − 全部负债；扣有息负债后资产价值 = 折价后资产 − 有息负债 ${amount(prof.interest_bearing_debt)}（只用于展示，不作清算价值）。调整后净现金 = 类现金 − 有息负债，不随情景变化，故不设列。</p>`;

  const rows = auditRows(a);
  const body = rows.map((r, i) => {
    const open = state.auditOpen.has(i);
    const tags = [
      r.method === 'llm' ? '<em class="audit-tag llm">LLM 分类</em>' : '',
      ungrounded(r) ? '<em class="audit-tag warn">无原文·判定不可复现</em>' : '',
      r.conflict ? '<em class="audit-tag warn">合计对不上</em>' : '',
      r.economic_class === 'OTHER_UNKNOWN' ? '<em class="audit-tag muted">未分类</em>' : '',
      r.restricted ? '<em class="audit-tag warn">受限</em>' : '',
    ].filter(Boolean).join(' ');
    const main = `<tr data-audit-toggle="${i}" class="audit-row${open ? ' open' : ''}">
      <td>${esc(r.metric)}${tags ? ' ' + tags : ''}</td>
      <td class="num">${amount(r.raw)}</td>
      <td>${esc(r.economic_label)}</td>
      <td class="num small">${scenarioCells(r.haircut, (v) => bad(v) ? absent() : `${(Number(v) * 100).toFixed(0)}%`)}</td>
      <td class="num small">${scenarioCells(r.adjusted, amount)}</td>
      <td class="num">${bad(r.source_page) ? absent() : `P${r.source_page}`}</td>
      <td>${esc(r.period || '—')}</td>
      <td class="num">${bad(r.confidence) ? absent() : `${(Number(r.confidence) * 100).toFixed(0)}%`}</td>
      <td class="audit-evidence">${esc(String(r.evidence || '无').slice(0, 42))}</td>
    </tr>`;
    if (!open) return main;
    return main + `<tr class="audit-detail"><td colspan="9"><dl>
      <dt>分类方法</dt><dd>${esc(r.method === 'llm' ? '规则未命中，交由 LLM 判定（§9 门槛内）' : r.method === 'rule' ? '规则字典命中，未调用 LLM（§6）' : '规则未命中，且未达 LLM 门槛')}</dd>
      <dt>完整证据</dt><dd>${esc(r.evidence || '无')}</dd>
      <dt>来源原文</dt><dd>${esc(r.source_text || (ungrounded(r) ? '（这条 LLM 判定没有附注原文可依据——模型手上只有科目名，属于推测，不可复现）' : '（附注未展开，按一级科目整笔计价）'))}</dd>
      <dt>来源</dt><dd>${esc(a.source_document || '—')}${bad(r.source_page) ? '' : ` · 第 ${r.source_page} 页`}</dd>
      <dt>期间</dt><dd>${esc(r.period || '—')} · 期初 ${amount(r.prior)}</dd>
    </dl></td></tr>`;
  }).join('') || '<tr><td colspan="9" class="table-empty">该筛选下没有资产项</td></tr>';

  const filters = AUDIT_FILTERS.map(([k, label]) =>
    `<button class="filter-chip ${k === state.auditFilter ? 'active' : ''}" data-audit-filter="${k}">${label}</button>`).join('');
  const table = `<h4 class="subsection-title">资产逐项审计 <span class="muted">（${rows.length} / ${a.rows.length} 项，可疑的排在最前）</span></h4>
    <div class="filter-scroll audit-filters">${filters}</div>
    <div class="table-scroll"><table class="data-table audit-table"><thead><tr>
      <th>指标</th><th class="num">原始值</th><th>经济分类</th><th class="num">折扣 保/中/乐</th><th class="num">调整值 保/中/乐</th><th class="num">来源</th><th>期间</th><th class="num">置信度</th><th>证据</th>
    </tr></thead><tbody>${body}</tbody></table></div>`;

  const conflicts = (a.note_conflicts || []).length;
  const llm = a.llm || {};
  // 只报「问了几次」不够用：截断和空响应以前都混在「模型没给结论」里，
  // 看不出那次调用是模型拒答而白花，还是输出预算不够。分开报才查得动。
  const llmNote = (llm.calls || llm.truncated || llm.empty)
    ? ` · LLM 调用 ${llm.calls || 0} 次（预算截断 ${llm.truncated || 0} · 无文本块 ${llm.empty || 0}）` : '';
  const foot = `<p class="footnote">引擎 ${esc(a.metric_version || '—')} · 折价表 ${esc(a.haircut_version || '—')} · 附注合计对不上的科目 ${conflicts} 个（按一级科目整笔计价并记 conflict，不挑不猜） · 覆盖率 ${bad(a.classification_coverage) ? '—' : `${(Number(a.classification_coverage) * 100).toFixed(1)}%`}（按金额计）${llmNote}</p>`;
  const nUngrounded = rows.filter(ungrounded).length;
  const ungroundedNote = nUngrounded ? `<p class="footnote warn">本页有 ${nUngrounded} 项判定标着「无原文·判定不可复现」：当时这些条目没有附注原文，LLM 手上只有科目名，同一份输入在历史上给出过不同答案。按既定口径旧快照只追加不改写，所以这里只标注、不重算；受影响的保守资产价值与保守清算价值请连同这一条一起看。</p>` : '';
  return section('资产审计', '把一级科目还原成经济实质：每一笔的原始值、经济分类、折价、来源与置信度都可追'
    + (a.report_period ? ` · 基于 ${esc(a.report_period)}` : '')
    + (unit ? ` · 按「${esc(unit)}」折算` : ''),
    head + basisNote + gateNote + `<h4 class="subsection-title">现金层级</h4>` + tierTable +
    `<h4 class="subsection-title">清算价值口径</h4>` + liqTable + ungroundedNote + table + foot,
    'audit-card');
}
async function loadAssetAudit(force = false) {
  const code = state.currentCode;
  state.assetsBusy = true; state.assetsCode = code; state.assets = null; state.assetsState = null;
  drawerBody.innerHTML = renderTab();
  try {
    const data = await api('/api/research/assets?code=' + encodeURIComponent(code) + (force ? '&refresh=1' : ''));
    if (state.currentCode !== code) return;
    // 接口现在给的是「状态 + 可选的 payload」，不再是快照本身：未审计的股票拿到
    // 的是 null payload，页面照状态面板渲染，而不是把空对象当快照画出一片 NaN。
    state.assetsState = data; state.assets = data.payload || null; state.assetsBusy = false;
    if (state.currentTab === '资产审计') { drawerBody.innerHTML = renderTab(); drawerBody.scrollTop = 0; }
  } catch (e) {
    if (state.currentCode !== code) return;
    state.assetsBusy = false; state.assetsCode = null;
    toast(e.message, true);
    if (state.currentTab === '资产审计') drawerBody.innerHTML = section('资产审计', '', `<div class="chart-empty">取资产语义快照失败：${esc(e.message)}</div>`);
  }
}

/* ---------- 猪行业数据（批 5.2 的只读证据视图）----------
   这一页只读。读数从 /api/research/pig-evidence 的 readings 来；点开某一行时**再取
   一次**那一组（概览不带候选明细——四家的完整载荷实测 1.8~2.0 MB，一次页面加载塞
   不下，而「一条都不藏」没有因此被削弱：明细永远取得回来）。

   页面上出现的每一个中文词——状态、来源层级、因子名、指标名——**一律从载荷里取**
   （后端 pig_evidence 已经把 status_label / source_level_label / factor_label /
   metric_label 放进读数与指标桶）。前端不写第二份状态表：这个仓里已经栽过一次，
   前端抄了一份风险等级表，抄成了另一句话。 */
const PIG_FILTERS = [['all', '全部'], ['value', '有值'], ['missing', '缺失']];
function pigText(v) { return (v === null || v === undefined || v === '') ? '—' : esc(String(v)); }
/* `missing` 是**这一格**缺失时该说的话，由载荷给（后端 `missing_text`）。
   成本链那几格要说的是「未获取可靠公开数据」——它比「数据缺失」更准：不是
   我们没抓到，是公司没有披露过。措辞只在后端维护一份；前端不写第二份。
   **不显示 0 / NaN / —**：缺失就是缺失，一个 0 会被读成「成本是 0」。 */
function pigNum(v, unit, missing) {
  if (bad(v)) return absent(missing);
  const n = Number(v);
  if (!isFinite(n)) return absent(missing);
  return esc(Number.isInteger(n) ? String(n) : n.toFixed(4)) + (unit ? ` <span class="muted">${esc(unit)}</span>` : '');
}
function pigFlag(v) { return (v === null || v === undefined) ? '—' : (v ? '是' : '否'); }
function pigRowKey(metricId, period, variant) { return `${metricId || ''}|${period || ''}|${variant || ''}`; }
/* 「这一格已经画过了」的判据是 **(指标, 口径) 这一对**，不是指标名。
   成本链把它逼了出来：`full_cost` 在 cost_grids 里占两格——正身
   `COMPLETE_COST_PER_KG`（公司自报口径不许升格，如实「未获取可靠公开数据」）
   与公司自报的 `FULL_COST_COMPANY_DISCLOSED`（真有值）。按指标名去重，第一格
   会把第二格一起挡掉，于是公司明明披露了 11.7 元/公斤，页面上却写「未获取
   可靠公开数据」——比缺一行糟得多。
   同一指标出现多行本来就是这个表的常态（一因子一行：`pig_sale_price` 同时被
   company_sale_price 与 sale_price_level 消费，`unit_margin` 同理），口径列
   就是用来分开它们的。**同一对**绝不出现两行，这才是那条「不许自相矛盾」的
   底线。 */
function pigPairKey(metricId, variant) { return `${metricId || ''}|${variant || ''}`; }
/* 读数行与「库里有、但没有任何读数消费」的指标行**共用同一个行构造器**：
   两处的列一样、展开方式一样，分两套渲染只会让一边先烂掉。 */
function pigRows() {
  const p = state.pig || {};
  const readings = p.readings || {}, metrics = p.metrics || {};
  const rows = [], seen = new Set(), seenPairs = new Set();
  Object.keys(readings).forEach((factorId) => {
    const r = readings[factorId];
    if (r.metric_id) seen.add(r.metric_id);
    if (r.metric_id) seenPairs.add(pigPairKey(r.metric_id, r.metric_variant));
    rows.push({
      kind: 'reading', key: pigRowKey(r.metric_id, r.period, r.metric_variant), openKey: 'r:' + factorId,
      label: r.factor_label || factorId, hint: factorId,
      metric_id: r.metric_id, metric_label: null,
      period: r.period, metric_variant: r.metric_variant, value: r.value, unit: r.unit,
      status: r.status, status_label: r.status_label, source_level_label: r.source_level_label,
      source_type: r.source_type, is_direct_disclosure: r.is_direct_disclosure,
      is_estimated: r.is_estimated, expected_variant: r.expected_variant,
      variant_fallback: !!r.variant_fallback, variant_fallback_reason: r.variant_fallback_reason,
      reason: r.reason || r.note, missing_metrics: r.missing_metrics || [],
    });
  });
  Object.keys(metrics).sort().forEach((metricId) => {
    if (seen.has(metricId)) return;
    const bucket = metrics[metricId] || {}, preferred = bucket.preferred || {};
    if (!preferred.period && !(bucket.candidates || []).length && !(bucket.series || []).length) return;
    /* 这个指标已经有读数行了，**不许再补一行同指标的「读数不消费」**：两行
       标签一样、值一样，只是多一个标签，纯噪声。（成本链那一段另说——它按
       **(指标, 口径)** 判重，见 `pigPairKey`：`full_cost` 的公司自报口径在
       读数里没有对应行，该显示就必须显示。） */
    seen.add(metricId);
    /* 也要记这一对：`metrics` 循环铺的这一行本身就是某个口径的值（新希望的
       育肥成本 12.2 元/公斤就是这么来的——它在库里、却没有因子消费它，于是
       从这一支长出来）。不记的话成本链那一段会**再铺一行一模一样**的。 */
    seenPairs.add(pigPairKey(metricId, preferred.metric_variant));
    rows.push({
      kind: 'store', key: pigRowKey(metricId, preferred.period, preferred.metric_variant),
      openKey: 's:' + metricId,
      label: bucket.metric_label || metricId, hint: metricId,
      metric_id: metricId, metric_label: bucket.metric_label,
      period: preferred.period, metric_variant: preferred.metric_variant,
      value: preferred.value, unit: preferred.unit,
      status: preferred.status, status_label: preferred.status_label,
      source_level_label: preferred.source_level_label, source_type: null,
      is_direct_disclosure: null, is_estimated: null, expected_variant: null,
      variant_fallback: false, variant_fallback_reason: null,
      reason: preferred.reason, missing_metrics: [],
    });
  });
  /* 批 7：成本链那几行。上面那个 `metrics` 循环只长得出「库里有观测、或有读数
     消费」的指标，而 `cash_cost`（语料里零披露）两样都不占——不显式补，它在
     页面上**根本不存在**，而不是显示成「未获取可靠公开数据」，两者在用户那里
     是「这一格不用看」与「这一格我们确实没有」的区别。
     标签与缺失措辞一律取载荷，前端不写第二份（见文件顶部那条注释）。 */
  const gridSeen = new Set();
  (p.cost_grids || []).forEach((g) => {
    const pair = pigPairKey(g.metric_id, g.metric_variant);
    if (!g.metric_id || seenPairs.has(pair) || gridSeen.has(pair)) return;
    gridSeen.add(pair);
    const pref = g.preferred || {};
    rows.push({
      kind: 'grid', key: pigRowKey(g.metric_id, pref.period, g.metric_variant),
      openKey: 'g:' + g.metric_id,
      label: g.metric_label || g.metric_id, hint: g.metric_id,
      metric_id: g.metric_id, metric_label: g.metric_label,
      period: pref.period || null, metric_variant: g.metric_variant,
      value: pref.value, unit: pref.unit || g.unit,
      status: pref.status, status_label: pref.status_label,
      source_level_label: pref.source_level_label, source_type: null,
      is_direct_disclosure: null, is_estimated: null, expected_variant: null,
      variant_fallback: false, variant_fallback_reason: null,
      reason: pref.reason, missing_metrics: [], missing_text: g.missing_text,
    });
  });
  return rows;
}
/* 展开里那一块：首选（含它自己的旁挂） + 候选并列 + 冲突 + 行业序列。
   全部来自同一次 /api/research/pig-evidence 响应，前端不再算任何数。 */
function renderPigEvidence(row, ev) {
  if (ev.error) return `<p class="footnote warn">取证据失败：${esc(ev.error)}</p>`;
  const bucket = ((ev.metrics || {})[row.metric_id]) || {};
  const groups = bucket.preferreds || [];
  const preferred = groups.find((g) => g.period === row.period && g.metric_variant === row.metric_variant)
    || groups.find((g) => g.period === row.period) || bucket.preferred || {};
  const o = preferred.observation || {};
  const cands = bucket.candidates || [], conflicts = bucket.conflicts || [], series = bucket.series || [];
  const items = [
    ['占优的观测', preferred.observation_hash
      ? `<b>★</b> ${pigText(preferred.period)} · ${pigText(preferred.metric_variant)} · 这一组共 ${preferred.observations || 0} 条`
      : (groups.length ? `这一组没有首选（${pigText(preferred.status_label || preferred.status)}）`
        : '这一格不在观测仓里：它的值来自行业序列（下方是逐月摘要）')],
    ['状态', `${pigText(preferred.status_label || preferred.status)}${preferred.reason ? ` —— ${esc(preferred.reason)}` : ''}`],
    ['来源文件', `${pigText(o.source_document || preferred.source_level_label)}${bad(o.source_page) ? '' : ` · 第 ${o.source_page} 页`}`],
    ['原文段落', o.evidence_text || o.paragraph
      || '（这份来源没有原文段落：它来自行业序列或派生，不是某份文件的解析结果）'],
    ['文件哈希 / 解析器版本', `${pigText(o.document_hash)} · v${pigText(o.parser_version)}`],
    ['observation_hash', pigText(o.observation_hash || preferred.observation_hash)],
    ['conflict_group_id', pigText(o.conflict_group_id)],
    ['benchmark_type', pigText(o.benchmark_type || preferred.benchmark_type)],
    ['derivation', pigText(o.derivation)],
    ['冲突情况', conflicts.length
      ? conflicts.map((c) => `第 ${esc(String(c.group_id).slice(0, 8))}… 组（层级 ${pigText(c.level)}）：值 ${c.values.map((v) => esc(String(v))).join(' / ')}`).join('<br>')
      : (preferred.competing || preferred.lower_conflicts
        ? `首选之外还有 ${preferred.competing || 0} 条同级、${preferred.lower_conflicts || 0} 条更低级别`
        : '无')],
  ].map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join('');
  const candTable = cands.length ? `<div class="table-scroll"><table class="data-table pig-cand-table"><thead><tr>
      <th>首选</th><th>期间</th><th>口径</th><th class="num">值</th><th>单位</th><th>来源层级</th><th>来源</th><th>状态</th><th>备注</th><th>observation_hash</th>
    </tr></thead><tbody>${cands.map((c) => `<tr class="${c.is_preferred ? 'pig-preferred-row' : ''}">
      <td>${c.is_preferred ? '★' : ''}</td><td>${pigText(c.period)}</td><td>${pigText(c.metric_variant)}</td>
      <td class="num">${pigNum(c.value, null)}</td><td>${pigText(c.unit)}</td>
      <td>${pigText(c.level_label || c.source_level)}</td><td class="muted">${pigText(c.source_name)}</td>
      <td>${pigText(c.status_label || c.status)}</td><td class="muted small">${pigText(c.reason)}</td>
      <td class="muted small">${pigText(c.observation_hash)}</td></tr>`).join('')}</tbody></table></div>`
    : '<p class="footnote">这一组没有候选明细（概览按 candidates=0 取过，点开时才会取全）。</p>';
  const seriesBlock = series.map((g) => `<dt>行业序列</dt><dd>${pigText(g.metric_variant)} · ${g.region ? `地区 ${esc(g.region)}` : '全国'} · ${g.points_total} 点（${pigText(g.min_date)} → ${pigText(g.max_date)}）<br>${
    (g.months || []).slice(-6).map((m) => `${esc(m.month)} 月均 ${pigNum(m.value, m.unit)} · ${m.points} 点 / ${m.rows} 行 / 修订 ${m.revisions}${m.enough_for_benchmark ? '' : ' · <em class="audit-tag warn">点数不足基准门槛</em>'}（${pigText(m.source_level_label)}）`).join('<br>')}</dd>`).join('');
  return `<dt>候选（${cands.length} 条）</dt><dd>${candTable}</dd>${seriesBlock}`;
}
function renderPigReadingDetail(row) {
  const ev = state.pigEvidence[row.key];
  const busy = state.pigEvidenceBusy.has(row.key);
  const head = `<dl>
    <dt>读数</dt><dd>${esc(row.label)}${row.hint && row.hint !== row.label ? ` <span class="muted">（${esc(row.hint)}）</span>` : ''}</dd>
    <dt>当前值</dt><dd>${pigNum(row.value, row.unit, row.missing_text)}</dd>
    <dt>口径 / 期间</dt><dd>${pigText(row.metric_variant)} · ${pigText(row.period)}${row.metric_id ? ` · 指标 ${esc(row.metric_id)}` : ''}</dd>
    <dt>状态</dt><dd>${pigText(row.status_label || row.status)}</dd>
    <dt>来源层级 / 类型</dt><dd>${pigText(row.source_level_label)} · ${pigText(row.source_type)}</dd>
    <dt>直接披露 / 估算</dt><dd>${pigFlag(row.is_direct_disclosure)} / ${pigFlag(row.is_estimated)}</dd>
    ${row.expected_variant ? `<dt>正身口径</dt><dd>${esc(row.expected_variant)} —— 这一条用的是同量纲后备，时间窗口不同${row.variant_fallback_reason ? `：${esc(row.variant_fallback_reason)}` : ''}</dd>` : ''}
    <dt>未取值原因</dt><dd>${pigText(row.reason)}</dd>
    ${row.missing_metrics.length ? `<dt>缺哪些指标</dt><dd>${row.missing_metrics.map(esc).join('、')}</dd>` : ''}
  </dl>`;
  if (!row.metric_id) return head + '<p class="footnote">这一格的依赖整段缺失，没有单独的指标可取证据（原因见上）。</p>';
  if (busy) return head + '<p class="footnote">正在取证据…</p>';
  if (!ev) return head + '<p class="footnote">这一行的证据载荷没有取到。</p>';
  return head + renderPigEvidence(row, ev);
}
/* ---------- 核心经营数据（批 8）----------
   组合成员的核心只有三项：**销售均价 / 完全成本 / 出栏量**。核心关系是
   `单位利润 = 销售均价 − 完全成本` 与 `盈利能力 ≈ 单位利润 × 出栏规模`，
   所以 PSY / MSY / 料肉比 / 出栏均重 / 断奶仔猪成本 / 现金成本 等一律是
   **扩展信息**：它们照旧在下面那张「读数与观测」表里，只是不进这张卡片、
   不触发补录、不再是首次研究的完整性要求。

   卡片**只读**后端载荷：指标名、单位、缺失措辞、期间写法、来源层级、组合名
   全部取 payload（见本文件顶部那条约定，`test_cycle_breakdown` 逐字钉着组合名
   那一份），前端算的唯一一件事是排版。 */
/* 后端给的理由（``pig_cost_core.derive`` 的 ``skipped``）里带 Markdown 的 ``**``
   强调，卡片的小字是纯文本，直接摊上去会露出两个星号；硬切 60 字还会把一个
   单词拦腰截断（实测「…是 weaned_piglet_cost / co」）。**只是显示层的收尾**：
   不重算、不改口径、不改后端那句话本身。 */
function pigReason(text, limit = 60) {
  const plain = String(text || '').replace(/\*\*/g, '');
  return limit && plain.length > limit ? plain.slice(0, limit) + '…' : plain;
}

function renderPigCoreCard(p, cohortLabel) {
  const core = p.pig_core;
  if (!core) return '';
  const cards = (core.metrics || []).map((m) => (m.obtained
    ? metricCard(esc(m.label), pigNum(m.value, m.unit, m.missing_text),
      `期间 ${pigText(m.period)} · ${pigText(m.source_level_label || m.source_type)}`)
    : metricCard(esc(m.label), absent(m.missing_text),
      `单位 ${pigText(m.unit)}`)));
  const um = core.unit_margin || {};
  // 单位利润**只在两侧期间足够匹配时**才是一个数（判据在批 7 的
  // ``pig_cost_core.derive``，前端不重算）。算不出来时显示 missing + 后端
  // 给的理由原文——那句话说的正是「为什么不匹配」，比一个 dash 有用得多。
  cards.push(metricCard(
    `${esc(um.label || '')} <span class="muted">（${esc(um.formula || '')}）</span>`,
    bad(um.value) ? absent(um.missing_text) : pigNum(um.value, um.unit, um.missing_text),
    um.value === null || um.value === undefined
      ? esc(pigReason(um.reason))
      : `期间 ${pigText(um.period)}`,
    // 悬停里的那句话**不截断**（40 字看不完的理由，悬停正是用来看完的），
    // 只去掉星号。
    pigReason(um.reason, 0)));
  // 每头利润 / 估算总利润：**一格**。两个数是同一条链子上的中间值与结果
  // （单位利润 → 每头利润 → 总利润），拆成两格只会让 missing 时的同一句理由
  // 在卡片上出现两遍。**词表全部来自载荷**：标签、公式、单位、缺口语、以及
  // 那个换算系数（``weight_kg`` 只用来核对，界面上的公式是后端写的字）。
  // 前端不写第二份，尤其**不写 120 这个数**——它改一次（``RULES_V1``）这里
  // 就该跟着变，写死一个 120 之后两者会悄悄不一致。
  const ep = core.estimated_profit || {};
  const epMissing = bad(ep.value);
  cards.push(metricCard(
    `${esc(ep.label || '')} <span class="muted">（${esc(ep.formula || '')}）</span>`,
    epMissing ? absent(ep.missing_text) : pigNum(ep.value, ep.unit, ep.missing_text),
    epMissing
      ? esc(pigReason(ep.reason))
      : `${esc(ep.per_head_label || '')} ${pigNum(ep.per_head, ep.per_head_unit, ep.missing_text)}`
        + ` · ${esc(ep.volume_label || '')} ${pigNum(ep.volume, ep.volume_unit, ep.missing_text)}`
        + ` · 期间 ${pigText(ep.period)}`,
    pigReason(ep.reason, 0)));
  const missing = (core.missing || []).map((m) => m.label).join('、');
  const hint = core.is_pig_company
    ? (missing ? `缺：${esc(missing)}。缺的可以直接补，也可以先跳过——不影响这只股票加入研究库。`
      : '三项都已获取，无需补充。')
    : `${cohortLabel ? esc(cohortLabel) + '组合' : '这个组合'}的行业映射里没有这一只，所以没有补录入口。`;
  const button = core.is_pig_company
    ? `<button class="btn btn-sm" data-pig-core-open>＋ 补充核心经营数据</button>` : '';
  return `<h4 class="subsection-title">核心经营数据 ${button}</h4>
    <div class="metric-grid group-2">${cards.join('')}</div>
    <p class="footnote">${hint}</p>`;
}

/* 补充核心经营数据：Dialog 的行**由载荷驱动**——已自动获取的项只读展示（§四
   只让用户补 missing），缺的那几项才给输入框。用户只填 value / period
   （+ 选填备注），单位按指标预设。scope / 置信度 / 来源层级 / 哈希**不进界面**：
   把一个数据库行摊给用户看，他要判断的事情就从「这个数是多少」变成了
   「这个字段该填什么」。 */
function pigCoreOverlay() { return document.getElementById('pig-core-overlay'); }
function openPigCoreDialog(payload) {
  if (!payload) return;
  state.pigCore = payload;
  const overlay = pigCoreOverlay();
  if (!overlay) return;
  overlay.hidden = false;
  renderPigCoreDialog();
}
function closePigCoreDialog() {
  const overlay = pigCoreOverlay();
  if (overlay) overlay.hidden = true;
  state.pigCore = null;
}
function renderPigCoreDialog() {
  const modal = document.getElementById('pig-core-modal'), core = state.pigCore;
  if (!modal || !core) return;
  const forms = (core.period_forms || []).join(' / ');
  const fields = (core.metrics || []).map((m) => (m.obtained
    ? `<div class="field full"><label>${esc(m.label)}（${esc(m.unit)}）· 已自动获取</label>
         <div class="muted">${pigNum(m.value, m.unit, m.missing_text)} · 期间 ${pigText(m.period)} · ${pigText(m.status_label)} · ${pigText(m.source_level_label)}</div></div>`
    : `<div class="field"><label>${esc(m.label)}（${esc(m.unit)}）</label>
         <input type="number" step="any" data-pig-value="${esc(m.metric_id)}" placeholder="数值"></div>
       <div class="field"><label>期间<span class="muted">（${esc(forms)}）</span></label>
         <input type="text" data-pig-period="${esc(m.metric_id)}" placeholder="${esc((core.period_forms || [])[0] || '')}"></div>`)).join('');
  const entries = (core.manual_entries || []).map((e) => `<li>${esc(e.metric_label)} · ${pigNum(e.value, e.unit, '')} · 期间 ${pigText(e.period)}
      <button class="btn btn-sm" data-pig-core-delete="${esc(e.observation_hash)}">删除</button>
      ${e.conflict_note ? `<div class="footnote warn">${esc(e.conflict_note)}</div>` : ''}</li>`).join('');
  // 组合名**只从载荷来**：它是 ``industry_margin.COHORTS`` 里的
  // 一份中文名，前端留第二份就会在改口径时两边说不一样的话（这条有测试钉着）。
  const cohort = state.currentDetail?.cohort_label;
  const names = (core.metrics || []).map((m) => esc(m.label)).join(' / ');
  const umLabel = esc(core.unit_margin?.label || '');
  const umFormula = esc(core.unit_margin?.formula || '');
  modal.innerHTML = `
    <div class="modal-head"><div><h2>补充核心经营数据</h2>
      <div class="code">${cohort ? esc(cohort) + ' · ' : ''}${esc(core.code)} · ${names} · 规则 ${esc(core.rule_version || '')}</div></div>
      <button class="modal-close" data-pig-core-close aria-label="关闭">×</button></div>
    <p class="footnote">核心关系：${umLabel} = ${umFormula}。
      这里只补缺的项，来源不是必填，填不出准确的数字就【暂不填写】——不影响这只股票进研究库。
      人工补录的数据只是研究数据，不作为公司披露值，也不进评分。</p>
    <div class="form">${fields}
      <div class="field full"><label>来源 / 备注（选填）</label>
        <textarea data-pig-note placeholder="例：2026 半年报业绩说明会 / 月度销售简报"></textarea></div>
      <div class="form-actions">
        <button class="btn" data-pig-core-close>暂不填写</button>
        <button class="btn btn-primary" id="pig-core-save">保存</button></div></div>
    <div id="pig-core-errors"></div>
    ${entries ? `<h4 class="subsection-title">已人工补录</h4><ul class="footnote">${entries}</ul>` : ''}`;
}
function pigCoreItems() {
  const items = [];
  (state.pigCore?.metrics || []).forEach((m) => {
    if (m.obtained) return;
    const value = document.querySelector(`[data-pig-value="${m.metric_id}"]`)?.value;
    const period = document.querySelector(`[data-pig-period="${m.metric_id}"]`)?.value;
    if ((value || '').trim() === '' && (period || '').trim() === '') return;
    items.push({ metric_id: m.metric_id, value: value, period: period,
      source_note: document.querySelector('[data-pig-note]')?.value || '' });
  });
  return items;
}
async function submitPigCore() {
  const core = state.pigCore;
  if (!core) return;
  const items = pigCoreItems();
  if (!items.length) { toast('没有要保存的项'); return; }
  const btn = document.getElementById('pig-core-save');
  if (btn) btn.disabled = true;
  try {
    const out = await api('/api/research/pig-core', { method: 'POST',
      body: JSON.stringify({ code: core.code, items }) });
    if (!out.ok) {
      // 逐字段的错误**原样放到填写的人眼前**（服务端已经写成中文），
      // 一条不合法就整批不写——写一半会留下一个「看起来完整」的状态。
      const box = document.getElementById('pig-core-errors');
      if (box) box.innerHTML = `<p class="footnote warn">${(out.errors || []).map((x) => esc(x.message)).join('<br>')}</p>`;
      toast('没有保存：填的内容有问题', true);
      return;
    }
    const replaced = (out.replaced || []).length;
    state.pigCore = out.pig_core;
    renderPigCoreDialog();
    toast(replaced ? `已保存（替换了 ${replaced} 条之前填的）` : '已保存');
    // 猪行业数据页就在后面：就地刷新那张卡片与那张表，不重新发一次请求。
    if (state.currentTab === '猪行业数据' && state.pig) {
      state.pig.pig_core = out.pig_core;
      drawerBody.innerHTML = renderTab();
    }
  } catch (e) { toast(e.message, true); }
  finally { if (btn) btn.disabled = false; }
}
async function deletePigCoreEntry(handle) {
  const core = state.pigCore;
  if (!core || !handle) return;
  try {
    const out = await api('/api/research/pig-core/delete', { method: 'POST',
      body: JSON.stringify({ code: core.code, observation_hashes: [handle] }) });
    if (!out.ok) { toast((out.errors || [{}])[0].message || '没能删除', true); return; }
    state.pigCore = out.pig_core;
    renderPigCoreDialog();
    toast('已删除这一条人工补录');
    if (state.currentTab === '猪行业数据' && state.pig) {
      state.pig.pig_core = out.pig_core;
      drawerBody.innerHTML = renderTab();
    }
  } catch (e) { toast(e.message, true); }
}

function renderPigIndustry(s) {
  if (state.pigBusy) return section('猪行业数据', '', '<div class="chart-empty">正在读取观测仓…</div>');
  if (state.pigError) return section('猪行业数据', '', `<div class="chart-empty">读取证据失败：${esc(state.pigError)}</div>`);
  const p = state.pig;
  if (!p) return section('猪行业数据', '', '<div class="chart-empty">暂无数据</div>');
  const all = pigRows();
  const nValue = all.filter((r) => !bad(r.value)).length;
  const filter = state.pigFilter;
  const rows = all.filter((r) => filter === 'all' || (filter === 'value' ? !bad(r.value) : bad(r.value)));
  const body = rows.map((r) => {
    const open = state.pigOpen.has(r.openKey);
    const tags = [
      r.variant_fallback ? `<em class="audit-tag warn">后备 ${esc(r.expected_variant || '')}</em>` : '',
      r.kind === 'store' ? '<em class="audit-tag muted">读数不消费</em>' : '',
      r.is_estimated ? '<em class="audit-tag muted">估算</em>' : '',
    ].filter(Boolean).join(' ');
    const main = `<tr data-pig-toggle="${esc(r.openKey)}" class="audit-row${open ? ' open' : ''}">
      <td>${esc(r.label)}${tags ? ' ' + tags : ''}</td>
      <td class="num">${pigNum(r.value, r.unit, r.missing_text)}</td>
      <td class="small">${pigText(r.metric_variant)}</td>
      <td>${pigText(r.period)}</td>
      <td>${pigText(r.status_label || r.status)}</td>
      <td>${pigText(r.source_level_label)}</td>
      <td class="small">${pigText(r.source_type)}</td>
      <td>${pigFlag(r.is_direct_disclosure)}</td>
      <td>${pigFlag(r.is_estimated)}</td>
      <td class="pig-evidence">${esc(String(r.reason || '—').slice(0, 42))}</td>
      <td><button class="btn btn-sm" data-pig-toggle="${esc(r.openKey)}">${open ? '收起' : '查看证据'}</button></td>
    </tr>`;
    if (!open) return main;
    return main + `<tr class="audit-detail"><td colspan="11">${renderPigReadingDetail(r)}</td></tr>`;
  }).join('') || '<tr><td colspan="11" class="table-empty">该筛选下没有读数</td></tr>';
  const chips = PIG_FILTERS.map(([k, label]) =>
    `<button class="filter-chip ${k === filter ? 'active' : ''}" data-pig-filter="${k}">${label}</button>`).join('');
  const notes = (p.notes || []).length ? `<p class="footnote warn">${p.notes.map(esc).join('；')}</p>` : '';
  const counts = p.counts || {};
  const table = `<h4 class="subsection-title">读数与观测 <span class="muted">（${rows.length} / ${all.length} 行，有值 ${nValue} 项）</span></h4>
    <div class="filter-scroll audit-filters">${chips}</div>
    <div class="table-scroll"><table class="data-table audit-table pig-table"><thead><tr>
      <th>读数 / 指标</th><th class="num">值</th><th>口径</th><th>期间</th><th>状态</th><th>来源层级</th><th>来源类型</th><th>直接披露</th><th>估算</th><th>未取值原因</th><th>证据</th>
    </tr></thead><tbody>${body}</tbody></table></div>`;
  const foot = `<p class="footnote">规则 ${esc(p.rule_version || '—')} · 载荷生成于 ${esc(p.generated_at || '—')} · 观测 ${counts.observations || 0} 条（${(counts.store || {}).groups || 0} 组）· 指标 ${counts.metrics || 0} 个 · 候选 ${counts.candidates || 0} 条 · 冲突组 ${counts.conflicts_group_pairs || 0} 对 · 简报缓存 ${(counts.store || {}).bulletins || 0} 条 · 行业序列 ${(counts.store || {}).series || 0} 点</p>`;
  return section('猪行业数据', '读数与它背后的每一条观测：值、口径、期间、状态、来源层级与原文段落。点开任一行看这一组的全部候选（不含在评分里）',
    renderPigCoreCard(p, s.cohort_label) + notes + table + foot, 'audit-card');
}
async function loadPigIndustry() {
  const code = state.currentCode;
  state.pigBusy = true; state.pigCode = code; state.pigError = null; state.pig = null;
  state.pigOpen = new Set(); state.pigEvidence = {}; state.pigEvidenceBusy = new Set();
  drawerBody.innerHTML = renderTab();
  try {
    const data = await api('/api/research/pig-evidence?code=' + encodeURIComponent(code) + '&candidates=0');
    if (state.currentCode !== code) return;
    state.pig = data; state.pigBusy = false;
  } catch (e) {
    if (state.currentCode !== code) return;
    state.pigBusy = false; state.pigCode = null; state.pigError = e.message;
  }
  if (state.currentTab === '猪行业数据') { drawerBody.innerHTML = renderTab(); drawerBody.scrollTop = 0; }
}
async function loadPigEvidence(row, scrollTop) {
  if (!row.metric_id || state.pigEvidence[row.key]) return;
  const code = state.currentCode;
  state.pigEvidenceBusy.add(row.key);
  drawerBody.innerHTML = renderTab(); drawerBody.scrollTop = scrollTop;
  let url = '/api/research/pig-evidence?code=' + encodeURIComponent(code)
    + '&metric_id=' + encodeURIComponent(row.metric_id);
  if (row.period) url += '&period=' + encodeURIComponent(row.period);
  try {
    const data = await api(url);
    if (state.currentCode !== code) return;
    state.pigEvidence[row.key] = data;
  } catch (e) {
    if (state.currentCode !== code) return;
    state.pigEvidence[row.key] = { error: e.message };
    toast(e.message, true);
  }
  state.pigEvidenceBusy.delete(row.key);
  if (state.currentCode === code && state.currentTab === '猪行业数据') {
    drawerBody.innerHTML = renderTab(); drawerBody.scrollTop = scrollTop;
  }
}

/* ---------- 现金流 ---------- */
function cashflowRows(s) {
  const direct = s.financial_series?.cashflow || [];
  if (direct.length) return direct;
  const operating = s.financial?.operating_cashflow || [], free = s.financial?.free_cashflow || [];
  const report = String(s.latest_report_period || ''), match = report.match(/^(\d{4})-(\d{2})/);
  const latestAnnualYear = match ? Number(match[1]) - (match[2] === '12' ? 0 : 1) : null;
  return operating.map((value, i) => ({ date: latestAnnualYear ? `${latestAnnualYear - operating.length + i + 1}-12-31` : '', operating: value, investing: null, financing: null, free_cashflow: free[i] }));
}
function renderCashflow(s) {
  const rows = cashflowRows(s);
  const labels = dateLabels(rows);
  const scale = (key) => rows.map((x) => bad(x[key]) ? null : Number(x[key]) / 1e8);
  const chart = svgChart([{ name: '经营现金流', values: scale('operating') }, { name: '投资现金流', values: scale('investing') }, { name: '筹资现金流', values: scale('financing') }, { name: '自由现金流', values: scale('free_cashflow') }], { labels, label: '年度现金流趋势', formatY: (v) => v.toFixed(0) + '亿' });
  const v = s.valuation || {}, latest = rows.length ? rows[rows.length - 1] : {};
  const netProfit = lastValue(trendSeries(s, 'net_profit'));
  // 两个时间窗口**各自具名**：1Y 与 3Y 累计。此前这里现算的是单年值，却和评分层
  // 的 3 年累计值共用一个裸名「CFO / 净利润」——青啤 1Y=1.00、3Y=0.95 分居 1 的两侧，
  // 同名不同义直接把人看反。canonical 值由 rules.analyze 写进 valuation_metrics。
  const cfoProfit1Y = v.cfo_net_profit_1y ?? (!bad(latest.operating) && !bad(netProfit) && Number(netProfit) !== 0 ? Number(latest.operating) / Number(netProfit) : null);
  const cfoProfit3Y = v.cfo_net_profit_3y ?? null;
  const cfDebt = !bad(latest.operating) && !bad(s.financial?.balance?.total_liabilities) && Number(s.financial.balance.total_liabilities) !== 0 ? Number(latest.operating) / Number(s.financial.balance.total_liabilities) : null;
  const table = rows.length ? `<div class="table-scroll"><table class="data-table"><thead><tr><th>年度</th><th>经营现金流</th><th>投资现金流</th><th>筹资现金流</th><th>自由现金流</th></tr></thead><tbody>${rows.map((r) => `<tr><td>${period(r.date).slice(0, 4)}</td><td>${amount(r.operating)}</td><td>${amount(r.investing)}</td><td>${amount(r.financing)}</td><td>${amount(r.free_cashflow)}</td></tr>`).join('')}</tbody></table></div>` : '<div class="empty-inline">现金流历史数据不足。</div>';
  return section('现金流', '经营、投资、筹资与自由现金流均按年度展示；缺失项会明确标识', `<div class="chart-panel">${chart}</div>${table}<div class="metric-grid flow-metrics">${metricCard('FCF 收益率', ratio(v.fcf_yield))}${metricCard('CFO / 净利润（3年累计）', ratio(cfoProfit3Y))}${metricCard('CFO / 净利润（1Y）', ratio(cfoProfit1Y))}${metricCard('经营现金流 / 总负债', ratio(cfDebt))}${metricCard('最近自由现金流', amount(latest.free_cashflow))}</div>`, 'chart-card');
}

/* ---------- 风险与历史 ---------- */
function renderRisk(s) {
  const risk = s.risk || {}, flags = risk.flags || [];
  const categories = [['财务风险', flags], ['经营风险', []], ['行业风险', []], ['估值风险', []], ['治理风险', []]];
  const cards = categories.map(([name, items]) => `<div class="risk-category"><div><h4>${name}</h4>${items.length ? '<span class="state attention">需要关注</span>' : '<span class="state normal">当前未触发</span>'}</div><p>${items.length ? items.map((x) => esc(x.detail || x.type)).join('；') : '现有规则未提供该类别的独立信号。'}</p></div>`).join('');
  const table = flags.length ? `<div class="table-scroll"><table class="data-table"><thead><tr><th>风险指标</th><th>状态</th><th>趋势 / 持续时间</th><th>说明</th></tr></thead><tbody>${flags.map((f) => `<tr><td>${esc(f.type)}</td><td>${esc(f.level || '需关注')}</td><td>${f.duration ? '连续 ' + esc(f.duration) + ' 期' : '当前接口未提供'}</td><td>${esc(f.detail || '说明缺失')}</td></tr>`).join('')}</tbody></table></div>` : `<div class="risk-ok large"><b>✓ ${riskLabel(risk.level)}</b><span>在当前数据覆盖与规则范围内，未触发结构化风险提示；请结合行业、治理与市场环境继续判断。</span></div>`;
  // 等级清单从后端下发的那一份现拼，别再手写一遍——手写过一次就已经和后端不一致了。
  const levelHint = `风险等级：${Object.keys(riskLabels()).map((k) => riskLabels()[k]).join('、')}。未覆盖类别会清楚标注，不以空白替代。`;
  return section('风险面板', levelHint, `<div class="risk-hero">${riskBadge(risk.level)}<div><b>${riskLabel(risk.level)}</b><span>当前规则版本：${esc(s.rule_version || '未知')}${legacyRuleMark(s, ' （旧实验结果，与当前规则不可比）')}</span></div></div><div class="risk-categories">${cards}</div><h4 class="subsection-title">风险明细</h4>${table}`);
}
/* 当前生效的规则版本：只从 /api/meta 取，不在前端写死。
   前端不复刻「哪个版本算当前」，否则改规则时这份副本不会跟着改，同一页上
   两处对「旧」的判断就会不一致。 */
function currentRuleVersion() { return state.meta?.rule?.rule_version || null; }
/* 这条记录不是当前口径算出来的——后端判定（engine.is_legacy_rule_version /
   _delta_score 的 audit_ok_at 守卫），前端只读布尔值，不解析版本串。两种成因：
   旧规则版本；或资产语义层还没跑（那时分数的分母里少了一批资产类分量）。
   主记录没重算过时必须标出来：顶栏写着当前规则，这一行却带着旧口径的分数，
   不标就像一个同口径的排名。 */
function legacyRuleMark(s, label) {
  if (!s.legacy_rule) return '';
  return `<i class="legacy-mark" title="这条结果不是在当前口径下算出的（旧规则版本，或资产语义层尚未跑，当时口径为 ${esc(s.rule_version || '未知')}），与当前结果不可比；重新分析后自动更新">${label}</i>`;
}
function uniqueSnapshots(snaps) {
  const seen = new Set();
  return (snaps || []).filter((s) => { const key = [s.current_price, s.report_period, s.legacy_rule, s.total_score, s.type_scores].join('|'); if (seen.has(key)) return false; seen.add(key); return true; });
}
function json(v, fallback) { try { return typeof v === 'string' ? JSON.parse(v) : (v || fallback); } catch (_) { return fallback; } }
/* 快照表。当前规则那张表**没有版本列**：每一行都是同一个版本号，那一列看着
   有信息、其实什么都没带；旧结果桶里版本号是真信息（V1.0/V1.1/V1.2 各不相同），
   所以只有那边带 withVersion。 */
function snapshotRows(snaps, withVersion) {
  return snaps.slice().reverse().map((x) => {
    const ts = json(x.type_scores, {});
    return `<tr><td>${period(x.date)}</td><td>${money(x.current_price)}</td><td><b>${score(x.total_score)}</b></td><td>${score(ts.growth)}</td><td>${score(ts.quality)}</td><td>${score(ts.value)}</td>${withVersion ? `<td>${text(x.rule_version, '版本缺失')}</td>` : ''}</tr>`;
  }).join('');
}
const HISTORY_TH = '<tr><th>日期</th><th>价格</th><th>总评分</th><th>成长</th><th>质量</th><th>价值</th></tr>';
/* 旧口径的快照不是「历史趋势」，是**别的口径下算出来的数**。混进同一张折线
   图会被读成「评分在变」，所以默认只标注、不参与趋势，收进折叠区。措辞沿用资产
   审计页那条先例：旧快照只追加不改写，所以这里只标注、不重算。
   成因有两种（旧规则版本 / 资产语义层尚未跑），都由后端 legacy_rule 一处判定。 */
function legacyHistoryBlock(snaps) {
  const open = state.historyLegacyOpen;
  const head = `<button class="score-accordion-head" type="button" data-history-legacy-toggle aria-expanded="${open}"><span>旧实验结果</span><em>${snaps.length} 条快照 · 与当前口径不可比</em><b></b><i aria-hidden="true">⌄</i></button>`;
  if (!open) return `<section class="score-accordion">${head}</section>`;
  return `<section class="score-accordion is-open">${head}<div class="score-accordion-content"><p class="footnote">这些快照不是在当前口径下算出的（旧规则版本，或当时资产语义层还没跑，分母里少了一批资产类分量）。按既定口径快照只追加不改写，所以这里只标注、不重算；口径不同，分数之间不可比，也不参与上方的趋势图与「较上次变化」。</p><div class="table-scroll"><table class="data-table app-table"><thead><tr>${HISTORY_TH}<th>规则版本</th></tr></thead><tbody>${snapshotRows(snaps, true)}</tbody></table></div></div></section>`;
}
function renderHistory(s) {
  const all = s.snapshots || [], metric = state.historyMetric;
  // 默认只展示**当前规则**重算出的结果。legacy_rule 由后端判定
  // （engine.is_legacy_rule_version + _mark_legacy_snapshots 的 audit_ok_at 守卫），
  // 前端不自己比版本串、也不自己比审计时间。
  const latest = all.filter((x) => !x.legacy_rule);
  const snaps = uniqueSnapshots(latest), legacy = all.filter((x) => x.legacy_rule);
  const values = snaps.map((x) => metric === 'total' ? x.total_score : json(x.type_scores, {})[metric]);
  const chart = snaps.length > 1
    ? `<div class="chart-panel">${svgChart([{ name: metric === 'total' ? '总评分' : TYPE_LABELS[metric], values }], { labels: snaps.map((x) => (x.date || '').slice(5, 10)), label: '评分历史' })}</div>`
    : (snaps.length === 0 && legacy.length
      ? `<div class="history-empty-state"><b>尚无当前口径下的评分</b><span>这只股票还没有在当前口径（${text(currentRuleVersion(), '版本未知')}）下重算过；旧口径的结果收在下方。</span></div>`
      : '<div class="history-empty-state"><b>尚无可比评分历史</b><span>当前仅有一条有效快照；后续评分发生真实变化后，这里会自动展示趋势。</span></div>');
  const merged = Math.max(0, latest.length - snaps.length);
  const note = `仅展示当前规则（${text(currentRuleVersion(), '版本未知')}）重算的结果`
    + (merged ? `，已合并 ${merged} 条内容相同的重复快照` : '')
    + (legacy.length ? `；另有 ${legacy.length} 条旧口径的快照收在下方` : '');
  const change = snaps.length > 1 && !bad(snaps.at(-1).total_score) && !bad(snaps.at(-2).total_score)
    ? delta(Number(snaps.at(-1).total_score) - Number(snaps.at(-2).total_score)) : '无可比历史';
  return section('历史评分', note, `<div class="metric-switch segmented-control history-switch">${[['total', '总评分'], ['growth', '成长'], ['quality', '质量'], ['value', '价值']].map(([key, label]) => `<button class="${key === metric ? 'active' : ''}" data-history="${key}">${label}</button>`).join('')}</div>${chart}<div class="history-change"><span>较上次变化</span><b>${change}</b></div><div class="table-scroll"><table class="data-table app-table"><thead>${HISTORY_TH}</thead><tbody>${snapshotRows(snaps, false) || '<tr><td colspan="6" class="table-empty">暂无评分历史</td></tr>'}</tbody></table></div>${legacy.length ? legacyHistoryBlock(legacy) : ''}`, 'chart-card');
}

/* ---------- 价格模拟 ---------- */
function renderSimulation(s) {
  const v = s.valuation || {}, ready = !bad(v.price);
  const preface = `<div class="sim-summary"><div>${metricCard('当前价格', money(v.price))}</div><div>${metricCard('当前总评分', score(s.total_score))}</div><div>${metricCard('估值分', score(s.attr_scores?.value?.score))}</div><div>${metricCard('合理区间状态', '<span class="range-missing">当前模型暂未定义合理价格区间</span>', '不会以虚构区间替代')}</div></div>`;
  const controls = `<div class="sim-controls"><label>自定义模拟价格 <input type="number" id="sim-price" min="0.01" step="0.01" value="${bad(v.price) ? '' : Number(v.price).toFixed(2)}"></label><button class="btn btn-sm btn-primary app-button" id="sim-one" ${ready ? '' : 'disabled'}>计算</button><span>模拟仅在内存中计算，不修改真实价格、快照、观察价或持仓。</span></div>`;
  return section('价格模拟', '默认展示当前价附近的敏感性；总评分和估值分分图呈现，避免混淆', preface + controls + '<div id="sim-result" class="sim-loading">正在生成默认价格敏感性…</div>', 'simulation-card');
}
async function runDefaultSimulation() {
  const s = state.currentDetail, current = s?.valuation?.price;
  const root = $('#sim-result');
  if (!root || bad(current)) { if (root) root.innerHTML = '<div class="chart-empty">当前价格缺失，无法运行价格模拟。</div>'; return; }
  const changes = [-.20, -.10, -.05, 0, .05, .10, .20];
  try {
    const results = await Promise.all(changes.map((change) => api('/api/research/simulate', { method: 'POST', body: JSON.stringify({ code: state.currentCode, price: Number((Number(current) * (1 + change)).toFixed(2)) }) })));
    if (state.currentTab !== '价格模拟' || !state.currentDetail || state.currentDetail.code !== s.code) return;
    state.sim = results.filter(Boolean); renderSimulationResult(state.sim, Number(current));
  } catch (e) { if (root) root.innerHTML = `<div class="chart-empty">默认模拟暂不可用：${esc(e.message)}</div>`; }
}
function renderSimulationResult(results, current) {
  const root = $('#sim-result'); if (!root) return;
  if (!results.length) { root.innerHTML = '<div class="chart-empty">模拟所需的财务基础数据不足。</div>'; return; }
  const labels = results.map((x) => moneyText(x.price));
  const totalChart = svgChart([{ name: '总评分', values: results.map((x) => x.total_score), color: '#2563eb' }], { labels, label: '价格与总评分关系', width: 560, height: 300 });
  const valueChart = svgChart([{ name: '估值分', values: results.map((x) => x.value_score), color: '#13a36f' }], { labels, label: '价格与估值分关系', width: 560, height: 300 });
  const table = `<div class="table-scroll"><table class="data-table sim-table"><thead><tr><th>模拟价格</th><th>涨跌幅</th><th>总评分</th><th>估值分</th><th>PE(TTM)</th><th>PB</th><th>FCF 收益率</th><th>股息率</th></tr></thead><tbody>${results.map((r) => { const val = r.valuation || {}, active = Math.abs(Number(r.price) - current) < .005; return `<tr class="${active ? 'current-row' : ''}"><td>${money(r.price)}${active ? '<span class="current-tag">当前</span>' : ''}</td><td>${percent((Number(r.price) / current - 1) * 100)}</td><td><b>${score(r.total_score)}</b></td><td>${score(r.value_score)}</td><td>${multiple(val.pe_ttm)}</td><td>${multiple(val.pb)}</td><td>${ratio(val.fcf_yield)}</td><td>${ratio(val.dividend_yield)}</td></tr>`; }).join('')}</tbody></table></div>`;
  const currentSim = results.find((r) => Math.abs(Number(r.price) - current) < .005);
  const persisted = state.currentDetail?.total_score;
  const baselineNote = currentSim && !bad(persisted) && !bad(currentSim.total_score)
    && Math.abs(Number(persisted) - Number(currentSim.total_score)) > .05
    ? '<p class="footnote">提示：详情页评分为最近一次已保存研究结果；0% 模拟按当前本地缓存重新计算。两者的基础字段覆盖范围不同，因此可能存在差异。</p>' : '';
  root.innerHTML = `<div class="sim-chart-title"><h4>价格敏感性</h4><span>横轴为模拟价格；两项评分独立呈现</span></div><div class="simulation-charts"><div class="chart-panel sim-chart">${totalChart}</div><div class="chart-panel sim-chart">${valueChart}</div></div>${table}${baselineNote}`;
}
function moneyText(v) { return bad(v) ? '缺失' : Number(v).toFixed(2); }
async function runOneSimulation() {
  const input = $('#sim-price'), price = Number(input?.value);
  if (!Number.isFinite(price) || price <= 0) { toast('请输入大于 0 的模拟价格', true); return; }
  try {
    const r = await api('/api/research/simulate', { method: 'POST', body: JSON.stringify({ code: state.currentCode, price }) });
    if (!r) throw new Error('模拟所需财务数据不足');
    const results = (state.sim || []).filter((x) => Math.abs(Number(x.price) - price) > .005).concat([r]).sort((a, b) => a.price - b.price);
    state.sim = results; renderSimulationResult(results, Number(state.currentDetail.valuation.price));
  } catch (e) { toast(e.message, true); }
}

/* ---------- 交互 ---------- */
document.addEventListener('click', (e) => {
  if (e.target.closest('[data-pig-core-open]')) {
    // 已有组合成员的轻量补充入口（§十）。载荷就在猪行业数据那一份里，不再发请求。
    openPigCoreDialog(state.pig?.pig_core);
    return;
  }
  if (e.target.closest('[data-pig-core-close]')) { closePigCoreDialog(); return; }
  const pigDelete = e.target.closest('[data-pig-core-delete]');
  if (pigDelete) { deletePigCoreEntry(pigDelete.dataset.pigCoreDelete); return; }
  if (e.target.closest('#pig-core-save')) { submitPigCore(); return; }
  const sortOption = e.target.closest('[data-sort-option]');
  if (sortOption) {
    state.sort = sortOption.dataset.sortOption;
    $('#sort-menu').hidden = true; $('#sort-toggle').setAttribute('aria-expanded', 'false'); renderLibrary(); return;
  }
  if (e.target.closest('#sort-toggle')) {
    const menu = $('#sort-menu'), open = menu.hidden;
    menu.hidden = !open; $('#sort-toggle').setAttribute('aria-expanded', String(open)); return;
  }
  if (!e.target.closest('#sort-select')) { $('#sort-menu').hidden = true; $('#sort-toggle').setAttribute('aria-expanded', 'false'); }
  const auditRetry = e.target.closest('[data-audit-retry]');
  if (auditRetry) {
    if (auditRetry.disabled) return;
    // 详情页那颗按钮 = 重新排队（analyze 会先入队、不给分）；资产审计页那颗 =
    // 快照在但取不出 payload 的兜底，带 refresh=1 直接重新审计这一只。
    if (auditRetry.dataset.auditRetry === 'refresh') loadAssetAudit(true);
    else analyze(state.currentCode, false);
    return;
  }
  const scoreToggle = e.target.closest('[data-score-toggle]');
  if (scoreToggle) {
    const key = scoreToggle.dataset.scoreToggle, scrollTop = drawerBody.scrollTop;
    if (state.scoreOpen.has(key)) state.scoreOpen.delete(key); else state.scoreOpen.add(key);
    drawerBody.innerHTML = renderTab(); drawerBody.scrollTop = scrollTop; return;
  }
  const auditFilter = e.target.closest('[data-audit-filter]');
  if (auditFilter) { state.auditFilter = auditFilter.dataset.auditFilter; state.auditOpen = new Set(); drawerBody.innerHTML = renderTab(); return; }
  const auditRow = e.target.closest('[data-audit-toggle]');
  if (auditRow) {
    const key = Number(auditRow.dataset.auditToggle), scrollTop = drawerBody.scrollTop;
    if (state.auditOpen.has(key)) state.auditOpen.delete(key); else state.auditOpen.add(key);
    drawerBody.innerHTML = renderTab(); drawerBody.scrollTop = scrollTop; return;
  }
  const pigFilter = e.target.closest('[data-pig-filter]');
  if (pigFilter) { state.pigFilter = pigFilter.dataset.pigFilter; state.pigOpen = new Set(); drawerBody.innerHTML = renderTab(); return; }
  const pigRow = e.target.closest('[data-pig-toggle]');
  if (pigRow) {
    // 与资产审计的展开同一条：先保留滚动位置再重渲染。点开一行时顺手把那一组的
    // 证据取回来（概览不带候选明细），取的过程里这一行显示「正在取证据…」。
    const key = pigRow.dataset.pigToggle, scrollTop = drawerBody.scrollTop;
    if (state.pigOpen.has(key)) { state.pigOpen.delete(key); drawerBody.innerHTML = renderTab(); drawerBody.scrollTop = scrollTop; return; }
    state.pigOpen.add(key);
    const found = pigRows().find((r) => r.openKey === key);
    drawerBody.innerHTML = renderTab(); drawerBody.scrollTop = scrollTop;
    if (found) loadPigEvidence(found, scrollTop);
    return;
  }
  const row = e.target.closest('tr[data-code]');
  if (row) { openDetail(row.dataset.code); return; }
  const tab = e.target.closest('.drawer-tab');
  if (tab) { state.currentTab = tab.dataset.tab; state.sim = null; renderTabs(); renderDetailBody(); return; }
  const trend = e.target.closest('[data-trend]');
  if (trend) { state.trendMetric = trend.dataset.trend; renderDetailBody(); return; }
  const history = e.target.closest('[data-history]');
  if (history) { state.historyMetric = history.dataset.history; renderDetailBody(); return; }
  if (e.target.closest('[data-history-legacy-toggle]')) {
    // 与评分细则的折叠一样保留滚动位置：展开位置在页面底部，滚回顶部等于
    // 把人刚点的东西甩出视野。
    const scrollTop = drawerBody.scrollTop;
    state.historyLegacyOpen = !state.historyLegacyOpen;
    drawerBody.innerHTML = renderTab(); drawerBody.scrollTop = scrollTop; return;
  }
  if (e.target.closest('#sim-one')) { runOneSimulation(); }
});
$('#type-seg').addEventListener('click', (e) => {
  const button = e.target.closest('.filter-chip'); if (!button) return;
  state.model = button.dataset.model; renderLibrary();
});
document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && !drawer.hidden) closeDetail(); });
window.addEventListener('popstate', () => { const code = new URLSearchParams(location.search).get('stock'); if (code) openDetail(code); else closeDetail(); });

const initStock = initialParams.get('stock');
// 先取模型字典再渲染列表：否则 chip 会先按「遇到的顺序」闪一下再重排。
loadMeta().then(loadList).then(() => { if (initStock) openDetail(initStock, initialTab !== '概览'); });
