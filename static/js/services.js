'use strict';
/* ============================================================
   services.js — 服务监控：表格行 / 关注进程 / 折叠分区
   ============================================================ */
import { $, el, setText, setChildren, setKpi, setKpiUnit, icon, iconBtn,
  post, act, toast, reconcile, state, truncateMiddle, shortHome,
  fmtUptime, fmtPct, fmtClock, localServiceUrl } from './core.js';
import { confirmKill, openAppModal } from './overlays.js';
import { configuredPort, actualPorts } from './ports.js';

const mineList = $('#mineList'), mineEmpty = $('#mineEmpty');
const bgHeader = $('#bgHeader'), bgBody = $('#bgBody'), bgList = $('#bgList');
const bgEmpty = $('#bgEmpty'), bgCount = $('#bgCount');
const hiddenPanel = $('#hiddenPanel'), hiddenHeader = $('#hiddenHeader');
const hiddenBody = $('#hiddenBody'), hiddenList = $('#hiddenList'), hiddenCount = $('#hiddenCount');
const watchChips = $('#watchChips'), watchInput = $('#watchInput');
const watchList = $('#watchList'), watchEmpty = $('#watchEmpty'), watchEmptyText = $('#watchEmptyText');
const statMine = $('#statMine'), statBg = $('#statBg'), statTime = $('#statTime');
const statCpu = $('#statCpu'), statMem = $('#statMem'), statWarn = $('#statWarn');
const portDiscovery = $('#portDiscovery'), portDiscoveryList = $('#portDiscoveryList');
const portDiscoveryCount = $('#portDiscoveryCount'), portDiscoveryIcon = $('#portDiscoveryIcon');
let serviceCommandId = 0;

function findSvc(key) {
  return ((state.data && state.data.services) || [])
    .find(s => serviceKey(s) === key);
}
function findWatch(key) { return ((state.data && state.data.watched) || []).find(w => String(w.pid) === key); }

/* ---------------- 本页会话内的新端口发现 ----------------
   seenKeys 故意不写入 localStorage：刷新页面后以当时的真实监听状态重新建立
   静默基线。只有用户明确选择的“忽略”或“加入启动台”才进入持久配置。 */
const discoverySeenKeys = new Set();
const discoveryItems = new Map();
let discoveryNeedsBaseline = true;

setChildren(portDiscoveryIcon, icon('activity', 17));

function serviceSnapshotHealthy(data) {
  return !(data.degradedReasons || [])
    .some(item => item && item.component === 'services');
}

function serviceKey(svc) {
  if (!svc) return '';
  if (typeof svc.instanceKey === 'string' && svc.instanceKey) return svc.instanceKey;
  return typeof svc.key === 'string' && svc.key ? svc.key : '';
}

function appKnownPorts(apps) {
  const ports = new Set();
  for (const app of apps || []) {
    if (!app || !app.running) continue;
    /* 停止的卡片只“配置”了端口，并不拥有后来出现的监听者。运行中的
       卡片也只在确实监听（或兼容旧后端未返回 listening）时排除配置端口。 */
    const configured = configuredPort(app);
    if (configured && app.listening !== false) ports.add(configured);
    /* 受管服务可能另外开启调试端口；actual ports 也必须排除。 */
    for (const port of actualPorts(app)) ports.add(port);
  }
  return ports;
}

function discoveryEligible(svc, knownPorts) {
  const port = Number(svc && svc.port);
  return !!svc
    && svc.group === 'mine'
    && !svc.hidden
    && !svc.pinned
    && !svc.promoted
    && Number.isInteger(port)
    && port > 0
    && !knownPorts.has(port);
}

export function suspendPortDiscovery() {
  discoveryNeedsBaseline = true;
}

