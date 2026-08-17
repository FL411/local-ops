'use strict';
/* ============================================================
   app.js — 入口：视图切换 / 轮询 / 命令面板 / 总控台自身
   ============================================================ */
import { $, el, setText, setChildren, icon, escapeHtml,
  post, act, toast, state, DISCONNECTED_TEXT, notifyTaskCompletions,
  applyTheme, initThemeToggle, applyUiTheme,
  currentUiTheme, reconcilePendingUiTheme, trapLayerFocus,
  openLayer, closeLayer, activeLayer,
  currentMutationEpoch, taskNotificationsEnabled, toggleTaskNotifications,
  localServiceUrl, MOD_KEY, applyModKeys, controlTokenAvailable,
  CONTROL_READONLY_TEXT } from './js/core.js';
import { renderLaunchpad, toggleApp, closePortDiagnostic, closeAppDiagnosis } from './js/launchpad.js';
import { renderServices, observePortDiscovery,
  suspendPortDiscovery } from './js/services.js';
import { initWidgets, renderWidgets, openLogsCenter, closeLogsCenter,
  openSettingsCenter, closeSettingsCenter, resetFeedBaseline } from './js/widgets.js';
import { buildGlyphGrid, initAppModal, initLogDrawer, openConfirm,
  openAppModal, closeAppModal, closeConfirm, openLogs, closeLogs,
  openConsoleLog } from './js/overlays.js';
import { configuredPort, actualPorts, portIsOpenable,
  preferredOpenPort } from './js/ports.js';

/* ---------------- DOM 引用 ---------------- */
const banner = $('#banner');
const sideNav = $('#sideNav');
const navBtns = [...sideNav.querySelectorAll('.nav-btn')];
const viewTitle = $('#viewTitle');
const viewOverline = $('#viewOverline');
const viewSub = $('#viewSub');
const navCountLaunch = $('#navCountLaunch'), navCountSvc = $('#navCountSvc');
const sideStats = $('#sideStats');
const cmdkTrigger = $('#cmdkTrigger');
const restartConsoleBtn = $('#restartConsoleBtn');
const restartConsoleIcon = $('#restartConsoleIcon');
const restartConsoleLabel = $('#restartConsoleLabel');
const consolePortLabel = $('#consolePortLabel');
const stopConsoleBtn = $('#stopConsoleBtn');
const stopConsoleIcon = $('#stopConsoleIcon');
const stopConsoleLabel = $('#stopConsoleLabel');
const viewLaunchpad = $('#view-launchpad');
const viewServices = $('#view-services');
/* 只有 data-view 的导航轨按钮参与视图切换；data-action 按钮由 widgets 代理 */
const railBtns = [...document.querySelectorAll('.rail-btn[data-view]')];
const sideLaunch = $('#sideLaunch');
const sideSvc = $('#sideSvc');

let firstRender = true;          // 首屏渲染（stagger 入场）

