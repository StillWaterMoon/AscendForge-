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
  remote: null,
  route: '',
  taskId: null,
  taskTab: 'attempts',
  lockedToastTask: null,
  summary: null,
  platform: null,
  model: null,
  capabilities: null,
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

function remoteFormValue(id) {
  const el = document.getElementById(id);
  return el ? el.value.trim() : '';
}

function setServerField(id, value) {
  const el = document.getElementById(id);
  if (el) el.value = value ?? '';
}

function applyServerConfig(c) {
  setServerField('server-host', c.host);
  setServerField('server-port', c.port);
  setServerField('server-user', c.username);
  setServerField('server-auth', c.auth_type || 'password');
  setServerField('server-key', '');
  setServerField('server-password', c.password || '');
  setServerField('jump-host', c.jump_host || '');
  setServerField('jump-port', c.jump_port || 22);
  setServerField('jump-user', c.jump_username || '');
  setServerField('jump-auth', c.jump_auth_type || 'password');
  setServerField('jump-key', '');
  setServerField('jump-password', c.jump_password || '');
  setServerField('server-root', c.remote_root || '/tmp/ascendforge');
}

async function parseServerCommand() {
  const errorBox = $('#server-error');
  if (errorBox) errorBox.innerHTML = '';
  try {
    const data = await api('/api/remote/parse', {
      method: 'POST',
      body: {
        command: document.getElementById('quick-ssh-command')?.value || '',
        connection_password: document.getElementById('quick-connection-password')?.value || '',
      },
      idem: false,
    });
    applyServerConfig(data.config || {});
    toast('已从官方命令填充连接信息', 'success');
  } catch (err) {
    if (errorBox) errorBox.innerHTML = `<div class="alert error">${esc(err.message || '解析失败')}</div>`;
  }
}

