// 两个市场共用收盘净值卡与趋势图，数据和请求按市场分开。
export function connectEquity({$,esc,num,signed,tone,when,table,api,post,busy,notice,formError,monitorJob}) {
  const regions = {US:'美股',CN:'A 股'};
  const panels = {US:'#us-equity-content',CN:'#cn-equity-content'};
  const views = {}, reads = {US:0,CN:0};
  const dialog = $('#equity-history');
  let region = 'US', period = 'month', request = 0, points = [];

  async function load(market) {
    const ticket = ++reads[market];
    try {
      const view = await api('/account-equity/' + market);
      if (ticket !== reads[market]) return;
      views[market] = view;
      const saved = view.latest;
      const estimate = view.estimate ?? saved?.equity;
      const mark = view.estimate != null ? view.target_session : saved?.session;
      const unsaved = view.estimate != null && !view.estimate_is_saved;
      const daily = saved?.session === view.target_session ? saved?.daily_pnl : null;
      const pnlNote = daily != null ? `${saved.session} · ${unsaved ? '上次保存' : '扣除外部净入金'}`
        : saved?.session === view.target_session ? '需要相邻两日净值和净入金记录' : '等待最近收盘净值保存';
      let note = !view.cash ? '请先填写账户现金' : mark ? `${mark}${unsaved ? ' · 待保存' : ' · 收盘估算'}` : '等待有效收盘报价';
      if (view.missing.length) note = `${mark ? mark + ' · ' : ''}${view.missing.length} 只持仓待补收盘价`;
      $(panels[market]).innerHTML = `<button type="button" class="equity-history-trigger" data-equity-history="${market}">
        <span>账户估算净值</span><strong>${estimate != null ? `${view.currency} ${num(estimate)}` : '—'}</strong>
        <span class="equity-card-meta"><span>${esc(note)}</span><span class="equity-link">查看走势 <span aria-hidden="true">↗</span></span></span>
      </button><div class="equity-day"><span>当日盈亏</span><strong class="${tone(daily)}">${daily != null ? `${view.currency} ${signed(daily)}` : '—'}</strong>
        <span class="subline">${esc(pnlNote)}</span></div>`;
      if (market === 'CN') {
        const button = $('#cn-equity-cash-open');
        button.disabled = false;
        button.textContent = view.cash ? '更新人民币现金' : '填写人民币现金';
        $('#cn-equity-cash-note').textContent = view.cash
          ? `总现金 CNY ${num(view.cash.total_cash)} · ${when(view.cash.cash_as_of)} 核对`
          : '录入券商总现金和累计净入金后可估算净值。';
      }
    } catch (error) {
      if (ticket === reads[market]) $(panels[market]).textContent = error.message;
    }
  }

  function selectPoint(index) {
    const point = points[index];
    if (!point) return;
    $('#equity-history-selection').textContent = `${point.session} · 净值 ${views[region]?.currency || (region === 'CN' ? 'CNY' : 'USD')} ${num(point.equity)} · 当日盈亏 ${signed(point.daily_pnl)}`;
    const marker = $('#equity-history-marker');
    if (marker) {
      marker.setAttribute('cx',String(point.x));
      marker.setAttribute('cy',String(point.y));
    }
    $('#equity-history-slider').setAttribute('aria-valuetext',`${point.session} 净值 ${num(point.equity)}`);
  }

  function renderHistory(items, currency) {
    const chart = $('#equity-history-chart');
    const slider = $('#equity-history-slider');
    const rows = items.filter(row => row.equity != null && Number.isFinite(Number(row.equity)));
    points = [];
    slider.hidden = rows.length < 2;
    if (!rows.length) {
      chart.textContent = '这段时间暂无已保存的收盘净值。';
      $('#equity-history-selection').textContent = '';
      $('#equity-history-table').hidden = true;
      return;
    }
    const amounts = rows.map(row => Number(row.equity));
    const times = rows.map(row => Date.parse(row.session+'T00:00:00Z'));
    const first = times[0], last = times.at(-1);
    const low = Math.min(...amounts), high = Math.max(...amounts), spread = high-low;
    points = rows.map((row,index) => ({...row,
      x:24+(last===first ? 0.5 : (times[index]-first)/(last-first))*552,
      y:20+(spread ? (high-amounts[index])/spread : 0.5)*140
    }));
    const line = points.map((point,index) => {
      const join = index && point.previous_session === points[index-1].session;
      return `${join ? 'L' : 'M'}${point.x.toFixed(2)} ${point.y.toFixed(2)}`;
    }).join(' ');
    chart.innerHTML = `<div class="equity-axis"><span>区间最高 ${num(high)} ${esc(currency)}</span><span>最低 ${num(low)} ${esc(currency)}</span></div>
      <svg viewBox="0 0 600 180" role="img" aria-label="收盘净值趋势，从 ${esc(rows[0].session)} 到 ${esc(rows.at(-1).session)}">
        <path class="equity-grid" d="M24 20H576 M24 90H576 M24 160H576"/>
        <path class="equity-line" d="${line}"/>
        ${points.map(p => `<circle class="equity-dot" cx="${p.x}" cy="${p.y}" r="2.5"/>`).join('')}
        <circle id="equity-history-marker" r="5" class="equity-marker"/>
      </svg><div class="equity-axis"><span>${esc(rows[0].session)}</span><span>${esc(rows.at(-1).session)}</span></div>`;
    slider.max = String(points.length-1);
    slider.value = slider.max;
    $('#equity-history-table').hidden = false;
    $('#equity-history-rows').innerHTML = table(['交易日',`估算净值 · ${currency}`,`当日盈亏 · ${currency}`],
      [...rows].reverse().map(row => [esc(row.session),num(row.equity),signed(row.daily_pnl)]),[1,2]);
    selectPoint(points.length-1);
  }

  async function loadHistory() {
    const ticket = ++request;
    $('#equity-history-chart').textContent = '正在读取净值记录…';
    $('#equity-history-selection').textContent = '';
    $('#equity-history-slider').hidden = true;
    $('#equity-history-table').hidden = true;
    for (const button of dialog.querySelectorAll('[data-equity-range]')) {
      button.setAttribute('aria-pressed',String(button.dataset.equityRange === period));
    }
    try {
      const data = await api(`/account-equity/${region}/history?period=${period}`);
      if (ticket === request && dialog.open) renderHistory(data.items,data.currency);
    } catch (error) {
      if (ticket === request && dialog.open) $('#equity-history-chart').textContent = error.message;
    }
  }

  document.addEventListener('click',event => {
    const trigger = event.target.closest('[data-equity-history]');
    if (!trigger) return;
    region = trigger.dataset.equityHistory;
    period = 'month';
    $('#equity-history-title').textContent = regions[region] + '账户净值走势';
    $('#equity-capture-status').textContent = '';
    dialog.showModal();
    void loadHistory();
  });
  dialog.addEventListener('close',() => { ++request; });
  dialog.addEventListener('click',event => {
    const button = event.target.closest('[data-equity-range]');
    if (!button || button.dataset.equityRange === period) return;
    period = button.dataset.equityRange;
    void loadHistory();
  });
  $('#equity-history-slider').addEventListener('input',event => selectPoint(Number(event.currentTarget.value)));
  $('#equity-capture').addEventListener('click',event => {
    const market = region;
    const status = $('#equity-capture-status');
    void busy(event.currentTarget,async () => {
      try {
        const job = await post(`/account-equity/${market}/capture`);
        if (dialog.open && region === market) status.textContent = '已排队，正在核对收盘行情…';
        const result = await monitorJob(job.job_id,market === 'CN' ? 'cn_account_equity_capture' : 'account_equity_capture');
        if (dialog.open && region === market) {
          status.textContent = result?.message || '请到任务状态查看记录进度。';
          if (result?.status === 'completed') await loadHistory();
        }
      } catch (error) { if (dialog.open && region === market) status.textContent = error.message; }
    });
  });

  const cashDialog = $('#cn-equity-cash');
  const cashForm = $('#cn-equity-cash-form');
  $('#cn-equity-cash-open').addEventListener('click',() => {
    const cash = views.CN?.cash;
    cashForm.reset();
    cashForm.elements.total_cash.value = cash?.total_cash ?? '';
    cashForm.elements.net_flows.value = cash?.net_flows ?? '0';
    cashForm.dataset.version = cash?.version || '';
    formError('#cn-equity-cash-error');
    cashDialog.showModal();
  });
  cashForm.addEventListener('submit',event => {
    event.preventDefault();
    void busy(cashForm.querySelector('[type=submit]'),async () => {
      const data = Object.fromEntries([...new FormData(cashForm)].map(([key,value]) => [key,value.trim()]));
      try {
        await api('/account-equity/CN/cash',{method:'PUT',body:JSON.stringify({...data,expected_version:cashForm.dataset.version || null})});
        cashDialog.close();
        notice('A 股现金已更新。');
        await load('CN');
      } catch (error) { formError('#cn-equity-cash-error',error.message); }
    });
  });
  return {load};
}
