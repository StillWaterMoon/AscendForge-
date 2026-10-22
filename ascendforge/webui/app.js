'use strict';

/* AscendForge browser control surface.
 * No framework, no CDN. The browser only renders state and sends control
 * requests; scheduling, model calls and submission polling happen server-side.
 */

const state = {
  me: null,
  csrf: null,
  timer: null,
  workerTimer: null,
  route: '',
  taskId: null,
  taskTab: 'overview',
  summary: null,
  platform: null,
  model: null,
  capabilities: null,
  skills: [],
  attemptTab: 'source',
  attemptData: null,
  toastTimer: null,
};

const TASK_STATUS = {
  pending: '待开始',
  running: '运行中',
  paused: '已暂停',
  needs_login: '需要登录',
  needs_review: '需要审查',
  completed: '已完成',
  failed: '失败',
  stopped: '已停止',
};
const ATTEMPT_STATUS = {
  generated: '已生成',
  local_checking: '本地校验',
  queued: '排队中',
  submitting: '提交中',
  submission_unknown: '提交未知',
  judging: '评测中',
  finished: '已结束',
  failed: '失败',
  running: '评测中',
};
const SUBMISSION_STATUS = {
  queued: '排队中',
  submitting: '发送中',
  submitted: '已提交',
  judging: '评测中',
  finished: '已结束',
  failed: '失败',
  unknown: '结果未知',
};
const OUTCOME_STATUS = {
  passed: '通过',
  failed: '答案错误',
  compile_error: '编译错误',
  runtime_error: '运行错误',
  timeout: '超时',
  system_error: '系统错误',
  running: '评测中',
  unknown: '未知',
};

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

function esc(value) {
  if (value === null || value === undefined) return '';
  return String(value)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;');
}

function fmtTime(ts) {
  if (!ts) return '—';
  const d = new Date(ts * 1000);
  if (Number.isNaN(d.getTime())) return '—';
  return d.toLocaleString('zh-CN', { hour12: false });
}

function fmtDuration(sec) {
  if (sec === null || sec === undefined) return '—';
  sec = Math.max(0, Math.round(sec));
  if (sec < 60) return `${sec}s`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m ${sec % 60}s`;
  return `${Math.floor(sec / 3600)}h ${Math.floor((sec % 3600) / 60)}m`;
}

function fmtBytes(n) {
  if (n === null || n === undefined) return '—';
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KiB`;
  return `${(n / 1024 / 1024).toFixed(2)} MiB`;
}

function badge(text, cls = '') {
  return `<span class="badge ${esc(cls)}">${esc(text)}</span>`;
}

function taskBadge(status) {
  return badge(TASK_STATUS[status] || status, status);
}

function attemptBadge(status) {
  return badge(ATTEMPT_STATUS[status] || status, status);
}

function outcomeBadge(outcome) {
  const cls = outcome === 'passed' ? 'pass' : outcome === 'running' ? 'running' : outcome ? 'fail' : 'unknown';
  return badge(OUTCOME_STATUS[outcome] || outcome || '未评测', cls);
}

function jsonPretty(value) {
  return JSON.stringify(value, null, 2);
}

function toast(message, type = '') {
  const el = $('#toast');
  el.textContent = message;
  el.className = `toast show ${type}`;
  clearTimeout(state.toastTimer);
  state.toastTimer = setTimeout(() => { el.className = 'toast'; }, 4200);
}

function openModal(title, body) {
  $('#modal-root').innerHTML = `
    <div class="modal-backdrop" data-modal-backdrop>
      <div class="modal" role="dialog" aria-modal="true" aria-label="${esc(title)}">
        <div class="modal-head">
          <h2>${esc(title)}</h2>
          <button class="modal-close" type="button" data-modal-close aria-label="关闭">×</button>
        </div>
        <div class="modal-body" id="modal-body">${body}</div>
      </div>
    </div>`;
  $('[data-modal-close]').addEventListener('click', closeModal);
  $('[data-modal-backdrop]').addEventListener('click', (e) => {
    if (e.target === e.currentTarget) closeModal();
  });
  document.removeEventListener('keydown', escClose);
  document.addEventListener('keydown', escClose);
}

function escClose(e) {
  if (e.key === 'Escape') closeModal();
}

function closeModal() {
  $('#modal-root').innerHTML = '';
  document.removeEventListener('keydown', escClose);
}

function setPoll(fn, ms) {
  clearInterval(state.timer);
  state.timer = null;
  if (fn) state.timer = setInterval(() => {
    if (document.hidden) return;
    // Do not re-render the page under an open modal or while the operator is
    // typing. Those re-renders replace the DOM and fight the current focus.
    if (document.querySelector('.modal')) return;
    const active = document.activeElement;
    if (active && ['INPUT', 'TEXTAREA', 'SELECT'].includes(active.tagName)) return;
    fn();
  }, ms);
}

function uuid() {
  if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
  return 'req-' + Date.now().toString(16) + '-' + Math.random().toString(16).slice(2);
}

async function api(path, options = {}) {
  const method = options.method || 'GET';
  const headers = { 'Accept': 'application/json' };
  if (options.body !== undefined) headers['Content-Type'] = 'application/json';
  if (method !== 'GET' && state.csrf) headers['X-CSRF-Token'] = state.csrf;
  if (method !== 'GET' && options.idem !== false) headers['X-Request-Id'] = options.requestId || uuid();
  const response = await fetch(path, {
    method,
    headers,
    credentials: 'same-origin',
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
  });
  let data = {};
  try { data = await response.json(); } catch (_) { data = {}; }
  if (!response.ok) {
    const error = data.error || { code: 'HTTP_' + response.status, message: response.statusText };
    const err = new Error(error.message || `HTTP ${response.status}`);
    err.code = error.code;
    err.status = response.status;
    err.details = error.details || {};
    if (error.code === 'AUTH_REQUIRED') {
      state.me = null;
      state.csrf = null;
      if (!document.getElementById('login-form')) renderLogin('登录已过期，请重新登录');
      err.handled = true;
    }
    throw err;
  }
  return data;
}

function setActiveNav(name) {
  $$('#nav a').forEach((a) => a.classList.toggle('active', a.dataset.nav === name));
}

function navigate(hash) {
  if (location.hash === hash) route();
  else location.hash = hash;
}

async function boot() {
  try {
    const me = await api('/api/auth/me');
    if (me.authenticated) {
      state.me = me.user;
      state.csrf = me.csrf_token;
      await showShell();
      route();
      startWorkerPolling();
    } else {
      const setup = await api('/api/setup/status');
      if (setup.needs_setup) renderSetup();
      else renderLogin();
    }
  } catch (err) {
    renderLogin(String(err.message || err));
  }
}

async function showShell() {
  $('#topbar').hidden = false;
  $('#current-user').textContent = state.me ? state.me.username : '';
  await refreshWorkerPill();
}

function startWorkerPolling() {
  clearInterval(state.workerTimer);
  state.workerTimer = setInterval(refreshWorkerPill, 15000);
}

async function refreshWorkerPill() {
  try {
    const data = await api('/api/worker');
    const pill = $('#worker-pill');
    const w = data.worker || {};
    pill.textContent = w.alive ? 'worker 在线' : 'worker 离线';
    pill.className = `worker-pill ${w.alive ? 'alive' : 'dead'}`;
    pill.title = w.alive ? `心跳 ${w.age_sec}s 前` : '后台 worker 未运行，任务不会推进';
  } catch (_) { /* non-critical */ }
}

/* ------------------------------------------------------------------ */
/* Auth                                                                */
/* ------------------------------------------------------------------ */
function authShell(inner) {
  return `<div class="auth-shell"><div class="auth-card">
    <div class="auth-brand">
      <img class="auth-lockup" src="/static/assets/ascendforge-logo.png" alt="AscendForge">
    </div>
    <div class="auth-tagline">昇腾工坊 · Agent 驱动的算子优化</div>
    ${inner}
    <div class="auth-footer">服务器托管优化循环。关闭网页不会停止任务；凭据只在服务端保存。</div>
  </div></div>`;
}

function renderLogin(message = '') {
  clearInterval(state.workerTimer);
  clearInterval(state.timer);
  $('#topbar').hidden = true;
  $('#view').innerHTML = authShell(`
    <div class="auth-sub">登录以管理 CANNJudge 算子优化任务</div>
    ${message ? `<div class="alert error" id="auth-error">${esc(message)}</div>` : '<div id="auth-error"></div>'}
    <form id="login-form" class="stack" style="margin-top:16px">
      <div class="field"><label for="login-user">用户名</label><input id="login-user" type="text" autocomplete="username" required></div>
      <div class="field"><label for="login-pass">密码</label><input id="login-pass" type="password" autocomplete="current-password" required></div>
      <button class="btn btn-primary btn-block" type="submit">登录</button>
    </form>`);
  $('#login-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const button = event.target.querySelector('button');
    button.disabled = true;
    try {
      await api('/api/auth/login', {
        method: 'POST',
        body: { username: $('#login-user').value.trim(), password: $('#login-pass').value },
        idem: false,
      }).then((data) => {
        state.me = data.user;
        state.csrf = data.csrf_token;
      });
      await showShell();
      toast('登录成功', 'success');
      if (!location.hash) location.hash = '#/dashboard';
      route();
      startWorkerPolling();
    } catch (err) {
      const box = $('#auth-error');
      box.className = 'alert error';
      box.textContent = err.message || '登录失败';
      button.disabled = false;
    }
  });
}