export function observePortDiscovery(data) {
  if (!data || !serviceSnapshotHealthy(data)) {
    suspendPortDiscovery();
    return;
  }
  const services = Array.isArray(data.services) ? data.services : [];
  const current = new Map();
  for (const svc of services) {
    const key = serviceKey(svc);
    if (key) current.set(key, svc);
  }
  const knownPorts = appKnownPorts(data.apps || []);

  /* 断线、后台恢复和首次加载后的第一个健康快照只建立基线。 */
  if (discoveryNeedsBaseline) {
    for (const key of current.keys()) discoverySeenKeys.add(key);
    discoveryNeedsBaseline = false;
  } else {
    const fresh = [];
    for (const [key, svc] of current) {
      const unseen = !discoverySeenKeys.has(key);
      discoverySeenKeys.add(key);
      if (unseen && discoveryEligible(svc, knownPorts)) fresh.push(svc);
    }
    for (const svc of fresh) {
      discoveryItems.set(serviceKey(svc), { ...svc, present: true });
    }
    if (fresh.length) {
      const detail = fresh.length === 1
        ? '：' + (fresh[0].project || fresh[0].name || '本地服务') + ' :' + fresh[0].port
        : '，共 ' + fresh.length + ' 个';
      toast('发现新的监听端口' + detail + '，可在服务监控中处理', 5200);
    }
  }

  /* 发现栏只显示当前仍在监听且尚未处理的服务。测试服务器、热重载端口等
     短命监听一旦结束立即移除，避免累积成无法操作的“当前已停止”记录。 */
  for (const [key] of [...discoveryItems]) {
    const svc = current.get(key);
    if (svc && discoveryEligible(svc, knownPorts)) {
      discoveryItems.set(key, { ...svc, present: true });
    } else {
      discoveryItems.delete(key);
    }
  }
}

function openServiceAppModal(s) {
  if (!s) return;
  if (s.appId) {
    const linked = ((state.data && state.data.apps) || [])
      .find(app => app.id === s.appId);
    if (linked) openAppModal(linked);
    if (linked) return;
  }
  openAppModal({
    name: s.appName || s.project || s.name || '',
    command: s.cmd || '',
    cwd: s.cwd || null,
    port: s.port != null ? s.port : null,
    attachPid: s.pid,
    attachInstanceKey: s.instanceKey || null,
  });
}

async function setServiceFlag(key, flag, value) {
  const result = await act(post('/api/services/flag', { key, flag, value }));
  if (result && result.ok !== false) window.__poll();
  return result;
}

/* pinned 在前，其余按端口升序 */
function svcSort(a, b) {
  if (!!a.pinned !== !!b.pinned) return a.pinned ? -1 : 1;
  const pa = a.port == null ? Infinity : a.port;
  const pb = b.port == null ? Infinity : b.port;
  if (pa !== pb) return pa - pb;
  return (a.name || '').localeCompare(b.name || '');
}

/* ---------------- 负载单元格（标签 + 数值 + CPU 迷你条） ---------------- */
function loadCell() {
  const wrap = el('span', 'c-load');
  wrap.setAttribute('role', 'cell');
  const mk = (lab, withBar) => {
    const s = el('span', 'ld');
    const l = el('span', 'ld-lab');
    l.textContent = lab;
    const v = el('span', 'ld-v');
    s.append(l, v);
    let fill = null;
    if (withBar) {
      const bar = el('span', 'ld-bar');
      bar.setAttribute('aria-hidden', 'true');
      fill = el('i');
      bar.append(fill);
      s.append(bar);
    }
    return { s, v, fill };
  };
  const cpu = mk('CPU', true), mem = mk('MEM', false);
  wrap.append(cpu.s, mem.s);
  return { wrap, cpu: cpu.v, mem: mem.v, cpuFill: cpu.fill };
}

