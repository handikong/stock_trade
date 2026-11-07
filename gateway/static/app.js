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
const template = () => ({eps_conservative:null,eps_base:null,eps_optimistic:null,pe_low:null,pe_median:null,pe_high:null,industry_pe:null});
const state = {page:'holdings',symbol:'',report:null,workspace:null,timezone:'Asia/Shanghai',generation:0,workspaceGeneration:0,
  positions:new Map(),editPosition:null,pendingRemoval:null,jobs:new Map(),drafts:new Map(),editorSymbol:'',booting:false,connected:false,valuationSaving:false};

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
  const preview = state.drafts.get(state.symbol)?.preview;
  $('#valuation-draft').disabled = !preview || state.valuationSaving;
  $('#valuation-confirm').disabled = !preview || state.valuationSaving;
  $('#valuation-json').readOnly = state.valuationSaving;
  $('#valuation-new').disabled = state.valuationSaving;
  $('#valuation-preview-button').disabled = state.valuationSaving || $('#valuation-preview-button').getAttribute('aria-busy') === 'true';
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
      return [stock(r.symbol,r.name,i),`${num(r.quantity,6)}<span class="subline">股</span>`,num(r.average_cost),
        `${num(quoted ? q.price : null)}<span class="subline">${esc(q.date || '尚无行情')} ${!quoted ? '· 币种待核对' : q.state !== 'fresh' ? '· 待更新' : ''}</span>`,
        `${num(r.reference_market_value)}${q.state !== 'fresh' ? '<span class="subline">仅历史参考</span>' : ''}`,
        `<span class="${tone(r.unrealized_pnl)}">${signed(r.unrealized_pnl)}</span><span class="subline">${pct(r.unrealized_return)}</span>`,
        `<div class="row-actions"><button class="text-button" data-edit-position="${esc(r.id)}">编辑</button><button class="text-button remove" data-remove-position="${esc(r.id)}">移除</button></div>`];
    }),[1,2,3,4,5]);
  }
  if (!s.complete) {
    $('#allocation-content').innerHTML = empty(s.count ? '行情尚未齐全' : '等待添加持仓',s.count ? '更新持仓行情后显示市值构成。缺失和过期价格不会计为零。' : '持仓构成会在这里展示。', '', true);
  } else {
    const rows = data.items;
    $('#allocation-content').innerHTML = `<div class="allocation-body"><div class="allocation-bar" aria-hidden="true">${rows.map((r,i) => `<span style="flex:${Math.max(0,Number(r.weight))};background:${palette[i % palette.length]}"></span>`).join('')}</div>${rows.map((r,i) => `<div class="allocation-row"><i style="background:${palette[i % palette.length]}" aria-hidden="true"></i><strong>${esc(r.symbol)}</strong><span>${pct(r.weight)}</span></div>`).join('')}<p class="allocation-note">仅表示已录入股票内部的市值构成，不包含账户现金。</p></div>`;
  }
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
}