function renderSetup(message = '') {
  clearInterval(state.workerTimer);
  clearInterval(state.timer);
  $('#topbar').hidden = true;
  $('#view').innerHTML = authShell(`
    <div class="auth-sub">首次初始化。初始化令牌打印在服务器控制台/容器日志中，不会在网页回显。</div>
    ${message ? `<div class="alert error" id="auth-error">${esc(message)}</div>` : '<div id="auth-error"></div>'}
    <form id="setup-form" class="stack" style="margin-top:16px">
      <div class="field"><label for="setup-token">初始化令牌</label><input id="setup-token" type="password" autocomplete="off" required></div>
      <div class="field"><label for="setup-user">管理员用户名</label><input id="setup-user" type="text" value="admin" autocomplete="username" required></div>
      <div class="field"><label for="setup-pass">密码（至少 10 位）</label><input id="setup-pass" type="password" autocomplete="new-password" minlength="10" required></div>
      <div class="field"><label for="setup-pass2">重复密码</label><input id="setup-pass2" type="password" autocomplete="new-password" minlength="10" required></div>
      <button class="btn btn-primary btn-block" type="submit">初始化 AscendForge</button>
    </form>`);
  $('#setup-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const pass = $('#setup-pass').value;
    if (pass !== $('#setup-pass2').value) {
      const box = $('#auth-error'); box.className = 'alert error'; box.textContent = '两次密码不一致'; return;
    }
    const button = event.target.querySelector('button');
    button.disabled = true;
    try {
      const data = await api('/api/setup', {
        method: 'POST',
        body: { token: $('#setup-token').value.trim(), username: $('#setup-user').value.trim(), password: pass },
        idem: false,
      });
      state.me = data.user;
      state.csrf = data.csrf_token;
      await showShell();
      toast('初始化完成', 'success');
      location.hash = '#/platform';
      route();
      startWorkerPolling();
    } catch (err) {
      const box = $('#auth-error'); box.className = 'alert error'; box.textContent = err.message || '初始化失败';
      button.disabled = false;
    }
  });
}

/* ------------------------------------------------------------------ */
/* Router                                                              */
/* ------------------------------------------------------------------ */
async function route() {
  if (!state.me) return;
  const hash = location.hash || '#/dashboard';
  const parts = hash.replace(/^#\//, '').split('/');
  const view = parts[0] || 'dashboard';
  state.route = view;
  state.taskId = parts[1] || null;
  setActiveNav(view === 'tasks' ? 'dashboard' : view);
  try {
    if (view === 'dashboard') await renderDashboard();
    else if (view === 'tasks' && state.taskId) await renderTask(state.taskId);
    else if (view === 'settings') await renderSettings();
    else if (view === 'platform') await renderPlatform();
    else if (view === 'backups') await renderBackups();
    else { location.hash = '#/dashboard'; }
  } catch (err) {
    if (!err.handled) {
      $('#view').innerHTML = `<div class="alert error">${esc(err.message || err)}</div>`;
    }
  }
}

window.addEventListener('hashchange', route);

/* ------------------------------------------------------------------ */
/* Dashboard                                                           */
/* ------------------------------------------------------------------ */
async function renderDashboard() {
  setPoll(() => renderDashboard().catch(() => {}), 5000);
  const [summary, taskData] = await Promise.all([api('/api/summary'), api('/api/tasks')]);
  const scroll = window.scrollY;
  state.summary = summary;
  const tasks = taskData.tasks || [];
  const counts = summary.counts || {};
  const attention = (counts.needs_login || 0) + (counts.needs_review || 0);
  const worker = summary.worker || {};
  const platform = summary.platform || {};
  const model = summary.model || {};
  $('#view').innerHTML = `
    <div class="row-between" style="margin-bottom:18px">
      <div>
        <h1>任务</h1>
        <div class="muted">服务器后台持续优化；浏览器只是控制入口。</div>
      </div>
      <div class="row">
        <button class="btn btn-ghost" id="refresh-dashboard" type="button">刷新</button>
        <button class="btn btn-primary" id="new-task" type="button">新建任务</button>
      </div>
    </div>

    ${attention ? `<div class="alert warn" style="margin-bottom:16px">有 ${attention} 个任务需要处理（登录失效或结果未知），提交调度已暂停。请进入任务详情处理。</div>` : ''}
    ${!worker.alive ? `<div class="alert warn" style="margin-bottom:16px">后台 worker 未运行。网页操作会保存状态，但不会推进模型调用或提交。请在服务器启动 <span class="mono">ascendforge worker</span>。</div>` : ''}

    <div class="grid grid-4" style="margin-bottom:16px">
      <div class="metric"><div class="metric-label">运行中</div><div class="metric-value">${esc(counts.running || 0)}</div><div class="metric-note">模型/提交循环</div></div>
      <div class="metric"><div class="metric-label">需处理</div><div class="metric-value" style="color:${attention ? 'var(--accent)' : 'inherit'}">${esc(attention)}</div><div class="metric-note">登录或未知提交</div></div>
      <div class="metric"><div class="metric-label">已完成</div><div class="metric-value">${esc(counts.completed || 0)}</div><div class="metric-note">含无提升完成</div></div>
      <div class="metric"><div class="metric-label">全部任务</div><div class="metric-value">${esc(tasks.length)}</div><div class="metric-note">持久化于服务器</div></div>
    </div>

    <div class="card" style="margin-bottom:16px">
      <div class="row" style="gap:22px">
        <div class="small"><span class="faint">CANNJudge：</span>${platform.connected ? badge('已连接', 'completed') : badge('未连接', 'pending')} <span class="muted">${esc(platform.account_masked || '')}</span></div>
        <div class="small"><span class="faint">模型：</span>${model.configured ? badge(model.model || '已配置', 'running') : badge('未配置', 'pending')}</div>
        <div class="small"><span class="faint">执行器：</span><span class="mono">${esc(summary.capabilities?.executor || '—')}</span></div>
        <div class="small"><span class="faint">NPU：</span>${summary.capabilities?.npu === 'available' ? badge('可用', 'completed') : badge('不可用', 'pending')}</div>
        <div class="small"><span class="faint">Worker：</span>${worker.alive ? badge('在线', 'completed') : badge('离线', 'failed')}</div>
      </div>
    </div>

    <div class="card task-board">
      <div class="card-head"><div><h2>全部任务</h2><div class="card-sub">点击任务查看代码、反馈和调度状态</div></div><div class="faint small">${esc(tasks.length)} 个任务</div></div>
      ${tasks.length ? `
      <div class="task-grid">
        ${tasks.map((task) => {
          const quota = task.quota || {};
          const next = quota.next_submit_at ? fmtTime(quota.next_submit_at) : '等待调度';
          const sends = quota.sends_total ?? 0;
          const maxSends = limitLabel(quota.max_submissions_per_task);
          const error = task.last_error ? String(task.last_error).slice(0, 110) : '';
          return `<article class="task-card status-${esc(task.status)}" data-task-open="${esc(task.id)}" tabindex="0">
            <div class="task-card-top">
              <div>${taskBadge(task.status)}</div>
              <span class="task-card-id mono" title="${esc(task.id)}">${esc(String(task.id).slice(0, 14))}…</span>
            </div>
            <h3 class="task-card-title">${esc(task.title)}</h3>
            <div class="task-card-problem">${esc(task.problem_title || '未命名题目')} <span class="mono">${esc(task.problem_id || '')}</span></div>
            <div class="task-card-metrics">
              <div><span class="task-k">最好成绩</span><div class="task-v">${renderBestCompact(task.best_metric)}</div></div>
              <div><span class="task-k">提交</span><div class="task-v mono">${esc(sends)}<span class="faint"> / ${esc(maxSends)}</span></div></div>
              <div><span class="task-k">下一次</span><div class="task-v small">${esc(next)}</div></div>
            </div>
            <div class="task-card-foot">
              <span class="task-error">${esc(error || taskFootNote(task))}</span>
              <span class="faint mono">${esc(fmtTime(task.updated_at))}</span>
            </div>
          </article>`;
        }).join('')}
      </div>` : `<div class="empty">还没有任务。先连接 CANNJudge 与模型，再新建一个题目任务。</div>`}
    </div>`;

  $('#new-task').addEventListener('click', () => openNewTaskModal().catch((e) => toast(e.message || '打开表单失败', 'error')));
  $('#refresh-dashboard').addEventListener('click', () => renderDashboard().catch((e) => toast(e.message, 'error')));
  $$('[data-task-open]').forEach((row) => {
    row.addEventListener('click', () => navigate(`#/tasks/${row.dataset.taskOpen}`));
    row.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        navigate(`#/tasks/${row.dataset.taskOpen}`);
      }
    });
  });
  if (Math.abs(window.scrollY - scroll) > 1) window.scrollTo(0, scroll);
}

function phaseLabel(phase) {
  return ({ baseline: '建立基线', compile: '编译修复', runtime: '运行修复', correctness: '正确性', performance: '性能优化', verify: '确认结果' })[phase] || phase || '等待结果';
}

function limitLabel(value) {
  return value === 0 || value === null || value === undefined ? '不限' : String(value);
}

function taskFootNote(task) {
  if (task.stop_reason) return `停止原因：${task.stop_reason}`;
  return TASK_STATUS[task.status] || task.status || '—';
}

function renderBestCompact(best) {
  if (!best) return '<span class="faint">—</span>';
  if (best.metric && typeof best.metric === 'object') {
    return `<span class="mono">${esc(best.metric.value ?? '—')}</span><div class="faint tiny">${esc(best.metric.metric || 'metric')}</div>`;
  }
  if (best.raw_status) return `<span class="mono">${esc(best.raw_status)}</span>`;
  return '<span class="faint">—</span>';
}

function renderBest(best) {
  if (!best) return '<span class="faint">—</span>';
  const metric = best.metric;
  if (metric && typeof metric === 'object') {
    return `<span class="mono">${esc(metric.metric)}=${esc(metric.value)}</span><div class="faint small">attempt ${esc(String(best.attempt_id || '').slice(0, 12))}</div>`;
  }
  return `<span class="mono">${esc(best.raw_status || '通过')}</span>`;
}

