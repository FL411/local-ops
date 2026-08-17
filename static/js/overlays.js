'use strict';
/* ============================================================
   overlays.js — 浮层：确认框 / 应用编辑模态 / 日志抽屉
   ============================================================ */
import { $, el, setText, setChildren, icon, escapeHtml,
  post, put, del, act, toast, openLayer, closeLayer,
  GLYPHS, findApp, bumpMutationEpoch, controlRequestHeaders, IS_MAC } from './core.js';

/* ---------------- DOM 引用 ---------------- */
const appModalMask = $('#appModalMask'), appModal = $('#appModal'), appModalTitle = $('#appModalTitle');
const fName = $('#fName'), fCmd = $('#fCmd'), fCwd = $('#fCwd'), fPort = $('#fPort');
const kindRow = $('#kindRow'), portField = $('#portField'), fCmdLabel = $('#fCmdLabel');
const autostartField = $('#autostartField'), fAutostart = $('#fAutostart');
const btnPickScript = $('#btnPickScript'), btnPickCwd = $('#btnPickCwd');
const btnDetectProject = $('#btnDetectProject');
const detectPanel = $('#detectPanel'), detectSummary = $('#detectSummary');
const detectFiles = $('#detectFiles'), detectList = $('#detectList');
const iconFile = $('#iconFile'), btnPickIcon = $('#btnPickIcon'), btnRemoveIcon = $('#btnRemoveIcon');
const glyphGrid = $('#glyphGrid');
const iconPreview = $('#iconPreview');
const iconPreviewImg = $('#iconPreviewImg'), iconPreviewGlyph = $('#iconPreviewGlyph');
const iconPreviewTxt = $('#iconPreviewTxt');
const appearanceDetails = $('#appearanceDetails'), appearanceChevron = $('#appearanceChevron');
const appCancel = $('#appCancel'), appSave = $('#appSave');
const appStopEdit = $('#appStopEdit'), editRunningNotice = $('#editRunningNotice');

const confirmMask = $('#confirmMask'), confirmTitle = $('#confirmTitle'), confirmBody = $('#confirmBody');
const forceRow = $('#forceRow'), forceCheck = $('#forceCheck');
const confirmCancel = $('#confirmCancel'), confirmOk = $('#confirmOk');

const drawerMask = $('#drawerMask'), logDrawer = $('#logDrawer');
const drawerTitle = $('#drawerTitle'), drawerClose = $('#drawerClose');
const logBody = $('#logBody'), logPre = $('#logPre');

const iconVer = new Map();   // appId → 图标版本号，上传/删除后刷新浏览器缓存
setChildren(appearanceChevron, icon('chevron-down', 16));
export function bumpIconVer(id) { iconVer.set(id, (iconVer.get(id) || 0) + 1); }
export function getIconVer(id) { return iconVer.get(id) || 0; }