function createServiceRow(kind) {
  const row = el('div', 'tr');
  row.setAttribute('role', 'row');

  const cDot = el('span', 'c-dot');
  cDot.setAttribute('role', 'cell');
  cDot.setAttribute('aria-label', kind === 'background' ? '状态：后台应用' : '状态：运行中');
  /* 运行绿，后台灰 */
  const dot = el('span', 'status-dot' + (kind === 'background' ? '' : ' running'));
  dot.setAttribute('aria-hidden', 'true');
  cDot.append(dot);

  const cTitle = el('span', 'c-title');
  cTitle.setAttribute('role', 'cell');
  const tMain = el('span', 't-main');
  const mark = el('span', 'app-mark');
  mark.title = '来自启动台';
  mark.setAttribute('aria-hidden', 'true');
  mark.hidden = true;
  mark.appendChild(icon('link-2', 12));
  const name = el('span', 't-name');
  tMain.append(mark, name);
  const sub = el('span', 't-sub');
  const subIcon = el('span', 'origin-ic');
  subIcon.setAttribute('aria-hidden', 'true');
  subIcon.hidden = true;
  const subText = el('span', 't-sub-text');
  sub.append(subIcon, subText);
  cTitle.append(tMain, sub);

  const cPort = el('button', 'c-port');
  cPort.type = 'button';
  const cPortCell = el('span', 'c-port-cell');
  cPortCell.setAttribute('role', 'cell');
  cPortCell.append(cPort);
  const cPid = el('span', 'c-pid');
  cPid.setAttribute('role', 'cell');
  const cCwd = el('span', 'c-cwd');
  cCwd.setAttribute('role', 'cell');
  const load = loadCell();
  const cUp = el('span', 'c-up');
  cUp.setAttribute('role', 'cell');
  const cStatus = el('span', 'c-status');
  cStatus.setAttribute('role', 'cell');
  const statusPill = el('span', 'status-pill' + (kind === 'mine' ? ' on' : ''));
  statusPill.textContent = kind === 'mine' ? '运行中'
    : kind === 'background' ? '后台' : '已隐藏';
  cStatus.append(statusPill);
  const cAct = el('span', 'c-act');
  cAct.setAttribute('role', 'cell');
  cAct.setAttribute('aria-label', '操作');
  const cmdRow = el('div', 'tr-cmd');
  cmdRow.id = 'service-command-' + (++serviceCommandId);
  cmdRow.setAttribute('role', 'note');
  cmdRow.setAttribute('aria-label', '完整命令');
  const cmdCode = el('code');
  cmdRow.append(cmdCode);

  row.append(cDot, cTitle, cPid, cPortCell, cCwd, load.wrap, cUp, cStatus, cAct, cmdRow);
  row._r = { name, mark, subText, subIcon, pid: cPid, port: cPort, cwd: cCwd,
    ldCpu: load.cpu, ldMem: load.mem, ldCpuFill: load.cpuFill, up: cUp, cmdCode };

  const key = () => row.dataset.key;
  cPort.addEventListener('click', () => {
    const s = findSvc(key());
    if (s && s.port) {
      window.open(localServiceUrl(s, s.port), '_blank', 'noopener,noreferrer');
    }
  });

  const flag = (f, value) => {
    const svc = findSvc(key());
    return svc ? setServiceFlag(svc.key, f, value) : null;
  };

  if (kind === 'mine') {
    const bAdd = iconBtn('plus', '添加到启动台');
    bAdd.addEventListener('click', () => {
      const s = findSvc(key());
      openServiceAppModal(s);
    });
    const bDemote = iconBtn('download', '移回应用后台');
    bDemote.hidden = true;   // 仅被提升(promoted)的服务显示
    bDemote.addEventListener('click', () => flag('promoted', false));
    row._r.demote = bDemote;
    const bCmd = iconBtn('terminal', '展开完整命令');
    bCmd.setAttribute('aria-expanded', 'false');
    bCmd.setAttribute('aria-controls', cmdRow.id);
    bCmd.addEventListener('click', () => {
      row.classList.toggle('expanded');
      bCmd.classList.toggle('active', row.classList.contains('expanded'));
      bCmd.setAttribute('aria-expanded', String(row.classList.contains('expanded')));
    });
    row._r.pin = iconBtn('pin-off', '置顶 / 取消置顶');
    row._r.pin.addEventListener('click', async () => {
      const s = findSvc(key());
      if (s) flag('pinned', !s.pinned);
    });
    const bHide = iconBtn('eye-off', '移入已隐藏列表');
    bHide.addEventListener('click', () => flag('hidden', true));
    const bKill = iconBtn('power', '结束进程', 'danger');
    bKill.addEventListener('click', () => {
      const s = findSvc(key());
      if (s) confirmKill(s);
    });
    cAct.append(bAdd, bDemote, bCmd, row._r.pin, bHide, bKill);
    Object.assign(row._r, {
      add: bAdd, demote: bDemote, command: bCmd, hide: bHide, kill: bKill,
    });
  } else if (kind === 'background') {
    const bPromote = iconBtn('upload', '移到我的服务');
    bPromote.addEventListener('click', () => flag('promoted', true));
    const bKill = iconBtn('power', '结束进程', 'danger');
    bKill.addEventListener('click', () => {
      const s = findSvc(key());
      if (s) confirmKill(s);
    });
    cAct.append(bPromote, bKill);
    Object.assign(row._r, { promote: bPromote, kill: bKill });
  } else { // hidden
    const bUnhide = iconBtn('eye', '取消隐藏');
    bUnhide.addEventListener('click', () => flag('hidden', false));
    cAct.append(bUnhide);
    row._r.unhide = bUnhide;
  }
  return row;
}