async function openNewTaskModal() {
  const [platformData, modelData] = await Promise.all([api('/api/platform/status'), api('/api/settings/model')]);
  const connected = platformData.platform?.connected;
  const configured = modelData.model?.configured;
  openModal('新建优化任务', `
    ${!connected ? `<div class="alert warn">CANNJudge 尚未连接。任务创建会先验证账号能否连接 judge。<a href="#/platform" data-close-and-go="#/platform">前往连接</a></div>` : ''}
    ${!configured ? `<div class="alert warn">模型 API 尚未配置。可以创建任务，但启动后需要模型才能推进。<a href="#/settings" data-close-and-go="#/settings">前往配置</a></div>` : ''}
    <form id="new-task-form" class="stack">
      <div class="form-grid">
        <div class="field span-2"><label for="nt-title">任务标题</label><input id="nt-title" type="text" maxlength="120" placeholder="例如：Add 核函数 · 第一轮优化" required></div>
        <div class="field span-2"><label for="nt-url">CANNJudge 题目链接</label><input id="nt-url" type="url" placeholder="https://cannjudge.cn/hc_cann_codelabs/cann/add" required>
          <div class="hint">必须是完整的小组/比赛/题目链接。导入时会下载模板并校验账号权限。</div></div>
        <div class="field span-2"><label for="nt-strategy">题目策略（可选）</label>
          <textarea id="nt-strategy" rows="4" maxlength="8000" placeholder="例如：本题进入性能优化阶段，重点研究 UB 分块与 DataCopy 重叠；先保证 int8/int16/int32/fp16/fp32 全部正确，再优化。"></textarea>
          <div class="hint">只放题目目标、约束和待验证假设；通用优化方法由 optimize-operator skill 提供。</div>
        </div>
      </div>
      <details class="advanced">
        <summary>预算与调度（可选）</summary>
        <div class="form-grid" style="margin-top:12px">
          <div class="field"><label>每题提交上限（0 = 不限）</label><input id="cfg-max-task-subs" type="number" min="0" value="30"></div>
          <div class="field"><label>每日提交上限（0 = 不限）</label><input id="cfg-max-day-subs" type="number" min="0" value="20"></div>
          <div class="field"><label>最小提交间隔（秒，≥60）</label><input id="cfg-interval" type="number" min="60" value="300"></div>
          <div class="field"><label>同账号在途提交上限</label><input id="cfg-inflight" type="number" min="1" max="3" value="1"></div>
          <div class="field"><label>最多尝试轮数</label><input id="cfg-max-attempts" type="number" min="1" value="30"></div>
          <div class="field"><label>最多模型调用</label><input id="cfg-max-calls" type="number" min="1" value="120"></div>
          <div class="field"><label>Token 上限</label><input id="cfg-max-tokens" type="number" min="1000" value="3000000"></div>
          <div class="field"><label>费用上限（可选，需配单价）</label><input id="cfg-max-cost" type="number" min="0" step="0.0001" placeholder="不设置"></div>
          <div class="field"><label>总运行时限（秒）</label><input id="cfg-max-runtime" type="number" min="60" value="21600"></div>
          <div class="field"><label>停滞轮数停止</label><input id="cfg-max-stall" type="number" min="1" value="5"></div>
          <div class="field"><label>指标</label><select id="cfg-metric">
            <option value="auto">自动（score / precision_ratio）</option>
            <option value="score">score 越高越好</option>
            <option value="precision_ratio">precision_ratio 越高越好</option>
            <option value="time">time 越低越好</option>
            <option value="none">不比较指标</option>
          </select></div>
          <div class="field"><label>目标成绩（可选）</label><input id="cfg-target" type="number" step="any" placeholder="达到后停止"></div>
          <div class="field"><label>time 指标单位（仅 time 时必填）</label><input id="cfg-time-unit" type="text" placeholder="例如 ms（平台未声明请勿猜）"></div>
          <div class="field"><label>Agent 每轮最多步骤</label><input id="cfg-agent-steps" type="number" min="1" max="30" value="10"></div>
        </div>
      </details>
      <div class="alert error" id="new-task-error" hidden></div>
      <div class="form-actions">
        <button class="btn" type="button" data-modal-close>取消</button>
        <button class="btn btn-primary" type="submit">导入题目并创建</button>
      </div>
    </form>`);
  $('#new-task-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const errorBox = $('#new-task-error');
    errorBox.hidden = true;
    const button = event.target.querySelector('button[type="submit"]');
    button.disabled = true;
    button.textContent = '正在下载模板并校验账号…';
    const config = {};
    const putNum = (id, key, integer = true) => {
      const raw = $(id).value.trim();
      if (raw === '') return;
      const value = Number(raw);
      if (!Number.isFinite(value)) return;
      config[key] = integer ? Math.trunc(value) : value;
    };
    putNum('#cfg-max-task-subs', 'max_submissions_per_task');
    putNum('#cfg-max-day-subs', 'max_submissions_per_day');
    putNum('#cfg-interval', 'min_submit_interval_sec');
    putNum('#cfg-inflight', 'max_inflight');
    putNum('#cfg-max-attempts', 'max_attempts');
    putNum('#cfg-max-calls', 'max_model_calls');
    putNum('#cfg-max-tokens', 'max_tokens');
    putNum('#cfg-max-runtime', 'max_runtime_sec');
    putNum('#cfg-max-stall', 'max_stall_rounds');
    putNum('#cfg-agent-steps', 'agent_max_steps');
    putNum('#cfg-max-cost', 'max_cost', false);
    putNum('#cfg-target', 'target_score', false);
    config.metric = $('#cfg-metric').value;
    if ($('#cfg-time-unit').value.trim()) config.time_unit = $('#cfg-time-unit').value.trim();
    if ($('#nt-strategy').value.trim()) config.strategy_prompt = $('#nt-strategy').value.trim();
    try {
      const data = await api('/api/tasks', {
        method: 'POST',
        body: { title: $('#nt-title').value.trim(), problem_url: $('#nt-url').value.trim(), config },
        requestId: uuid(),
      });
      closeModal();
      toast('任务已创建，题目模板已保存', 'success');
      navigate(`#/tasks/${data.task.id}`);
    } catch (err) {
      errorBox.hidden = false;
      errorBox.className = 'alert error';
      errorBox.textContent = err.message || '创建失败';
      button.disabled = false;
      button.textContent = '导入题目并创建';
      if (err.code === 'ACCOUNT_CANNOT_CONNECT_JUDGE') {
        errorBox.innerHTML += ' <a href="#/platform" data-close-and-go="#/platform">前往平台连接</a>';
      }
    }
  });
}

document.addEventListener('click', (event) => {
  const link = event.target.closest('[data-close-and-go]');
  if (link) {
    event.preventDefault();
    closeModal();
    navigate(link.dataset.closeAndGo);
    return;
  }
  const closer = event.target.closest('[data-modal-close]');
  if (closer && closer.closest('#modal-root')) {
    closeModal();
  }
});

