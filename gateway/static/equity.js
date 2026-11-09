// 两个市场共用净值卡与收盘趋势图，盘中估算由美股行情帧提供。
export function connectEquity({$,esc,num,signed,tone,when,table,api,post,busy,notice,formError,monitorJob,loadCnHoldings}) {
  const regions = {US:'美股',CN:'A 股'};
  const panels = {US:'#us-equity-content',CN:'#cn-equity-content'};
  const views = {};
  const dialog = $('#equity-history');
  let region = 'US', period = 'month', request = 0, points = [];
  let intradayUS = null;

  function setText(node, value) {
    if (node.textContent !== value) node.textContent = value;
  }

  function renderNetWorth(market) {
    const panel = $(panels[market]);
    const value = panel.querySelector('[data-equity-value]');
    if (!value) return;
    const view = views[market], saved = view.latest;
    const live = market === 'US' ? intradayUS : null;
    const equity = live ? live.equity : view.estimate ?? (view.estimate_reason ? null : saved?.equity);
    let note;
    if (live) {
      note = equity != null ? `${live.session} · 盘中估算 · IEX` : live.reason || '等待全部持仓的有效盘中报价';
    } else if (view.estimate != null) {
      note = `${view.target_session} · 收盘估算${view.estimate_is_saved ? '' : ' · 待保存'}`;
    } else {
      const reason = view.estimate_reason || (!view.cash ? '请先填写账户现金' : view.missing.length
        ? `${view.missing.length} 只持仓待补收盘价` : '等待有效收盘报价');
      note = equity != null ? `${saved.session} · 历史参考 · ${reason}` : reason;
    }
    setText(value,equity != null ? `${view.currency} ${num(equity)}` : '—');
    setText(panel.querySelector('[data-equity-note]'),note);
  }

  function renderDaily(market) {
    const card = $(panels[market]).querySelector('.equity-day');
    if (!card) return;
    const view = views[market], saved = view.latest;
    const settled = saved?.session === view.target_session ? saved : null;
    const live = market === 'US' ? intradayUS : null;
    const daily = live ? live.pnl : view.estimate != null ? view.estimate_daily_pnl : view.estimate_reason ? null : settled?.daily_pnl;
    const label = live ? '盘中持仓盈亏' : '当日盈亏';
    let note;
    if (live) {
      note = daily != null ? `${live.session} · 相对前收 · 按当前股数估算`
        : live.count ? '等待全部持仓的有效盘中价与前收' : '暂无持仓';
    } else if (daily != null) {
      const basis = view.estimate != null && !view.estimate_is_saved ? '收盘估算' : '收盘记录';
      note = `${view.target_session} · ${basis} · 扣除外部净入金`;
    } else {
      note = view.estimate_reason || (view.estimate != null || settled ? '需要前一交易日净值和净入金记录' : '等待有效收盘净值');
    }
    const amount = daily != null ? `${view.currency} ${signed(daily)}` : '—';
    const value = card.querySelector('strong');
    setText(value,amount);
    value.className = tone(daily);
    const heading = card.querySelector('[data-day-label]');
    setText(heading,label);
    const detail = card.querySelector('.subline');
    setText(detail,note);
  }

  function setIntraday(view) {
    intradayUS = view;
    renderNetWorth('US');
    renderDaily('US');
  }

  function apply(market, view) {
    views[market] = view;
    const panel = $(panels[market]);
    if (!panel.querySelector('[data-equity-value]')) {
      panel.innerHTML = `<button type="button" class="equity-history-trigger" data-equity-history="${market}">
        <span>账户估算净值</span><strong data-equity-value aria-live="off"></strong>
        <span class="equity-card-meta"><span data-equity-note></span><span class="equity-link">查看走势 <span aria-hidden="true">↗</span></span></span>
      </button><div class="equity-day"${market === 'US' ? ' aria-live="off"' : ''}><span data-day-label></span><strong></strong>
        <span class="subline"></span></div>`;
    }
    renderNetWorth(market);
    renderDaily(market);
    if (market === 'CN') {
      const button = $('#cn-equity-cash-open');
      button.disabled = false;
      setText(button,view.cash ? '更新人民币现金' : '填写人民币现金');
      setText($('#cn-equity-cash-note'),view.cash
        ? `总现金 CNY ${num(view.cash.total_cash)} · ${when(view.cash.cash_as_of)} 核对`
        : '录入券商总现金和累计净入金后可估算净值。');
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
        await loadCnHoldings();
      } catch (error) { formError('#cn-equity-cash-error',error.message); }
    });
  });
  return {apply,setIntraday};
}