function updateServiceRow(row, svc) {
  const r = row._r;
  /* 主标题：关联启动台应用 > 项目名 > 进程名；副标题：进程名 · PID */
  const title = svc.appName || svc.project || svc.name || '';
  setText(r.name, title);
  r.name.title = title;
  r.mark.hidden = !svc.appId;
  setText(r.subText, svc.name || '');
  /* 来源徽标：哪个应用/AI 助手启动了这个进程（尽力判断） */
  const origin = svc.origin && svc.origin.label ? svc.origin : null;
  if (origin) {
    if (r._originIcon !== origin.icon) {
      r._originIcon = origin.icon;
      setChildren(r.subIcon, icon(origin.icon || 'package', 10));
    }
    r.subIcon.hidden = false;
    r.subIcon.title = '由 ' + origin.label + ' 启动';
    setText(r.subText, (svc.name || '') + ' · ' + origin.label);
  } else {
    r._originIcon = null;
    r.subIcon.hidden = true;
    r.subIcon.removeAttribute('title');
  }
  setText(r.pid, svc.pid ? String(svc.pid) : '—');
  if (r.ldCpuFill) {
    const pct = typeof svc.cpu === 'number' && !isNaN(svc.cpu) ? svc.cpu : 0;
    // 后端 CPU 为「占全部核心百分比」（Windows 任务管理器口径）；
    // 迷你条按相对满核还原宽度，满载仍显示满条。
    const cores = (state.data && state.data.coreCount) || 1;
    const width = pct * cores;
    r.ldCpuFill.style.width = width <= 0 ? '0%' : Math.max(2, Math.min(100, width)) + '%';
  }
  if (svc.port != null) {
    r.port.hidden = false;
    setText(r.port, ':' + svc.port);
    r.port.title = '打开 ' + localServiceUrl(svc, svc.port);
    r.port.setAttribute('aria-label', '打开 ' + title + '，端口 ' + svc.port);
  } else {
    r.port.hidden = true;
    r.port.removeAttribute('aria-label');
  }
  const full = svc.cwd || '';
  setText(r.cwd, full ? truncateMiddle(shortHome(full)) : '');
  r.cwd.title = full;
  setText(r.ldCpu, fmtPct(svc.cpu));
  setText(r.ldMem, fmtPct(svc.mem));
  setText(r.up, fmtUptime(svc.uptimeSec));
  if (r.pin) {
    if (r._pinned !== !!svc.pinned) {
      r._pinned = !!svc.pinned;
      setChildren(r.pin, icon(svc.pinned ? 'pin' : 'pin-off', 15));
    }
    r.pin.classList.toggle('active', !!svc.pinned);
  }
  if (r.demote) r.demote.hidden = !svc.promoted;
  setText(r.cmdCode, svc.cmd || '');
  if (r.add) {
    const linked = !!svc.appId;
    if (r._linkedApp !== linked) {
      r._linkedApp = linked;
      setChildren(r.add, icon(linked ? 'pencil' : 'plus', 15));
    }
    r.add.classList.toggle('active', linked);
    r.add.disabled = linked && !((state.data && state.data.apps) || [])
      .some(app => app.id === svc.appId);
  }
  const label = (action, button) => {
    if (!button) return;
    button.title = action + '：' + title;
    button.setAttribute('aria-label', action + '：' + title);
  };
  label(svc.appId ? '编辑启动台应用' : '添加到启动台', r.add);
  label('移回应用后台', r.demote);
  label(row.classList.contains('expanded') ? '收起完整命令' : '展开完整命令', r.command);
  label(svc.pinned ? '取消置顶' : '置顶', r.pin);
  label('移入已隐藏列表', r.hide);
  label('结束进程', r.kill);
  label('移到我的服务', r.promote);
  label('取消隐藏', r.unhide);
}