/* ------------------------------------------------------------------ */
/* Task detail                                                         */
/* ------------------------------------------------------------------ */
async function renderTask(taskId) {
  setPoll(() => renderTask(taskId).catch(() => {}), 3000);
  const [data, eventData] = await Promise.all([
    api(`/api/tasks/${encodeURIComponent(taskId)}`),
    api(`/api/tasks/${encodeURIComponent(taskId)}/events?limit=150`),
  ]);
  const task = data.task;
  state.task = task;
  const quota = task.quota || {};
  const config = task.config || {};
  const usage = task.usage || {};
  const best = task.best_metric || task.best_metric_json;
  const isTerminal = ['completed', 'failed', 'stopped'].includes(task.status);
  const events = (eventData.events || []).slice(-120);
  const scroll = window.scrollY;

  $('#view').innerHTML = `
    <div class="row-between" style="margin-bottom:16px">
      <div style="min-width:0">
        <div class="row" style="margin-bottom:4px">
          <h1 style="margin:0">${esc(task.title)}</h1>
          ${taskBadge(task.status)}
        </div>
        <div class="muted small">${esc(task.problem_title || '')} <span class="mono">${esc(task.problem_id || '')}</span></div>
      </div>
      <div class="toolbar">
        ${task.status === 'pending' ? '<button class="btn btn-primary" data-task-action="start">开始</button>' : ''}
        ${(task.counts?.attempts || 0) === 0 && ['pending', 'running'].includes(task.status) ? '<button class="btn" data-baseline-submit="1" title="不依赖模型，直接提交一次模板基线">提交基线（手动）</button>' : ''}
        ${task.status === 'running' ? '<button class="btn" data-task-action="pause" title="暂停：停止新的模型调用和调度；排队提交保留，可恢复">暂停</button>' : ''}
        ${['paused', 'needs_login', 'needs_review'].includes(task.status) ? '<button class="btn btn-primary" data-task-action="resume" title="恢复：继续模型调用和排队提交">恢复</button>' : ''}
        ${!isTerminal ? '<button class="btn btn-danger" data-task-action="stop" title="停止：取消尚未发送的排队提交；远端已接受的提交不能撤回">停止</button>' : ''}
        ${['completed', 'stopped'].includes(task.status) && task.model_configured ? `<button class="btn btn-primary" data-continue-opt="1">${task.status === 'stopped' ? '继续优化' : '继续自动优化'}</button>` : ''}
        ${task.best_attempt_id ? `<a class="btn" href="/api/tasks/${encodeURIComponent(task.id)}/download/best">下载最好代码</a>` : ''}
        <a class="btn btn-ghost" href="/api/tasks/${encodeURIComponent(task.id)}/archive">导出档案</a>
        <button class="btn btn-ghost" data-budget="1" type="button">调整提交上限</button>
        <button class="btn btn-ghost" id="task-refresh" type="button">刷新</button>
      </div>
    </div>

    ${renderTaskAlert(task, quota)}

    <div class="strategy-strip">
      <div class="strategy-strip-main">
        <span class="strategy-strip-label">当前策略 · ${esc(phaseLabel(task.active_strategy?.phase))}</span>
        <span class="strategy-strip-text" title="${esc(task.active_strategy?.text || '')}">${esc(task.active_strategy?.text || '等待第一条远端结果')}</span>
        <span class="strategy-owner">长期约束：${config.strategy_prompt ? esc(String(config.strategy_prompt).slice(0, 120)) : '未设置'}</span>
      </div>
      <button class="btn btn-sm btn-ghost" data-message="1" type="button">给 Agent 留言</button>
    </div>

    <div class="grid grid-4" style="margin-bottom:16px">
      <div class="metric"><div class="metric-label">远端最好</div><div class="metric-value small">${renderBestValue(best)}</div><div class="metric-note">${esc(task.best_attempt_id ? '仅通过且可比的远端结果' : '暂无')}</div></div>
      <div class="metric"><div class="metric-label">候选 / 已提交</div><div class="metric-value">${esc(task.counts?.candidates ?? 0)} <span class="faint" style="font-size:14px">/ ${esc(quota.sends_total ?? 0)}</span></div><div class="metric-note">尝试上限 ${esc(config.max_attempts ?? '—')} · 提交上限 ${esc(limitLabel(config.max_submissions_per_task))}</div></div>
      <div class="metric"><div class="metric-label">下一次提交</div><div class="metric-value small">${esc(quota.next_submit_at ? fmtTime(quota.next_submit_at) : '等待 agent')}</div><div class="metric-note">间隔 ${esc(config.min_submit_interval_sec ?? '—')}s · 在途 ${esc(quota.inflight ?? 0)}</div></div>
      <div class="metric"><div class="metric-label">模型用量</div><div class="metric-value small">${esc(usage.calls ?? 0)} 次</div><div class="metric-note">${esc(usage.total_tokens ?? 0)} tokens · ${usage.cost_complete ? esc(formatCost(usage.known_cost)) : '费用未知'}</div></div>
    </div>

    <div class="grid grid-2" style="margin-bottom:16px">
      <div class="card">
        <div class="card-head"><h3>运行预算</h3><span class="row" style="gap:10px"><span class="faint small">服务器强制执行</span><button class="btn btn-sm btn-ghost" data-budget="1" type="button">调整上限</button></span></div>
        ${budgetBar('模型调用', usage.calls || 0, config.max_model_calls)}
        ${budgetBar('Tokens', usage.total_tokens || 0, config.max_tokens)}
        ${config.max_cost !== null && config.max_cost !== undefined ? budgetBar('费用（已知）', Number(usage.known_cost || 0), Number(config.max_cost), usage.cost_complete ? '' : '未知费用不可当作 0') : '<div class="faint small">未设置费用上限；费用按调用数/token/时间约束。</div>'}
        <div class="row" style="margin-top:12px; gap:18px">
          <span class="small muted">运行 ${esc(fmtDuration(task.budget?.runtime_sec ?? 0))} / ${esc(fmtDuration(config.max_runtime_sec ?? 0))}</span>
          <span class="small muted">停滞 ${esc(task.budget?.stall_rounds ?? 0)} / ${esc(config.max_stall_rounds ?? '—')} 轮</span>
        </div>
      </div>
      <div class="card">
        <div class="card-head"><h3>调度与设备</h3></div>
        <div class="stack small">
          <div class="row-between"><span class="muted">今日提交（${esc(quota.day || '')}）</span><span class="mono">${esc(quota.sends_today ?? 0)} / ${esc(limitLabel(quota.max_submissions_per_day))}</span></div>
          <div class="row-between"><span class="muted">账号在途</span><span class="mono">${esc(quota.inflight ?? 0)} / ${esc(quota.max_inflight ?? '—')}</span></div>
          <div class="row-between"><span class="muted">未知提交</span><span class="mono" style="color:${quota.unknown ? 'var(--accent)' : 'inherit'}">${esc(quota.unknown ?? 0)}</span></div>
          <div class="row-between"><span class="muted">执行器 / NPU</span><span class="mono">${esc(task.capabilities?.executor?.selected || '—')} / ${task.capabilities?.npu?.available ? '可用' : '不可用'}</span></div>
          <div class="row-between"><span class="muted">候选本地执行</span><span class="mono">${esc(task.capabilities?.candidate_execution?.status || '—')}</span></div>
          <div class="row-between"><span class="muted">Worker</span><span>${task.worker?.alive ? badge('在线', 'completed') : badge('离线', 'failed')}</span></div>
        </div>
      </div>
    </div>

    <div class="card">
      <div class="tabs" id="task-tabs">
        ${taskTabButton('overview', '概览')}
        ${taskTabButton('attempts', '尝试记录')}
        ${taskTabButton('submissions', '提交与反馈')}
        ${taskTabButton('config', '配置')}
      </div>
      <div id="task-tab-body">${renderTaskTab(task, events, config, usage)}</div>
    </div>`;

  $$('[data-task-action]').forEach((button) => {
    button.addEventListener('click', () => taskAction(task.id, button.dataset.taskAction));
  });
  const baselineButton = $('[data-baseline-submit]');
  if (baselineButton) baselineButton.addEventListener('click', () => baselineSubmit(task.id));
  const continueButton = $('[data-continue-opt]');
  if (continueButton) continueButton.addEventListener('click', () => continueOptimization(task.id));
  $$('[data-message]').forEach((button) => button.addEventListener('click', () => openMessageModal(task)));
  $$('[data-budget]').forEach((button) => button.addEventListener('click', () => openBudgetModal(task)));
  $('#task-refresh').addEventListener('click', () => renderTask(task.id).catch((e) => toast(e.message, 'error')));
  $$('[data-task-tab]').forEach((button) => {
    button.addEventListener('click', () => { state.taskTab = button.dataset.taskTab; renderTask(task.id).catch(() => {}); });
  });
  $$('[data-attempt-open]').forEach((row) => {
    row.addEventListener('click', () => openAttempt(task.id, row.dataset.attemptOpen));
  });
  $$('[data-resolve]').forEach((button) => {
    button.addEventListener('click', (event) => {
      event.stopPropagation();
      openResolveModal(task.id, button.dataset.resolve);
    });
  });
  window.scrollTo(0, scroll);
}

async function openBudgetModal(task) {
  const config = task.config || {};
  const quota = task.quota || {};
  const sent = quota.sends_total ?? 0;
  const candidates = task.counts?.candidates ?? 0;
  openModal('调整运行上限', `
    <form id="budget-form" class="stack">
      <div class="alert info">提交上限可以动态调整，但不能绕过调度器。当前已提交 <strong>${esc(sent)}</strong> 次，候选 <strong>${esc(candidates)}</strong> 个；调到已用值以下会在下一次调度时停止。</div>
      <div class="form-grid">
        <div class="field"><label for="budget-task-subs">每题提交上限</label><div class="row"><input id="budget-task-subs" type="number" min="0" max="500" value="${esc(config.max_submissions_per_task ?? 30)}"><label class="checkbox"><input id="budget-task-unlimited" type="checkbox" ${Number(config.max_submissions_per_task) === 0 ? 'checked' : ''}>不限</label></div></div>
        <div class="field"><label for="budget-day-subs">每日提交上限</label><div class="row"><input id="budget-day-subs" type="number" min="0" max="200" value="${esc(config.max_submissions_per_day ?? 20)}"><label class="checkbox"><input id="budget-day-unlimited" type="checkbox" ${Number(config.max_submissions_per_day) === 0 ? 'checked' : ''}>不限</label></div></div>
        <div class="field"><label for="budget-attempts">最多尝试轮数</label><input id="budget-attempts" type="number" min="1" max="500" value="${esc(config.max_attempts ?? 30)}"></div>
        <div class="field"><label for="budget-stall">停滞轮数停止</label><input id="budget-stall" type="number" min="1" max="100" value="${esc(config.max_stall_rounds ?? 5)}"></div>
        <div class="field"><label for="budget-interval">最小提交间隔（秒，≥60）</label><input id="budget-interval" type="number" min="60" max="86400" value="${esc(config.min_submit_interval_sec ?? 300)}"></div>
        <div class="field"><label for="budget-inflight">同账号在途上限</label><input id="budget-inflight" type="number" min="1" max="3" value="${esc(config.max_inflight ?? 1)}"></div>
        <div class="field"><label for="budget-poll">结果轮询间隔（秒）</label><input id="budget-poll" type="number" min="3" max="600" value="${esc(config.poll_interval_sec ?? 10)}"></div>
      </div>
      <div class="hint">“不限”等于 0，调度器会按无限上限处理；模型调用、Token 和运行时长仍由原预算控制。</div>
      <div class="alert error" id="budget-error" hidden></div>
      <div class="form-actions">
        <button class="btn" type="button" data-modal-close>取消</button>
        <button class="btn btn-primary" type="submit">保存上限</button>
      </div>
    </form>`);
  $('#budget-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const errorBox = $('#budget-error');
    errorBox.hidden = true;
    const button = event.target.querySelector('button[type="submit"]');
    button.disabled = true;
    try {
      await api(`/api/tasks/${encodeURIComponent(task.id)}/budget`, {
        method: 'POST',
        body: {
          max_submissions_per_task: $('#budget-task-unlimited').checked ? 0 : Number($('#budget-task-subs').value),
          max_submissions_per_day: $('#budget-day-unlimited').checked ? 0 : Number($('#budget-day-subs').value),
          max_attempts: Number($('#budget-attempts').value),
          max_stall_rounds: Number($('#budget-stall').value),
          min_submit_interval_sec: Number($('#budget-interval').value),
          max_inflight: Number($('#budget-inflight').value),
          poll_interval_sec: Number($('#budget-poll').value),
        },
        requestId: uuid(),
      });
      closeModal();
      toast('运行上限已更新', 'success');
      renderTask(task.id).catch(() => {});
    } catch (err) {
      errorBox.hidden = false;
      errorBox.textContent = err.message || '保存失败';
      button.disabled = false;
    }
  });
}

async function openMessageModal(task) {
  const messages = task.messages || [];
  const thread = messages.length
    ? messages.map((item) => `<div class="message-row">
        <div class="message-meta"><strong>${esc(item.author || '你')}</strong><span>${esc(fmtTime(item.created_at))}</span></div>
        <div class="message-body">${esc(item.content || '')}</div>
      </div>`).join('')
    : '<div class="empty">还没有留言。告诉 Agent 下一轮要关注什么。</div>';
  openModal('给 Agent 留言', `
    <div class="alert info">这是一条异步指令：保存后，下一次新建的 attempt 会读到它。Agent 不会即时聊天，而会在下一轮的 hypothesis / 实验记录里体现是否采纳。</div>
    <div class="message-thread">${thread}</div>
    <form id="message-form" class="stack" style="margin-top:14px">
      <div class="field"><label for="message-text">新的需求 / 观察 / 约束</label>
        <textarea id="message-text" rows="5" maxlength="4000" placeholder="例如：现在先不要动性能，优先确认 int16 的符号位；或者：下一轮试试 DataCopyPad 的尾部对齐。"></textarea>
        <div class="hint">最多 4000 字符。会作为 operator message 注入下一次 attempt 的上下文。</div>
      </div>
      <div class="alert error" id="message-error" hidden></div>
      <div class="form-actions">
        <button class="btn" type="button" data-modal-close>关闭</button>
        <button class="btn btn-primary" type="submit">发送给 Agent</button>
      </div>
    </form>`);
  $('#message-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const errorBox = $('#message-error');
    errorBox.hidden = true;
    const button = event.target.querySelector('button[type="submit"]');
    button.disabled = true;
    try {
      await api(`/api/tasks/${encodeURIComponent(task.id)}/messages`, {
        method: 'POST',
        body: { content: $('#message-text').value.trim() },
        requestId: uuid(),
      });
      closeModal();
      toast('留言已保存；下一次 attempt 会读取', 'success');
      renderTask(task.id).catch(() => {});
    } catch (err) {
      errorBox.hidden = false;
      errorBox.textContent = err.message || '发送失败';
      button.disabled = false;
    }
  });
}

