'use strict';
/* ============================================================
   widgets.js — 右侧信息栏与导航轨
   实时动态/实时告警（会话内状态差异事件流，首帧静默建立基线）、
   端口/资源 TOP 5、小贴士、快捷操作、导航轨连接状态与版本。
   全部数据来自 /api/state 轮询快照，不新增后端接口。
   ============================================================ */
import { $, el, setText, setChildren, icon, state, fmtClock, taskExitStatus,
  openLayer, closeLayer, act, post, toast, escapeHtml, applyTheme,
  taskNotificationsEnabled, toggleTaskNotifications, MOD_KEY } from './core.js';
import { openAppModal, openLogs, openConsoleLog, openConfirm } from './overlays.js';
import { configuredPort } from './ports.js';

const FEED_CAP = 50;
let feedSeq = 0;
let feedEvents = [];
let prevSnap = null;              // 上一份用于差异对比的快照

/* 断线、页面转入后台或总控台重启后由 app.js 调用：
   丢弃旧基线，下一份快照只重建基线，避免把断档期积压的变化
   一次性当作“刚刚发生”的事件灌进实时动态/告警。 */
export function resetFeedBaseline() {
  prevSnap = null;
}

const feedListL = $('#feedListL'), feedListS = $('#feedListS');
const topPortsL = $('#topPortsL'), topPortsS = $('#topPortsS');
const topResS = $('#topResS'), resTabs = $('#resTabs');
const tipsText = $('#tipsText'), tipsAction = $('#tipsAction');
const railConnDot = $('#railConnDot'), railConnText = $('#railConnText');
const railVer = $('#railVer');
let resMetric = 'cpu';

/* ---------------- 静态装饰图标与快捷操作 ---------------- */
export function initWidgets() {
  document.querySelectorAll('[data-ov-icon]').forEach(node => {
    setChildren(node, icon(node.dataset.ovIcon, 17));
  });
  document.querySelectorAll('[data-qa-icon]').forEach(node => {
    setChildren(node, icon(node.dataset.qaIcon, 13));
  });
  setChildren($('#tipsIcon'), icon('brain', 14));

  /* 顶栏与侧栏的快捷操作统一走 data-qa 代理 */
  document.addEventListener('click', e => {
    const btn = e.target.closest('[data-qa]');
    if (!btn) return;
    const action = btn.dataset.qa;
    if (action === 'add-svc') openAppModal(null, 'service');
    else if (action === 'add-task') openAppModal(null, 'task');
    else if (action === 'refresh' && window.__poll) window.__poll();
    else if (action === 'logs') openLogsCenter();
    else if (action === 'settings') openSettingsCenter();
    else if (action === 'batch-stop') batchStopApps();
  });
  /* 导航轨动作按钮（非视图切换） */
  document.querySelectorAll('.rail-btn[data-action]').forEach(btn => {
    btn.addEventListener('click', () => {
      if (btn.dataset.action === 'logs') openLogsCenter();
      else if (btn.dataset.action === 'settings') openSettingsCenter();
    });
  });
  setChildren($('#railIconLogs'), icon('file-text', 19));
  setChildren($('#railIconSettings'), icon('settings', 19));
  $('#logsMaskClose').addEventListener('click', closeLogsCenter);
  $('#logsMask').addEventListener('mousedown', e => {
    if (e.target === $('#logsMask')) closeLogsCenter();
  });
  $('#settingsMaskClose').addEventListener('click', closeSettingsCenter);
  $('#settingsMask').addEventListener('mousedown', e => {
    if (e.target === $('#settingsMask')) closeSettingsCenter();
  });
  $('#setNotify').addEventListener('click', () => {
    toggleTaskNotifications();
    syncSettings();
  });
  $('#setAppearance').addEventListener('click', e => {
    const tab = e.target.closest('.mini-tab');
    if (!tab) return;
    const mode = tab.dataset.appearance;
    if (mode === 'auto') localStorage.removeItem('console-theme');
    else localStorage.setItem('console-theme', mode);
    applyTheme();
    syncSettings();
  });

  $('#feedClearL').addEventListener('click', clearFeed);
  $('#feedClearS').addEventListener('click', clearFeed);
  resTabs.addEventListener('click', e => {
    const tab = e.target.closest('.mini-tab');
    if (!tab) return;
    resMetric = tab.dataset.metric === 'mem' ? 'mem' : 'cpu';
    for (const t of resTabs.querySelectorAll('.mini-tab')) {
      t.classList.toggle('active', t === tab);
    }
    if (state.data) renderTopRes(state.data);
  });

  /* 导航轨连接状态跟随断连横幅（banner 是唯一连接状态出口） */
  const banner = $('#banner');
  const syncConn = () => {
    const down = banner.classList.contains('show');
    railConnDot.classList.toggle('running', !down);
    railConnDot.classList.toggle('danger', down);
    setText(railConnText, down ? '连接中断' : '已连接');
  };
  new MutationObserver(syncConn)
    .observe(banner, { attributes: true, attributeFilter: ['class'] });
  syncConn();

  tipsAction.addEventListener('click', () => {
    const tab = $('#tab-services');
    if (tab) tab.click();
  });
}