function createDiscoveryRow() {
  const row = el('article', 'port-discovery-item');
  row.setAttribute('role', 'listitem');

  const copy = el('div', 'port-discovery-copy');
  const titleRow = el('div', 'port-discovery-title-row');
  const title = el('strong', 'port-discovery-name');
  const status = el('span', 'port-discovery-status');
  titleRow.append(title, status);
  const meta = el('div', 'port-discovery-meta');
  const detail = el('div', 'port-discovery-detail');
  copy.append(titleRow, meta, detail);

  const actions = el('div', 'port-discovery-actions');
  const add = el('button', 'btn btn-accent port-discovery-add');
  add.type = 'button';
  add.textContent = '加入启动台';
  add.addEventListener('click', () => {
    /* 只打开模态框，不立即移除发现项：用户取消后该项仍可再次操作；
       创建成功且应用开始监听后，轮询清理循环会按端口归属自动移除。 */
    openServiceAppModal(discoveryItems.get(row.dataset.key));
  });
  const ignore = el('button', 'btn port-discovery-ignore');
  ignore.type = 'button';
  ignore.textContent = '忽略并隐藏';
  ignore.addEventListener('click', async () => {
    const key = row.dataset.key;
    const svc = discoveryItems.get(key);
    if (!svc) return;
    const result = await setServiceFlag(svc.key, 'hidden', true);
    if (!result || result.ok === false) return;
    discoveryItems.delete(key);
    renderPortDiscovery();
  });
  const dismiss = el('button', 'btn port-discovery-dismiss');
  dismiss.type = 'button';
  dismiss.textContent = '暂时关闭';
  dismiss.addEventListener('click', () => {
    discoveryItems.delete(row.dataset.key);
    renderPortDiscovery();
  });
  actions.append(add, ignore, dismiss);
  row.append(copy, actions);
  row._r = { title, status, meta, detail, add, ignore, dismiss };
  return row;
}

function updateDiscoveryRow(row, svc) {
  const r = row._r;
  const title = svc.project || svc.name || '本地服务';
  const process = svc.name || '未知进程';
  setText(r.title, title);
  setText(r.status, '刚刚发现');
  setText(r.meta, process +
    (svc.pid ? ' · PID ' + svc.pid : '') +
    (svc.port != null ? ' · :' + svc.port : ''));
  const detail = svc.cwd || svc.cmd || '';
  setText(r.detail, detail ? truncateMiddle(shortHome(detail), 72) : '未能读取工作目录');
  r.detail.title = detail;
  row.classList.remove('is-offline');
  r.add.textContent = '加入启动台';
  r.add.setAttribute('aria-label', '将 ' + title + ' 加入启动台');
  r.ignore.setAttribute('aria-label', '忽略并隐藏 ' + title + ' 的端口 ' + svc.port);
  r.dismiss.setAttribute('aria-label', '暂时关闭 ' + title + ' 的新端口提醒');
}

function renderPortDiscovery() {
  const items = [...discoveryItems.values()];
  /* 先解除 hidden，再插入 live region 内容，确保首次发现可被读屏宣布。 */
  portDiscovery.hidden = items.length === 0;
  reconcile(portDiscoveryList, items, serviceKey,
    createDiscoveryRow, updateDiscoveryRow, true);
  setText(portDiscoveryCount, String(items.length));
  portDiscoveryCount.setAttribute('aria-label', items.length + ' 个待处理的新端口');
}