async function openStrategyModal(task) {
  const current = String((task.config || {}).strategy_prompt || '');
  openModal('题目策略', `
    <form id="strategy-form" class="stack">
      <div class="alert info">通用优化方法由 <span class="mono">optimize-operator</span> skill 提供。这里只写本题的目标、约束和待验证假设，例如“先覆盖全部 dtype，再优化 UB 分块”。</div>
      <div class="field"><label for="strategy-text">长期目标 / 约束</label>
        <textarea id="strategy-text" rows="10" maxlength="8000" placeholder="例如：先保证 15/15 Pass；再优化 UB 分块与搬运重叠。">${esc(current)}</textarea>
        <div class="hint">这里只保存长期目标/约束，不会自动改写；系统会根据最新结果自动派生“当前策略”。保存后只影响下一次新建的 attempt。</div>
      </div>
      <div class="alert error" id="strategy-error" hidden></div>
      <div class="form-actions">
        <button class="btn" type="button" data-modal-close>取消</button>
        <button class="btn btn-primary" type="submit">保存策略</button>
      </div>
    </form>`);
  $('#strategy-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const errorBox = $('#strategy-error');
    errorBox.hidden = true;
    const button = event.target.querySelector('button[type="submit"]');
    button.disabled = true;
    try {
      await api(`/api/tasks/${encodeURIComponent(task.id)}/strategy`, {
        method: 'POST',
        body: { strategy_prompt: $('#strategy-text').value.trim() },
        requestId: uuid(),
      });
      closeModal();
      toast('题目策略已保存', 'success');
      renderTask(task.id).catch(() => {});
    } catch (err) {
      errorBox.hidden = false;
      errorBox.textContent = err.message || '保存失败';
      button.disabled = false;
    }
  });
}

function taskTabButton(key, label) {
  return `<button type="button" class="${state.taskTab === key ? 'active' : ''}" data-task-tab="${key}">${label}</button>`;
}

function renderTaskAlert(task, quota) {
  if (task.status === 'needs_login') {
    return `<div class="alert warn" style="margin-bottom:16px">CANNJudge 登录失效，新的模型调用/提交已暂停，已在途的结果仍会收集。请前往 <a href="#/platform">平台连接</a> 重新登录，然后恢复任务。${task.last_error ? `<div class="faint small" style="margin-top:6px">${esc(task.last_error)}</div>` : ''}</div>`;
  }
  if (task.status === 'needs_review') {
    return `<div class="alert warn" style="margin-bottom:16px">任务需要人工审查：${esc(task.last_error || '存在未知提交或模型配置问题')}。处理完成后点击“恢复”。</div>`;
  }
  if (quota.unknown && !['completed', 'failed', 'stopped'].includes(task.status)) {
    return `<div class="alert error" style="margin-bottom:16px">账号存在 ${esc(quota.unknown)} 个结果未知的提交，所有新提交已冻结。请在“提交与反馈”中先对账或人工确认。</div>`;
  }
  if (task.last_error && task.status === 'running') {
    return `<div class="alert info" style="margin-bottom:16px">最近错误：${esc(task.last_error_code || '')} ${esc(task.last_error)}</div>`;
  }
  return '';
}

function budgetBar(label, value, max, note = '') {
  const pct = max ? Math.min(100, Math.max(0, (Number(value) / Number(max)) * 100)) : 0;
  return `<div style="margin-bottom:12px">
    <div class="row-between small"><span class="muted">${esc(label)}</span><span class="mono">${esc(value)} / ${esc(max ?? '—')}</span></div>
    <div class="budget-track">
      <div class="budget-fill" style="width:${pct}%"></div>
    </div>
    ${note ? `<div class="faint" style="font-size:11px;margin-top:3px">${esc(note)}</div>` : ''}
  </div>`;
}

function renderBestValue(best) {
  if (!best) return '<span class="faint">—</span>';
  if (best.metric && typeof best.metric === 'object') {
    return `${esc(best.metric.metric)} = ${esc(best.metric.value)}${best.metric.unit ? ' ' + esc(best.metric.unit) : ''}`;
  }
  return esc(best.raw_status || '通过');
}

function renderTaskTab(task, events, config, usage) {
  if (state.taskTab === 'attempts') {
    if (!(task.attempts || []).length) return '<div class="empty">还没有尝试。启动任务后 Agent 会创建第一份快照。</div>';
    return `<div class="table-wrap"><table>
      <thead><tr><th>#</th><th>状态</th><th>结果</th><th>用例</th><th>指标</th><th>假设</th><th>父版本</th><th></th></tr></thead>
      <tbody>${task.attempts.map((a) => {
        const n = a.normalized_json || {};
        return `<tr class="clickable record-row" data-attempt-open="${esc(a.id)}" title="点击查看详情">
          <td class="mono">${esc(a.ordinal)}</td>
          <td>${attemptBadge(a.status)}</td>
          <td>${a.normalized_json ? outcomeBadge(n.outcome) : '<span class="faint">—</span>'}</td>
          <td class="mono">${n.testcase_total ? `${esc(n.testcase_passed)}/${esc(n.testcase_total)}` : '—'}</td>
          <td class="mono">${n.score !== undefined && n.score !== null ? 'score=' + esc(n.score) : n.precision_ratio !== undefined && n.precision_ratio !== null ? 'prec=' + esc(n.precision_ratio) : '—'}</td>
          <td class="record-cell" title="${esc(a.hypothesis || '')}">${esc(a.hypothesis || '')}</td>
          <td class="mono faint">${esc(a.parent_attempt_id || '—')}</td>
          <td class="faint">›</td>
        </tr>`;
      }).join('')}</tbody></table></div>`;
  }
  if (state.taskTab === 'submissions') {
    const submissions = task.submissions || [];
    if (!submissions.length) return '<div class="empty">还没有远端提交。</div>';
    return `<div class="table-wrap"><table>
      <thead><tr><th>提交</th><th>状态</th><th>远端 ID</th><th>发送时间</th><th>结果</th><th>错误</th><th></th></tr></thead>
      <tbody>${submissions.map((s) => {
        const attempt = (task.attempts || []).find((a) => a.id === s.attempt_id) || {};
        const n = attempt.normalized_json || {};
        const open = attempt.id ? ` data-attempt-open="${esc(attempt.id)}"` : '';
        return `<tr class="clickable record-row"${open} title="点击查看详情">
          <td class="mono small">${esc(s.id)}</td>
          <td>${badge(SUBMISSION_STATUS[s.state] || s.state, s.state === 'finished' ? 'completed' : s.state === 'unknown' ? 'unknown' : s.state === 'failed' ? 'failed' : 'running')}</td>
          <td class="mono small">${esc(s.remote_id || '—')}</td>
          <td class="small">${esc(fmtTime(s.sent_at))}</td>
          <td>${n.outcome ? outcomeBadge(n.outcome) : '<span class="faint">—</span>'}</td>
          <td class="record-cell" title="${esc(s.error_message || '')}">${esc(s.error_message || '')}</td>
          <td>${['unknown', 'submitting'].includes(s.state) ? `<button class="btn btn-sm btn-primary" type="button" data-resolve="${esc(s.id)}">对账/确认</button>` : '<span class="faint">›</span>'}</td>
        </tr>`;
      }).join('')}</tbody></table></div>`;
  }
  if (state.taskTab === 'config') {
    return `<pre class="card config-json">${esc(jsonPretty({ config, problem_id: task.problem_id, problem_url: task.problem_url, best_metric: task.best_metric, local_best: task.local_best, usage }))}</pre>`;
  }
  return `<div class="grid grid-2">
    <div>
      <h3>题目合同</h3>
      <pre class="small" style="max-height:260px;overflow:auto">${esc(jsonPretty(task.problem || {}))}</pre>
      <h3 style="margin-top:16px">模板文件</h3>
      <div class="table-wrap"><table><thead><tr><th>路径</th><th>可编辑</th><th>大小</th></tr></thead><tbody>
        ${(task.template?.files || []).map((f) => `<tr><td class="mono">${esc(f.path)}</td><td>${f.editable ? '是' : '只读'}</td><td>${esc(fmtBytes(f.bytes))}</td></tr>`).join('') || '<tr><td colspan="3" class="faint">无模板文件</td></tr>'}
      </tbody></table></div>
    </div>
    <div>
      <h3>事件</h3>
      <div class="log">${events.length ? events.map(renderEvent).join('') : '<div class="empty">暂无事件</div>'}</div>
    </div>
  </div>`;
}

function renderEvent(event) {
  return `<div class="log-item">
    <span class="log-time">${esc(fmtTime(event.created_at))}</span>
    <span class="log-type">${esc(event.type)}</span>
    <span class="log-payload">${esc(JSON.stringify(event.payload || {}).slice(0, 800))}</span>
  </div>`;
}

async function baselineSubmit(taskId) {
  const ok = confirm(
    '这会用题目模板的可编辑文件创建一份快照，并通过调度器向 CANNJudge 真实提交一次（消耗一次提交额度，不需要模型）。\n\n确认提交基线？'
  );
  if (!ok) return;
  try {
    await api(`/api/tasks/${encodeURIComponent(taskId)}/baseline-submit`, {
      method: 'POST',
      body: { confirm: true },
    });
    toast('人工基线已排队；worker 将在限频允许时发送', 'success');
    await renderTask(taskId);
  } catch (err) {
    toast(err.message || '基线提交失败', 'error');
    if (!err.handled) await renderTask(taskId).catch(() => {});
  }
}