/* ---------------- 实时动态 / 实时告警 ----------------
   对比相邻两份轮询快照产生事件；首份快照只建立基线，
   断线/后台恢复后同样静默重建，避免把存量当新闻。 */
function snapshotMaps(data) {
  const apps = new Map();
  for (const a of data.apps || []) {
    apps.set(a.id, {
      name: a.name || '未命名',
      kind: a.kind || 'service',
      running: !!a.running,
      port: configuredPort(a),
      occupied: !!a.portOccupied,
      exitAt: a.lastExit && a.lastExit.at ? a.lastExit.at : 0,
      exit: a.lastExit || null,
    });
  }
  const services = new Map();
  for (const s of data.services || []) {
    const key = s.instanceKey || s.key;
    if (!key) continue;
    services.set(key, {
      name: s.appName || s.project || s.name || '本地服务',
      port: s.port,
      mine: s.group === 'mine' && !s.hidden,
      linked: !!s.appId,   // 已关联启动台卡片的服务由应用事件覆盖，不重复上报
    });
  }
  return { apps, services, degraded: !!data.degraded };
}

function pushEvent(level, title, sub) {
  feedEvents.unshift({ seq: ++feedSeq, at: new Date(), level, title, sub });
  if (feedEvents.length > FEED_CAP) feedEvents.length = FEED_CAP;
}

function diffSnapshot(prev, next) {
  for (const [id, app] of next.apps) {
    const before = prev.apps.get(id);
    if (!before) continue;    // 新建卡片不算动态
    if (!before.running && app.running) {
      pushEvent('info', app.name + (app.kind === 'task' ? ' 开始运行' : ' 服务已启动'),
        app.port ? ':' + app.port : '');
    } else if (before.running && !app.running) {
      pushEvent('info', app.name + (app.kind === 'task' ? ' 运行结束' : ' 已停止'),
        app.port ? ':' + app.port : '');
    }
    if (!before.occupied && app.occupied) {
      pushEvent('warn', '端口冲突', app.name + (app.port ? ' :' + app.port + ' 被占用' : ''));
    }
    if (app.exitAt && app.exitAt !== before.exitAt && app.exit) {
      const status = taskExitStatus(app.exit);
      if (app.kind === 'task') {
        if (status === 'succeeded') pushEvent('ok', app.name + ' 任务执行成功', '');
        else if (status === 'failed') {
          pushEvent('error', app.name + ' 任务执行失败',
            app.exit.code != null ? '退出码 ' + app.exit.code : '');
        } else if (status === 'canceled') pushEvent('warn', app.name + ' 任务已取消', '');
        else pushEvent('warn', app.name + ' 任务已中止', '');
      } else if (app.exit.code) {
        pushEvent('error', app.name + ' 异常退出', '退出码 ' + app.exit.code);
      }
    }
  }
  for (const [key, svc] of next.services) {
    if (!prev.services.has(key) && svc.mine && !svc.linked) {
      pushEvent('info', svc.name + ' 服务已启动', svc.port ? ':' + svc.port : '');
    }
  }
  for (const [key, svc] of prev.services) {
    if (!next.services.has(key) && svc.mine && !svc.linked) {
      pushEvent('info', svc.name + ' 服务已停止', svc.port ? ':' + svc.port : '');
    }
  }
  if (!prev.degraded && next.degraded) {
    pushEvent('error', '总控台进入降级模式', '部分数据可能不完整');
  }
}

function feedItem(ev) {
  const item = el('div', 'feed-item');
  const dot = el('span', 'feed-dot lvl-' + ev.level);
  dot.setAttribute('aria-hidden', 'true');
  const main = el('div', 'feed-main');
  const title = el('div', 'feed-title');
  title.textContent = ev.title;
  main.appendChild(title);
  if (ev.sub) {
    const sub = el('div', 'feed-sub');
    sub.textContent = ev.sub;
    main.appendChild(sub);
  }
  const time = el('span', 'feed-time mono');
  time.textContent = fmtClock(ev.at).slice(0, 5);
  item.append(dot, main, time);
  return item;
}