function currentDraft() {
  if (!state.drafts.has(state.symbol)) state.drafts.set(state.symbol,{raw:JSON.stringify(template(),null,2),preview:null});
  return state.drafts.get(state.symbol);
}
function renderValuation(view, preview = false) {
  if (!view || !view.input) return empty('尚无已确认估值',view?.draft ? '已有草稿，已载入下方编辑区；核对后预览并确认保存。' : '把大模型返回的EPS与PE粘贴到下方，预览计算后确认保存。','',true);
  if (view.status === 'needs_review') return `<div class="notice error">${esc(view.message)}</div>`;
  let groups = view.valuation_sets || [];
  if (!groups.length && view.scenarios?.length) groups = [{label:view.input.target?.date ? `目标日 ${view.input.target.date} · EPS估值` : 'EPS估值参考',scenarios:view.scenarios}];
  let html = groups.map(g => `<div class="valuation-group"><h3>${esc(g.label)}</h3><div class="scenario-grid">${g.scenarios.map((r,i) => `<div class="scenario ${i === 1 ? 'base' : ''}"><span>${esc(r.name)}</span><strong>${num(r.price)}</strong><p>EPS ${num(r.eps,4)} × PE ${num(r.pe)}</p></div>`).join('')}</div></div>`).join('');
  if (view.dcf_result) html += `<p class="inline-note">已保存DCF资料日 ${esc(view.dcf_result.as_of)}，每股价值 ${num(view.dcf_result.price)}；详见导出JSON。</p>`;
  if (!html) html = empty('估值数据尚未齐全','补齐EPS及对应PE后，程序会计算三档估值。','',true);
  const gaps = view.missing_fields || [], ref = view.reference || {};
  if (gaps.length) html += `<p class="inline-note">待补：${esc(gaps.join('、'))}</p>`;
  if (ref.warnings?.length) html += `<p class="inline-note">${esc(ref.warnings.join('；'))}</p>`;
  const p = view.input, e = p.eps;
  html += `<p class="inline-note">${preview ? '仅预览，尚未保存。' : `录入日 ${esc(p.as_of || '未知')}。`}${e ? ` EPS期间 ${esc(e.period_start)}～${esc(e.period_end)}。` : ''} ${esc(p.currency || '')} · ${esc(view.message || '情景估值不代表未来价格保证。')}</p>`;
  if (view.draft && !preview) html += '<p class="inline-note">另有未确认草稿，当前继续展示已确认版本。</p>';
  return html;
}
function renderWeeklyValuation(view) {
  const heading = '<h3>每周自动估值参考</h3>';
  if (!view) return `<div class="valuation-group">${heading}<p class="inline-note">尚无已保存的周六刷新结果。</p></div>`;
  const {eps, forward_pe:pe, attempt, scenarios} = view;
  const grid = scenarios.length ? `<div class="scenario-grid">${scenarios.map((row,i) =>
    `<div class="scenario ${i === 1 ? 'base' : ''}"><span>${esc(row.name)}</span><strong>${num(row.price)} USD</strong><p>EPS ${num(row.eps,4)} × PE ${num(row.pe)}</p></div>`).join('')}</div>` : '';
  const note = [
    `Stock Analysis · 最近尝试 ${attempt.date}（北京时间）${view.stale ? ' · 含一周前资料' : ''}`,
    eps && `财年截至 ${eps.period_end} · 三档 EPS ${num(eps.low,4)} / ${num(eps.mean,4)} / ${num(eps.high,4)} ${eps.currency_per_share} · ${when(eps.fetched_at)}`,
    pe && `历史 Forward PE ${num(pe.p25)} / ${num(pe.p50)} / ${num(pe.p75)} · ${pe.sample_count} 个季度样本 · ${when(pe.fetched_at)}`,
    view.message,
    ...attempt.issues,
  ].filter(Boolean).map(line => `<p class="inline-note">${esc(line)}</p>`).join('');
  return `<div class="valuation-group">${heading}${grid}${note}</div>`;
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
  $('#research-metrics').innerHTML = metric('最近完整日收盘',num(r.market?.last_price),`${r.market?.last_complete_session || '尚无行情'} · ${r.profile.currency || '币种待确认'}`)
    + metric('行情状态',({fresh:'有效',stale:'待更新',missing:'未获取',unknown:'待核对'})[r.cache.state] || '待核对',r.market?.fetched_at ? `取得 ${when(r.market.fetched_at)}` : '仅后台获取K线')
    + metric('手工估值',({available:'已确认',partial:'部分待补',needs_review:'待复核',missing:'未录入'})[r.manual_valuation.status] || '待核对','EPS与PE均由本人录入');
  const fund = ['ETF','MUTUALFUND'].includes(r.profile.type);
  $('#valuation-content').innerHTML = fund ? '<p class="inline-note">基金不套用公司EPS×PE估值；下方继续展示技术支撑压力。</p>'
    : renderWeeklyValuation(r.automatic_valuation_reference) + renderValuation(r.manual_valuation);
  $('#valuation-editor').hidden = fund; $('#valuation-jump').hidden = fund;
  $('#technical-content').innerHTML = renderTechnical(r);
  $('#combined-content').innerHTML = renderCombined(r);
  if (state.editorSymbol !== state.symbol) {
    if (!state.drafts.has(state.symbol)) {
      const input = r.manual_valuation.draft?.input;
      const savedDraft = input?.schema_version === 'manual-eps-pe-v4';
      // 继续草稿时保留固定NTM期间；不把旧确认值默认为新一年的预测。
      state.drafts.set(state.symbol,{raw:JSON.stringify(savedDraft ? {...template(),...input.values} : template(),null,2),preview:null,sourceInput:savedDraft ? input : null});
    }
    state.editorSymbol = state.symbol;
    $('#valuation-json').value = currentDraft().raw;
    formError('#valuation-error');
    showPreview();
  }
  updateBusy();
}
function showPreview() {
  const preview = currentDraft().preview;
  $('#valuation-preview-content').hidden = !preview;
  $('#valuation-preview-content').innerHTML = preview ? renderValuation(preview,true) : '';
  updateBusy();
}
async function loadStock(code) {
  const ticket = ++state.generation;
  notice('正在读取研究档案…'); $('#research-empty').hidden = false; $('#research-body').hidden = true; $('#research-job').hidden = true;
  try {
    const item = await post('/workspace/research',{symbol:code});
    const value = await api('/research/' + encodeURIComponent(item.symbol));
    if (ticket !== state.generation || state.page !== 'research') return;
    state.symbol = item.symbol; state.report = value; state.editorSymbol = ''; renderResearch(); notice('');
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
  $('#position-symbol').disabled = Boolean(p); $('#position-dialog-title').textContent = p ? `编辑 ${p.symbol} 持仓` : '添加持仓';
  $('#position-dialog').showModal();
}
function openPool() { $('#pool-form').reset(); formError('#pool-error'); $('#pool-dialog').showModal(); }
function removeRecord(title, description, action) {
  $('#remove-title').textContent = title; $('#remove-description').textContent = description;
  state.pendingRemoval = action; formError('#remove-error'); $('#remove-dialog').showModal();
}
function route() {
  // 页内锚点与页面路由分开，跳到主要内容不会切回持仓页。
  if (location.hash && !location.hash.startsWith('#/')) return;
  const [page, query = ''] = location.hash.replace(/^#\/?/,'').split('?');
  const next = ['holdings','watchlist','research'].includes(page) ? page : 'holdings';
  ++state.generation;
  state.page = next;
  for (const p of ['holdings','watchlist','research']) {
    $('#page-' + p).hidden = p !== next;
    const nav = document.querySelector(`[data-nav="${p}"]`);
    if (p === next) nav.setAttribute('aria-current','page'); else nav.removeAttribute('aria-current');
  }
  document.title = `${({holdings:'我的持仓',watchlist:'扫描股票池',research:'个股研究'})[next]} · 见微`;
  if (next === 'research') {
    const code = new URLSearchParams(query).get('symbol');
    if (state.connected && code && (code !== state.symbol || !state.report)) void loadStock(code);
    else if (state.report) renderResearch();
  }
}
function researchHash(code) { location.hash = '/research?symbol=' + encodeURIComponent(code.trim().toUpperCase()); }

$('#position-form').addEventListener('submit', event => {
  event.preventDefault(); const form = event.currentTarget;
  void busy($('#position-save'), async () => {
    try {
      const data = Object.fromEntries(['symbol','name','quantity','average_cost','notes'].map(k => [k,form.elements.namedItem(k).value.trim()]));
      const p = state.editPosition;
      if (p) data.expected_version = p.version;
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

$('#valuation-json').addEventListener('input',() => {
  const draft = currentDraft(); draft.raw = $('#valuation-json').value; draft.preview = null; showPreview(); formError('#valuation-error');
});
$('#valuation-preview-button').addEventListener('click',event => {
  const code = state.symbol, draft = currentDraft(), raw = draft.raw;
  void busy(event.currentTarget,async () => {
    try {
      let value; try { value = JSON.parse(raw); } catch { throw new Error('JSON格式不正确，请检查引号、逗号和括号。'); }
      // 草稿中的数字可编辑，但其预测期间不随重新打开页面而滚动。
      const simple = value && !Array.isArray(value) && typeof value === 'object' && !value.schema_version;
      const data = draft.sourceInput?.schema_version === 'manual-eps-pe-v4' && simple ? {...draft.sourceInput,values:value} : value;
      const preview = await post('/research/' + encodeURIComponent(code) + '/manual-valuations/preview',data);
      if (draft.raw !== raw) return;
      draft.preview = preview;
      if (state.symbol === code) { showPreview(); formError('#valuation-error'); }
    } catch (error) { if (state.symbol === code) formError('#valuation-error',error.message); }
  });
});
for (const [id,confirm] of [['valuation-draft',false],['valuation-confirm',true]]) $('#' + id).addEventListener('click',event => {
  const code = state.symbol, draft = currentDraft(), preview = draft.preview, raw = draft.raw;
  if (!preview || state.valuationSaving) return;
  void busy(event.currentTarget,async () => {
    state.valuationSaving = true; updateBusy();
    try {
      // 保存预览生成的固定输入，避免跨日保存把NTM期间重新滚动。
      await post('/research/' + encodeURIComponent(code) + '/manual-valuations',{data:preview.input,confirm,expected_hash:preview.input_hash});
      if (draft.raw === raw) {
        draft.preview = null;
        if (preview.input.schema_version === 'manual-eps-pe-v4') draft.sourceInput = preview.input;
      }
      if (state.symbol === code) { showPreview(); await reloadResearch(code); notice(confirm ? '已保存本人确认版本，研究结果已更新。' : '草稿已保存，尚未替换当前确认版本。'); }
    } catch (error) { if (state.symbol === code) formError('#valuation-error',error.message); }
    finally { state.valuationSaving = false; updateBusy(); }
  });
});
$('#valuation-jump').addEventListener('click',() => { $('#valuation-editor').scrollIntoView({block:'start'}); $('#valuation-json').focus({preventScroll:true}); });
$('#valuation-new').addEventListener('click',() => {
  if (state.valuationSaving) return;
  state.drafts.set(state.symbol,{raw:JSON.stringify(template(),null,2),preview:null,sourceInput:null});
  $('#valuation-json').value = currentDraft().raw; showPreview(); formError('#valuation-error');
  notice('已新建空白输入，预览时按本次录入日生成固定NTM期间；已保存版本保留。');
});
$('#research-export').addEventListener('click',() => {
  if (!state.report) return;
  const blob = new Blob([JSON.stringify(state.report,null,2)],{type:'application/json'});
  const url = URL.createObjectURL(blob), a = document.createElement('a'); a.href = url; a.download = `${state.symbol}-research.json`; a.click(); setTimeout(() => URL.revokeObjectURL(url),1000);
});
$('#copy-prompt').addEventListener('click',async () => {
  const text = `请联网整理${state.symbol}：未来12个月（NTM）同一盈利口径的保守、基准、乐观EPS；近5年同口径历史Forward PE的25%、50%、75%分位数；可比行业Forward PE参照。不要把下一财年EPS冒充NTM，也不要把当前PE或历史均值冒充分位数。无法核实填null，不编造。只返回以下七字段JSON，数值使用十进制字符串，不附其他文字：\n\n${JSON.stringify(template(),null,2)}`;
  try { await navigator.clipboard.writeText(text); notice('提问模板已复制。'); }
  catch { $('#prompt-text').value = text; $('#prompt-dialog').showModal(); }
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
icons(); route(); void boot();