async function continueOptimization(taskId) {
  const ok = confirm('将保留已有快照和远端结果，从当前任务继续 Agent 自动优化。需要模型已配置。确认继续？');
  if (!ok) return;
  try {
    await api(`/api/tasks/${encodeURIComponent(taskId)}/continue-optimization`, {
      method: 'POST',
      body: {},
    });
    toast('已恢复任务，Agent 将基于已有反馈创建新版本', 'success');
    await renderTask(taskId);
  } catch (err) {
    toast(err.message || '恢复失败', 'error');
    if (!err.handled) await renderTask(taskId).catch(() => {});
  }
}

async function taskAction(taskId, action) {
  if (action === 'stop' && !confirm('停止会取消尚未发送的排队提交；已经提交到 CANNJudge、等待或已有结果的提交不能撤回。确认停止？')) return;
  try {
    await api(`/api/tasks/${encodeURIComponent(taskId)}/${action}`, { method: 'POST', body: {} });
    toast(`已${action === 'start' ? '开始' : action === 'pause' ? '暂停' : action === 'resume' ? '恢复' : '停止'}任务`, 'success');
    await renderTask(taskId);
  } catch (err) {
    toast(err.message || '操作失败', 'error');
    if (!err.handled) await renderTask(taskId).catch(() => {});
  }
}

function formatCost(value) {
  if (value === null || value === undefined) return '未知';
  return Number(value).toFixed(6);
}

/* ------------------------------------------------------------------ */
/* Attempt modal                                                       */
/* ------------------------------------------------------------------ */
async function openAttempt(taskId, attemptId) {
  state.attemptTab = 'source';
  try {
    const data = await api(`/api/tasks/${encodeURIComponent(taskId)}/attempts/${encodeURIComponent(attemptId)}`);
    state.attemptData = data;
    renderAttemptModal();
  } catch (err) {
    toast(err.message || '读取尝试失败', 'error');
  }
}

function renderAttemptModal() {
  const data = state.attemptData;
  if (!data) return;
  const attempt = data.attempt;
  const normalized = attempt.normalized_json || {};
  const source = data.source || [];
  const selected = state.attemptFile && source.find((f) => f.path === state.attemptFile) ? state.attemptFile : (source[0]?.path || '');
  state.attemptFile = selected;
  const file = source.find((f) => f.path === selected);
  openModal(`Attempt #${attempt.ordinal} · ${ATTEMPT_STATUS[attempt.status] || attempt.status}`, `
    <div class="row-between" style="margin-bottom:12px">
      <div>${attemptBadge(attempt.status)} ${normalized.outcome ? outcomeBadge(normalized.outcome) : ''}</div>
      <div class="row">
        <a class="btn btn-sm" href="/api/tasks/${encodeURIComponent(attempt.task_id)}/attempts/${encodeURIComponent(attempt.id)}/download">下载源码</a>
      </div>
    </div>
    <div class="grid grid-2" style="margin-bottom:12px">
      <div class="metric"><div class="metric-label">假设</div><div class="small" style="margin-top:4px">${esc(attempt.hypothesis || '—')}</div></div>
      <div class="metric"><div class="metric-label">用例 / 指标</div><div class="small mono" style="margin-top:4px">${esc(normalized.testcase_passed ?? '—')}/${esc(normalized.testcase_total ?? '—')} · score ${esc(normalized.score ?? '—')} · precision ${esc(normalized.precision_ratio ?? '—')}</div></div>
    </div>
    <div class="tabs" id="attempt-tabs">
      ${attemptTab('source', '源码')}
      ${attemptTab('testcases', '测试点')}
      ${attemptTab('prompt', '提示词')}
      ${attemptTab('result', '远端结果')}
      ${attemptTab('diff-parent', '对比父版本')}
      ${attemptTab('diff-best', '对比 best')}
      ${attemptTab('local', '本地校验')}
    </div>
    <div id="attempt-body">${renderAttemptBody(data, file, selected)}</div>
  `);
  $$('#attempt-tabs [data-attempt-tab]').forEach((button) => {
    button.addEventListener('click', () => { state.attemptTab = button.dataset.attemptTab; renderAttemptModal(); });
  });
  const select = $('#attempt-file-select');
  if (select) select.addEventListener('change', () => { state.attemptFile = select.value; renderAttemptModal(); });
}

function attemptTab(key, label) {
  return `<button type="button" class="${state.attemptTab === key ? 'active' : ''}" data-attempt-tab="${key}">${label}</button>`;
}

function renderAttemptBody(data, file, selected) {
  if (state.attemptTab === 'testcases') {
    const normalized = data.attempt.normalized_json || {};
    const cases = normalized.testcases || [];
    if (!cases.length) return '<div class="empty">该 attempt 还没有逐测试点结果。</div>';
    return `<div class="case-summary">
      <div><span class="task-k">结果</span><strong>${esc(normalized.raw_status || normalized.outcome || '—')}</strong></div>
      <div><span class="task-k">通过</span><strong>${esc(normalized.testcase_passed ?? '—')} / ${esc(normalized.testcase_total ?? '—')}</strong></div>
      <div><span class="task-k">聚合 precision</span><strong class="mono">${esc(normalized.precision_ratio ?? '—')}</strong></div>
    </div>
    <div class="case-list">
      ${cases.map((item) => {
        const p = Number(item.precision_ratio);
        const width = Number.isFinite(p) ? Math.max(0, Math.min(100, p * 100)) : 0;
        const raw = item.raw || {};
        return `<div class="case-row">
          <div class="case-index mono">#${esc((item.index ?? 0) + 1)}</div>
          <div class="case-main">
            <div class="row-between"><span>${item.passed ? badge('Pass', 'pass') : badge(item.status || '失败', 'fail')}</span><span class="mono small">${Number.isFinite(p) ? (p * 100).toFixed(2) + '%' : '—'}</span></div>
            <div class="case-bar"><i style="width:${width}%"></i></div>
            <div class="case-meta"><span>time <b class="mono">${esc(item.time ?? '—')}</b></span><span>best <b class="mono">${esc(raw.best_time ?? '—')}</b></span>${item.message ? `<span class="wrap">${esc(item.message)}</span>` : ''}</div>
          </div>
        </div>`;
      }).join('')}
    </div>`;
  }
  if (state.attemptTab === 'prompt') {
    const messages = data.messages || [];
    const renderMessage = (item) => {
      const label = `${item.role}${item.name ? ` · ${item.name}` : ''}${item.tool_call_id ? ` · ${item.tool_call_id}` : ''}`;
      const body = item.tool_calls
        ? jsonPretty(item.tool_calls)
        : (item.content || '');
      return `<details class="prompt-block">
        <summary><span class="prompt-role prompt-role-${esc(item.role)}">${esc(item.role)}</span> <span class="faint mono">#${esc(item.seq)} ${esc(label)}</span></summary>
        <pre>${esc(body)}</pre>
      </details>`;
    };
    return `<div class="alert info">${esc(data.prompt_note || '')}</div>
      <details class="prompt-block prompt-system" open>
        <summary><span class="prompt-role">system</span> <span class="faint">system prompt + skills</span></summary>
        <pre>${esc(data.system_prompt || '(empty)')}</pre>
      </details>
      <details class="prompt-block">
        <summary><span class="prompt-role">tools</span> <span class="faint">native tool schemas</span></summary>
        <pre>${esc(jsonPretty(data.tools || []))}</pre>
      </details>
      <div class="prompt-transcript">
        ${messages.length ? messages.map(renderMessage).join('') : '<div class="empty">该 attempt 还没有模型消息。</div>'}
      </div>`;
  }
  if (state.attemptTab === 'result') {
    return `<div class="code-shell"><div class="code-head"><span class="path">normalized + raw</span></div><div class="code-body"><pre>${esc(jsonPretty({ normalized: data.attempt.normalized_json, raw: data.submission?.response_json || data.attempt.result_json || null }))}</pre></div></div>`;
  }
  if (state.attemptTab === 'diff-parent') {
    return `<div class="code-shell"><div class="code-head"><span class="path">diff vs parent</span></div><div class="code-body"><pre class="diff">${renderDiff(data.parent_diff)}</pre></div></div>`;
  }
  if (state.attemptTab === 'diff-best') {
    return `<div class="code-shell"><div class="code-head"><span class="path">diff vs best</span></div><div class="code-body"><pre class="diff">${renderDiff(data.best_diff)}</pre></div></div>`;
  }
  if (state.attemptTab === 'local') {
    return `<div class="code-shell"><div class="code-head"><span class="path">local validation</span></div><div class="code-body"><pre>${esc(jsonPretty(data.attempt.local_validation_json || { status: 'not run' }))}</pre></div></div>`;
  }
  if (!(data.source || []).length) return '<div class="empty">该 attempt 没有源码快照。</div>';
  return `
    <div class="row" style="margin-bottom:10px">
      <select id="attempt-file-select" style="max-width:520px">
        ${data.source.map((f) => `<option value="${esc(f.path)}" ${f.path === selected ? 'selected' : ''}>${esc(f.path)} · ${esc(fmtBytes(f.bytes))}</option>`).join('')}
      </select>
      <span class="faint small mono">${esc(file?.sha256 || '')}</span>
    </div>
    <div class="code-shell"><div class="code-head"><span class="path">${esc(file?.path || '')}</span></div><div class="code-body"><pre>${esc(file?.content || '')}</pre></div></div>`;
}

function renderDiff(text) {
  if (!text) return '<span class="faint">无差异</span>';
  return text.split('\n').map((line) => {
    const cls = line.startsWith('+') && !line.startsWith('+++') ? 'add' : line.startsWith('-') && !line.startsWith('---') ? 'del' : line.startsWith('@@') ? 'hunk' : '';
    return `<span class="${cls}">${esc(line)}</span>`;
  }).join('\n');
}