function renderFeedInto(list, events, emptyText) {
  list.replaceChildren();
  if (!events.length) {
    const empty = el('div', 'feed-empty');
    empty.textContent = emptyText;
    list.appendChild(empty);
    return;
  }
  for (const ev of events.slice(0, 12)) list.appendChild(feedItem(ev));
}

function renderFeeds() {
  renderFeedInto(feedListL, feedEvents, '暂无动态；启动、停止与端口事件会显示在这里');
  renderFeedInto(feedListS,
    feedEvents.filter(ev => ev.level === 'warn' || ev.level === 'error'),
    '运行良好，暂无告警');
}

function clearFeed() {
  feedEvents = [];
  renderFeeds();
}

/* ---------------- TOP 5 ---------------- */
function mineServices(data) {
  return (data.services || []).filter(s => s.group === 'mine' && !s.hidden);
}

function renderTopPortsInto(container, data) {
  const apps = data.apps || [];
  const rows = mineServices(data)
    .filter(s => Number.isInteger(s.port))
    .sort((a, b) => a.port - b.port)
    .slice(0, 5);
  container.replaceChildren();
  if (!rows.length) {
    const empty = el('div', 't5-empty');
    empty.textContent = '暂无监听端口';
    container.appendChild(empty);
    return;
  }
  rows.forEach((svc, i) => {
    const row = el('div', 't5-row');
    const rank = el('span', 't5-rank');
    rank.textContent = String(i + 1);
    const port = el('span', 't5-port');
    port.textContent = ':' + svc.port;
    const name = el('span', 't5-name');
    name.textContent = svc.appName || svc.project || svc.name || '本地服务';
    name.title = name.textContent;
    row.append(rank, port, name);
    const conflict = apps.some(a => a.portOccupied && configuredPort(a) === svc.port);
    if (conflict) {
      const tag = el('span', 't5-tag');
      tag.textContent = '冲突';
      row.appendChild(tag);
    }
    container.appendChild(row);
  });
}

function renderTopRes(data) {
  const rows = mineServices(data)
    .slice()
    .sort((a, b) => (b[resMetric] || 0) - (a[resMetric] || 0))
    .slice(0, 5);
  topResS.replaceChildren();
  if (!rows.length) {
    const empty = el('div', 't5-empty');
    empty.textContent = '暂无服务进程';
    topResS.appendChild(empty);
    return;
  }
  rows.forEach((svc, i) => {
    const row = el('div', 't5-row');
    const rank = el('span', 't5-rank');
    rank.textContent = String(i + 1);
    const name = el('span', 't5-name');
    name.textContent = svc.appName || svc.project || svc.name || '本地服务';
    name.title = name.textContent;
    const val = el('span', 't5-val');
    const pct = typeof svc[resMetric] === 'number' ? svc[resMetric] : 0;
    val.textContent = pct.toFixed(1) + '%';
    const bar = el('span', 't5-bar');
    const fill = el('i');
    fill.style.width = Math.max(2, Math.min(100, pct)) + '%';
    bar.appendChild(fill);
    row.append(rank, name, bar, val);
    topResS.appendChild(row);
  });
}

/* ---------------- 小贴士 ---------------- */
function renderTips(data) {
  const conflicts = (data.apps || []).filter(a => a.portOccupied).length;
  let text;
  let actionable = false;
  if (conflicts > 0) {
    text = '检测到 ' + conflicts + ' 个端口冲突，建议尽快处理以避免服务异常。';
    actionable = true;
  } else if (data.degraded) {
    text = '当前处于降级模式，部分组件数据可能不完整；可尝试重启总控台恢复。';
  } else {
    text = '所有服务运行正常。小技巧：按 ' + MOD_KEY + 'K 打开命令面板，可以快速启动、停止任意应用。';
  }
  setText(tipsText, text);
  tipsAction.hidden = !actionable;
}

/* ---------------- 主入口（每轮轮询调用） ---------------- */
export function renderWidgets(data) {
  if (!data) return;
  const next = snapshotMaps(data);
  if (prevSnap) diffSnapshot(prevSnap, next);
  prevSnap = next;
  renderFeeds();
  renderTopPortsInto(topPortsL, data);
  renderTopPortsInto(topPortsS, data);
  renderTopRes(data);
  renderTips(data);
  setText(railVer, data.version ? 'v' + data.version : 'v—');
}

/* ============================================================
   日志中心（聚合弹层，Ctrl/⌘ + J）：所有应用与总控台日志的目录页
   ============================================================ */