/* ---------------- 视图切换 ---------------- */
function switchView(v) {
  if (state.view === v) return;
  state.view = v;
  localStorage.setItem('console-view', v);
  applyView();
  /* 强制重排以重播视图进入动画 */
  const active = v === 'launchpad' ? viewLaunchpad : viewServices;
  active.classList.remove('active');
  void active.offsetWidth;
  active.classList.add('active');
}
function applyView() {
  const v = state.view;
  navBtns.forEach(b => {
    const active = b.dataset.view === v;
    b.classList.toggle('active', active);
    b.setAttribute('aria-selected', String(active));
    b.tabIndex = active ? 0 : -1;
  });
  railBtns.forEach(b => {
    const active = b.dataset.view === v;
    b.classList.toggle('active', active);
    if (active) b.setAttribute('aria-current', 'page');
    else b.removeAttribute('aria-current');
  });
  sideLaunch.hidden = v !== 'launchpad';
  sideSvc.hidden = v !== 'services';
  viewLaunchpad.classList.toggle('active', v === 'launchpad');
  viewServices.classList.toggle('active', v === 'services');
  viewLaunchpad.setAttribute('aria-hidden', String(v !== 'launchpad'));
  viewServices.setAttribute('aria-hidden', String(v !== 'services'));
  setText(viewTitle, v === 'launchpad' ? '启动台' : '服务监控');
  document.documentElement.dataset.view = v;
  setText(viewOverline, v === 'launchpad' ? 'Launchpad' : 'Services');
  setText(viewSub, v === 'launchpad'
    ? '一键启动与管理你的本地服务和批处理任务'
    : '实时掌握本机监听端口与进程负载');
}
navBtns.forEach(b => b.addEventListener('click', () => switchView(b.dataset.view)));
railBtns.forEach(b => b.addEventListener('click', () => switchView(b.dataset.view)));
sideNav.addEventListener('keydown', e => {
  if (!['ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(e.key)) return;
  e.preventDefault();
  let index = navBtns.indexOf(document.activeElement);
  if (index < 0) return;
  if (e.key === 'Home') index = 0;
  else if (e.key === 'End') index = navBtns.length - 1;
  else index = (index + ((e.key === 'ArrowDown' || e.key === 'ArrowRight') ? 1 : -1) + navBtns.length) % navBtns.length;
  switchView(navBtns[index].dataset.view);
  navBtns[index].focus();
});

/* ============================================================
   轮询
   ============================================================ */
const POLL_INTERVAL_MS = 2000;
const POLL_TIMEOUT_MS = 7000;
let pollPromise = null;
let pollController = null;
let pollTimer = null;
let restartDeadlineTimer = null;

function poll(force = false) {
  if (document.hidden && !force) return Promise.resolve();
  if (pollPromise) return pollPromise;
  const controller = new AbortController();
  pollController = controller;
  let timedOut = false;
  const timeout = setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, POLL_TIMEOUT_MS);
  const run = (async () => {
    try {
      const epochAtStart = currentMutationEpoch();
      const r = await fetch('/api/state', {
        cache: 'no-store',
        signal: controller.signal,
      });
      if (!r.ok) {
        const error = new Error('HTTP ' + r.status);
        error.status = r.status;
        throw error;
      }
      const data = await r.json();
      /* 请求发出期间发生了写操作：这份快照是操作生效前的旧状态，
         丢弃并立即补一轮，避免卡片短暂回退到旧状态。 */
      if (epochAtStart !== currentMutationEpoch()) {
        schedulePoll(0);
        return;
      }
      reconcilePendingUiTheme(data);
      if (state.restartingFrom) {
        suspendPortDiscovery();
        resetFeedBaseline();
      }
      observePortDiscovery(data);
      notifyTaskCompletions(state.data, data);
      state.data = data;
      state.lastUpdate = new Date();
      const restartCompleted = state.restartingFrom && data.consolePid
        && data.consolePid !== state.restartingFrom;
      if (restartCompleted) {
        clearTimeout(restartDeadlineTimer);
        restartDeadlineTimer = null;
        state.restartingFrom = null;
        setConnected(true);
        toast('总控台已重新启动');
      } else if (!state.restartingFrom && !state.stopping) {
        setConnected(true);
      }
      render();
    } catch (e) {
      suspendPortDiscovery();
      resetFeedBaseline();
      if (e && e.name !== 'AbortError') console.error('状态刷新失败', e);
      /* 页面进入后台时主动取消请求，不把它误报成断连。 */
      if (!document.hidden || timedOut) {
        const denied = e.status === 401 || e.status === 403;
        setConnected(false, denied ? '控制台拒绝了当前页面的访问，请重新打开总控台。' : '');
      }
    } finally {
      clearTimeout(timeout);
      if (pollController === controller) pollController = null;
    }
  })();
  pollPromise = run.finally(() => { pollPromise = null; });
  return pollPromise;
}

function schedulePoll(delay = POLL_INTERVAL_MS) {
  clearTimeout(pollTimer);
  pollTimer = null;
  if (document.hidden) return;
  pollTimer = setTimeout(async () => {
    await poll();
    schedulePoll();
  }, delay);
}

window.__poll = () => poll(true);   // 模块间共享轮询入口
document.addEventListener('visibilitychange', () => {
  if (document.hidden) {
    suspendPortDiscovery();
    resetFeedBaseline();
    clearTimeout(pollTimer);
    pollTimer = null;
    if (pollController) pollController.abort();
    return;
  }
  poll(true).finally(() => schedulePoll());
});

const HEALTH_COMPONENT_NAMES = {
  services: '服务监控',
  watched: '关注进程',
  apps: '启动台',
  version: '版本',
  config: '配置',
};
function stateHealthNotice(data) {
  if (!data) return '';
  const health = data.configHealth || {};
  const messages = [];
  if (data.degraded) {
    const components = [...new Set((data.degradedReasons || [])
      .map(item => HEALTH_COMPONENT_NAMES[item && item.component] || '部分组件'))];
    messages.push('降级运行：' + (components.length ? components.join('、') : '部分组件') +
      '数据可能不完整');
  }
  if (health.writable === false) {
    messages.push('配置处于只读保护，修改不会保存');
  } else if (health.recoveredFromBackup) {
    messages.push('配置已从备份恢复，请核对内容');
  }
  if (health.migratedFromSchema != null) {
    messages.push('配置已从旧版本升级');
  }
  return messages.length ? messages.join('；') + '。' : '';
}
function setConnected(ok, message = '') {
  if (!ok) {
    if (!state.restartingFrom && !state.stopping) {
      banner.textContent = message || DISCONNECTED_TEXT;
    }
    banner.classList.add('show');
    banner.setAttribute('aria-hidden', 'false');
    return;
  }
  if (state.restartingFrom || state.stopping) return;
  const notice = stateHealthNotice(state.data);
  const authNotice = controlTokenAvailable() ? '' : CONTROL_READONLY_TEXT;
  const combinedNotice = [authNotice, notice].filter(Boolean).join(' ');
  banner.textContent = combinedNotice || DISCONNECTED_TEXT;
  banner.classList.toggle('show', !!combinedNotice);
  banner.setAttribute('aria-hidden', String(!combinedNotice));
}
function render() {
  if (!state.data) return;
  const consolePid = Number(state.data.consolePid);
  const restartSupported = Number.isInteger(consolePid) && consolePid > 0;
  setText(consolePortLabel, state.data.consolePort ? ':' + state.data.consolePort : ':----');
  setText(restartConsoleLabel, state.restartingFrom
    ? '重启中' : restartSupported ? '重启' : '启用');
  setText(stopConsoleLabel, state.stopping ? '停止中' : '停止');
  restartConsoleBtn.disabled = !!state.restartingFrom || state.stopping;
  stopConsoleBtn.disabled = !!state.restartingFrom || state.stopping;
  restartConsoleBtn.classList.toggle('needs-activation', !restartSupported);
  restartConsoleBtn.classList.toggle('restarting', !!state.restartingFrom);
  restartConsoleBtn.setAttribute('aria-label', restartSupported ? '重启总控台' : '启用一键重启');
  restartConsoleBtn.title = restartSupported
    ? '重启总控台 · PID ' + consolePid +
      (state.data.consoleCwd ? ' · ' + state.data.consoleCwd : '')
    : '当前是旧版后台，点击查看启用方法';
  /* 侧栏计数：启动台 = 运行中应用数；服务监控 = 我的服务数 */
  const apps = state.data.apps || [];
  const runningApps = apps.filter(a => a.running).length;
  const mineCount = (state.data.services || [])
    .filter(s => s.group === 'mine' && !s.hidden).length;
  setText(navCountLaunch, runningApps ? String(runningApps) : '');
  setText(navCountSvc, mineCount ? String(mineCount) : '');
  setText(sideStats, '运行 ' + runningApps + ' · 服务 ' + mineCount +
    (state.data.consolePort ? ' · :' + state.data.consolePort : ''));
  applyUiTheme(currentUiTheme());
  renderLaunchpad(state.data.apps || [], firstRender);
  renderServices(state.data, firstRender);
  renderWidgets(state.data);
  firstRender = false;
}

function showConsoleActivationInfo(action) {
  openConfirm({
    title: '先启用后台控制',
    bodyHtml: '当前 <b>' + escapeHtml(consolePortLabel.textContent || '总控台') +
      '</b> 是修改前启动的旧后台，所以页面还不能直接' + escapeHtml(action) + '。' +
      '<div class="confirm-detail">请双击项目里的 <b>总控台.app</b>，在弹窗中选择“重新启动”。只需做这一次；以后就能直接在页面里重启或停止。</div>',
    okText: '知道了',
    tone: 'primary',
    onOk: () => {},
  });
}

restartConsoleBtn.addEventListener('click', () => {
  const consolePid = Number(state.data && state.data.consolePid);
  if (state.restartingFrom) return;
  if (!Number.isInteger(consolePid) || consolePid <= 0) {
    showConsoleActivationInfo('重启');
    return;
  }
  openConfirm({
    title: '重启总控台',
    bodyHtml: '确定要重启总控台吗？' +
      '<div class="confirm-detail">页面会自动重连；启动台里正在运行的应用不会停止。</div>',
    okText: '重新启动',
    tone: 'primary',
    onOk: async () => {
      suspendPortDiscovery();
      state.restartingFrom = consolePid;
      banner.textContent = '总控台正在重新启动，页面会自动恢复…';
      banner.classList.add('show');
      banner.setAttribute('aria-hidden', 'false');
      render();
      const r = await act(post('/api/console/restart'));
      if (!r || r.ok === false) {
        clearTimeout(restartDeadlineTimer);
        restartDeadlineTimer = null;
        state.restartingFrom = null;
        setConnected(true);
        render();
        return;
      }
      clearTimeout(restartDeadlineTimer);
      restartDeadlineTimer = setTimeout(() => {
        if (!state.restartingFrom) return;
        state.restartingFrom = null;
        setConnected(false, '总控台重启超时，请双击“总控台.app”重新打开。');
        render();
      }, 25000);
    },
  });
});

stopConsoleBtn.addEventListener('click', () => {
  const consolePid = Number(state.data && state.data.consolePid);
  if (state.restartingFrom || state.stopping) return;
  if (!Number.isInteger(consolePid) || consolePid <= 0) {
    showConsoleActivationInfo('停止');
    return;
  }
  openConfirm({
    title: '停止总控台',
    bodyHtml: '确定要停止总控台吗？' +
      '<div class="confirm-detail">当前页面会断开；启动台里已经运行的应用不会被停止。再次使用时，双击“总控台.app”即可。</div>',
    okText: '停止运行',
    onOk: async () => {
      state.stopping = true;
      banner.textContent = '总控台正在停止…再次启动请双击“总控台.app”。';
      banner.classList.add('show');
      banner.setAttribute('aria-hidden', 'false');
      render();
      const r = await act(post('/api/console/stop'));
      if (!r || r.ok === false) {
        state.stopping = false;
        setConnected(true);
        render();
        return;
      }
      banner.textContent = '总控台已停止。再次启动请双击“总控台.app”。';
    },
  });
});

/* ============================================================
   命令面板（Ctrl/⌘ + K）
   ============================================================ */
const paletteMask = $('#paletteMask'), paletteInput = $('#paletteInput');
const paletteList = $('#paletteList');
let paletteSel = 0;
let paletteItems = [];

function appPortHint(app) {
  const configured = configuredPort(app);
  const actual = actualPorts(app);
  if (app && app.running && configured && app.listening === false && actual.length) {
    return ':' + actual[0] + '（实际）';
  }
  const port = configured || actual[0];
  return port ? ':' + port : '服务';
}
/* 与 portIsOpenable 语义一致：仅运行中且确实存在可用端口时可打开。 */
function openableAppPort(app) {
  return app && app.running && portIsOpenable(app)
    ? preferredOpenPort(app) : null;
}

function paletteActions() {
  const items = [
    {
      icon: 'plus',
      title: '添加服务',
      hint: '启动台 · 选择项目',
      run: () => {
        switchView('launchpad');
        openAppModal(null, 'service');
      },
    },
    {
      icon: 'file-text',
      title: '添加批处理任务',
      hint: '启动台 · 选择脚本',
      run: () => {
        switchView('launchpad');
        openAppModal(null, 'task');
      },
    },
  ];
  const apps = (state.data && state.data.apps) || [];
  for (const a of apps) {
    const running = !!a.running;
    const isTask = (a.kind || 'service') === 'task';
    const port = openableAppPort(a);
    const name = a.name || '未命名';
    items.push({
      icon: running ? 'square' : 'play',
      title: (running ? (isTask ? '中止 ' : '停止 ')
        : (isTask ? '运行 ' : '启动 ')) + name,
      hint: isTask ? '任务' : appPortHint(a),
      on: running,
      run: () => toggleApp(a.id),
    });
    if (running && !isTask) {
      items.push({
        icon: 'refresh-cw', title: '重启 ' + name, hint: '重新启动', on: true,
        run: () => act(post('/api/apps/' + a.id + '/restart', {})),
      });
    }
    if (running && port) {
      items.push({
        icon: 'arrow-up-right', title: '打开 ' + name, hint: ':' + port, on: true,
        run: () => window.open(localServiceUrl(a, port), '_blank', 'noopener,noreferrer'),
      });
    }
    items.push({ icon: 'file-text', title: '查看日志 · ' + name, hint: '日志', on: running, run: () => openLogs(a) });
    items.push({ icon: 'pencil', title: '编辑 ' + name, hint: '', on: running, run: () => openAppModal(a) });
  }
  items.push({ icon: 'layout-grid', title: '切换到启动台', hint: '视图', run: () => switchView('launchpad') });
  items.push({ icon: 'activity', title: '切换到服务监控', hint: '视图', run: () => switchView('services') });
  items.push({
    icon: 'file-text',
    title: '打开日志中心',
    hint: '日志 · ' + MOD_KEY + 'J',
    run: openLogsCenter,
  });
  items.push({
    icon: 'settings',
    title: '打开设置中心',
    hint: '通知 · 外观 · 版本',
    run: openSettingsCenter,
  });
  const notifyOn = taskNotificationsEnabled();
  items.push({
    icon: 'clock',
    title: '任务完成通知：' + (notifyOn ? '开' : '关'),
    hint: '系统通知 · 页面切走后也能收到',
    on: notifyOn,
    run: toggleTaskNotifications,
  });
  items.push({
    icon: 'terminal',
    title: '总控台日志',
    hint: '系统 · data/logs/console.log',
    run: openConsoleLog,
  });
  return items;
}

function paletteFiltered() {
  const q = paletteInput.value.trim().toLowerCase();
  if (!q) return paletteItems;
  return paletteItems.filter(it => (it.title + ' ' + (it.hint || '')).toLowerCase().includes(q));
}

function renderPalette() {
  const items = paletteFiltered();
  paletteSel = Math.max(0, Math.min(paletteSel, items.length - 1));
  paletteList.replaceChildren();
  if (!items.length) {
    const empty = el('div', 'palette-empty');
    empty.textContent = '没有匹配的结果';
    paletteList.appendChild(empty);
    paletteInput.removeAttribute('aria-activedescendant');
    return;
  }
  items.forEach((it, i) => {
    const row = el('button', 'pi' + (i === paletteSel ? ' sel' : ''));
    row.type = 'button';
    /* 焦点停留在 combobox，由 aria-activedescendant 表示当前选项。 */
    row.tabIndex = -1;
    row.setAttribute('role', 'option');
    row.id = 'palette-option-' + i;
    row.setAttribute('aria-selected', String(i === paletteSel));
    row.appendChild(el('span', 'pi-dot' + (it.on ? ' on' : '')));
    row.appendChild(icon(it.icon, 15));
    const t = el('span', 'pi-title');
    t.textContent = it.title;
    row.appendChild(t);
    if (it.hint) {
      const h = el('span', 'pi-hint');
      h.textContent = it.hint;
      row.appendChild(h);
    }
    row.addEventListener('click', () => execPalette(it));
    row.addEventListener('mousemove', () => {
      if (paletteSel !== i) { paletteSel = i; syncPaletteSel(); }
    });
    it._row = row;
    paletteList.appendChild(row);
  });
  const selRow = items[paletteSel] && items[paletteSel]._row;
  if (selRow) {
    paletteInput.setAttribute('aria-activedescendant', selRow.id);
    selRow.scrollIntoView({ block: 'nearest' });
  }
}

function syncPaletteSel() {
  const items = paletteFiltered();
  items.forEach((it, i) => {
    if (!it._row) return;
    const selected = i === paletteSel;
    it._row.classList.toggle('sel', selected);
    it._row.setAttribute('aria-selected', String(selected));
  });
  const selected = items[paletteSel] && items[paletteSel]._row;
  if (selected) paletteInput.setAttribute('aria-activedescendant', selected.id);
  else paletteInput.removeAttribute('aria-activedescendant');
}

function openPalette() {
  paletteItems = paletteActions();
  paletteSel = 0;
  paletteInput.value = '';
  renderPalette();
  paletteInput.setAttribute('aria-expanded', 'true');
  openLayer(paletteMask, paletteInput);
}
function closePalette() {
  paletteInput.setAttribute('aria-expanded', 'false');
  paletteInput.removeAttribute('aria-activedescendant');
  closeLayer(paletteMask);
}
function execPalette(it) {
  closePalette();
  Promise.resolve(it.run()).catch(e => toast('操作失败：' + e.message));
}

cmdkTrigger.addEventListener('click', openPalette);
paletteInput.addEventListener('input', () => { paletteSel = 0; renderPalette(); });
paletteInput.addEventListener('keydown', e => {
  const items = paletteFiltered();
  if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
    e.preventDefault();
    if (!items.length) return;
    paletteSel = (paletteSel + (e.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length;
    syncPaletteSel();
    const row = items[paletteSel] && items[paletteSel]._row;
    if (row) row.scrollIntoView({ block: 'nearest' });
  } else if (e.key === 'Enter') {
    e.preventDefault();
    const it = items[paletteSel];
    if (it) execPalette(it);
  }
});
paletteMask.addEventListener('mousedown', e => { if (e.target === paletteMask) closePalette(); });

/* Ctrl/⌘ + K 呼出命令面板 */
document.addEventListener('keydown', e => {
  if ((e.metaKey || e.ctrlKey) && !e.shiftKey && !e.altKey && e.key.toLowerCase() === 'k') {
    e.preventDefault();
    if (paletteMask.classList.contains('open')) closePalette();
    else if (!activeLayer()) openPalette();
  }
});
/* Ctrl/⌘ + J 呼出日志中心（Ctrl/⌘ + L 是浏览器地址栏保留键，无法拦截） */
document.addEventListener('keydown', e => {
  if ((e.metaKey || e.ctrlKey) && !e.shiftKey && !e.altKey && e.key.toLowerCase() === 'j') {
    e.preventDefault();
    if ($('#logsMask').classList.contains('open')) closeLogsCenter();
    else if (!activeLayer()) openLogsCenter();
  }
});
window.__openPalette = openPalette;   // hero 卡等跨模块入口

/* Esc 逐层关闭浮层 */
document.addEventListener('keydown', e => {
  trapLayerFocus(e);
  if (e.key === 'Escape') {
    if ($('#confirmMask').classList.contains('open')) closeConfirm();
    else if ($('#logsMask').classList.contains('open')) closeLogsCenter();
    else if ($('#settingsMask').classList.contains('open')) closeSettingsCenter();
    else if ($('#portDiagMask').classList.contains('open')) closePortDiagnostic();
    else if ($('#appDiagMask').classList.contains('open')) closeAppDiagnosis();
    else if ($('#appModalMask').classList.contains('open')) closeAppModal();
    else if (paletteMask.classList.contains('open')) closePalette();
    else if ($('#logDrawer').classList.contains('open')) closeLogs();
  }
});

/* ============================================================
   初始化
   ============================================================ */
setChildren(restartConsoleIcon, icon('refresh-cw', 14));
setChildren(stopConsoleIcon, icon('power', 14));
setChildren($('#githubLink'), icon('github', 15));
setChildren($('#navIconLaunch'), icon('layout-grid', 15));
setChildren($('#navIconSvc'), icon('activity', 15));
setChildren($('#railIconLaunch'), icon('rocket', 19));
setChildren($('#railIconSvc'), icon('activity', 19));
setChildren($('#cmdkIcon'), icon('search', 14));
setChildren($('#paletteIcon'), icon('search', 15));
buildGlyphGrid();
applyModKeys();
initAppModal({ onAddService: $('#addSvcCard'), onAddTask: $('#addTaskCard') });
initLogDrawer();
initThemeToggle();
initWidgets();
applyTheme();
applyUiTheme(currentUiTheme());
applyView();
poll(true).finally(() => schedulePoll());