/* ------------------------------------------------------------------ */
/* Resolve unknown submission                                          */
/* ------------------------------------------------------------------ */
function openResolveModal(taskId, submissionId) {
  openModal('结果未知提交 · 对账', `
    <div class="alert warn">系统不会自动重发。请先在 CANNJudge 网页确认该次提交是否已受理，再选择处理方式。</div>
    <div class="stack" style="margin-top:14px">
      <div class="field"><label>远端 submission id（若已受理）</label><input id="resolve-remote" type="text" placeholder="例如 6aa27abe2d3dd2c5ae243b01"></div>
      <div class="field"><label>备注</label><input id="resolve-note" type="text" placeholder="证据/说明，可选"></div>
      <div class="row">
        <button class="btn btn-primary" id="resolve-adopt" type="button">采用远端 ID 并对账</button>
        <button class="btn btn-danger" id="resolve-fail" type="button">确认远端未受理，标记失败</button>
      </div>
      <div class="faint small">采用后会查询远端结果；如果查询仍在运行，任务会继续轮询。标记失败不会重发同一快照。</div>
    </div>`);
  const submit = async (action) => {
    try {
      await api(`/api/tasks/${encodeURIComponent(taskId)}/submissions/${encodeURIComponent(submissionId)}/resolve`, {
        method: 'POST',
        body: { action, remote_id: $('#resolve-remote').value.trim(), note: $('#resolve-note').value.trim() },
      });
      closeModal();
      toast('已处理未知提交', 'success');
      renderTask(taskId);
    } catch (err) { toast(err.message || '处理失败', 'error'); }
  };
  $('#resolve-adopt').addEventListener('click', () => submit('adopt'));
  $('#resolve-fail').addEventListener('click', () => {
    if (confirm('确认该提交未被远端受理？此操作不会重发源码。')) submit('mark_failed');
  });
}

/* ------------------------------------------------------------------ */
/* Settings                                                            */
/* ------------------------------------------------------------------ */
async function renderSettings() {
  setPoll(null);
  const data = await api('/api/settings/model');
  const model = data.model || {};
  state.model = model;
  state.capabilities = data.capabilities;
  state.skills = data.skills || [];
  const caps = data.capabilities || {};
  $('#view').innerHTML = `
    <div class="row-between" style="margin-bottom:18px">
      <div><h1>模型与设置</h1><div class="muted">模型 API 只负责推理；工具由服务器校验并执行。</div></div>
    </div>

    <div class="card">
      <div class="card-head"><div><h2>模型 Provider</h2><div class="card-sub">兼容 OpenAI Chat Completions；密钥加密保存在服务器，浏览器只看到掩码。</div></div>
        ${model.configured ? badge('已配置', 'completed') : badge('未配置', 'pending')}</div>
      <form id="model-form" class="stack">
        <div class="form-grid">
          <div class="field span-2"><label for="m-base">API base URL</label><input id="m-base" type="url" value="${esc(model.base_url || '')}" placeholder="https://your-relay.example.com/v1" required>
            <div class="hint">可填到 /v1 或完整 /chat/completions；服务端不会猜测其他协议。</div></div>
          <div class="field"><label for="m-model">模型名称</label><input id="m-model" type="text" value="${esc(model.model || '')}" placeholder="deepseek-chat" required></div>
          <div class="field"><label for="m-key">API Key</label><input id="m-key" type="password" autocomplete="off" placeholder="${model.configured ? '留空保持现有密钥（' + esc(model.api_key_masked || '') + '）' : '粘贴 API Key'}">
            <div class="hint">不写入浏览器存储，不进入提示词或实验档案。</div></div>
          <div class="field"><label for="m-maxtok">max_tokens</label><input id="m-maxtok" type="number" min="16" max="32768" value="${esc(model.max_tokens || 2048)}"></div>
          <div class="field"><label for="m-temp">temperature</label><input id="m-temp" type="number" min="0" max="2" step="0.05" value="${esc(model.temperature ?? 0.2)}"></div>
          <div class="field"><label for="m-timeout">请求超时（秒）</label><input id="m-timeout" type="number" min="10" max="600" value="${esc(model.timeout || 120)}"></div>
          <div class="field"><label for="m-reasoning">推理模式</label><select id="m-reasoning">
            <option value="auto" ${model.reasoning_mode === 'disabled' ? '' : 'selected'}>自动</option>
            <option value="disabled" ${model.reasoning_mode === 'disabled' ? 'selected' : ''}>禁用长推理（推荐给工具 Agent）</option>
          </select><div class="hint">部分 deepseek-flash/Qwen 网关会先进行长推理；禁用后直接输出工具调用。</div></div>
          <div class="field"><label for="m-price-p">输入单价 / 1K tokens（可选）</label><input id="m-price-p" type="number" min="0" step="0.000001" value="${esc(model.price_prompt_per_1k ?? '')}"></div>
          <div class="field"><label for="m-price-c">输出单价 / 1K tokens（可选）</label><input id="m-price-c" type="number" min="0" step="0.000001" value="${esc(model.price_completion_per_1k ?? '')}"></div>
          <div class="field span-2"><label class="checkbox"><input id="m-tools" type="checkbox" ${model.supports_tool_calls ? 'checked' : ''}> 该模型/网关支持原生 tool calling（不支持时使用 JSON 动作协议）</label></div>
        </div>
        <div class="alert error" id="model-error" hidden></div>
        <div class="form-actions">
          ${model.configured ? '<button class="btn btn-danger" id="model-delete" type="button">删除配置</button>' : ''}
          <button class="btn" id="model-test" type="button">测试连接</button>
          <button class="btn btn-primary" type="submit">保存配置</button>
        </div>
      </form>
    </div>

    <div class="grid grid-2" style="margin-top:16px">
      <div class="card">
        <div class="card-head"><h3>服务器能力</h3><span class="faint small">检测时间 ${esc(fmtTime(caps.checked_at))}</span></div>
        <div class="stack small">
          <div class="row-between"><span class="muted">NPU</span><span>${caps.npu?.available ? badge('可用', 'completed') : badge('不可用', 'pending')} <span class="faint">${esc(caps.npu?.evidence || '')}</span></span></div>
          <div class="row-between"><span class="muted">CANN</span><span>${caps.cann?.available ? badge('可用', 'completed') : badge('不可用', 'pending')}</span></div>
          <div class="row-between"><span class="muted">Docker</span><span>${caps.docker?.available ? badge('可用', 'completed') : badge('不可用', 'pending')} ${caps.docker?.image_present ? '<span class="faint">image ready</span>' : ''}</span></div>
          <div class="row-between"><span class="muted">隔离执行器</span><span class="mono">${esc(caps.executor?.selected || '—')} ${caps.executor?.network_enforced ? '（禁网）' : '（网络未强制隔离）'}</span></div>
          <div class="row-between"><span class="muted">候选本地执行</span><span>${caps.candidate_execution?.status === 'available' ? badge('可用', 'completed') : badge('不可用', 'pending')}</span></div>
          <div class="row-between"><span class="muted">远端自定义测试</span><span class="mono">${caps.remote_custom_tests?.supported ? '已核实支持' : '未核实/禁用'}</span></div>
        </div>
        ${caps.executor?.warning ? `<div class="alert warn small" style="margin-top:12px">${esc(caps.executor.warning)}</div>` : ''}
      </div>
      <div class="card">
        <div class="card-head"><h3>已加载 Skills</h3><span class="faint small">显式注入 Agent 提示词</span></div>
        ${(state.skills || []).length ? `<div class="stack small">${state.skills.map((s) => `<div><div class="mono">${esc(s.name)}</div><div class="faint small">${esc(s.description)}</div><div class="faint mono" style="font-size:10.5px">${esc(s.sha256)}</div></div>`).join('')}</div>` : '<div class="alert warn">skills 目录缺少 SKILL.md，Agent 会拒绝运行并标记需要审查。</div>'}
      </div>
    </div>

    <div class="card" style="margin-top:16px">
      <h3>修改登录密码</h3>
      <form id="password-form" class="form-grid" style="margin-top:10px">
        <div class="field"><label for="pw-current">当前密码</label><input id="pw-current" type="password" autocomplete="current-password"></div>
        <div class="field"><label for="pw-new">新密码（≥10 位）</label><input id="pw-new" type="password" minlength="10" autocomplete="new-password"></div>
        <div class="form-actions" style="grid-column:span 2"><button class="btn" type="submit">更新密码</button></div>
      </form>
    </div>`;

  $('#model-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const errorBox = $('#model-error');
    errorBox.hidden = true;
    try {
      await api('/api/settings/model', {
        method: 'PUT',
        body: {
          base_url: $('#m-base').value.trim(),
          model: $('#m-model').value.trim(),
          api_key: $('#m-key').value,
          supports_tool_calls: $('#m-tools').checked,
          max_tokens: Number($('#m-maxtok').value),
          temperature: Number($('#m-temp').value),
          timeout: Number($('#m-timeout').value),
          reasoning_mode: $('#m-reasoning').value,
          price_prompt_per_1k: $('#m-price-p').value === '' ? null : Number($('#m-price-p').value),
          price_completion_per_1k: $('#m-price-c').value === '' ? null : Number($('#m-price-c').value),
        },
      });
      toast('模型配置已保存', 'success');
      await renderSettings();
    } catch (err) {
      errorBox.hidden = false; errorBox.className = 'alert error'; errorBox.textContent = err.message;
    }
  });
  $('#model-test').addEventListener('click', async (event) => {
    const button = event.target;
    button.disabled = true; button.textContent = '测试中…';
    try {
      const result = await api('/api/settings/model/test', { method: 'POST', body: {}, idem: false });
      toast(`连接成功 · ${result.model} · ${result.latency_ms}ms`, 'success');
    } catch (err) { toast(err.message || '连接失败', 'error'); }
    button.disabled = false; button.textContent = '测试连接';
  });
  const del = $('#model-delete');
  if (del) del.addEventListener('click', async () => {
    if (!confirm('删除模型配置？')) return;
    await api('/api/settings/model', { method: 'DELETE' });
    toast('已删除', 'success');
    renderSettings();
  });
  $('#password-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    try {
      await api('/api/auth/password', { method: 'POST', body: { current_password: $('#pw-current').value, new_password: $('#pw-new').value } });
      toast('密码已更新', 'success');
      $('#pw-current').value = ''; $('#pw-new').value = '';
    } catch (err) { toast(err.message || '更新失败', 'error'); }
  });
}