async function serverAction(action, onSuccess) {
  const errorBox = $('#server-error');
  if (errorBox) errorBox.innerHTML = '';
  const quickCommand = document.getElementById('quick-ssh-command')?.value.trim() || '';
  if (action !== 'disconnect' && quickCommand) {
    try {
      const data = await api('/api/remote/parse', {
        method: 'POST',
        body: {
          command: quickCommand,
          connection_password: document.getElementById('quick-connection-password')?.value || '',
        },
        idem: false,
      });
      applyServerConfig(data.config || {});
    } catch (err) {
      if (errorBox) errorBox.innerHTML = `<div class="alert error">${esc(err.message || '命令解析失败')}</div>`;
      return;
    }
  }
  if (action === 'disconnect') {
    try {
      await api('/api/remote/disconnect', { method: 'POST', body: {} });
      toast('远程服务器已断开', 'success');
      if (onSuccess) await onSuccess();
    } catch (err) {
      toast(err.message || '断开失败', 'error');
    }
    return;
  }
  const body = {
    host: remoteFormValue('server-host'),
    port: Number(remoteFormValue('server-port') || 22),
    username: remoteFormValue('server-user'),
    auth_type: remoteFormValue('server-auth') || 'key',
    key_path: remoteFormValue('server-key'),
    password: document.getElementById('server-password')?.value || '',
    remote_root: remoteFormValue('server-root') || '/tmp/ascendforge',
    jump_host: remoteFormValue('jump-host'),
    jump_port: Number(remoteFormValue('jump-port') || 22),
    jump_username: remoteFormValue('jump-user'),
    jump_auth_type: remoteFormValue('jump-auth') || 'password',
    jump_key_path: remoteFormValue('jump-key'),
    jump_password: document.getElementById('jump-password')?.value || '',
  };
  const path = action === 'test' ? '/api/remote/test' : '/api/remote/connect';
  try {
    await api(path, { method: 'POST', body });
    toast(action === 'test' ? 'SSH 连接测试通过' : '远程服务器已连接', 'success');
    if (onSuccess) await onSuccess();
  } catch (err) {
    if (errorBox) errorBox.innerHTML = `<div class="alert error">${esc(err.message || '连接失败')}</div>`;
  }
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
      location.hash = '#/settings';
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
  setActiveNav(view === 'tasks' ? 'dashboard' : (view === 'platform' || view === 'settings' ? 'connections' : view));
  try {
    if (view === 'dashboard') await renderDashboard();
    else if (view === 'tasks' && state.taskId) await renderTask(state.taskId);
    else if (view === 'settings' || view === 'platform') await renderConnections();
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
    ${!worker.alive ? `<div class="alert warn" style="margin-bottom:16px">后台 worker 未运行。网页操作会保存状态，但不会推进提交或结果轮询。请在服务器启动 <span class="mono">ascendforge worker</span>。</div>` : ''}

    <div class="card" style="margin-bottom:16px">
      <div class="row" style="gap:22px">
        <div class="small"><span class="faint">CANNJudge：</span>${platform.connected ? badge('已连接', 'completed') : badge('未连接', 'pending')} <span class="muted">${esc(platform.account_masked || '')}</span></div>
        <div class="small"><span class="faint">执行器：</span>${badge('本地 CLI', 'running')} <span class="mono">Codex</span></div>
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
              ${task.status !== 'running' ? `<button class="btn btn-sm btn-ghost task-delete" data-task-delete="${esc(task.id)}" type="button" title="删除任务及其本地文件">删除</button>` : ''}
            </div>
          </article>`;
        }).join('')}
      </div>` : `<div class="empty">还没有任务。先连接 CANNJudge，再新建一个 CLI 优化任务。</div>`}
    </div>`;

  $('#new-task').addEventListener('click', () => openNewTaskModal().catch((e) => toast(e.message || '打开表单失败', 'error')));
  $('#refresh-dashboard').addEventListener('click', () => renderDashboard().catch((e) => toast(e.message, 'error')));
  $$('[data-task-delete]').forEach((button) => {
    button.addEventListener('click', async (event) => {
      event.preventDefault();
      event.stopPropagation();
      const task = tasks.find((item) => item.id === button.dataset.taskDelete);
      if (!task) return;
      if (!confirm(`删除任务“${task.title}”？会同时删除快照、workspace、日志和提交记录，不能恢复。`)) return;
      button.disabled = true;
      try {
        const result = await api(`/api/tasks/${encodeURIComponent(task.id)}`, { method: 'DELETE' });
        toast(result.warnings?.length ? `任务已删除，但有 ${result.warnings.length} 个文件未清理` : '任务已删除', result.warnings?.length ? 'error' : 'success');
        await renderDashboard();
      } catch (err) {
        button.disabled = false;
        toast(err.message || '删除失败', 'error');
      }
    });
  });
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

async function uploadProjectTemplate(file) {
  const response = await fetch('/api/project-templates', {
    method: 'POST',
    headers: {
      'Accept': 'application/json',
      'Content-Type': 'application/zip',
      'X-CSRF-Token': state.csrf || '',
      'X-Request-Id': uuid(),
      'X-Filename': file.name,
    },
    credentials: 'same-origin',
    body: file,
  });
  let data = {};
  try { data = await response.json(); } catch (_) { data = {}; }
  if (!response.ok) {
    const error = data.error || { code: 'HTTP_' + response.status, message: response.statusText };
    const err = new Error(error.message || `HTTP ${response.status}`);
    err.code = error.code;
    throw err;
  }
  return data;
}

async function openNewTaskModal() {
  const platformData = await api('/api/platform/status');
  const connected = platformData.platform?.connected;
  openModal('新建优化任务', `
    ${!connected ? `<div class="alert warn">CANNJudge 尚未连接。任务创建会先验证账号能否连接 judge。<a href="#/settings" data-close-and-go="#/settings">前往连接</a></div>` : ''}
    <form id="new-task-form" class="stack">
      <div class="form-grid">
        <div class="field span-2"><label for="nt-title">任务标题</label><input id="nt-title" type="text" maxlength="120" placeholder="例如：Add 核函数 · 第一轮优化" required></div>
        <div class="field span-2"><label for="nt-url">CANNJudge 题目链接</label><input id="nt-url" type="url" placeholder="https://cannjudge.cn/hc_cann_codelabs/cann/add" required>
          <div class="hint">必须是完整的小组/比赛/题目链接；用于读取题面和官方提交。</div></div>
        <div class="field span-2"><label for="nt-cli">CLI 运行器</label>
          <select id="nt-cli">
            <option value="codex" selected>Codex CLI（当前可用）</option>
            <option value="deepseek-harness" disabled>DeepSeek Harness（预留）</option>
            <option value="claude-code" disabled>Claude Code（预留）</option>
          </select>
          <div class="hint">当前任务固定由本地 CLI 执行；CLI 使用自己的认证，不需要模型 API Key。</div>
        </div>
        <div class="field span-2"><label for="nt-strategy">题目策略（可选）</label>
          <textarea id="nt-strategy" rows="4" maxlength="8000" placeholder="例如：本题进入性能优化阶段，重点研究 UB 分块与 DataCopy 重叠；先保证 int8/int16/int32/fp16/fp32 全部正确，再优化。"></textarea>
          <div class="hint">只放题目目标、约束和待验证假设。</div>
        </div>
      </div>
      <div id="nt-cli-pane" class="stack">
        <div class="field"><label for="nt-project">项目模板 ZIP（可选）</label><input id="nt-project" type="file" accept=".zip,application/zip">
          <div class="hint">上传后从该工程开始；不上传就是空项目，由 CLI 从 0 创建。</div></div>
        <div class="field"><label for="nt-codex-prompt">初始指令（可选）</label>
          <textarea id="nt-codex-prompt" rows="4" maxlength="8000" placeholder="例如：先阅读题面，检查模板；优先保证全部 dtype 正确，再优化性能。"></textarea>
        </div>
        <div class="alert info">创建任务后自动启动本地 CLI 会话。启动时注入真实 SKILL.md 与 TOOLS.md；会话由 AscendForge 托管。</div>
      </div>
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
    button.textContent = '正在创建任务…';
    const cliBackend = $('#nt-cli').value;
    const config = { execution_mode: 'codex', cli_backend: cliBackend };
    if ($('#nt-strategy').value.trim()) config.strategy_prompt = $('#nt-strategy').value.trim();
    try {
      let projectUploadId = null;
      const file = $('#nt-project').files?.[0];
      if (file) {
        button.textContent = '正在上传项目模板…';
        const uploaded = await uploadProjectTemplate(file);
        projectUploadId = uploaded.upload_id;
      }
      button.textContent = '正在创建 CLI 会话…';
      const data = await api('/api/tasks', {
        method: 'POST',
        body: {
          title: $('#nt-title').value.trim(),
          problem_url: $('#nt-url').value.trim(),
          execution_mode: 'codex',
          cli_backend: cliBackend,
          project_upload_id: projectUploadId,
          codex_prompt: $('#nt-codex-prompt').value,
          config,
        },
        requestId: uuid(),
      });
      closeModal();
      const startError = data.task?.codex_start_error;
      toast(startError ? `任务已创建，但 CLI 启动失败：${startError}` : '任务已创建，CLI 会话已启动', startError ? 'error' : 'success');
      navigate(`#/tasks/${data.task.id}`);
    } catch (err) {
      errorBox.hidden = false;
      errorBox.className = 'alert error';
      errorBox.textContent = err.message || '创建失败';
      button.disabled = false;
      button.textContent = '创建任务';
      if (err.code === 'ACCOUNT_CANNOT_CONNECT_JUDGE') {
        errorBox.innerHTML += ' <a href="#/settings" data-close-and-go="#/settings">前往平台连接</a>';
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
  const data = await api(`/api/tasks/${encodeURIComponent(taskId)}`);
  const task = data.task;
  state.task = task;
  if (!['attempts', 'submissions'].includes(state.taskTab)) state.taskTab = 'attempts';
  if (task.last_error_code === 'CODEX_THREAD_LOCKED' && state.lockedToastTask !== task.id) {
    state.lockedToastTask = task.id;
    toast('原会话正被另一个 Codex 进程占用；请关闭占用端会话后重试。原会话与工作区已保留。', 'error');
  }
  const quota = task.quota || {};
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
        ${(() => {
          const codexActive = task.config?.execution_mode === 'codex' && task.config?.codex_runner_active;
          const pausing = task.config?.execution_mode === 'codex' && task.config?.codex_pause_phase === 'requested';
          const running = task.status === 'running' && (task.config?.execution_mode !== 'codex' || codexActive);
          if (pausing) return '<button class="btn btn-primary" type="button" disabled>暂停中…</button>';
          return running
            ? '<button class="btn btn-primary" data-pause-task="1" type="button">暂停任务</button>'
            : '<button class="btn btn-primary" data-continue-task="1" type="button">继续任务</button>';
        })()}
        <button class="btn" data-workload-recovery="1" type="button" ${task.workload_recovery?.requires_pass_baseline ? 'disabled title="需要优化任务先产生一个 Pass 提交"' : ''}>逆向评测数据</button>
      </div>
    </div>

    ${renderTaskAlert(task, quota)}
    ${renderTestcaseOverview(task)}
    ${renderWorkloadRecovery(task)}

    <div class="card">
      <div class="tabs" id="task-tabs">
        ${taskTabButton('attempts', '尝试记录')}
        ${taskTabButton('submissions', '提交与反馈')}
      </div>
      <div id="task-tab-body">${renderTaskTab(task)}</div>
    </div>`;

  const pauseButton = $('[data-pause-task]');
  if (pauseButton) pauseButton.addEventListener('click', () => pauseTask(task.id));
  const continueButton = $('[data-continue-task]');
  if (continueButton) continueButton.addEventListener('click', () => continueTask(task.id));
  $$('[data-workload-recovery]').forEach((button) => {
    button.addEventListener('click', () => startWorkloadRecovery(task.id));
  });
  $$('[data-recovery-action]').forEach((button) => {
    button.addEventListener('click', () => workloadRecoveryAction(button.dataset.recoveryJob, button.dataset.recoveryAction));
  });
  $$('[data-recovery-reprobe]').forEach((button) => {
    button.addEventListener('click', () => startWorkloadRecovery(task.id, true));
  });
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

function taskTabButton(key, label) {
  return `<button type="button" class="${state.taskTab === key ? 'active' : ''}" data-task-tab="${key}">${label}</button>`;
}

function renderTaskAlert(task, quota) {
  if (task.last_error_code === 'CODEX_THREAD_LOCKED') {
    return `<div class="alert warn" style="margin-bottom:16px">原会话正被另一个 Codex 进程占用，暂时无法恢复。请在占用端结束该会话的使用并释放占用后重试。原会话与工作区已保留。<div class="faint small" style="margin-top:6px">${esc(task.last_error || '')}</div></div>`;
  }
  if (task.status === 'needs_login') {
    return `<div class="alert warn" style="margin-bottom:16px">CANNJudge 登录失效，新的模型调用/提交已暂停，已在途的结果仍会收集。请前往 <a href="#/settings">平台连接</a> 重新登录，然后恢复任务。${task.last_error ? `<div class="faint small" style="margin-top:6px">${esc(task.last_error)}</div>` : ''}</div>`;
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

function ojResult(normalized) {
  const raw = String(normalized?.raw_status || '').trim();
  if (!raw) return { label: '—', cls: 'unknown' };
  const label = raw === 'Wrong Answer' ? 'WA' : raw;
  const cls = /pass/i.test(raw) ? 'pass' : /(wrong|compile|runtime|error|fail)/i.test(raw) ? 'fail' : 'unknown';
  return { label, cls };
}

function renderTestcaseOverview(task) {
  const attempts = (task.attempts || [])
    .filter((attempt) => (attempt.normalized_json?.testcases || []).length)
    .sort((a, b) => Number(b.ordinal || 0) - Number(a.ordinal || 0));
  if (!attempts.length) return '';
  const cases = new Map();
  for (const attempt of attempts) {
    for (const [fallbackIndex, testcase] of (attempt.normalized_json.testcases || []).entries()) {
      const raw = testcase.raw || {};
      const key = String(raw.testcase_id ?? testcase.index ?? fallbackIndex);
      const index = Number.isFinite(Number(testcase.index)) ? Number(testcase.index) : fallbackIndex;
      const numeric = (value) => (
        value !== null && value !== undefined && value !== '' && Number.isFinite(Number(value))
          ? Number(value)
          : null
      );
      const time = numeric(raw.time);
      const bestTime = numeric(raw.best_time);
      if (!cases.has(key)) {
        cases.set(key, {
          index,
          bestTime,
          myTime: time,
          score: raw.score,
          status: raw.testcase_status || testcase.status || '',
        });
      }
      const item = cases.get(key);
      if (item.bestTime === null && bestTime !== null) item.bestTime = bestTime;
      if (time !== null && (item.myTime === null || time < item.myTime)) {
        item.myTime = time;
        item.score = raw.score;
        item.status = raw.testcase_status || testcase.status || '';
      }
    }
  }
  const rows = [...cases.values()].sort((a, b) => a.index - b.index);
  const display = (value) => (value === null || value === undefined ? '—' : String(value));
  const timeDisplay = (value) => (value === null || value === undefined ? '—' : `${value} µs`);
  return `<div class="card" style="margin-bottom:16px">
    <div class="card-head"><div><h3>测试点最优时间</h3><div class="card-sub">OJ best_time 与当前任务历史最小 time；按 OJ 页面单位 µs 显示，数值不做换算。</div></div></div>
    <div class="table-wrap"><table>
      <thead><tr><th>测试点</th><th>OJ 最优时间 (µs)</th><th>我的最优时间 (µs)</th><th>得分</th></tr></thead>
      <tbody>${rows.map((item) => `<tr>
        <td class="mono">#${esc(item.index + 1)}</td>
        <td class="mono">${esc(timeDisplay(item.bestTime))}</td>
        <td class="mono">${esc(timeDisplay(item.myTime))}</td>
        <td class="mono">${esc(display(item.score))}</td>
      </tr>`).join('')}</tbody>
    </table></div>
  </div>`;
}

function recordCells(attempt, normalized) {
  const result = ojResult(normalized);
  const cases = normalized?.testcase_total
    ? `${normalized.testcase_passed ?? 0}/${normalized.testcase_total}`
    : '—';
  return {
    result: result.label === '—' ? '<span class="faint">—</span>' : badge(result.label, result.cls),
    cases: esc(cases),
    thought: esc(attempt.hypothesis || ''),
  };
}

const RECOVERY_STATUS = {
  pending: ['待开始', 'pending'],
  running: ['运行中', 'running'],
  paused: ['已暂停', 'paused'],
  external_blocked: ['外部阻塞', 'unknown'],
  verified: ['已验证', 'completed'],
  partial: ['部分完成', 'pending'],
  completed: ['已完成', 'completed'],
  failed: ['失败', 'failed'],
  stopped: ['已停止', 'failed'],
};

const EVIDENCE_STATUS = {
  measured: ['已验证', 'pass'],
  confirmed: ['Codex 确认', 'completed'],
  provided: ['用户提供', 'unknown'],
  inferred: ['推断', 'pending'],
  unknown: ['未知', 'unknown'],
};

function renderWorkloadRecovery(task) {
  const rec = task.workload_recovery;
  if (!rec) return '';
  const fp = rec.fingerprint || {};
  const job = rec.job;
  const spec = rec.spec;
  const jobStatus = job ? (RECOVERY_STATUS[job.status] || [job.status, 'unknown']) : null;
  const specStatus = spec ? (RECOVERY_STATUS[spec.status] || [spec.status, 'unknown']) : null;
  const tensorText = (tensor) => {
    const shape = Array.isArray(tensor.shape) ? `[${tensor.shape.join('×')}]` : '?';
    const numel = tensor.numel === null || tensor.numel === undefined ? '' : ` · ${tensor.numel}`;
    return `${tensor.name || '?'}: ${tensor.dtype || '?'} ${shape}${numel}`;
  };
  const cases = spec?.testcases || [];
  const warnings = spec?.validation?.warnings || [];
  return `<div class="card" style="margin-bottom:16px" id="workload-recovery">
    <div class="card-head">
      <div>
        <h3>逆向评测数据</h3>
        <div class="card-sub">
          ${esc(fp.platform || 'cannjudge')} · ${esc(fp.scope || '')} · ${esc(fp.problem_id || '')}
          · 版本 ${esc(fp.problem_version || '未知')} · 测试集 ${esc(fp.testset_key || '未知')}
        </div>
      </div>
      <div class="row">
        ${job ? badge(jobStatus[0], jobStatus[1]) : ''}
        ${spec ? badge(`规格 ${specStatus[0]}`, specStatus[1]) : ''}
        ${job && job.codex_runner_active ? '<span class="faint small">Codex 运行中</span>' : ''}
      </div>
    </div>
    ${job ? `<div class="alert ${job.status === 'failed' ? 'error' : job.status === 'external_blocked' ? 'warn' : 'info'}" style="margin-bottom:12px">
      <div class="row-between"><strong>逆向任务 ${esc(job.id)}</strong><span class="mono small">${esc(job.phase || '')}</span></div>
      ${job.base_attempt_id ? `<div class="small mono" style="margin-top:4px">基座：Pass attempt ${esc(job.base_attempt_id)}</div>` : ''}
      ${job.error_message ? `<div class="small" style="margin-top:6px">${esc(job.error_code || '')} · ${esc(job.error_message)}</div>` : ''}
      <div class="row" style="margin-top:8px">
        ${['pending', 'running', 'external_blocked'].includes(job.status) ? `<button class="btn btn-sm" data-recovery-action="pause" data-recovery-job="${esc(job.id)}" type="button">暂停逆向</button>` : ''}
        ${['paused', 'external_blocked', 'partial'].includes(job.status) && !rec.requires_pass_baseline ? `<button class="btn btn-sm btn-primary" data-recovery-action="resume" data-recovery-job="${esc(job.id)}" type="button">继续逆向</button>` : ''}
        ${['pending', 'running', 'paused', 'external_blocked', 'partial'].includes(job.status) ? `<button class="btn btn-sm btn-danger" data-recovery-action="stop" data-recovery-job="${esc(job.id)}" type="button">停止逆向</button>` : ''}
      </div>
    </div>` : ''}
    ${spec ? `<div class="table-wrap"><table>
      <thead><tr><th>测试点</th><th>输入</th><th>输出</th><th>属性 / 派生</th><th>状态</th><th>证据</th></tr></thead>
      <tbody>${cases.map((item) => {
        const status = EVIDENCE_STATUS[item.evidence_status] || [item.evidence_status || '未知', 'unknown'];
        const refs = (item.evidence_refs || []).map((ref) => typeof ref === 'string' ? ref : (ref.submission_id || ref.attempt_id || ref.snapshot_id || '')).filter(Boolean);
        return `<tr>
          <td class="mono">${esc(item.testcase_id)}</td>
          <td class="small">${(item.inputs || []).map((tensor) => `<div class="mono">${esc(tensorText(tensor))}</div>`).join('') || '—'}</td>
          <td class="small">${(item.outputs || []).map((tensor) => `<div class="mono">${esc(tensorText(tensor))}</div>`).join('') || '—'}</td>
          <td class="small mono">${esc(JSON.stringify({ attributes: item.attributes || {}, derived: item.derived || {} }))}</td>
          <td>${badge(status[0], status[1])}</td>
          <td class="small mono">${refs.length ? refs.map(esc).join(', ') : '—'}</td>
        </tr>`;
      }).join('')}</tbody>
    </table></div>
    <div class="small faint" style="margin-top:10px">
      记录来源：${esc(spec.source_task_id || '—')} · 更新于 ${esc(fmtTime(spec.updated_at))}
      ${rec.base_attempt_id && !job ? ` · 基座 ${esc(rec.base_attempt_id)}` : ''}
      · 已确认测试点 ${esc(cases.filter((item) => ['measured', 'confirmed'].includes(item.evidence_status)).length)}/${esc(cases.length)}
      · 未知/推断 ${esc(spec.unknown_count ?? 0)}
      ${rec.diagnostics ? ` · 诊断提交 ${esc(rec.diagnostics)}` : ''}
    </div>
    ${rec.diagnostic_rows?.length ? `<details class="recovery-details" style="margin-top:10px">
      <summary>诊断提交 ${esc(rec.diagnostics)} 条</summary>
      <div class="table-wrap"><table>
        <thead><tr><th>#</th><th>提交</th><th>状态</th><th>远端 ID</th><th>错误</th></tr></thead>
        <tbody>${rec.diagnostic_rows.map((row) => `<tr>
          <td class="mono">${esc(row.ordinal ?? '—')}</td>
          <td class="mono small">${esc(row.submission_id || row.attempt_id || '—')}</td>
          <td class="mono">${esc(row.state || '—')}</td>
          <td class="mono small">${esc(row.remote_id || '—')}</td>
          <td class="small">${esc(row.error_code || '—')}</td>
        </tr>`).join('')}</tbody>
      </table></div>
    </details>` : ''}
    ${warnings.length ? `<div class="alert warn small" style="margin-top:10px">${warnings.slice(0, 8).map(esc).join('<br>')}</div>` : ''}
    <div class="row" style="margin-top:10px">
      ${(!job || !['pending', 'running', 'paused', 'external_blocked'].includes(job.status)) && !rec.requires_pass_baseline ? `<button class="btn btn-sm" data-recovery-reprobe="1" type="button">重新验证/重新探测</button>` : ''}
    </div>` : `<div class="empty">${rec.requires_pass_baseline ? '逆向评测需要优化任务先产生一个 Pass 提交。' : '还没有可复用的输入规格。点击右上角“逆向评测数据”启动独立 Codex 逆向会话。'}</div>`}
  </div>`;
}

async function startWorkloadRecovery(taskId, force = false) {
  const rec = state.task?.workload_recovery;
  if (rec?.active) {
    toast('已有逆向任务正在运行，直接复用其进度');
    document.getElementById('workload-recovery')?.scrollIntoView({ behavior: 'smooth' });
    return;
  }
  if (rec?.spec?.status === 'verified') {
    toast('已有已验证规格，直接复用，不新建会话');
    document.getElementById('workload-recovery')?.scrollIntoView({ behavior: 'smooth' });
    return;
  }
  try {
    const result = await api(`/api/tasks/${encodeURIComponent(taskId)}/workload-recovery`, {
      method: 'POST',
      body: { force: Boolean(force || rec?.spec?.status === 'partial') },
      requestId: uuid(),
    });
    if (result.mode === 'cache') toast('复用已有已验证规格');
    else if (result.mode === 'active') toast('复用正在运行的逆向任务');
    else if (result.mode === 'partial') toast('已有部分规格，可点击“重新验证/重新探测”继续');
    else toast('已启动独立逆向会话');
    await renderTask(taskId);
  } catch (error) {
    toast(error.message || '启动逆向失败', 'error');
  }
}

async function workloadRecoveryAction(jobId, action) {
  try {
    await api(`/api/workload-recovery/${encodeURIComponent(jobId)}/${action}`, {
      method: 'POST', body: {}, requestId: uuid(),
    });
    toast(action === 'pause' ? '已请求暂停逆向' : action === 'stop' ? '已停止逆向任务' : '已继续逆向');
    await renderTask(state.task.id);
  } catch (error) {
    toast(error.message || '操作失败', 'error');
  }
}

function renderTaskTab(task) {
  if (state.taskTab === 'submissions') {
    const submissions = task.submissions || [];
    if (!submissions.length) return '<div class="empty">还没有远端提交。</div>';
    return `<div class="table-wrap"><table>
      <thead><tr><th>#</th><th>状态</th><th>结果</th><th>用例</th><th>思路</th></tr></thead>
      <tbody>${submissions.map((submission) => {
        const attempt = (task.attempts || []).find((item) => item.id === submission.attempt_id) || {};
        const cells = recordCells(attempt, attempt.normalized_json || {});
        const open = attempt.id ? ` data-attempt-open="${esc(attempt.id)}"` : '';
        const resolving = ['unknown', 'submitting'].includes(submission.state);
        return `<tr class="clickable record-row"${open} title="点击查看详情">
          <td class="mono">${esc(attempt.ordinal ?? '—')}</td>
          <td>${badge(SUBMISSION_STATUS[submission.state] || submission.state, submission.state === 'finished' ? 'completed' : submission.state === 'failed' ? 'failed' : submission.state === 'unknown' ? 'unknown' : 'running')}
            ${resolving ? `<button class="btn btn-sm btn-primary" type="button" data-resolve="${esc(submission.id)}">对账</button>` : ''}</td>
          <td>${cells.result}</td>
          <td class="mono">${cells.cases}</td>
          <td class="record-cell" title="${cells.thought}">${cells.thought}</td>
        </tr>`;
      }).join('')}</tbody></table></div>`;
  }
  if (!(task.attempts || []).length) return '<div class="empty">还没有尝试。</div>';
  return `<div class="table-wrap"><table>
    <thead><tr><th>#</th><th>状态</th><th>结果</th><th>用例</th><th>思路</th></tr></thead>
    <tbody>${task.attempts.map((attempt) => {
      const normalized = attempt.normalized_json || {};
      const cells = recordCells(attempt, normalized);
      return `<tr class="clickable record-row" data-attempt-open="${esc(attempt.id)}" title="点击查看详情">
        <td class="mono">${esc(attempt.ordinal)}</td>
        <td>${attemptBadge(attempt.status)}</td>
        <td>${cells.result}</td>
        <td class="mono">${cells.cases}</td>
        <td class="record-cell" title="${cells.thought}">${cells.thought}</td>
      </tr>`;
    }).join('')}</tbody></table></div>`;
}

async function pauseTask(taskId) {
  try {
    await api(`/api/tasks/${encodeURIComponent(taskId)}/pause`, { method: 'POST', body: {} });
    toast('任务已请求暂停', 'success');
    await renderTask(taskId);
  } catch (err) {
    toast(err.message || '暂停失败', 'error');
    if (!err.handled) await renderTask(taskId).catch(() => {});
  }
}

async function continueTask(taskId) {
  const task = state.task;
  if (!task) return;
  try {
    if (task.config?.execution_mode === 'codex') {
      // Codex 任务用同一个入口恢复：已有 codex_session_ref 时继续原会话。
      await api(`/api/tasks/${encodeURIComponent(taskId)}/codex-run`, {
        method: 'POST',
        body: {},
      });
      toast('Codex 会话继续中', 'success');
    } else if (task.status === 'pending') {
      await api(`/api/tasks/${encodeURIComponent(taskId)}/start`, { method: 'POST', body: {} });
      toast('任务已开始', 'success');
    } else if (['paused', 'needs_login', 'needs_review'].includes(task.status)) {
      await api(`/api/tasks/${encodeURIComponent(taskId)}/resume`, { method: 'POST', body: {} });
      toast('任务已恢复', 'success');
    } else {
      await api(`/api/tasks/${encodeURIComponent(taskId)}/continue-optimization`, {
        method: 'POST',
        body: {},
      });
      toast('任务已继续', 'success');
    }
    await renderTask(taskId);
  } catch (err) {
    toast(err.message || '继续失败', 'error');
    if (!err.handled) await renderTask(taskId).catch(() => {});
  }
}

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
async function renderConnections() {
  setPoll(null);
  const [data, platformData, remoteData] = await Promise.all([
    api('/api/capabilities'),
    api('/api/platform/status'),
    api('/api/remote/status'),
  ]);
  const platform = platformData.platform || {};
  const remote = remoteData.remote || {};
  state.platform = platform;
  state.remote = remote;
  state.capabilities = data.capabilities || data;
  const caps = state.capabilities || {};
  $('#view').innerHTML = `
    <div class="row-between" style="margin-bottom:18px">
      <div><h1>连接与设置</h1><div class="muted">CANNJudge 用于官方题目与提交；本地 Codex 使用本机 Codex CLI 认证，不需要模型 API Key。</div></div>
    </div>

    <div class="card" style="margin-bottom:16px">
      <div class="card-head"><div><h2>CANNJudge 平台连接</h2><div class="card-sub">官方题面读取、提交和结果查询仍然必须经过平台账号。</div></div>
        ${platform.connected ? badge('已连接', 'completed') : badge('未连接', 'pending')}</div>
      ${platform.last_error ? `<div class="alert error" style="margin-bottom:12px">${esc(platform.last_error.message || JSON.stringify(platform.last_error))}</div>` : ''}
      ${platform.connected ? `
        <div class="stack small">
          <div class="row-between"><span class="muted">账号</span><span class="mono">${esc(platform.account_masked || '—')}</span></div>
          <div class="row-between"><span class="muted">Judge 题目访问</span><span>${platform.judge_verified ? badge('已验证', 'completed') : badge('未验证', 'pending')}</span></div>
        </div>
        <div class="toolbar" style="margin-top:14px">
          <button class="btn" id="platform-test" type="button">测试当前连接</button>
          <button class="btn btn-danger" id="platform-disconnect" type="button">断开并清除会话</button>
        </div>
        <div id="evidence-box" class="mt-12"></div>
      ` : `
        <form id="platform-form" class="stack">
          <div class="form-grid">
            <div class="field"><label for="p-account">CANNJudge 账号（邮箱或手机号）</label><input id="p-account" type="text" autocomplete="username" required></div>
            <div class="field"><label for="p-password">密码</label><input id="p-password" type="password" autocomplete="current-password" required></div>
            <div class="field span-2"><label for="p-problem">用于验证 judge 访问的题目链接</label><input id="p-problem" type="url" value="https://cannjudge.cn/hc_cann_codelabs/cann/add"></div>
            <div class="field"><label for="p-captcha-code">验证码（平台要求时）</label><input id="p-captcha-code" type="text" placeholder="验证码"></div>
            <div class="field"><label>&nbsp;</label><div class="row"><button class="btn btn-sm" id="load-captcha" type="button">获取验证码</button><input id="p-captcha-id" type="hidden"></div></div>
          </div>
          <div id="captcha-box" class="mt-8"></div>
          <div class="alert error" id="platform-error" hidden></div>
          <div class="form-actions"><button class="btn btn-primary" type="submit">登录并验证</button></div>
        </form>
      `}
    </div>

    <div class="card" style="margin-bottom:16px">
      <div class="card-head"><div><h2>远程服务器</h2><div class="card-sub">Codex 仍在本机运行；这里提供 SSH 通道，让 Codex 在远端编译和测试。远端不需要安装 Codex。</div></div>
        ${remote.status === 'connected' ? badge('已连接', 'completed') : remote.status === 'connecting' ? badge('连接中', 'pending') : remote.status === 'error' ? badge('连接失败', 'failed') : badge('未连接', 'pending')}</div>
      ${remote.last_error ? `<div class="alert error small" style="margin-bottom:12px">${esc(remote.last_error)}</div>` : ''}
      <form id="server-form" class="stack">
        <div class="alert info small">官方 NPU 环境给的命令通常形如 <span class="mono">ssh -J user:password@jump:port root@target</span>，有效期约 5 分钟。把整条命令和连接密码粘贴到下面，点击“解析并填充”。</div>
        <div class="field"><label for="quick-ssh-command">官方 SSH 连接命令</label><textarea id="quick-ssh-command" rows="3" placeholder="ssh -J jt_xxx:xxx@113.47.8.48:2234 root@179.62.58.131" style="width:100%;font-family:var(--mono)"></textarea></div>
        <div class="form-grid">
          <div class="field"><label for="quick-connection-password">连接密码</label><input id="quick-connection-password" type="password" autocomplete="new-password" placeholder="官方弹窗里的“连接密码”"></div>
          <div class="field"><label>&nbsp;</label><button class="btn" id="parse-server-command" type="button">解析并填充</button></div>
        </div>
        <div class="field span-2"><div class="small faint" style="margin-top:6px">目标机（最终运行命令的 NPU 服务器）</div></div>
        <div class="form-grid">
          <div class="field"><label for="server-host">目标机主机</label><input id="server-host" value="${esc(remote.host || '')}" placeholder="179.62.58.131" required></div>
          <div class="field"><label for="server-port">目标机 SSH 端口</label><input id="server-port" type="number" value="${esc(remote.port || 22)}" required></div>
          <div class="field"><label for="server-user">目标机用户名</label><input id="server-user" value="${esc(remote.username || '')}" placeholder="root" required></div>
          <div class="field"><label for="server-auth">目标机认证方式</label>
            <select id="server-auth"><option value="key" ${(remote.auth_type || 'key') === 'key' ? 'selected' : ''}>SSH key / agent</option><option value="password" ${remote.auth_type === 'password' ? 'selected' : ''}>密码</option></select>
          </div>
          <div class="field"><label for="server-key">目标机私钥路径（key 模式）</label><input id="server-key" value="${esc(remote.key_path || '')}" placeholder="C:\\Users\\you\\.ssh\\id_ed25519"></div>
          <div class="field"><label for="server-password">目标机密码 / 连接密码（留空保持原值）</label><input id="server-password" type="password" autocomplete="new-password" placeholder="${remote.auth_type === 'password' ? '已保存，留空不修改' : '仅密码认证需要'}"></div>
          <div class="field span-2"><div class="small faint" style="margin-top:6px">跳板机（可选；对应 ssh -J user:password@jump:port target）</div></div>
          <div class="field"><label for="jump-host">跳板机主机</label><input id="jump-host" value="${esc(remote.jump_host || '')}" placeholder="留空表示直连"></div>
          <div class="field"><label for="jump-port">跳板机端口</label><input id="jump-port" type="number" value="${esc(remote.jump_port || 22)}"></div>
          <div class="field"><label for="jump-user">跳板机用户名</label><input id="jump-user" value="${esc(remote.jump_username || '')}"></div>
          <div class="field"><label for="jump-auth">跳板机认证</label>
            <select id="jump-auth"><option value="password" ${(remote.jump_auth_type || 'password') === 'password' ? 'selected' : ''}>密码</option><option value="key" ${remote.jump_auth_type === 'key' ? 'selected' : ''}>SSH key</option></select>
          </div>
          <div class="field"><label for="jump-key">跳板机私钥路径</label><input id="jump-key" value="${esc(remote.jump_key_path || '')}"></div>
          <div class="field"><label for="jump-password">跳板机密码（留空保持原值）</label><input id="jump-password" type="password" autocomplete="new-password" placeholder="${remote.jump_auth_type === 'password' ? '已保存，留空不修改' : '仅密码认证需要'}"></div>
          <div class="field span-2"><label for="server-root">远程工作目录</label><input id="server-root" value="${esc(remote.remote_root || '/tmp/ascendforge')}" required></div>
        </div>
        <div id="server-error"></div>
        <div class="form-actions">
          <button class="btn" type="button" data-server-action="test">测试连接</button>
          <button class="btn btn-primary" type="button" data-server-action="connect">${remote.status === 'connected' ? '重新连接' : '连接'}</button>
          ${remote.status === 'connected' ? '<button class="btn btn-danger" type="button" data-server-action="disconnect">断开</button>' : ''}
        </div>
      </form>
      <div class="small faint" style="margin-top:10px">连接后，新启动或恢复的 Codex 会话会获得 <span class="mono">remote_exec</span>；远端命令统一走宿主持有的 SSH bridge。</div>
    </div>

    <div class="card" style="margin-bottom:16px">
      <div class="card-head"><div><h2>CLI 运行器</h2><div class="card-sub">当前只启用本机 Codex CLI；不在这里保存任何模型 API Key。</div></div>
        ${badge('Codex CLI', 'completed')}</div>
      <div class="stack small">
        <div class="row-between"><span class="muted">当前运行器</span><span class="mono">Codex CLI</span></div>
        <div class="row-between"><span class="muted">认证来源</span><span>本机 Codex 登录 / 配置</span></div>
        <div class="row-between"><span class="muted">DeepSeek Harness</span><span>${badge('预留', 'pending')}</span></div>
        <div class="row-between"><span class="muted">Claude Code</span><span>${badge('预留', 'pending')}</span></div>
      </div>
      <div class="alert info small" style="margin-top:12px">运行器由任务配置中的 <span class="mono">cli_backend</span> 指定；目前只接受 <span class="mono">codex</span>，以后接入其他 CLI 时保持同一工具合同。</div>
    </div>

    <div class="card" style="margin-top:16px">
      <div class="card-head"><h3>服务器能力</h3><span class="faint small">检测时间 ${esc(fmtTime(caps.checked_at))}</span></div>
      <div class="stack small">
        <div class="row-between"><span class="muted">NPU</span><span>${caps.npu?.available ? badge('可用', 'completed') : badge('不可用', 'pending')} <span class="faint">${esc(caps.npu?.evidence || '')}</span></span></div>
        <div class="row-between"><span class="muted">CANN</span><span>${caps.cann?.available ? badge('可用', 'completed') : badge('不可用', 'pending')}</span></div>
        <div class="row-between"><span class="muted">远端自定义测试</span><span class="mono">${caps.remote_custom_tests?.supported ? '已核实支持' : '未核实/禁用'}</span></div>
      </div>
    </div>`;

  const platformForm = $('#platform-form');
  if (platformForm) platformForm.addEventListener('submit', async (event) => {
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
      await renderConnections();
    } catch (err) {
      box.hidden = false; box.className = 'alert error'; box.textContent = err.message;
      button.disabled = false; button.textContent = '登录并验证';
    }
  });
  const captchaButton = $('#load-captcha');
  if (captchaButton) captchaButton.addEventListener('click', async () => {
    const box = $('#captcha-box');
    box.innerHTML = '<span class="faint small">加载中…</span>';
    try {
      const data = await api('/api/platform/captcha');
      $('#p-captcha-id').value = data.captcha_id;
      box.innerHTML = `<img alt="验证码" style="max-width:220px;border-radius:8px;border:1px solid var(--border);background:#fff" src="${data.image_data_url}"><div class="faint small">由平台返回，仅本次登录使用。</div>`;
    } catch (err) { box.innerHTML = `<div class="alert error small">${esc(err.message)}</div>`; }
  });
  const platformTest = $('#platform-test');
  if (platformTest) platformTest.addEventListener('click', async () => {
    const box = $('#evidence-box');
    box.innerHTML = '<span class="faint small">检查中…</span>';
    try {
      const problemUrl = $('#p-problem')?.value.trim() || 'https://cannjudge.cn/hc_cann_codelabs/cann/add';
      const data = await api('/api/platform/test', { method: 'POST', body: { problem_url: problemUrl }, idem: false });
      box.innerHTML = `<pre class="small" style="max-height:280px;overflow:auto">${esc(jsonPretty(data.evidence))}</pre>`;
    } catch (err) { box.innerHTML = `<div class="alert error small">${esc(err.message)}</div>`; }
  });
  const disconnect = $('#platform-disconnect');
  if (disconnect) disconnect.addEventListener('click', async () => {
    if (!confirm('清除服务器保存的 CANNJudge 会话？运行中的任务会暂停提交。')) return;
    await api('/api/platform/disconnect', { method: 'POST', body: {} });
    toast('已断开', 'success');
    await renderConnections();
  });
  $$('[data-server-action]').forEach((button) => {
    button.addEventListener('click', () => serverAction(button.dataset.serverAction, renderConnections));
  });
  const parseCommand = $('#parse-server-command');
  if (parseCommand) parseCommand.addEventListener('click', parseServerCommand);

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
