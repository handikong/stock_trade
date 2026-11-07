import {connectPortfolio} from './portfolio.js?v=20261107-principal-loss';

const $ = selector => document.querySelector(selector);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const validNumber = v => v !== null && v !== undefined && v !== '' && Number.isFinite(Number(v));
const num = (v, digits = 2) => validNumber(v) ? Number(v).toLocaleString('en-US', {minimumFractionDigits:digits === 2 ? 2 : 0, maximumFractionDigits:digits}) : '—';
const signed = v => validNumber(v) ? `${Number(v) > 0 ? '+' : ''}${num(v)}` : '—';
const pct = v => validNumber(v) ? `${(Number(v) * 100).toFixed(2)}%` : '—';
const tone = v => !validNumber(v) || Number(v) === 0 ? 'muted' : Number(v) > 0 ? 'gain' : 'loss';
const when = value => {
  if (!value) return '—';
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? '时间待核对' : date.toLocaleString('zh-CN', {timeZone:state.timezone, hour12:false, month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'});
};
const paths = {
  wallet:'<path d="M20 8V5a1 1 0 0 0-1-1H5a3 3 0 0 0 0 6h15v10H5a3 3 0 0 1-3-3V7m18 6h-5v4h5"/>',
  radar:'<circle cx="12" cy="12" r="9"/><path d="M12 3v9l6 6M5.7 5.7 12 12"/><circle cx="12" cy="12" r="4"/>',
  bell:'<path d="M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9M10 21h4"/>',
  search:'<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/>',
  plus:'<path d="M12 5v14M5 12h14"/>', refresh:'<path d="M20 7v5h-5M4 17v-5h5M19 11a7 7 0 0 0-12-5L4 9m16 6-3 3a7 7 0 0 1-12-5"/>',
  arrow:'<path d="M4 12h16m-6-6 6 6-6 6"/>', close:'<path d="m6 6 12 12M6 18 18 6"/>',
  pie:'<path d="M12 3v9h9M9 3.5A9 9 0 1 0 20.5 15"/>',
  info:'<circle cx="12" cy="12" r="9"/><path d="M12 11v6m0-10v1"/>', play:'<path d="m8 4 12 8-12 8z"/>',
  research:'<path d="M5 3h10l4 4v13H5zM14 3v5h5M8 12h8m-8 4h5"/>',
  download:'<path d="M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5"/>',
  copy:'<rect x="8" y="8" width="12" height="13" rx="2"/><path d="M16 8V3H3v13h5"/>'
};
const icon = name => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths[name] || paths.info}</svg>`;
const badge = (label, kind = '') => `<span class="badge ${kind}">${esc(label)}</span>`;
const palette = ['#558d88','#627fa5','#9280ad','#c29765','#75959c','#b67d81','#808f69'];
function icons(root = document) { root.querySelectorAll('[data-icon]').forEach(node => { node.innerHTML = icon(node.dataset.icon); }); }
function empty(title, description, button = '', small = false) {
  return `<div class="empty-state ${small ? 'small' : ''}"><div class="empty-icon">${icon('wallet')}</div><h3>${esc(title)}</h3><p>${esc(description)}</p>${button}</div>`;
}
function table(headers, rows, numeric = []) {
  return `<div class="table-wrap"><table><thead><tr>${headers.map((h,i) => `<th class="${numeric.includes(i) ? 'numeric' : ''}">${esc(h)}</th>`).join('')}</tr></thead><tbody>${rows.map(row => `<tr>${row.map((cell,i) => `<td class="${numeric.includes(i) ? 'numeric' : ''}">${cell}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
}
function stock(code, name = '', index = 0) {
  return `<div class="stock-cell"><span class="stock-avatar" style="color:${palette[index % palette.length]}">${esc(code.slice(0,2))}</span><div><a class="stock-link" href="#/research?symbol=${encodeURIComponent(code)}">${esc(code)}</a><span class="stock-name">${esc(name && name !== code ? name : '美股 · USD')}</span></div></div>`;
}
const state = {page:'holdings',symbol:'',report:null,workspace:null,timezone:'Asia/Shanghai',generation:0,workspaceGeneration:0,
  positions:new Map(),editPosition:null,pendingRemoval:null,jobs:new Map(),valuationEdit:null,booting:false,connected:false};
const pushHistory = {generation:0,query:'',cursor:null,groups:new Map()};
let liveHoldings = null, intradayPending = false;

function notice(message = '', error = false) {
  const node = $('#notice'); node.textContent = message; node.hidden = !message; node.classList.toggle('error',error);
  node.setAttribute('role',error ? 'alert' : 'status');
}
function formError(id, message = '') { const node = $(id); node.textContent = message; node.hidden = !message; }
async function api(path, options = {}) {
  const headers = {...options.headers};
  if (options.body) headers['Content-Type'] = 'application/json';
  let response;
  try { response = await fetch('/api/v1' + path, {...options,headers,cache:'no-store'}); }
  catch { throw new Error('无法连接服务，请确认后台已启动后重试。'); }
  let data;
  try { data = await response.json(); }
  catch { throw new Error('服务未返回有效结果，请稍后重试。'); }
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '请求未完成，请核对输入。');
  return data;
}
const post = (path, data = {}) => api(path,{method:'POST',body:JSON.stringify(data)});
async function busy(button, fn) {
  if (button.disabled) return;
  button.disabled = true; button.setAttribute('aria-busy','true');
  try { return await fn(); }
  finally { button.disabled = false; button.removeAttribute('aria-busy'); updateBusy(); }
}
function updateBusy() {
  const active = kind => [...state.jobs.values()].some(j => j.kind === kind);
  $('#scan-start').disabled = !state.workspace?.watchlist?.items?.length || active('watchlist_scan');
  $('#refresh-holdings').disabled = !state.workspace?.holdings?.items?.length || active('holdings_refresh');
  $('#research-refresh').disabled = !state.symbol || [...state.jobs.values()].some(j => j.kind === 'refresh' && j.symbol === state.symbol);

}

function metric(label, value, note = '', valueTone = '', currency = false) {
  return `<div class="metric"><span>${esc(label)}</span><strong class="${valueTone}">${currency && value !== '—' ? '<small>USD</small>' : ''}${esc(value)}</strong><p>${esc(note)}</p></div>`;
}
function renderHoldings() {
  const data = state.workspace.holdings, s = data.summary;
  state.positions = new Map(data.items.map(r => [r.id,r]));
  $('#holding-count').textContent = s.count;
  $('#positions-total').textContent = `${s.count} 只`;
  $('#portfolio-metrics').innerHTML = metric('持仓市值',num(s.market_value),s.count ? `有效报价 ${s.priced_count} / ${s.count} 只` : '添加持仓后计算', '', true)
    + metric('持仓成本',num(s.count ? s.cost_value : null),'按录入数量与成本均价', '', true)
    + metric('持仓浮动盈亏',signed(s.unrealized_pnl),validNumber(s.unrealized_return) ? `相对成本 ${pct(s.unrealized_return)}` : '等待完整有效行情',tone(s.unrealized_pnl),true)
    + metric('持仓股票',String(s.count),'持仓与扫描名单分别管理');
  $('#portfolio-note').textContent = data.message;
  if (!data.items.length) {
    $('#positions-content').innerHTML = empty('记录你的第一笔持仓','输入股票、数量与成本均价，建立属于自己的持仓总览。',`<button class="secondary" data-add-position>${icon('plus')}添加持仓</button>`);
  } else {
    $('#positions-content').innerHTML = table(['股票','持股数量','成本均价','收盘参考','参考市值','浮动盈亏','操作'],data.items.map((r,i) => {
      const q = r.quote, quoted = q.currency === r.currency;
      const live = field => `<span class="subline" data-intraday-field="${field}" data-position-id="${esc(r.id)}" hidden></span>`;
      return [stock(r.symbol,r.name,i),`${num(r.quantity,6)}<span class="subline">股</span>`,num(r.average_cost),
        `${num(quoted ? q.price : null)}<span class="subline">${esc(q.date || '尚无行情')} ${!quoted ? '· 币种待核对' : q.state !== 'fresh' ? '· 待更新' : ''}</span>${live('price')}`,
        `${num(r.reference_market_value)}${q.state !== 'fresh' ? '<span class="subline">仅历史参考</span>' : ''}${live('market_value')}`,
        `<span class="${tone(r.unrealized_pnl)}">${signed(r.unrealized_pnl)}</span><span class="subline">${pct(r.unrealized_return)}</span>${live('pnl')}`,
        `<div class="row-actions"><button class="text-button" data-edit-position="${esc(r.id)}">编辑</button><button class="text-button remove" data-remove-position="${esc(r.id)}">移除</button></div>`];
    }),[1,2,3,4,5]);
  }
  if (!s.complete) {
    $('#allocation-content').innerHTML = empty(s.count ? '行情尚未齐全' : '等待添加持仓',s.count ? '更新持仓行情后显示市值构成。缺失和过期价格不会计为零。' : '持仓构成会在这里展示。', '', true);
  } else {
    const rows = data.items;
    $('#allocation-content').innerHTML = `<div class="allocation-body"><div class="allocation-bar" aria-hidden="true">${rows.map((r,i) => `<span style="flex:${Math.max(0,Number(r.weight))};background:${palette[i % palette.length]}"></span>`).join('')}</div>${rows.map((r,i) => `<div class="allocation-row"><i style="background:${palette[i % palette.length]}" aria-hidden="true"></i><strong>${esc(r.symbol)}</strong><span>${pct(r.weight)}</span></div>`).join('')}<p class="allocation-note">仅表示已录入股票内部的市值构成，不包含账户现金。</p></div>`;
  }
  renderIntraday();
}
function renderIntraday() {
  const data = liveHoldings;
  const status = $('#intraday-state'), summary = $('#intraday-summary');
  const note = [data?.state || '正在读取盘中监控状态',data?.market && `大盘 ${data.market}`,
    data?.session ? (data.push_active ? '盘中提醒开启' : '盘中提醒静默') : '',
    data?.excluded?.length ? `${data.excluded.length} 只超出订阅上限` : ''].filter(Boolean).join(' · ');
  if (status.textContent !== note) status.textContent = note;
  const info = Object.entries(data?.benchmarks || {}).map(([code,quote]) => `${code} ${num(quote.price)}`);
  if (data?.total) info.push(`盘中市值 ${num(data.total.market_value)} USD`, `盘中浮盈亏 ${signed(data.total.pnl)} USD`);
  summary.textContent = info.join(' · ');
  summary.hidden = !info.length;
  for (const node of document.querySelectorAll('[data-intraday-field]')) {
    const position = state.positions.get(node.dataset.positionId);
    const quote = data?.items?.[position?.symbol];
    const field = node.dataset.intradayField;
    const value = quote?.version === position?.version ? quote?.[field] : null;
    node.hidden = !validNumber(value);
    node.classList.toggle('gain',field === 'pnl' && tone(value) === 'gain');
    node.classList.toggle('loss',field === 'pnl' && tone(value) === 'loss');
    if (!node.hidden) {
      if (field === 'price') {
        const evidence = quote.scale_review ? '报价口径待核对' : [quote.vwap && `VWAP ${num(quote.vwap)}`,
          quote.ema20 && `日线 EMA20 ${num(quote.ema20)}`, quote.relative_volume && `量比 ${num(quote.relative_volume)} 倍`,
          quote.support && `支撑 ${quote.support.map(v => num(v)).join('–')}`,
          quote.resistance && `压力 ${quote.resistance.map(v => num(v)).join('–')}`,
          quote.signal].filter(Boolean).join(' · ');
        node.textContent = `IEX ${num(value)} · ${when(quote.at)}${evidence ? ' · ' + evidence : ''}`;
      } else node.textContent = `盘中 ${field === 'pnl' ? signed(value) : num(value)}`;
    }
  }
}
async function loadIntraday() {
  if (!state.connected || state.page !== 'holdings' || document.hidden || intradayPending) return;
  intradayPending = true;
  try { liveHoldings = await api('/intraday'); }
  catch { liveHoldings = {state:'暂时无法读取盘中监控状态'}; }
  finally { intradayPending = false; }
  renderIntraday();
}
function renderPool() {
  const data = state.workspace.watchlist, run = data.last_scan;
  $('#pool-count').textContent = data.summary.count; $('#pool-total').textContent = `${data.summary.count} 只`;
  $('#scan-summary-title').textContent = `关注 ${data.summary.count} 只股票`;
  $('#scan-summary-detail').textContent = data.configuration_error || `当前有效信号涉及 ${data.summary.signal_stocks} 只股票；扫描不会改变持仓或发送通知。`;
  $('#last-scan-label').textContent = run ? `最近扫描 ${when(run.finished_at || run.created_at)} · ${({complete:'完成',partial:'部分数据待核对',running:'进行中',interrupted:'已中断'})[run.status] || run.status}` : '尚无扫描记录，添加股票后开始扫描';
  if (!data.items.length) {
    $('#pool-content').innerHTML = empty('把值得关注的股票放进来','这里与命令行共用扫描名单，你可以添加、移除，再手动开始扫描。',`<button class="secondary" data-add-pool>${icon('plus')}添加股票</button>`);
    return;
  }
  $('#pool-content').innerHTML = table(['股票','收盘参考','行情状态','最近策略结果','扫描截至','操作'],data.items.map((r,i) => {
    const signals = r.current_signals || [], old = r.scan;
    const signal = !old ? badge('尚未扫描') : !r.scan_is_current ? badge(old.status === 'ERROR' ? '扫描未完成' : '历史记录 · 需重扫','caution') : signals.length ? signals.map(s => badge(s.event_kind === 'EXIT' ? '退出提示' : '买入提示',s.event_kind === 'EXIT' ? 'caution' : 'positive')).join(' ') : badge('暂无当前信号');
    const alerts = r.scan_is_current ? old.current_exit_alerts || [] : [];
    const alertDetails = alerts.length ? `<details class="inline-note"><summary>退出状态变化 · ${alerts.length} 项</summary>${alerts.map(a => `<p>${esc(a.strategy_id)} · 原买点 ${esc(a.entry_signal_date)}：${esc(a.status_label)}</p>`).join('')}</details>` : '';
    const info = old?.error?.message || (signals.length ? signals.map(s => s.name || s.strategy_name || s.strategy_id).join(' / ') : old?.warnings?.[0]?.message || '');
    const strategyDetails = old?.strategies?.length ? `<details class="inline-note"><summary>策略判断${r.scan_is_current ? '' : '（历史）'}</summary>${old.strategies.map(s => `<p>${esc(s.name)}：${esc(s.status_label)}${s.unmet?.length ? ` · ${esc(s.unmet.join('；'))}` : ''}</p>`).join('')}</details>` : '';
    return [stock(r.symbol,r.name,i),num(r.quote.currency === 'USD' ? r.quote.price : null),badge(({fresh:'有效',stale:'待更新',missing:'未获取',unknown:'待核对'})[r.quote.state] || '待核对',r.quote.state === 'fresh' ? 'positive' : 'caution'),`${signal}${info ? `<span class="subline">${esc(info)}</span>` : ''}${alertDetails}${strategyDetails}`,esc(old?.data_as_of || '—'),`<div class="row-actions"><a class="text-link" href="#/research?symbol=${encodeURIComponent(r.symbol)}">研究</a><button class="text-button remove" data-remove-pool="${esc(r.symbol)}">移出</button></div>`];
  }),[1]);
}
async function loadWorkspace() {
  const ticket = ++state.workspaceGeneration;
  const value = await api('/workspace');
  if (ticket !== state.workspaceGeneration) return;
  state.workspace = value; renderHoldings(); renderPool(); updateBusy();
  window.dispatchEvent(new Event("portfolio:workspace"));
}

function renderWeeklyValuation(view) {
  const {eps, forward_pe:pe, attempt, scenarios, corrected_at:corrected} = view;
  const grid = scenarios.length ? `<div class="scenario-grid">${scenarios.map((row,i) =>
    `<div class="scenario ${i === 1 ? 'base' : ''}"><span>${esc(row.name)}</span><strong>${num(row.price)} USD</strong><p>EPS ${num(row.eps,4)} × PE ${num(row.pe)}</p></div>`).join('')}</div>` : '';
  const note = [
    attempt && `Stock Analysis · 最近尝试 ${attempt.date}（北京时间）${view.stale ? ' · 含一周前资料' : ''}`,
    corrected && `含网页更正 · ${when(corrected)}`,
    eps && `财年截至 ${eps.period_end} · 三档 EPS ${num(eps.low,4)} / ${num(eps.mean,4)} / ${num(eps.high,4)} ${eps.currency_per_share}${eps.fetched_at ? ' · 源数据 ' + when(eps.fetched_at) : ''}`,
    pe && `Forward PE ${num(pe.p25)} / ${num(pe.p50)} / ${num(pe.p75)}${pe.sample_count != null ? ' · 源表样本 ' + pe.sample_count : ''}${pe.fetched_at ? ' · 源数据 ' + when(pe.fetched_at) : ''}`,
    view.message,
    ...(attempt?.issues || []).map(issue => '自动来源：' + issue),
  ].filter(Boolean).map(line => `<p class="inline-note">${esc(line)}</p>`).join('');
  return grid + note;
}
function zoneCard(z) {
  const quoted = z.quote_low != null && z.quote_high != null, low = quoted ? z.quote_low : z.low, high = quoted ? z.quote_high : z.high;
  const reasons = [...(z.strength_reasons || []),...(z.weakness_reasons || [])];
  return `<article class="level"><div class="level-top"><strong class="level-price">${num(low)} — ${num(high)}</strong>${badge(z.strength_label || z.strength || '待核对',z.strength === 'strong' ? 'positive' : z.role_status !== 'confirmed' ? 'caution' : '')}</div><p>${quoted ? '报价尺度' : '技术调整价 · 不直接等同报价'} · ${z.role_status === 'confirmed' ? '角色已确认' : '角色待确认'}</p>${reasons.length ? `<details><summary>查看技术依据</summary>${reasons.map(r => `<p>${esc(r)}</p>`).join('')}</details>` : ''}</article>`;
}
function renderTechnical(report) {
  const t = report.market?.technical;
  if (!t) return empty('尚无技术结果','首次查询需要获取日线，可点击“更新行情”。','',true);
  if (t.stale || report.cache.state !== 'fresh' || t.status !== 'ready') return `<div class="notice">${esc(t.message || '行情或技术结果需要更新，当前不发布旧支撑压力。')}</div>`;
  const zones = t.zones || [];
  let html = `<div class="technical-grid">${[['support','支撑区间','support'],['resistance','压力区间','resistance']].map(([kind,label,cls]) => `<div><h3 class="level-heading ${cls}">${icon(kind === 'support' ? 'pie' : 'radar')}${label}</h3>${zones.filter(z => z.kind === kind).slice(0,4).map(zoneCard).join('') || '<p class="inline-note">尚未形成可展示的区间，不补造价位。</p>'}</div>`).join('')}</div>`;
  const inside = zones.filter(z => z.kind === 'inside');
  if (inside.length) html += `<details class="inline-note"><summary>现价所在区间 · 方向待确认</summary>${inside.map(zoneCard).join('')}</details>`;
  return html;
}
function renderCombined(report) {
  const c = report.fundamental_technical || {}, zones = report.market?.technical?.zones || [];
  const notes = [...(c.issues || []),...(c.excluded_models || []).map(r => r.message)];
  let html = notes.map(n => `<p class="inline-note">${esc(n)}</p>`).join('');
  if (report.cache.state !== 'fresh' || report.market?.technical?.stale) return html || '<p class="inline-note">行情或技术需要更新，暂不展示旧区间的估值配合。</p>';
  html += (c.zones || []).filter(r => ['support','resistance'].includes(r.kind)).map(r => {
    const z = zones[r.zone_index] || {};
    return `<div class="comparison-row"><div><strong>${esc(z.strength_label || (r.kind === 'support' ? '支撑' : '压力'))} · ${num(z.quote_low)} — ${num(z.quote_high)}</strong><p>${esc((r.reasons || []).join('；'))}</p></div>${badge(r.confluence ? '研究共振' : r.label || '资料不足',r.status === 'SUPPORTED' ? 'positive' : r.status === 'CONFLICT' ? 'negative' : '')}</div>`;
  }).join('');
  return html || '<p class="inline-note">资料齐全后显示每个区间与估值的配合情况，原技术强弱保持独立。</p>';
}
function renderResearch() {
  const r = state.report;
  if (!r) return;
  $('#research-empty').hidden = true; $('#research-body').hidden = false;
  $('#company-symbol').textContent = r.symbol; $('#company-name').textContent = r.profile.name || r.symbol;
  $('#company-type').textContent = ({EQUITY:'股票',ETF:'ETF',MUTUALFUND:'基金'})[r.profile.type] || '类型待确认';
  $('#company-avatar').textContent = r.symbol.slice(0,2); $('#research-symbol').value = r.symbol;
  const valuation = r.automatic_valuation_reference;
  $('#research-metrics').innerHTML = metric('最近完整日收盘',num(r.market?.last_price),`${r.market?.last_complete_session || '尚无行情'} · ${r.profile.currency || '币种待确认'}`)
    + metric('行情状态',({fresh:'有效',stale:'待更新',missing:'未获取',unknown:'待核对'})[r.cache.state] || '待核对',r.market?.fetched_at ? `取得 ${when(r.market.fetched_at)}` : '仅后台获取K线')
    + metric('估值状态',valuation ? (valuation.scenarios.length ? '可试算' : '待补齐') : '不适用',valuation?.corrected_at ? '含网页更正' : '周六自动更新');
  $('#valuation-content').innerHTML = valuation ? renderWeeklyValuation(valuation)
    : '<p class="inline-note">基金不套用公司EPS估值，可继续查看技术支撑压力。</p>';
  $('#valuation-jump').hidden = !valuation;
  $('#technical-content').innerHTML = renderTechnical(r);
  $('#combined-content').innerHTML = renderCombined(r);
  updateBusy();
  window.dispatchEvent(new Event('portfolio:research'));
}
async function loadStock(code) {
  const ticket = ++state.generation;
  notice('正在读取研究档案…'); $('#research-empty').hidden = false; $('#research-body').hidden = true; $('#research-job').hidden = true;
  try {
    const item = await post('/workspace/research',{symbol:code});
    const value = await api('/research/' + encodeURIComponent(item.symbol));
    if (ticket !== state.generation || state.page !== 'research') return;
    state.symbol = item.symbol; state.report = value; renderResearch(); notice('');
    const active = (value.jobs || []).find(j => j.kind === 'refresh' && ['queued','running'].includes(j.status));
    if (active) void monitorJob(active.id,'refresh',item.symbol);
    else if (value.cache.state === 'missing') void startResearchRefresh();
  } catch (error) { if (ticket === state.generation) notice(error.message,true); }
}
async function reloadResearch(code) {
  const ticket = state.generation;
  const value = await api('/research/' + encodeURIComponent(code));
  if (ticket === state.generation && state.symbol === code && state.page === 'research') { state.report = value; renderResearch(); }
}
async function startResearchRefresh() {
  if (!state.symbol) return;
  const code = state.symbol;
  try { const job = await post('/research/' + encodeURIComponent(code) + '/refresh'); await monitorJob(job.job_id,'refresh',code); }
  catch (error) { notice(error.message,true); }
}

function jobView(job, kind, code) {
  const research = kind === 'refresh';
  if (research && (code !== state.symbol || state.page !== 'research')) return;
  const prefix = research ? '#research-job' : '#workspace-job';
  $(prefix).hidden = false; $(prefix + '-label').textContent = job.message;
  $(prefix + '-percent').textContent = `${job.progress}%`; $(prefix + '-progress').value = job.progress;
  $(prefix + '-detail').textContent = (job.result?.warnings || []).map(w => `${w.symbol ? w.symbol + '：' : ''}${w.message}`).join('\n');
}
function monitorJob(id, kind, code = '') {
  if (state.jobs.has(id)) return state.jobs.get(id).task;
  const task = (async () => {
    try {
      for (let i = 0; i < 1200; i++) {
        const job = await api('/jobs/' + encodeURIComponent(id)); jobView(job,kind,code);
        if (['completed','failed'].includes(job.status)) {
          if (kind === 'refresh' && code === state.symbol && state.page === 'research') await reloadResearch(code);
          await loadWorkspace();
          notice(job.result?.status === 'partial' ? '扫描已完成，部分股票资料待更新，请查看名单中的状态。' : job.message,job.status === 'failed');
          return job;
        }
        await new Promise(resolve => setTimeout(resolve,1500));
      }
      notice('任务仍在后台处理，可稍后重新读取；没有自动重复提交。');
    } catch (error) { notice(error.message,true); }
    finally { state.jobs.delete(id); updateBusy(); }
  })();
  state.jobs.set(id,{kind,symbol:code,task}); updateBusy();
  return task;
}

function openPosition(id = null) {
  state.editPosition = id ? state.positions.get(id) : null;
  const form = $('#position-form'); form.reset(); formError('#position-error');
  const p = state.editPosition;
  for (const key of ['symbol','name','quantity','average_cost','notes']) form.elements.namedItem(key).value = p?.[key] || '';
  $('#position-symbol').disabled = Boolean(p); $('#position-dialog-title').textContent = p ? `编辑 ${p.symbol} 持仓` : '导入已有持仓';
  $('#position-dialog').showModal();
}
function openPool() { $('#pool-form').reset(); formError('#pool-error'); $('#pool-dialog').showModal(); }
function removeRecord(title, description, action) {
  $('#remove-title').textContent = title; $('#remove-description').textContent = description;
  state.pendingRemoval = action; formError('#remove-error'); $('#remove-dialog').showModal();
}
function pushItem(item, expanded) {
  const tones = {SENT:'positive',FAILED:'negative',UNKNOWN:'caution'};
  const timeNote = item.estimated_time ? '（估计）' : '';
  const content = item.body || '旧档案没有保存可显示的正文。';
  return `<details class="push-record" data-record="${esc(item.id)}"${expanded ? ' open' : ''}><summary>
    <span class="push-time">${esc(item.time)}${timeNote}</span>
    ${badge(item.category_label)}
    <strong>${esc(item.title)}</strong>
    ${badge(item.status_label,tones[item.status])}
  </summary><div class="push-content">
    ${item.error ? `<p class="push-error">${esc(item.error)}</p>` : ''}
    <p>${esc(content)}</p>
  </div></details>`;
}
function renderPushHistory() {
  const open = new Set([...document.querySelectorAll('#push-groups details[open]')].map(node => node.dataset.record));
  $('#push-groups').innerHTML = [...pushHistory.groups].map(([day,items]) =>
    `<section class="panel push-group"><div class="panel-heading"><h2><time datetime="${esc(day)}">${esc(day)}</time></h2></div>
    ${items.map(item => pushItem(item,open.has(item.id))).join('')}</section>`).join('');
  if (!pushHistory.groups.size) {
    $('#push-groups').innerHTML = empty('暂无推送记录','当前筛选范围内没有投递档案。', '', true);
  }
}
async function loadPushHistory(more = false) {
  const form = $('#push-filters'), status = $('#push-status'), button = $('#push-more');
  if (more && (!pushHistory.cursor || button.disabled)) return;
  const generation = ++pushHistory.generation;
  if (!more) {
    const filters = new URLSearchParams();
    for (const [key,value] of new FormData(form)) if (value) filters.set(key,value);
    pushHistory.query = filters.toString();
    pushHistory.cursor = null;
    pushHistory.groups.clear();
    $('#push-groups').replaceChildren();
    button.hidden = true;
  }
  const filters = new URLSearchParams(pushHistory.query);
  if (more) filters.set('cursor',pushHistory.cursor);
  status.classList.remove('loss');
  status.textContent = '正在读取推送记录…';
  button.disabled = true;
  try {
    const data = await api('/push-history?' + filters.toString());
    if (state.page !== 'push-history' || generation !== pushHistory.generation) return;
    for (const group of data.groups) {
      const items = pushHistory.groups.get(group.date) || [];
      pushHistory.groups.set(group.date,[...items,...group.items]);
    }
    pushHistory.cursor = data.next_cursor;
    renderPushHistory();
    status.textContent = '';
    button.hidden = !pushHistory.cursor;
  } catch (error) {
    if (state.page !== 'push-history' || generation !== pushHistory.generation) return;
    status.classList.add('loss');
    status.textContent = error.message;
  } finally {
    if (generation === pushHistory.generation) button.disabled = false;
  }
}
function route() {
  // 页内锚点与页面路由分开，跳到主要内容不会切回持仓页。
  if (location.hash && !location.hash.startsWith('#/')) return;
  const [page, query = ''] = location.hash.replace(/^#\/?/,'').split('?');
  const pages = {holdings:'我的持仓',watchlist:'扫描股票池',research:'个股研究','push-history':'推送记录'};
  const next = Object.hasOwn(pages,page) ? page : 'holdings';
  ++state.generation;
  state.page = next;
  for (const p of Object.keys(pages)) {
    $('#page-' + p).hidden = p !== next;
    const nav = document.querySelector(`[data-nav="${p}"]`);
    if (p === next) nav.setAttribute('aria-current','page'); else nav.removeAttribute('aria-current');
  }
  document.title = `${pages[next]} · 见微`;
  if (next === 'research') {
    const code = new URLSearchParams(query).get('symbol');
    if (state.connected && code && (code !== state.symbol || !state.report)) void loadStock(code);
    else if (state.report) renderResearch();
  }
  if (next === 'push-history' && state.connected) void loadPushHistory();
  if (next === 'holdings') void loadIntraday();
}
function researchHash(code) { location.hash = '/research?symbol=' + encodeURIComponent(code.trim().toUpperCase()); }

$('#push-filters').addEventListener('submit', event => {
  event.preventDefault();
  void loadPushHistory();
});
$('#push-more').addEventListener('click', () => { void loadPushHistory(true); });

$('#position-form').addEventListener('submit', event => {
  event.preventDefault(); const form = event.currentTarget;
  void busy($('#position-save'), async () => {
    try {
      const data = Object.fromEntries(['symbol','name','quantity','average_cost','notes'].map(k => [k,form.elements.namedItem(k).value.trim()]));
      const p = state.editPosition;
      if (p) data.expected_version = p.version;
      const bucket = form.elements.namedItem("tactical").value;
      if (bucket) data.tactical = bucket === "tactical";
      await api(p ? '/holdings/' + p.id : '/holdings',{method:p ? 'PUT' : 'POST',body:JSON.stringify(data)});
      $('#position-dialog').close(); await loadWorkspace(); notice(p ? '持仓已更新。' : '持仓已添加，可点击“更新行情”计算市值。');
    } catch (error) { formError('#position-error',error.message); }
  });
});
$('#pool-form').addEventListener('submit', event => {
  event.preventDefault(); const form = event.currentTarget;
  void busy(form.querySelector('[type=submit]'),async () => {
    try { await post('/watchlist',{symbol:form.elements.symbol.value.trim()}); $('#pool-dialog').close(); await loadWorkspace(); notice('已加入扫描名单。'); }
    catch (error) { formError('#pool-error',error.message); }
  });
});
$('#remove-confirm').addEventListener('click', event => {
  void busy(event.currentTarget,async () => {
    try { await state.pendingRemoval(); $('#remove-dialog').close(); await loadWorkspace(); notice('记录已移除，历史保留。'); }
    catch (error) { formError('#remove-error',error.message); }
  });
});
document.addEventListener('click', event => {
  const close = event.target.closest('[data-close-dialog]');
  if (close) { close.closest('dialog').close(); return; }
  if (event.target.closest('[data-add-position]')) { openPosition(); return; }
  if (event.target.closest('[data-add-pool]')) { openPool(); return; }
  const edit = event.target.closest('[data-edit-position]');
  if (edit) { openPosition(edit.dataset.editPosition); return; }
  const remove = event.target.closest('[data-remove-position]');
  if (remove) {
    const p = state.positions.get(remove.dataset.removePosition);
    removeRecord(`移除 ${p.symbol} 持仓？`,'仅移除本地持仓记录，不代表已经卖出；历史快照会保留。',() => api(`/holdings/${p.id}?version=${encodeURIComponent(p.version)}`,{method:'DELETE'}));
    return;
  }
  const pool = event.target.closest('[data-remove-pool]');
  if (pool) { const code = pool.dataset.removePool; removeRecord(`移出 ${code}？`,'移出后不再参与股票池扫描，已有持仓和扫描历史不受影响。',() => api('/watchlist/' + encodeURIComponent(code),{method:'DELETE'})); return; }
  const recent = event.target.closest('[data-recent-stock]');
  if (recent) researchHash(recent.dataset.recentStock);
});
for (const id of ['global-search','research-search']) $('#' + id).addEventListener('submit', event => {
  event.preventDefault(); const code = event.currentTarget.elements.symbol.value.trim(); if (!code) return;
  const expected = '#/research?symbol=' + encodeURIComponent(code.toUpperCase());
  if (location.hash === expected) void loadStock(code); else researchHash(code);
});
$('#pool-add-open').addEventListener('click',openPool);
$('#reload-pool').addEventListener('click',event => { void busy(event.currentTarget,async () => { try { await loadWorkspace(); notice('已读取最新记录。'); } catch (error) { notice(error.message,true); } }); });
$('#scan-start').addEventListener('click',event => { void busy(event.currentTarget,async () => { try { const job = await post('/watchlist/scan',{refresh:$('#scan-refresh').checked}); await monitorJob(job.job_id,'watchlist_scan'); } catch (error) { notice(error.message,true); } }); });
$('#refresh-holdings').addEventListener('click',event => { void busy(event.currentTarget,async () => { try { const job = await post('/holdings/refresh'); await monitorJob(job.job_id,'holdings_refresh'); } catch (error) { notice(error.message,true); } }); });
$('#research-refresh').addEventListener('click',event => { void busy(event.currentTarget,startResearchRefresh); });
$('#research-pool-add').addEventListener('click',event => { void busy(event.currentTarget,async () => { try { await post('/watchlist',{symbol:state.symbol}); await loadWorkspace(); notice('已加入扫描名单。'); } catch (error) { notice(error.message,true); } }); });

$('#valuation-jump').addEventListener('click',() => {
  const view = state.report?.automatic_valuation_reference;
  if (!view || $('#valuation-save').disabled) return;
  const form = $('#valuation-form');
  for (const [key,value] of Object.entries(view.inputs)) form.elements.namedItem(key).value = value ?? '';
  state.valuationEdit = {code:state.symbol, generation:state.generation, view};
  $('#valuation-reset').hidden = !view.corrected_fields.length;
  formError('#valuation-error');
  $('#valuation-editor').showModal();
});
async function saveValuation(button, restore = false) {
  const edit = state.valuationEdit;
  if (!edit || edit.generation !== state.generation) {
    formError('#valuation-error','当前股票已切换，请关闭并重新打开编辑。');
    return;
  }
  const {code,generation,view} = edit, form = $('#valuation-form');
  const changes = restore ? Object.fromEntries(view.corrected_fields.map(key => [key,null])) :
    Object.fromEntries([...new FormData(form)].flatMap(([key,raw]) => {
      const value = raw.trim();
      return value === (view.inputs[key] ?? '') ? [] : [[key,value || null]];
    }));
  if (!Object.keys(changes).length) { $('#valuation-editor').close(); return; }
  $('#valuation-reset').disabled = true; $('#valuation-save').disabled = true;
  button.setAttribute('aria-busy','true');
  formError('#valuation-error');
  try {
    await post('/research/' + encodeURIComponent(code) + '/valuation-correction',{expected_id:view.id,changes});
    $('#valuation-editor').close();
    if (generation === state.generation) {
      notice(restore ? '已恢复自动估值数据。' : '已保存更正。');
      try { await reloadResearch(code); }
      catch { notice('更正已保存，刷新页面可查看最新估值。'); }
    }
  } catch (error) { formError('#valuation-error',error.message); }
  finally {
    button.removeAttribute('aria-busy');
    $('#valuation-reset').disabled = false; $('#valuation-save').disabled = false;
  }
}
$('#valuation-form').addEventListener('submit',event => {
  event.preventDefault();
  const button = $('#valuation-save');
  if (!button.disabled) void saveValuation(button);
});
$('#valuation-reset').addEventListener('click',event => { void saveValuation(event.currentTarget,true); });
$('#research-export').addEventListener('click',() => {
  if (!state.report) return;
  const blob = new Blob([JSON.stringify(state.report,null,2)],{type:'application/json'});
  const url = URL.createObjectURL(blob), a = document.createElement('a'); a.href = url; a.download = `${state.symbol}-research.json`; a.click(); setTimeout(() => URL.revokeObjectURL(url),1000);
});
window.addEventListener('hashchange',route);
async function boot() {
  if (state.booting) return;
  state.booting = true;
  try {
    const config = await api('/config'); state.timezone = config.display_timezone || 'Asia/Shanghai';
    await loadWorkspace(); state.connected = true; $('#connection').textContent = '已连接本地服务'; $('#connection').classList.add('ready');
    route();
    try {
      const recent = await api('/research');
      $('#recent-stocks').innerHTML = recent.filter(r => /^[A-Z][A-Z0-9.\-]{0,14}$/.test(r.symbol) && !/\.(SS|SZ|BJ)$/.test(r.symbol)).slice(0,8).map(r => `<button data-recent-stock="${esc(r.symbol)}">${esc(r.symbol)}</button>`).join('');
      const jobs = await api('/workspace/jobs');
      for (const job of jobs.filter(j => ['queued','running'].includes(j.status))) void monitorJob(job.id,job.kind);
    } catch (error) { notice(`工作台已连接，最近记录读取不完整：${error.message}`,true); }
  } catch (error) {
    $('#connection').textContent = '服务待连接'; $('#connection').classList.remove('ready'); notice(error.message,true);
    $('#positions-content').innerHTML = empty('暂时无法读取持仓','请确认后台正在运行，然后刷新页面重试。');
  } finally { state.booting = false; }
}
connectPortfolio({$,esc,num,pct,when,tone,badge,table,metric,api,post,busy,notice,formError,state,researchHash,loadWorkspace,removeRecord});
icons(); route(); void boot();
window.setInterval(() => { void loadIntraday(); },5000);
document.addEventListener('visibilitychange', () => { void loadIntraday(); });