/* ------------------------------------------------------------------ */
/* Platform                                                            */
/* ------------------------------------------------------------------ */
async function renderPlatform() {
  setPoll(null);
  const data = await api('/api/platform/status');
  const platform = data.platform || {};
  state.platform = platform;
  const caps = platform.capabilities || {};
  $('#view').innerHTML = `
    <div class="row-between" style="margin-bottom:18px">
      <div><h1>平台连接</h1><div class="muted">账号密码只在本次登录请求中经服务器转发，不保存密码；会话 Cookie 加密保存。</div></div>
      ${platform.connected ? badge('已连接', 'completed') : badge('未连接', 'pending')}
    </div>

    ${platform.last_error ? `<div class="alert error" style="margin-bottom:16px">${esc(platform.last_error.message || JSON.stringify(platform.last_error))}</div>` : ''}

    <div class="grid grid-2">
      <div class="card">
        <div class="card-head"><h2>账号登录</h2><span class="faint small">${esc(platform.account_masked || '')}</span></div>
        <form id="platform-form" class="stack">
          <div class="field"><label for="p-account">CANNJudge 账号（邮箱或手机号）</label><input id="p-account" type="text" autocomplete="username" required></div>
          <div class="field"><label for="p-password">密码</label><input id="p-password" type="password" autocomplete="current-password" required>
            <div class="hint">密码不会写入数据库或日志；登录成功只保存会话 Cookie 与用户 ID。</div></div>
          <div class="field"><label for="p-problem">用于验证 judge 访问的题目链接</label><input id="p-problem" type="url" value="https://cannjudge.cn/hc_cann_codelabs/cann/add">
            <div class="hint">登录后会解析该题并读取模板；403/401 会明确返回“账号无法连接 judge”。</div></div>
          <div class="field">
            <label>图形验证码（仅平台要求时）</label>
            <div class="row">
              <input id="p-captcha-code" type="text" placeholder="验证码" style="max-width:160px">
              <button class="btn btn-sm" id="load-captcha" type="button">获取验证码</button>
            </div>
            <input id="p-captcha-id" type="hidden">
            <div id="captcha-box" class="mt-8"></div>
          </div>
          <div class="alert error" id="platform-error" hidden></div>
          <div class="form-actions">
            <button class="btn btn-primary" type="submit">登录并验证</button>
          </div>
        </form>
      </div>
      <div class="card">
        <div class="card-head"><h2>连接状态</h2><span class="faint small">${esc(platform.base_url || '')}</span></div>
        <div class="stack small">
          <div class="row-between"><span class="muted">账号</span><span class="mono">${esc(platform.account_masked || '—')}</span></div>
          <div class="row-between"><span class="muted">用户 ID</span><span class="mono">${esc(platform.user_id || '—')}</span></div>
          <div class="row-between"><span class="muted">Judge 题目访问</span><span>${platform.judge_verified ? badge('已验证', 'completed') : badge('未验证', 'pending')}</span></div>
          <div class="row-between"><span class="muted">实时线上验证</span><span>${badge('尚未用真实账号提交', 'pending')}</span></div>
          <div class="row-between"><span class="muted">自定义测试数据</span><span>${badge('未核实，已禁用', 'pending')}</span></div>
        </div>
        <div class="toolbar" style="margin-top:14px">
          <button class="btn" id="platform-test" type="button">测试当前连接</button>
          ${platform.connected ? '<button class="btn btn-danger" id="platform-disconnect" type="button">断开并清除会话</button>' : ''}
        </div>
        <div id="evidence-box" class="mt-12"></div>
        <div class="alert info small" style="margin-top:14px">
          接口依据公开的 CANNJudge 接入资料实现，尚未在本部署完成真实提交验证。能力表中标注“documented”不等于线上验证通过。
        </div>
      </div>
    </div>

    <div class="card" style="margin-top:16px">
      <div class="card-head"><h2>能力矩阵</h2></div>
      <div class="table-wrap"><table><thead><tr><th>能力</th><th>状态</th><th>说明</th></tr></thead><tbody>
        ${Object.entries(caps).filter(([k]) => !['evidence', 'notes', 'platform', 'base_url'].includes(k)).map(([key, value]) => `<tr><td class="mono">${esc(key)}</td><td>${esc(typeof value === 'object' ? (value.status || JSON.stringify(value)) : String(value))}</td><td class="small muted">${esc(JSON.stringify(value).slice(0, 300))}</td></tr>`).join('')}
      </tbody></table></div>
    </div>

    <div class="card" style="margin-top:16px">
      <h3>高级：导入浏览器会话</h3>
      <p class="faint small">仅在你已掌握 CANNJudge 用户 ID 与 Cookie 时使用。Cookie 按凭据加密保存，不在页面回显。</p>
      <form id="import-form" class="stack">
        <div class="field"><label for="i-user">CANNJudge 用户 ID</label><input id="i-user" type="text" placeholder="登录响应中的 _id"></div>
        <div class="field"><label for="i-cookies">Cookie 头</label><textarea id="i-cookies" placeholder="name=value; name2=value2"></textarea></div>
        <div class="form-actions"><button class="btn" type="submit">导入会话</button></div>
      </form>
    </div>`;

  $('#platform-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const box = $('#platform-error'); box.hidden = true;
    const button = event.target.querySelector('button[type="submit"]');
    button.disabled = true; button.textContent = '连接中…';
    try {
      await api('/api/platform/login', {
        method: 'POST',
        body: {
          account: $('#p-account').value.trim(),
          password: $('#p-password').value,
          problem_url: $('#p-problem').value.trim(),
          captcha_id: $('#p-captcha-id').value,
          captcha_code: $('#p-captcha-code').value.trim(),
        },
        idem: false,
      });
      $('#p-password').value = '';
      toast('CANNJudge 已连接并验证题目访问', 'success');
      renderPlatform();
    } catch (err) {
      box.hidden = false; box.className = 'alert error'; box.textContent = err.message;
      button.disabled = false; button.textContent = '登录并验证';
    }
  });
  $('#load-captcha').addEventListener('click', async (event) => {
    const box = $('#captcha-box');
    box.innerHTML = '<span class="faint small">加载中…</span>';
    try {
      const data = await api('/api/platform/captcha');
      $('#p-captcha-id').value = data.captcha_id;
      box.innerHTML = `<img alt="验证码" style="max-width:220px;border-radius:8px;border:1px solid var(--border);background:#fff" src="${data.image_data_url}"><div class="faint small">由平台返回，仅本次登录使用。</div>`;
    } catch (err) { box.innerHTML = `<div class="alert error small">${esc(err.message)}</div>`; }
  });
  $('#platform-test').addEventListener('click', async () => {
    const box = $('#evidence-box');
    box.innerHTML = '<span class="faint small">检查中…</span>';
    try {
      const data = await api('/api/platform/test', { method: 'POST', body: { problem_url: $('#p-problem').value.trim() }, idem: false });
      box.innerHTML = `<pre class="small" style="max-height:280px;overflow:auto">${esc(jsonPretty(data.evidence))}</pre>`;
    } catch (err) { box.innerHTML = `<div class="alert error small">${esc(err.message)}</div>`; }
  });
  const disconnect = $('#platform-disconnect');
  if (disconnect) disconnect.addEventListener('click', async () => {
    if (!confirm('清除服务器保存的 CANNJudge 会话？运行中的任务会暂停提交。')) return;
    await api('/api/platform/disconnect', { method: 'POST', body: {} });
    toast('已断开', 'success');
    renderPlatform();
  });
  $('#import-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    try {
      await api('/api/platform/import-session', {
        method: 'POST',
        body: { user_id: $('#i-user').value.trim(), cookies: $('#i-cookies').value.trim() },
        idem: false,
      });
      toast('会话已导入', 'success');
      renderPlatform();
    } catch (err) { toast(err.message || '导入失败', 'error'); }
  });
}

/* ------------------------------------------------------------------ */
/* Backups                                                             */
/* ------------------------------------------------------------------ */
async function renderBackups() {
  setPoll(null);
  const data = await api('/api/backups');
  const backups = data.backups || [];
  $('#view').innerHTML = `
    <div class="row-between" style="margin-bottom:18px">
      <div><h1>服务器备份</h1><div class="muted">数据库 + 实验文件的服务器端备份；任务档案不等于灾备。</div></div>
      <button class="btn btn-primary" id="create-backup" type="button">创建服务器备份</button>
    </div>
    <div class="alert warn" style="margin-bottom:16px">
      默认不包含主密钥。没有 <span class="mono">secrets/master.key</span>，备份中的模型密钥和 CANNJudge 会话无法解密。包含密钥的备份等同于全部凭据明文，请离线保护。
    </div>
    <div class="card">
      <div class="card-head"><h2>备份列表</h2><span class="faint small">${esc(backups.length)} 个</span></div>
      ${backups.length ? `<div class="table-wrap"><table><thead><tr><th>名称</th><th>创建时间</th><th>文件</th><th>大小</th><th>主密钥</th><th></th></tr></thead><tbody>
        ${backups.map((b) => `<tr><td class="mono">${esc(b.name)}</td><td>${esc(fmtTime(b.created_at))}</td><td>${esc(b.files)}</td><td>${esc(fmtBytes(b.bytes))}</td><td>${b.master_key_included ? badge('包含', 'failed') : badge('不包含', 'completed')}</td><td><a class="btn btn-sm" href="/api/backups/${encodeURIComponent(b.name)}">下载 ZIP</a></td></tr>`).join('')}
      </tbody></table></div>` : '<div class="empty">还没有备份。建议在首次真实提交前创建一次。</div>'}
    </div>`;
  $('#create-backup').addEventListener('click', async (event) => {
    if (!confirm('创建服务器备份？包含数据库和实验文件，但不包含主密钥。')) return;
    const button = event.target; button.disabled = true; button.textContent = '创建中…';
    try {
      const result = await api('/api/backups', { method: 'POST', body: {} });
      toast(`备份完成：${result.backup.files} 个文件`, 'success');
      renderBackups();
    } catch (err) { toast(err.message || '备份失败', 'error'); button.disabled = false; button.textContent = '创建服务器备份'; }
  });
}

/* ------------------------------------------------------------------ */
/* Globals                                                             */
/* ------------------------------------------------------------------ */
$('#logout-btn').addEventListener('click', async () => {
  try { await api('/api/auth/logout', { method: 'POST', body: {} }); } catch (_) { /* ignore */ }
  state.me = null; state.csrf = null;
  clearInterval(state.workerTimer); clearInterval(state.timer);
  renderLogin();
});

window.addEventListener('beforeunload', () => { clearInterval(state.timer); clearInterval(state.workerTimer); });

boot();