/* ---------------- 关注的进程 ---------------- */
function createWatchRow() {
  const row = el('div', 'tr');
  row.setAttribute('role', 'row');
  const cTitle = el('span', 'w-title');
  cTitle.setAttribute('role', 'cell');
  const name = el('span', 'w-name');
  const kw = el('span', 'kw-tag');
  cTitle.append(name, kw);
  const cPid = el('span', 'c-pid');
  cPid.setAttribute('role', 'cell');
  const load = loadCell();
  const cUp = el('span', 'c-up');
  cUp.setAttribute('role', 'cell');
  const cAct = el('span', 'c-act');
  cAct.setAttribute('role', 'cell');
  cAct.setAttribute('aria-label', '操作');
  const bKill = iconBtn('power', '结束进程', 'danger');
  bKill.addEventListener('click', () => {
    const w = findWatch(row.dataset.key);
    if (w) confirmKill({ pid: w.pid, name: w.name, port: null });
  });
  cAct.append(bKill);
  row.append(cTitle, cPid, load.wrap, cUp, cAct);
  row._r = { name, kw, pid: cPid, ldCpu: load.cpu, ldMem: load.mem, up: cUp, kill: bKill };
  return row;
}

function updateWatchRow(row, w) {
  const r = row._r;
  setText(r.name, w.name || '');
  r.name.title = w.cmd || w.name || '';
  setText(r.kw, w.keyword || '');
  setText(r.pid, String(w.pid));
  setText(r.ldCpu, fmtPct(w.cpu));
  setText(r.ldMem, fmtPct(w.mem));
  setText(r.up, fmtUptime(w.uptimeSec));
  const target = w.name || ('PID ' + w.pid);
  r.kill.title = '结束进程：' + target;
  r.kill.setAttribute('aria-label', '结束进程：' + target);
}

function createChip(kw) {
  const c = el('span', 'chip');
  const t = el('span');
  t.textContent = kw;
  const x = el('button');
  x.type = 'button';
  x.textContent = '×';
  x.title = '删除关键字';
  x.setAttribute('aria-label', '删除关注关键字：' + kw);
  x.addEventListener('click', async () => {
    await act(post('/api/watch', { keyword: kw, action: 'remove' }));
    window.__poll();
  });
  c.append(t, x);
  return c;
}

/* ---------------- 服务监控渲染 ---------------- */
const cpuHistory = [], memHistory = [];
const SPARK_CAP = 40;

function renderSpark(id, history) {
  const svg = $(id);
  if (!svg) return;
  const line = svg.querySelector('polyline');
  const n = history.length;
  if (n < 2) return;
  const max = Math.max(5, ...history);
  const step = 100 / (SPARK_CAP - 1);
  const offset = 100 - step * (n - 1);
  const points = history.map((v, i) =>
    (offset + step * i).toFixed(1) + ',' + (25 - (v / max) * 22).toFixed(1));
  line.setAttribute('points', points.join(' '));
}

const MINE_FILTERS = [['all', '全部'], ['linked', '关联启动台'], ['pinned', '已置顶']];
let mineFilter = 'all';
/* 芯片按钮只创建一次，点击时必须读取当轮数据而不是首次渲染的闭包快照 */
let latestMine = [];

function matchMineFilter(svc, filter) {
  if (filter === 'linked') return !!svc.appId;
  if (filter === 'pinned') return !!svc.pinned;
  return true;
}

function renderMineFilterChips(mine, onPick) {
  const row = $('#mineFilter');
  if (!row) return;
  if (!row._btns) {
    row._btns = new Map();
    for (const [key, label] of MINE_FILTERS) {
      const btn = el('button', 'fchip');
      btn.type = 'button';
      const text = el('span');
      text.textContent = label;
      const count = el('span', 'fc-n');
      btn.append(text, count);
      btn.addEventListener('click', () => onPick(key));
      row.appendChild(btn);
      row._btns.set(key, { btn, count });
    }
  }
  for (const [key] of MINE_FILTERS) {
    const item = row._btns.get(key);
    item.btn.classList.toggle('active', key === mineFilter);
    setText(item.count, String(mine.filter(s => matchMineFilter(s, key)).length));
  }
}

function applyMineFilter(mineListEl, mine) {
  const byKey = new Map(mine.map(s => [serviceKey(s), s]));
  for (const row of mineListEl.querySelectorAll('.tr[data-key]')) {
    const svc = byKey.get(row.dataset.key);
    row.hidden = svc ? !matchMineFilter(svc, mineFilter) : false;
  }
}

