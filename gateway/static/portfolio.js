// 账户评估和人工确认独立于导入持仓与自动扫描。
export function connectPortfolio({$, esc, num, pct, when, tone, badge, table, metric, api, post, busy, notice, formError,
                                  state, researchHash, loadWorkspace, removeRecord}) {
  const model = {portfolio:null, generation:0, requested:'', evaluated:null, evaluationKey:'', manual:null, fill:null};
  const STAGE = {active:'进行中',filled:'已确认成交',expired:'已过期',invalidated:'已失效',cancelled:'已取消'};
  const money = value => value == null ? '—' : 'USD ' + num(value);
  const plain = selector => Object.fromEntries([...new FormData($(selector))].map(([key,value]) => [key,value.trim()]));
  const stageRange = stage => `${num(stage.min_price)}–${num(stage.max_price)}`;
  const list = rows => rows.length ? `<ul class="portfolio-reasons">${rows.map(line => `<li>${esc(line)}</li>`).join('')}</ul>` : '';
  const marketNote = view => view ? [`大盘${view.state}`, ...(view.notes || [])].join(' · ') : '大盘资料待补';
  let principal = null;

  function renderPrincipal() {
    const view = model.portfolio;
    const loss = $('#portfolio-account-content [data-principal-loss]');
    const floor = $('#portfolio-account-content [data-principal-floor]');
    if (!view?.account || !loss || !floor) return;
    const live = principal?.live;
    const values = principal?.values ?? (live ? {} : view.summary);
    const source = live ? '盘中' : '收盘';
    const limit = values.loss_percent ?? view.account.loss_percent;
    const write = (metric, value, note, color) => {
      const amount = metric.querySelector('strong');
      const markup = value == null ? '—' : `<small>USD</small>${num(value)}`;
      if (amount.innerHTML !== markup) amount.innerHTML = markup;
      amount.className = color;
      const detail = metric.querySelector('p');
      if (detail.textContent !== note) detail.textContent = note;
    };
    write(loss,values.principal_loss,values.principal_loss_rate == null ? `等待完整${source}净值`
      : `${source} · 占本金 ${pct(values.principal_loss_rate)} · 上限 ${num(limit)}%`,Number(values.principal_loss)>0 ? 'loss' : '');
    write(floor,values.floor ?? view.summary.floor,`${source} · 距底线 ${money(values.buffer)}`,tone(values.buffer));
  }

  function setPrincipal(values, live) {
    principal = {values,live};
    renderPrincipal();
  }

  function renderAccount(view) {
    const a = view.account, s = view.summary;
    $('#portfolio-account-open').textContent = a ? '更新现金与本金' : '填写账户';
    if (!a) {
      $('#portfolio-account-content').innerHTML = '<p class="inline-note">填写参考本金、总现金和可用现金后，系统会计算新仓额度。</p>';
      return;
    }
    const metrics = [
      metric('券商可用现金',num(a.available_cash),`总现金 ${money(a.total_cash)}`,'',true),
      '<div class="metric" data-principal-loss aria-live="off"><span>本金亏损</span><strong></strong><p></p></div>',
      '<div class="metric" data-principal-floor aria-live="off"><span>本金保护参考线</span><strong></strong><p></p></div>',
      metric('剩余新增风险',num(s.risk_free),`计划预留 ${money(s.reserved_risk)}`,'',true),
    ].join('');
    const rows = view.positions.map(p => [
      esc(p.symbol),esc(p.label),num(p.market_value),num(p.guard_risk),
      p.alerts.length ? p.alerts.map(a => `${esc(a.state)} · 预警 ${num(a.warning)} / 保护 ${num(a.stop)}`).join('<br>') :
        p.market_value ? '按历史/波动压力估算' : '行情待补',
    ]);
    $('#portfolio-account-content').innerHTML = `<div class="metric-strip portfolio-budget">${metrics}</div>`
      + `<p class="inline-note">${esc(marketNote(view.market))} · 现金核对于 ${esc(when(a.cash_as_of))}</p>`
      + `<p class="inline-note">组合压力 ${money(s.stress_risk)} · 保护估算 ${money(s.guard_risk)} · 进攻型 ${money(s.aggressive_value)} · 机动仓 ${money(s.tactical_value)} · 待执行现金 ${money(s.reserved_cash)}</p>`
      + (s.unknown.length ? `<p class="inline-note">行情待补 ${esc(s.unknown.join('、'))}</p>` : '')
      + (rows.length ? `<details class="inline-note"><summary>查看持仓风险与保护位</summary>${table(['股票','风险类型','市值','保护估算','风险提示'],rows,[2,3])}</details>` : '');
    renderPrincipal();
  }

  function renderPlans(view) {
    if (!view.plans.length) {
      $('#portfolio-plans-content').innerHTML = '<p class="inline-note">暂无已保存计划。使用「评估买入」开始。</p>';
      return;
    }
    $('#portfolio-plans-content').innerHTML = view.plans.map(plan => {
      const active = plan.status === 'active';
      const rows = plan.stages.map(s => [
        esc(s.label),stageRange(s),String(s.remaining),num(s.stop),
        s.remaining > 0 && plan.status !== 'filled' ?
          `<button type="button" class="text-button" data-plan-fill="${esc(plan.id)}" data-plan-stage="${esc(s.id)}">复核／登记成交</button>` : '已完成',
      ]);
      const fills = plan.fills.map(f => `${f.session} · ${f.quantity} 股 × ${money(f.price)}`);
      return `<details class="portfolio-plan" ${active ? 'open' : ''}><summary><strong>${esc(plan.symbol)}</strong> ${badge(STAGE[plan.status] || plan.status,active ? 'positive' : '')} ${plan.override ? badge('人工计划','caution') : ''} <span class="inline-note">至 ${esc(when(plan.expires_at))}</span></summary>
        ${table(['档位','计划价','剩余股数','保护位','操作'],rows,[1,2,3])}
        ${fills.length ? `<p class="inline-note">成交记录 · ${esc(fills.join('；'))}</p>` : ''}
        ${active ? `<button type="button" class="text-button" data-plan-cancel="${esc(plan.id)}">取消剩余计划</button>` : ''}
        </details>`;
    }).join('');
  }

  async function loadPortfolio() {
    const token = ++model.generation;
    try {
      const view = await api('/portfolio');
      if (token !== model.generation) return;
      model.portfolio = view;
      renderAccount(view);
      renderPlans(view);
    } catch (error) {
      if (token === model.generation) $('#portfolio-account-content').textContent = error.message;
    }
  }

  function renderEvaluation(view) {
    const rows = view.stages.map(s => [
      esc(s.label) + (s.tactical ? ' · 机动' : ''),stageRange(s),String(s.quantity),
      `${num(s.warning)} / ${num(s.stop)} <small>(${pct(s.stop_percent)})</small>`,
      money(s.stop_risk),
    ]);
    const flag = view.decision === '建议执行' ? 'positive' : view.decision === '拒绝' ? 'negative' : 'caution';
    const valuations = view.valuation.prices ? ` · ${view.valuation.prices.map(p => num(p)).join(' / ')} USD` : '';
    const reason = [view.signal ? view.signal.name : '', view.risk_profile.label, view.valuation.state + valuations].filter(Boolean).join(' · ');
    const group = view.related.length ? view.related.map(p => `${p.symbol} ${money(p.value)}（${p.reasons.join('、')}）${p.counted ? '' : ' · 成分重叠参考'}`) : [];
    const amounts = Object.entries(view.headroom).map(([key,value]) => `${key} ${money(value)}`);
    $('#portfolio-evaluation').innerHTML = `<div>${badge(view.decision,flag)} <strong>${esc(view.kind)}</strong> · ${esc(view.as_of)}</div>
      <p class="inline-note">${esc(reason)}</p><p class="inline-note">${esc(marketNote(view.market))}</p>
      ${list([...view.limits,...view.waiting,...view.notes])}
      ${amounts.length ? `<p class="inline-note">${esc(amounts.join(' · '))}</p>` : ''}
      ${group.length ? `<details class="inline-note"><summary>同向仓及预留 · ${money(view.similar_value)}</summary>${list(group)}</details>` : ''}
      ${rows.length ? table(['档位','可接受价','最多股数','预警 / 保护','到保护位风险含缓冲'],rows,[1,2,3,4]) : '<p class="inline-note">价位和保护依据待补。</p>'}
      <p class="inline-note">三档合计占用 ${money(view.planned_cash)} · 风险 ${money(view.planned_risk)}。保护距离按最高接受价计算。</p>
      <p class="inline-note">左侧等收回支撑上沿；右侧等连续收盘突破。执行前可按最新账户和日线复核。</p>
      <div class="portfolio-actions">
        ${view.decision === '建议执行' ? '<button type="button" class="primary" data-portfolio-create>保存建议计划</button>' : ''}
        <button type="button" class="secondary" data-portfolio-manual>人工开仓／加仓</button>
      </div>`;
  }

  async function evaluate() {
    const code = state.symbol, generation = state.generation;
    if (!code) return;
    const key = `${code}/${state.report?.market?.id || ''}/${state.report?.automatic_valuation_reference?.id || ''}`;
    model.evaluationKey = key;
    model.evaluated = null;
    $('#portfolio-evaluation').textContent = '正在计算账户风险和分档计划…';
    try {
      const view = await post('/portfolio/evaluate',{symbol:code});
      if (generation !== state.generation || model.evaluationKey !== key) return;
      model.evaluated = view;
      renderEvaluation(view);
    } catch (error) {
      if (generation === state.generation && model.evaluationKey === key) $('#portfolio-evaluation').textContent = error.message;
    }
  }

  function syncResearch() {
    const code = state.symbol;
    const key = `${code}/${state.report?.market?.id || ''}/${state.report?.automatic_valuation_reference?.id || ''}`;
    if (model.evaluationKey !== key) {
      model.evaluated = null;
      $('#portfolio-evaluation').textContent = '点击「评估买入」读取最新现金、持仓和行情。';
      if (model.requested === code) void evaluate();
    }
  }

  $('#portfolio-request-open').addEventListener('click',() => {
    $('#portfolio-request-form').reset(); $('#portfolio-request').showModal();
  });
  $('#portfolio-request-form').addEventListener('submit',event => {
    event.preventDefault();
    const code = plain('#portfolio-request-form').symbol.toUpperCase().replace(/^\$/,'');
    $('#portfolio-request').close();
    requestEvaluation(code);
  });

  function requestEvaluation(code) {
    model.requested = code;
    model.evaluationKey = '';
    if (state.page === 'research' && state.symbol === code) void evaluate();
    else researchHash(code);
  }
  $('#portfolio-evaluate').addEventListener('click',event => {
    model.requested = state.symbol;
    void busy(event.currentTarget,evaluate);
  });
  $('#portfolio-reload').addEventListener('click',event => { void busy(event.currentTarget,loadPortfolio); });

  $('#portfolio-account-open').addEventListener('click',() => {
    const a = model.portfolio?.account;
    const form = $('#portfolio-account-form'); form.reset();
    for (const key of ['principal','loss_percent','total_cash','available_cash','net_flows']) {
      if (a) form.elements.namedItem(key).value = a[key];
    }
    form.dataset.version = a?.version || '';
    formError('#portfolio-account-error'); $('#portfolio-account').showModal();
  });
  $('#portfolio-account-form').addEventListener('submit',event => {
    event.preventDefault();
    const form = event.currentTarget;
    void busy(form.querySelector('[type=submit]'),async () => {
      try {
        const data = {...plain('#portfolio-account-form'),expected_version:form.dataset.version || null};
        await api('/portfolio/account',{method:'PUT',body:JSON.stringify(data)});
        window.dispatchEvent(new Event('equity:cash-us'));
        $('#portfolio-account').close(); await loadPortfolio(); notice('账户和现金已更新。');
      } catch (error) { formError('#portfolio-account-error',error.message); }
    });
  });

  async function savePlan(view,manual = null) {
    const plan = await post('/portfolio/plans',{symbol:view.symbol,expected_version:view.version,manual});
    await loadPortfolio();
    notice(`${plan.symbol} 已保存${manual ? '人工' : '建议'}计划。到「我的持仓 → 买入计划」复核和登记实际成交。`);
  }

  function openManual() {
    const v = model.evaluated;
    const form = $('#portfolio-manual-form'); form.reset();
    model.manual = null;
    form.dataset.symbol = v.symbol;
    form.elements.price.value = v.price || '';
    const entry = v.stages.find(s => s.id === 'entry');
    if (entry) form.elements.stop.value = entry.stop;
    $('#portfolio-manual-title').textContent = `${v.symbol} · 人工${v.kind}`;
    $('#portfolio-manual-preview').textContent = '';
    $('#portfolio-manual-save').disabled = true;
    formError('#portfolio-manual-error'); $('#portfolio-manual').showModal();
  }
  $('#portfolio-manual-form').addEventListener('input',() => {
    model.manual = null; $('#portfolio-manual-save').disabled = true;
    $('#portfolio-manual-preview').textContent = ''; formError('#portfolio-manual-error');
  });
  $('#portfolio-manual-form').addEventListener('submit',event => {
    event.preventDefault();
    const form = event.currentTarget, code = form.dataset.symbol;
    const values = plain('#portfolio-manual-form');
    const manual = {...values,quantity:Number(values.quantity),tactical:form.elements.tactical.checked};
    void busy(form.querySelector('[type=submit]'),async () => {
      try {
        const view = await post('/portfolio/evaluate',{symbol:code,manual});
        const current = plain('#portfolio-manual-form');
        if (!$('#portfolio-manual').open || form.dataset.symbol !== code || JSON.stringify(current) !== JSON.stringify(values)) return;
        model.manual = {view,manual};
        const m = view.manual;
        $('#portfolio-manual-preview').textContent = `系统原建议 ${view.decision} · 计划占用 ${money(m.cash)} · 新增风险 ${money(m.added_risk)} · 合计压力 ${money(m.combined_risk)} · 同向持仓增至 ${money(m.similar_after)} · 计划后可用 ${money(m.available_after)}`;
        $('#portfolio-manual-save').disabled = m.available_after === null || Number(m.available_after) < 0;
      } catch (error) { formError('#portfolio-manual-error',error.message); }
    });
  });
  $('#portfolio-manual-save').addEventListener('click',event => {
    const item = model.manual;
    if (!item) return;
    void busy(event.currentTarget,async () => {
      try {
        await savePlan(item.view,item.manual);
        $('#portfolio-manual').close();
        if (state.symbol === item.view.symbol) {
          model.evaluated = null;
          $('#portfolio-evaluation').textContent = '人工计划已保存，可在持仓页查看。';
        }
      } catch (error) { formError('#portfolio-manual-error',error.message); }
    });
  });

  async function openFill(plan,stage) {
    if ($('#portfolio-fill').open) return;
    const form = $('#portfolio-fill-form'), button = form.querySelector('[type=submit]');
    form.reset(); form.elements.quantity.value = stage.remaining;
    model.fill = null; formError('#portfolio-fill-error');
    $('#portfolio-fill-title').textContent = `${plan.symbol} · ${stage.label}成交`;
    $('#portfolio-fill-check').textContent = '正在复核当前资金和成交条件…';
    button.disabled = true; $('#portfolio-fill').showModal();
    try {
      const check = await api(`/portfolio/plans/${plan.id}/check?stage=${stage.id}`);
      if (!$('#portfolio-fill').open) return;
      model.fill = {plan,stage,check};
      $('#portfolio-fill-check').textContent = (check.ready ? '当前条件已满足' : check.reasons.join('；'))
        + `。计划价 ${stageRange(stage)} · 保护位 ${money(stage.stop)}。这里只登记你在券商的实际成交。`;
      button.disabled = false;
    } catch (error) { formError('#portfolio-fill-error',error.message); }
  }
  $('#portfolio-fill-form').addEventListener('submit',event => {
    event.preventDefault();
    const current = model.fill, form = event.currentTarget;
    if (!current) return;
    void busy(form.querySelector('[type=submit]'),async () => {
      try {
        const data = plain('#portfolio-fill-form');
        await post(`/portfolio/plans/${current.plan.id}/fills`,{...data,stage:current.stage.id,
          quantity:Number(data.quantity),cash_already_recorded:form.elements.cash_already_recorded.checked,expected_version:current.check.version});
        $('#portfolio-fill').close(); await loadWorkspace(); notice('实际成交已登记，持仓和现金已同步。');
      } catch (error) { formError('#portfolio-fill-error',error.message); }
    });
  });

  document.addEventListener('click',event => {
    const create = event.target.closest('[data-portfolio-create]');
    if (create && model.evaluated?.symbol === state.symbol) {
      const view = model.evaluated;
      void busy(create,async () => {
        try {
          await savePlan(view);
          if (state.symbol === view.symbol) {
            model.evaluated = null;
            $('#portfolio-evaluation').textContent = '建议计划已保存，可在持仓页查看。';
          }
        } catch (error) { notice(error.message,true); }
      });
      return;
    }
    if (event.target.closest('[data-portfolio-manual]') && model.evaluated?.symbol === state.symbol) { openManual(); return; }
    const fill = event.target.closest('[data-plan-fill]');
    if (fill) {
      const plan = model.portfolio?.plans.find(p => p.id === fill.dataset.planFill);
      const stage = plan?.stages.find(s => s.id === fill.dataset.planStage);
      if (stage) void openFill(plan,stage);
      return;
    }
    const cancel = event.target.closest('[data-plan-cancel]');
    if (cancel) {
      const plan = model.portfolio?.plans.find(p => p.id === cancel.dataset.planCancel);
      if (plan) removeRecord(`取消 ${plan.symbol} 的剩余计划？`,'取消后释放预留资金和风险，已登记的成交继续保留。',async () => {
        await post(`/portfolio/plans/${plan.id}/cancel`,{expected_version:plan.version});
      });
    }
  });
  window.addEventListener('portfolio:workspace',() => { void loadPortfolio(); });
  window.addEventListener('portfolio:research',syncResearch);
  return {setPrincipal,requestEvaluation};
}