const logsMask = $('#logsMask'), logsList = $('#logsList');

function logsRow(app) {
  const row = el('button', 'logs-item');
  row.type = 'button';
  const box = el('span', 'logs-ic');
  if (app.icon) {
    const img = new Image();
    img.src = app.icon;
    img.alt = '';
    box.appendChild(img);
  } else if (app.glyph && window.LUCIDE && window.LUCIDE[app.glyph]) {
    box.appendChild(icon(app.glyph, 14));
  } else {
    box.textContent = app.name ? [...app.name][0].toUpperCase() : '?';
  }
  const main = el('span', 'logs-main');
  const name = el('span', 'logs-name');
  name.textContent = app.name || '未命名';
  const sub = el('span', 'logs-sub');
  const isTask = (app.kind || 'service') === 'task';
  const port = configuredPort(app);
  sub.textContent = (app.running ? '运行中' : '已停止') +
    (isTask ? ' · 任务' : port ? ' · :' + port : '');
  main.append(name, sub);
  row.append(box, main, icon('chevron-right', 14));
  row.addEventListener('click', () => {
    closeLogsCenter();
    openLogs(app);
  });
  return row;
}

function renderLogsList() {
  logsList.replaceChildren();
  const apps = (state.data && state.data.apps) || [];
  const sorted = apps.slice().sort((a, b) => (!!b.running) - (!!a.running));
  for (const app of sorted) logsList.appendChild(logsRow(app));
  /* 总控台自身日志固定在最后 */
  const row = el('button', 'logs-item');
  row.type = 'button';
  const box = el('span', 'logs-ic');
  box.appendChild(icon('terminal', 14));
  const main = el('span', 'logs-main');
  const name = el('span', 'logs-name');
  name.textContent = '总控台日志';
  const sub = el('span', 'logs-sub');
  sub.textContent = '系统 · console.log';
  main.append(name, sub);
  row.append(box, main, icon('chevron-right', 14));
  row.addEventListener('click', () => {
    closeLogsCenter();
    openConsoleLog();
  });
  logsList.appendChild(row);
  if (!apps.length) {
    const empty = el('div', 'logs-empty');
    empty.textContent = '启动台还没有应用；上方是总控台自身日志';
    logsList.prepend(empty);
  }
}

export function openLogsCenter() {
  renderLogsList();
  openLayer(logsMask, $('#logsMaskClose'));
}
export function closeLogsCenter() { closeLayer(logsMask); }

/* ============================================================
   设置中心（聚合弹层）：通知开关 / 外观 / 版本与目录信息
   ============================================================ */
const settingsMask = $('#settingsMask');

function syncSettings() {
  const on = taskNotificationsEnabled();
  const sw = $('#setNotify');
  sw.classList.toggle('on', on);
  sw.setAttribute('aria-checked', String(on));
  const stored = localStorage.getItem('console-theme');
  const mode = stored === 'dark' ? 'dark' : stored === 'light' ? 'light' : 'auto';
  for (const tab of $('#setAppearance').querySelectorAll('.mini-tab')) {
    tab.classList.toggle('active', tab.dataset.appearance === mode);
  }
  const d = state.data || {};
  setText($('#setVersion'), d.version ? 'v' + d.version : '—');
  setText($('#setPort'), d.consolePort ? ':' + d.consolePort : '—');
  setText($('#setCwd'), d.consoleCwd || '—');
}

export function openSettingsCenter() {
  syncSettings();
  openLayer(settingsMask, $('#settingsMaskClose'));
}
export function closeSettingsCenter() { closeLayer(settingsMask); }

/* ============================================================
   批量停止服务：确认后逐个走安全停止，绝不按端口结束进程
   ============================================================ */
function batchStopApps() {
  const running = ((state.data && state.data.apps) || []).filter(a => a.running);
  if (!running.length) {
    toast('当前没有运行中的应用');
    return;
  }
  const names = running.map(a => a.name || '未命名').join('、');
  openConfirm({
    title: '批量停止服务',
    bodyHtml: '确定要停止全部 <b>' + running.length + '</b> 个运行中的应用吗？' +
      '<div class="confirm-detail">' + escapeHtml(names) +
      '。将逐个安全停止，不会按端口结束其他进程。</div>',
    okText: '全部停止',
    tone: 'danger',
    onOk: async () => {
      let stopped = 0;
      for (const app of running) {
        const result = await act(post('/api/apps/' + app.id + '/stop', {}));
        if (result && result.ok !== false) stopped += 1;
      }
      toast('已停止 ' + stopped + ' 个应用');
      if (window.__poll) window.__poll();
    },
  });
}