function syncMineFilterUI() {
  renderMineFilterChips(latestMine, key => {
    mineFilter = key;
    syncMineFilterUI();
  });
  applyMineFilter(mineList, latestMine);
}

export function renderServices(d, firstRender) {
  const svcs = d.services || [];
  const mine = svcs.filter(s => s.group === 'mine' && !s.hidden).sort(svcSort);
  const bg = svcs.filter(s => s.group === 'background' && !s.hidden).sort(svcSort);
  const hid = svcs.filter(s => s.hidden).sort(svcSort);
  const watched = d.watched || [];
  const keywords = d.watchedKeywords || [];

  setKpi(statMine, String(mine.length));
  setText($('#statMineSub'), '监听中');
  setKpi(statBg, String(bg.length));
  if (state.lastUpdate) {
    const clock = fmtClock(state.lastUpdate);
    setKpiUnit(statTime, clock.slice(0, 5), clock.slice(5));
  } else {
    setKpiUnit(statTime, '--:--', ':--');
  }
  /* 全局概览：我的服务负载合计 + 启动台端口警告数 */
  let cpuSum = 0, memSum = 0;
  for (const s of mine) { cpuSum += s.cpu || 0; memSum += s.mem || 0; }
  setKpiUnit(statCpu, cpuSum.toFixed(1), '%');
  setKpiUnit(statMem, memSum.toFixed(1), '%');
  cpuHistory.push(cpuSum);
  memHistory.push(memSum);
  if (cpuHistory.length > SPARK_CAP) cpuHistory.shift();
  if (memHistory.length > SPARK_CAP) memHistory.shift();
  renderSpark('#sparkCpu', cpuHistory);
  renderSpark('#sparkMem', memHistory);
  const warnCount = ((state.data && state.data.apps) || [])
    .filter(a => a.portOccupied).length;
  setText(statWarn, String(warnCount));
  statWarn.classList.toggle('bad', warnCount > 0);
  setText($('#statWarnSub'), warnCount ? '需要处理' : '无需处理');
  renderPortDiscovery();

  reconcile(mineList, mine, serviceKey, () => createServiceRow('mine'), updateServiceRow, firstRender);
  reconcile(bgList, bg, serviceKey, () => createServiceRow('background'), updateServiceRow, firstRender);
  reconcile(hiddenList, hid, serviceKey, () => createServiceRow('hidden'), updateServiceRow, firstRender);
  reconcile(watchList, watched, w => w.pid, createWatchRow, updateWatchRow, firstRender);
  reconcile(watchChips, keywords, k => k, createChip, () => {}, firstRender);
  latestMine = mine;
  syncMineFilterUI();

  setText($('#mineSecCount'), mine.length ? String(mine.length) : '');
  setText(bgCount, String(bg.length));
  hiddenPanel.hidden = hid.length === 0;
  setText(hiddenCount, '已隐藏 ' + hid.length + ' 个');

  mineEmpty.hidden = mine.length > 0;
  bgEmpty.hidden = bg.length > 0;
  watchEmpty.hidden = watched.length > 0;
  if (!watched.length) {
    setText(watchEmptyText, keywords.length
      ? '暂无匹配「' + keywords.join('、') + '」的进程'
      : '添加关键字后，匹配的进程会显示在这里');
  }
}

/* 折叠分区 */
bgHeader.addEventListener('click', () => {
  bgBody.hidden = !bgBody.hidden;
  bgHeader.classList.toggle('open', !bgBody.hidden);
  bgHeader.setAttribute('aria-expanded', String(!bgBody.hidden));
});
hiddenHeader.addEventListener('click', () => {
  hiddenBody.hidden = !hiddenBody.hidden;
  hiddenHeader.classList.toggle('open', !hiddenBody.hidden);
  hiddenHeader.setAttribute('aria-expanded', String(!hiddenBody.hidden));
});

/* 添加关注关键字 */
watchInput.addEventListener('keydown', async e => {
  if (e.key !== 'Enter') return;
  const kw = watchInput.value.trim();
  if (!kw) return;
  const r = await act(post('/api/watch', { keyword: kw, action: 'add' }));
  if (r && r.ok !== false) watchInput.value = '';
  window.__poll();
});