/* 兼容尚未重启的旧后端；新后端会返回经过同样规则生成的 command。 */
function shellQuotePath(path) {
  return "'" + String(path).replace(/'/g, "'\"'\"'") + "'";
}
function fallbackScriptCommand(path) {
  const suffix = (String(path).match(/(\.[^./\\]+)$/) || [])[1]?.toLowerCase();
  if (IS_MAC) {
    const quoted = shellQuotePath(path);
    if (suffix === '.py') return 'python3 -- ' + quoted;
    if (suffix === '.zsh') return '/bin/zsh -- ' + quoted;
    return '/bin/bash -- ' + quoted;
  }
  /* Windows:cmd 不识别单引号,用双引号;.bat/.cmd 直接执行。 */
  const quoted = '"' + String(path).replace(/"/g, '\\"') + '"';
  if (suffix === '.py') return 'python -- ' + quoted;
  if (suffix === '.ps1') return 'powershell -NoProfile -ExecutionPolicy Bypass -File ' + quoted;
  if (suffix === '.bat' || suffix === '.cmd') return quoted;
  if (suffix === '.sh' || suffix === '.bash') return 'bash -- ' + quoted;
  return quoted;
}

/* ============================================================
   确认模态
   ============================================================ */
let confirmCb = null;
export function openConfirm({ title, bodyHtml, okText = '确认', showForce = false,
                       tone = 'danger', onOk }) {
  confirmTitle.textContent = title;
  confirmBody.innerHTML = bodyHtml;
  forceRow.hidden = !showForce;
  forceCheck.checked = false;
  confirmOk.textContent = okText;
  confirmOk.classList.toggle('btn-stop', tone === 'danger');
  confirmOk.classList.toggle('btn-accent', tone === 'primary');
  confirmCb = onOk;
  openLayer(confirmMask, confirmCancel);
}
export function closeConfirm() {
  closeLayer(confirmMask);
  confirmCb = null;
}
confirmOk.addEventListener('click', () => {
  const cb = confirmCb;
  const force = forceCheck.checked;
  closeConfirm();
  if (cb) cb(force);
});
confirmCancel.addEventListener('click', closeConfirm);
confirmMask.addEventListener('mousedown', e => { if (e.target === confirmMask) closeConfirm(); });

/* ---------------- 结束进程确认 ---------------- */
export function confirmKill(svc) {
  openConfirm({
    title: '结束进程',
    bodyHtml: '确定要结束进程 <b>' + escapeHtml(svc.name || '') + '</b> 吗？' +
      '<div class="confirm-detail mono">PID ' + escapeHtml(String(svc.pid)) +
      (svc.port ? ' · 端口 :' + escapeHtml(String(svc.port)) : '') + '</div>',
    okText: '结束',
    showForce: true,
    onOk: async force => {
      await act(post('/api/kill', { pid: svc.pid, force }));
    },
  });
}

/* ============================================================
   添加 / 编辑应用模态（图标库 + 上传）
   ============================================================ */
let editingAppId = null;
let editingAppOriginal = null;
let appSaving = false;
let pendingIcon = null;      // { blob, type, url }
let selectedGlyph = null;    // 选中的 Lucide 图标名
let removeStoredIcon = false; // 仅在保存成功后删除，取消编辑不触碰后端
let pendingAttach = null;     // 从服务监控添加时待认领的来源进程信息
let detectingProject = false; // 认领流程必须等项目命令识别完成后再允许保存

export function buildGlyphGrid() {
  GLYPHS.forEach(g => {
    const b = el('button', 'glyph-btn');
    b.type = 'button';
    b.title = g;
    b.setAttribute('aria-label', '选择图标 ' + g);
    b.setAttribute('aria-pressed', 'false');
    b.dataset.glyph = g;
    b.appendChild(icon(g, 17));
    b.addEventListener('click', () => {
      const selecting = selectedGlyph !== g;
      selectedGlyph = selecting ? g : null;
      if (selecting) {
        clearPendingIcon();
        const app = editingAppId ? findApp(editingAppId) : null;
        if (app && app.icon) removeStoredIcon = true;
      }
      syncGlyphGrid();
      renderIconPreview();
    });
    glyphGrid.appendChild(b);
  });
}
function syncGlyphGrid() {
  for (const b of glyphGrid.children) {
    const selected = b.dataset.glyph === selectedGlyph;
    b.classList.toggle('sel', selected);
    b.setAttribute('aria-pressed', String(selected));
  }
}

function clearPendingIcon() {
  if (pendingIcon) URL.revokeObjectURL(pendingIcon.url);
  pendingIcon = null;
}
function setPendingIcon(file) {
  clearPendingIcon();
  selectedGlyph = null;
  removeStoredIcon = false;
  pendingIcon = { blob: file, type: file.type || 'image/png', url: URL.createObjectURL(file) };
  syncGlyphGrid();
  renderIconPreview();
}
/* 预览优先级：待上传图片 > 已上传图片 > glyph > 名称首字 */
function renderIconPreview() {
  const app = editingAppId ? findApp(editingAppId) : null;
  const showImg = pendingIcon || (!removeStoredIcon && app && app.icon);
  const glyph = selectedGlyph;
  if (showImg) {
    const v = getIconVer(app && app.id);
    iconPreviewImg.src = pendingIcon ? pendingIcon.url : app.icon + (v ? '?v=' + v : '');
    iconPreviewImg.hidden = false;
    iconPreviewGlyph.hidden = true;
    iconPreviewTxt.hidden = true;
  } else if (glyph && window.LUCIDE && window.LUCIDE[glyph]) {
    iconPreviewImg.hidden = true;
    iconPreviewGlyph.hidden = false;
    iconPreviewTxt.hidden = true;
    setChildren(iconPreviewGlyph, icon(glyph, 20));
  } else {
    iconPreviewImg.hidden = true;
    iconPreviewGlyph.hidden = true;
    iconPreviewTxt.hidden = false;
    const nm = fName.value.trim();
    iconPreviewTxt.textContent = nm ? [...nm][0].toUpperCase() : '?';
  }
  btnRemoveIcon.hidden = !(pendingIcon || selectedGlyph ||
    (!removeStoredIcon && app && (app.icon || app.glyph)));
}

let modalKind = 'service';
let detectRequestSeq = 0;
let detectedPortValue = null;

function readPortValue() {
  const raw = fPort.value.trim();
  if (!raw) return null;
  if (!/^\d+$/.test(raw)) return NaN;
  const value = Number(raw);
  return Number.isInteger(value) && value >= 1 && value <= 65535 ? value : NaN;
}

function resetDetection(clearAutoPort = false) {
  detectRequestSeq += 1;
  detectingProject = false;
  if (clearAutoPort && detectedPortValue != null &&
      fPort.value.trim() === String(detectedPortValue)) fPort.value = '';
  detectedPortValue = null;
  btnDetectProject.disabled = false;
  btnPickCwd.disabled = false;
  detectPanel.hidden = true;
  detectList.replaceChildren();
  detectSummary.textContent = '';
  detectFiles.textContent = '';
}

function modalLifecycleChanged() {
  if (!editingAppOriginal) return false;
  const currentPort = modalKind === 'task' ? null
    : readPortValue();
  return fCmd.value.trim() !== (editingAppOriginal.command || '') ||
    (fCwd.value.trim() || null) !== (editingAppOriginal.cwd || null) ||
    currentPort !== (editingAppOriginal.port == null ? null : editingAppOriginal.port) ||
    modalKind !== (editingAppOriginal.kind || 'service');
}

function refreshEditSaveMode() {
  const running = !!(editingAppOriginal && editingAppOriginal.running);
  const needsStop = running && modalLifecycleChanged();
  const isTask = modalKind === 'task';
  const stopVerb = isTask ? '中止任务' : '停止服务';
  editRunningNotice.hidden = !running;
  if (running) {
    setText(editRunningNotice, needsStop
      ? '修改内容已保留。请先' + stopVerb + '，再继续保存。'
      : (isTask ? '任务' : '服务') + '正在运行。可在这里先' + stopVerb +
        '，编辑面板不会关闭，当前填写内容也不会丢失。');
  }
  setText(appStopEdit, stopVerb);
  appStopEdit.hidden = !running;
  appStopEdit.disabled = appSaving;
  appSave.hidden = false;
  const willAttach = !editingAppId && pendingAttach && modalKind === 'service'
    && readPortValue() === pendingAttach.port;
  setText(appSave, willAttach ? '保存并认领' : '保存');
  appSave.disabled = appSaving || needsStop || (willAttach && detectingProject);
  appSave.title = needsStop ? '请先在当前面板' + stopVerb
    : (willAttach && detectingProject ? '正在识别可靠的项目启动命令' : '');
}

function setModalKind(kind) {
  modalKind = kind === 'task' ? 'task' : 'service';
  kindRow.querySelectorAll('.kind-btn').forEach(b => {
    const active = b.dataset.kind === modalKind;
    b.classList.toggle('active', active);
    b.setAttribute('aria-pressed', String(active));
  });
  portField.hidden = modalKind === 'task';
  fPort.disabled = modalKind === 'task';
  autostartField.hidden = modalKind === 'task';
  setText(fCmdLabel, modalKind === 'task' ? '执行命令' : '启动命令');
  fName.placeholder = modalKind === 'task' ? '如：每日备份' : '如：本地博客';
  fCmd.placeholder = modalKind === 'task'
    ? '选择脚本后自动生成执行命令，也可以手动填写'
    : '选择项目后自动识别启动命令，也可以手动填写';
  appModalTitle.textContent = (editingAppId ? '编辑' : '添加') +
    (modalKind === 'task' ? '批处理任务' : '服务');
  refreshEditSaveMode();
}
kindRow.querySelectorAll('.kind-btn').forEach(b =>
  b.addEventListener('click', () => setModalKind(b.dataset.kind)));
fAutostart.addEventListener('click', () => {
  const on = !fAutostart.classList.contains('on');
  fAutostart.classList.toggle('on', on);
  fAutostart.setAttribute('aria-checked', String(on));
});

export function openAppModal(app, presetKind, focusAction = '') {
  editingAppId = app ? app.id : null;
  pendingAttach = !editingAppId && app && Number.isInteger(app.attachPid)
    && app.attachPid > 0 && Number.isInteger(Number(app.port))
    ? {
        pid: app.attachPid,
        port: Number(app.port),
        instanceKey: app.attachInstanceKey || null,
        command: (app.command || '').trim(),
      }
    : null;
  editingAppOriginal = app ? {
    command: app.command || '', cwd: app.cwd || null,
    port: app.port == null ? null : app.port,
    kind: app.kind || 'service', running: !!app.running,
  } : null;
  resetDetection();
  clearPendingIcon();
  removeStoredIcon = false;
  selectedGlyph = (app && app.glyph) || null;
  fName.value = (app && app.name) || '';
  fCmd.value = (app && app.command) || '';
  fCwd.value = (app && app.cwd) || '';
  fPort.value = app && app.port != null ? app.port : '';
  const autostartOn = !!(app && app.autostart);
  fAutostart.classList.toggle('on', autostartOn);
  fAutostart.setAttribute('aria-checked', String(autostartOn));
  [fName, fCmd, fCwd, fPort].forEach(clearFieldError);
  setModalKind(presetKind || (app && app.kind) || 'service');
  appearanceDetails.open = !!(app && (app.icon || app.glyph));
  syncGlyphGrid();
  renderIconPreview();
  const focusTarget = focusAction === 'pick-script' ? btnPickScript
    : focusAction === 'pick-cwd' ? btnPickCwd
      : focusAction === 'edit-command' ? fCmd
        : app ? fName : (modalKind === 'task' ? btnPickScript : btnPickCwd);
  openLayer(appModalMask, focusTarget);
  /* 监听进程的 argv 往往只是框架子进程（如 next-server），不一定适合作为
     下次启动命令。打开认领表单时同时读取项目配置，让用户选择可靠命令。 */
  if (pendingAttach && fCwd.value.trim()) detectProject();
}
export function closeAppModal() {
  closeLayer(appModalMask);
  resetDetection();
  editingAppId = null;
  editingAppOriginal = null;
  clearPendingIcon();
  selectedGlyph = null;
  removeStoredIcon = false;
  pendingAttach = null;
}

function applyDetectedCandidate(candidate, option) {
  const previousAutoPort = detectedPortValue == null ? '' : String(detectedPortValue);
  const currentPort = fPort.value.trim();
  fCmd.value = candidate.command || '';
  clearFieldError(fCmd);
  setModalKind(candidate.kind || 'service');
  if (candidate.port != null) {
    if (!currentPort || currentPort === previousAutoPort) {
      fPort.value = String(candidate.port);
      detectedPortValue = candidate.port;
    } else {
      detectedPortValue = null;
    }
  } else {
    if (previousAutoPort && currentPort === previousAutoPort) fPort.value = '';
    detectedPortValue = null;
  }
  detectList.querySelectorAll('.detect-option').forEach(node => {
    const active = node === option;
    node.classList.toggle('selected', active);
    node.setAttribute('aria-pressed', String(active));
  });
  const portText = candidate.port != null && fPort.value === String(candidate.port)
    ? '，端口 ' + candidate.port : '';
  refreshEditSaveMode();
  toast('已填入“' + candidate.label + '”' + portText);
}

function renderDetection(result) {
  const candidates = Array.isArray(result.candidates) ? result.candidates : [];
  detectPanel.hidden = false;
  detectList.replaceChildren();
  const files = Array.isArray(result.files) ? result.files : [];
  detectFiles.textContent = files.length ? '读取了 ' + files.join('、') : '';
  if (!candidates.length) {
    detectSummary.textContent = '没有识别到可直接启动的配置';
    const empty = el('p', 'detect-empty');
    empty.textContent = '仍可选择脚本，系统会按文件类型生成执行命令；也可以手动填写。';
    detectList.appendChild(empty);
    return;
  }
  detectSummary.textContent = '找到 ' + candidates.length + ' 个候选，选择一个填入';
  candidates.forEach((candidate, index) => {
    const option = el('button', 'detect-option');
    option.type = 'button';
    option.setAttribute('aria-pressed', 'false');
    const head = el('span', 'detect-option-head');
    const title = el('span', 'detect-option-title');
    title.textContent = candidate.label || '启动项目';
    head.appendChild(title);
    if (index === 0) {
      const recommended = el('span', 'detect-recommended');
      recommended.textContent = '推荐';
      head.appendChild(recommended);
    }
    if (candidate.kind === 'task') {
      const kind = el('span', 'detect-kind');
      kind.textContent = '任务';
      head.appendChild(kind);
    }
    if (candidate.port != null) {
      const port = el('span', 'detect-port mono');
      port.textContent = ':' + candidate.port;
      head.appendChild(port);
    }
    const command = el('span', 'detect-command mono');
    command.textContent = candidate.command || '';
    const source = el('span', 'detect-source');
    source.textContent = candidate.source || '';
    option.append(head, command, source);
    option.addEventListener('click', () => applyDetectedCandidate(candidate, option));
    detectList.appendChild(option);
  });
}

async function detectProject() {
  const cwd = fCwd.value.trim();
  if (!cwd) return fieldError(fCwd, '请先选择项目文件夹');
  const requestSeq = ++detectRequestSeq;
  detectPanel.hidden = false;
  detectSummary.textContent = '正在读取项目配置…';
  detectFiles.textContent = '';
  detectList.replaceChildren();
  btnDetectProject.disabled = true;
  btnPickCwd.disabled = true;
  detectingProject = true;
  refreshEditSaveMode();
  try {
    const result = await act(post('/api/project/detect', { cwd }));
    if (requestSeq !== detectRequestSeq) return;
    if (!result || result.ok === false) {
      detectSummary.textContent = '识别失败，请检查文件夹后重试';
      return;
    }
    if (!fName.value.trim() && result.name) {
      fName.value = result.name;
      renderIconPreview();
    }
    renderDetection(result);
    if (pendingAttach && !editingAppId &&
        fCmd.value.trim() === pendingAttach.command) {
      const candidates = Array.isArray(result.candidates) ? result.candidates : [];
      const index = candidates.findIndex(candidate =>
        candidate.kind !== 'task' && Number(candidate.port) === pendingAttach.port);
      if (index >= 0) {
        const option = detectList.querySelectorAll('.detect-option')[index];
        applyDetectedCandidate(candidates[index], option);
      }
    }
  } finally {
    if (requestSeq === detectRequestSeq) {
      detectingProject = false;
      btnDetectProject.disabled = false;
      btnPickCwd.disabled = false;
      refreshEditSaveMode();
    }
  }
}

function fieldError(input, msg) {
  toast(msg);
  input.classList.add('invalid');
  input.setAttribute('aria-invalid', 'true');
  input.focus();
}
function clearFieldError(input) {
  input.classList.remove('invalid');
  input.removeAttribute('aria-invalid');
}

async function stopEditingApp() {
  if (!editingAppId || !editingAppOriginal || !editingAppOriginal.running) return;
  appSaving = true;
  refreshEditSaveMode();
  const isTask = modalKind === 'task';
  toast((isTask ? '正在中止任务' : '正在停止服务') + '，编辑内容会保留…');
  try {
    const result = await act(post('/api/apps/' + editingAppId + '/stop'));
    await window.__poll();
    const latest = findApp(editingAppId);
    if ((result && result.ok !== false) || (latest && !latest.running)) {
      editingAppOriginal.running = false;
      toast((isTask ? '任务已中止' : '服务已停止') + '，可以继续编辑并保存');
    }
  } finally {
    appSaving = false;
    refreshEditSaveMode();
  }
}

function rememberSavedApp(app, id, body) {
  editingAppId = id;
  editingAppOriginal = {
    command: body.command,
    cwd: body.cwd,
    port: body.port,
    kind: body.kind,
    running: !!app.running,
  };
  setModalKind(body.kind);
}

async function saveApp() {
  const name = fName.value.trim();
  const command = fCmd.value.trim();
  if (!name) return fieldError(fName, '请填写名称');
  if (!command) return fieldError(
    fCmd, modalKind === 'task' ? '请填写执行命令' : '请填写启动命令');
  const port = modalKind === 'task' ? null : readPortValue();
  if (Number.isNaN(port)) return fieldError(fPort, '端口必须是 1–65535 之间的整数');
  const body = {
    name,
    command,
    cwd: fCwd.value.trim() || null,
    port,
    glyph: selectedGlyph || null,
    kind: modalKind,
    autostart: modalKind === 'service' && fAutostart.classList.contains('on'),
  };
  const wasCreating = !editingAppId;
  const attachRequest = wasCreating && pendingAttach && modalKind === 'service'
    && port === pendingAttach.port ? { ...pendingAttach } : null;
  if (attachRequest) body.attachPid = attachRequest.pid;
  appSaving = true;
  refreshEditSaveMode();
  try {
    const app = editingAppId
      ? await act(put('/api/apps/' + editingAppId, body))
      : await act(post('/api/apps', body));
    if (!app || app.ok === false) {
      if (app && app.requiresStop && editingAppOriginal) {
        editingAppOriginal.running = true;
        refreshEditSaveMode();
      }
      return;
    }
    const id = app.id || editingAppId;
    const attachSucceeded = !!(attachRequest && app.attached);
    if (attachSucceeded && app.cwd) {
      body.cwd = app.cwd;
      fCwd.value = app.cwd;
    }
    rememberSavedApp(
      attachSucceeded ? { ...app, running: true } : app,
      id,
      body,
    );
    if (pendingIcon && id) {
      try {
        const r = await fetch('/api/apps/' + id + '/icon', {
          method: 'POST',
          headers: controlRequestHeaders({ 'Content-Type': pendingIcon.type }),
          body: pendingIcon.blob,
        });
        const j = await r.json();
        if (!r.ok || (j && j.ok === false)) {
          toast((j && j.error) || '图标上传失败，配置已保存，可直接重试');
          await window.__poll();
          return;
        }
        bumpIconVer(id);
        bumpMutationEpoch();   // 原生 fetch 不经过 req，手动作废在途旧快照
      } catch (e) {
        toast('图标上传失败：' + e.message + '。配置已保存，可直接重试');
        await window.__poll();
        return;
      }
    } else if (removeStoredIcon && id) {
      const result = await act(del('/api/apps/' + id + '/icon'));
      if (!result || result.ok === false) {
        toast('配置已保存，但图标清除失败，可直接重试');
        await window.__poll();
        return;
      }
      removeStoredIcon = false;
      bumpIconVer(id);
    }
    closeAppModal();
    await window.__poll();
    if (attachSucceeded) toast('已加入启动台并认领正在运行的进程');
  } finally {
    appSaving = false;
    refreshEditSaveMode();
  }
}

export function initAppModal({ onAddService, onAddTask }) {
  onAddService.addEventListener('click', () => openAppModal(null, 'service'));
  onAddTask.addEventListener('click', () => openAppModal(null, 'task'));
  appCancel.addEventListener('click', closeAppModal);
  appSave.addEventListener('click', saveApp);
  appStopEdit.addEventListener('click', stopEditingApp);
  appModalMask.addEventListener('mousedown', e => { if (e.target === appModalMask) closeAppModal(); });

  /* 选择批处理脚本：自动填命令 / 工作目录 / 名称 */
  btnPickScript.addEventListener('click', async () => {
    btnPickScript.disabled = true;
    try {
      const r = await act(post('/api/pick', { what: 'script' }));
      if (!r || r.canceled || !r.path) return;  // 取消或失败均静默
      const p = r.path;
      fCmd.value = r.command || fallbackScriptCommand(p);
      const lastSep = Math.max(p.lastIndexOf('/'), p.lastIndexOf('\\'));
      const dir = lastSep >= 0 ? p.slice(0, lastSep) : '';
      if (dir && !fCwd.value.trim()) fCwd.value = dir;
      if (!fName.value.trim()) {
        const base = p.split(/[\\/]/).pop().replace(/\.(command|sh|bash|zsh|py)$/i, '');
        if (base) fName.value = base;
      }
      fCmd.classList.remove('invalid');
      refreshEditSaveMode();
      detectList.querySelectorAll('.detect-option').forEach(node => {
        node.classList.remove('selected');
        node.setAttribute('aria-pressed', 'false');
      });
      toast('已按脚本类型生成执行命令');
    } finally {
      btnPickScript.disabled = false;
    }
  });

  /* 浏览工作目录（macOS 原生选择框） */
  btnPickCwd.addEventListener('click', async () => {
    btnPickCwd.disabled = true;
    try {
      const r = await act(post('/api/pick', { what: 'dir' }));
      if (r && !r.canceled && r.path) {
        fCwd.value = r.path;
        fCwd.classList.remove('invalid');
        refreshEditSaveMode();
        await detectProject();
      }
    } finally {
      btnPickCwd.disabled = false;
    }
  });
  btnDetectProject.addEventListener('click', detectProject);
  fCwd.addEventListener('input', () => resetDetection(true));
  [fName, fCmd, fCwd, fPort].forEach(input =>
    input.addEventListener('input', () => {
      clearFieldError(input);
      refreshEditSaveMode();
    }));

  /* 图标：上传 / 粘贴 / 清除 */
  btnPickIcon.addEventListener('click', () => iconFile.click());
  iconFile.addEventListener('change', () => {
    const f = iconFile.files && iconFile.files[0];
    if (f) {
      if (!/^image\/(png|jpeg|webp)$/.test(f.type)) toast('仅支持 png / jpg / webp 图片');
      else if (f.size > 5 * 1024 * 1024) toast('图标不能超过 5MB');
      else setPendingIcon(f);
    }
    iconFile.value = '';
  });
  appModal.addEventListener('paste', e => {
    const items = e.clipboardData && e.clipboardData.items;
    if (!items) return;
    for (const it of items) {
      if (it.type && /^image\/(png|jpeg|webp)$/.test(it.type)) {
        const f = it.getAsFile();
        if (f) {
          if (f.size > 5 * 1024 * 1024) toast('图标不能超过 5MB');
          else {
            setPendingIcon(f);
            toast('已从剪贴板读取图片');
          }
          e.preventDefault();
          break;
        }
      }
    }
  });
  btnRemoveIcon.addEventListener('click', () => {
    clearPendingIcon();
    selectedGlyph = null;
    syncGlyphGrid();
    if (editingAppId) {
      const a = findApp(editingAppId);
      removeStoredIcon = !!(a && a.icon);
    }
    renderIconPreview();
  });
  fName.addEventListener('input', renderIconPreview);
  /* 非 textarea 字段回车直接保存 */
  [fName, fCwd, fPort].forEach(inp =>
    inp.addEventListener('keydown', e => { if (e.key === 'Enter') saveApp(); }));
}

/* ============================================================
   日志抽屉
   ============================================================ */
let logTimer = null;
let logAppId = null;
let logRequestSeq = 0;
let logController = null;
let logIsConsole = false;

function logEndpoint(appId) {
  return logIsConsole ? '/api/console/log?tail=300'
    : '/api/apps/' + appId + '/logs?tail=300';
}

export function openLogs(app) {
  openLogDrawer(app.id, (app.name || '') + ' · 日志');
}
export function openConsoleLog() {
  openLogDrawer('console', '总控台 · 日志');
}
function openLogDrawer(appId, title) {
  closeLogs();
  logAppId = appId;
  logIsConsole = appId === 'console';
  const requestSeq = ++logRequestSeq;
  drawerTitle.textContent = title;
  logPre.textContent = '加载中…';
  logBody.setAttribute('aria-busy', 'true');
  openLayer(logDrawer, drawerClose);
  drawerMask.classList.add('open');
  drawerMask.setAttribute('aria-hidden', 'false');
  fetchLogs(appId, requestSeq);
}
async function fetchLogs(appId, requestSeq) {
  if (!logAppId || logAppId !== appId || requestSeq !== logRequestSeq) return;
  const controller = new AbortController();
  logController = controller;
  try {
    const r = await fetch(logEndpoint(appId), {
      cache: 'no-store',
      signal: controller.signal,
    });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const j = await r.json();
    if (logAppId !== appId || requestSeq !== logRequestSeq) return;
    const firstLoad = logPre.textContent === '加载中…';
    const nearBottom = firstLoad ||
      logBody.scrollHeight - logBody.scrollTop - logBody.clientHeight < 48;
    const text = j.text || '';
    /* 增量追加新行：全量重写会打断用户选区并让滚动位置漂移。 */
    const previous = firstLoad ? '' : logPre.textContent;
    if (previous && text.startsWith(previous)) {
      logPre.append(document.createTextNode(text.slice(previous.length)));
    } else {
      logPre.textContent = text;
    }
    logBody.setAttribute('aria-busy', 'false');
    if (nearBottom) requestAnimationFrame(() => {
      if (logAppId === appId && requestSeq === logRequestSeq) {
        logBody.scrollTop = logBody.scrollHeight;
      }
    });
  } catch (e) {
    if (e.name !== 'AbortError' && logAppId === appId && requestSeq === logRequestSeq) {
      if (logPre.textContent === '加载中…') logPre.textContent = '日志加载失败，正在重试…';
      logBody.setAttribute('aria-busy', 'false');
    }
  } finally {
    if (logController === controller) logController = null;
    if (!document.hidden && logAppId === appId && requestSeq === logRequestSeq) {
      logTimer = setTimeout(() => fetchLogs(appId, requestSeq), 1500);
    }
  }
}
export function closeLogs() {
  logRequestSeq += 1;
  if (logTimer) { clearTimeout(logTimer); logTimer = null; }
  if (logController) { logController.abort(); logController = null; }
  logAppId = null;
  logBody.setAttribute('aria-busy', 'false');
  closeLayer(logDrawer);
  drawerMask.classList.remove('open');
  drawerMask.setAttribute('aria-hidden', 'true');
}
export function initLogDrawer() {
  drawerClose.addEventListener('click', closeLogs);
  drawerMask.addEventListener('click', closeLogs);
  document.addEventListener('visibilitychange', () => {
    if (!logAppId) return;
    if (document.hidden) {
      logRequestSeq += 1;
      if (logTimer) { clearTimeout(logTimer); logTimer = null; }
      if (logController) { logController.abort(); logController = null; }
    } else {
      fetchLogs(logAppId, ++logRequestSeq);
    }
  });
}
