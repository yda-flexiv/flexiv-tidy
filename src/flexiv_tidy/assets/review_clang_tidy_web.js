const token = new URLSearchParams(location.hash.slice(1)).get('token') || '';
const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

let snapshot = { summary: {}, findings: [], outcome: 'reviewing' };
let detail = null;
let selectedId = null;
let filter = 'pending';
let view = 'diff';
let toastTimer;

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      'X-Review-Token': token,
      ...(options.headers || {}),
    },
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || `Request failed (${response.status})`);
  return payload;
}

function toast(message, error = false) {
  clearTimeout(toastTimer);
  const element = $('#toast');
  element.textContent = message;
  element.className = `toast show${error ? ' error' : ''}`;
  toastTimer = setTimeout(() => { element.className = 'toast'; }, 3200);
}

function filteredFindings() {
  const needle = $('#search').value.trim().toLowerCase();
  return snapshot.findings.filter((finding) => {
    const matchesText = !needle || [finding.check, finding.message, finding.path, ...finding.files]
      .some((value) => value.toLowerCase().includes(needle));
    const matchesFilter = filter === 'all'
      || (filter === 'pending' && finding.status === 'pending')
      || (filter === 'fixable' && finding.fixable);
    return matchesText && matchesFilter;
  });
}

function statusLabel(status) {
  return ({
    pending: 'PENDING', accepted: 'ACCEPTED', rejected: 'REJECTED',
    deferred: 'DEFERRED', stale: 'STALE',
  })[status] || status.toUpperCase();
}

function renderSummary() {
  const summary = snapshot.summary;
  const percent = summary.total ? Math.round((summary.reviewed / summary.total) * 100) : 100;
  $('#progress-label').textContent = `${summary.reviewed} of ${summary.total} reviewed · ${summary.pending} remaining`;
  $('#changed-label').textContent = `${summary.changed_files} file${summary.changed_files === 1 ? '' : 's'} changed`;
  $('#progress-bar').style.width = `${percent}%`;
}

function renderQueue() {
  const visible = filteredFindings();
  $('#visible-count').textContent = visible.length;
  const list = $('#finding-list');
  list.replaceChildren();
  visible.forEach((finding) => {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = `finding-card${finding.id === selectedId ? ' active' : ''}`;

    const row = document.createElement('div');
    const check = document.createElement('code');
    check.textContent = finding.check;
    const status = document.createElement('i');
    status.className = `mini-status ${finding.status}`;
    status.title = statusLabel(finding.status);
    row.append(check, status);

    const message = document.createElement('p');
    message.textContent = finding.message;
    const locationLabel = document.createElement('small');
    locationLabel.textContent = `${finding.path}:${finding.line}`;
    button.append(row, message, locationLabel);
    button.addEventListener('click', () => select(finding.id));
    list.append(button);
  });
}

function appendCode(target, contents, focusLine = 0) {
  target.replaceChildren();
  contents.split('\n').forEach((text, index) => {
    const line = document.createElement('span');
    line.className = 'code-line';
    if (text.startsWith('+++') || text.startsWith('---')) line.classList.add('meta');
    else if (text.startsWith('+')) line.classList.add('add');
    else if (text.startsWith('-')) line.classList.add('remove');
    else if (text.startsWith('@@')) line.classList.add('hunk');
    if (focusLine && index + 1 === focusLine) line.classList.add('focus');
    line.textContent = text || ' ';
    target.append(line);
  });
  if (focusLine) {
    const focused = target.querySelector('.focus');
    if (focused) target.scrollTop = Math.max(0, focused.offsetTop - target.clientHeight / 2);
  }
}

function combinedFileText(kind) {
  if (!detail?.files.length) return '';
  return detail.files.map((file) => {
    const text = kind === 'before' ? file.before : file.proposed;
    return detail.files.length > 1 ? `// ===== ${file.path} =====\n${text}` : text;
  }).join('\n\n');
}

function renderCode() {
  if (!detail) return;
  const target = $('#code-view');
  if (view === 'diff') {
    $('#code-label').textContent = detail.diff ? 'Proposed change' : 'Source context';
    appendCode(target, detail.diff || combinedFileText('before'), detail.diff ? 0 : detail.finding.line);
  } else {
    $('#code-label').textContent = view === 'before' ? 'Current version' : 'Proposed version';
    appendCode(target, combinedFileText(view), detail.files.length === 1 ? detail.finding.line : 0);
  }
  $('#file-count').textContent = `${detail.files.length} file${detail.files.length === 1 ? '' : 's'}`;
}

function renderDetail() {
  const finding = detail?.finding;
  const hasFinding = Boolean(finding);
  $('#finding-detail').hidden = !hasFinding;
  $('#empty-state').hidden = hasFinding;
  if (!finding) return;

  $('#detail-check').textContent = finding.check;
  $('#detail-message').textContent = finding.message;
  $('#detail-location').textContent = finding.path;
  $('#detail-position').textContent = `:${finding.line}:${finding.column}`;
  const status = $('#detail-status');
  status.textContent = statusLabel(finding.status);
  status.className = `status-pill ${finding.status}`;

  const notice = $('#detail-notice');
  const noticeText = detail.error || (!finding.fixable
    ? 'No automatic fix is available. Choose Edit to make a manual correction, or reject/defer this finding.'
    : '');
  notice.textContent = noticeText;
  notice.hidden = !noticeText;
  $('#finding-index').textContent = `${finding.id} of ${snapshot.findings.length}`;

  const pending = finding.status === 'pending' && !detail.error;
  $('#reject').disabled = finding.status !== 'pending';
  $('#defer').disabled = finding.status !== 'pending';
  $('#edit').disabled = !(pending && detail.files.length > 0);
  $('#accept').disabled = !(pending && finding.fixable && detail.files.some((file) => file.changed));
  const position = snapshot.findings.findIndex((item) => item.id === finding.id);
  $('#previous').disabled = position <= 0;
  $('#next').disabled = position >= snapshot.findings.length - 1;
  renderCode();
}

async function select(id) {
  selectedId = id;
  renderQueue();
  try {
    detail = await api(`/api/findings/${id}`);
    renderDetail();
  } catch (error) {
    toast(error.message, true);
  }
}

function chooseDefaultSelection() {
  const visible = filteredFindings();
  if (visible.some((finding) => finding.id === selectedId)) return selectedId;
  return visible[0]?.id ?? null;
}

async function refresh(preferredId) {
  snapshot = await api('/api/state');
  renderSummary();
  const nextId = preferredId ?? chooseDefaultSelection();
  renderQueue();
  if (nextId) await select(nextId);
  else { detail = null; renderDetail(); }
}

function nextPending(afterId) {
  const candidates = filteredFindings().filter((finding) => finding.status === 'pending');
  const pending = candidates.find((finding) => finding.id > afterId) ?? candidates[0];
  return pending?.id ?? (filter === 'all' ? afterId : null);
}

async function decide(decision, edits = undefined) {
  if (!selectedId) return;
  const current = selectedId;
  try {
    snapshot = await api(`/api/findings/${current}`, {
      method: 'POST',
      body: JSON.stringify({ decision, ...(edits ? { edits } : {}) }),
    });
    renderSummary();
    const preferred = nextPending(current);
    renderQueue();
    if (preferred) await select(preferred);
    else { selectedId = null; detail = null; renderDetail(); }
  } catch (error) {
    toast(error.message, true);
    await refresh(current);
  }
}

function navigate(delta) {
  if (!selectedId) return;
  const index = snapshot.findings.findIndex((finding) => finding.id === selectedId);
  const next = snapshot.findings[index + delta];
  if (next) select(next.id);
}

function openEditor() {
  if (!detail?.files.length || $('#edit').disabled) return;
  const container = $('#editors');
  container.replaceChildren();
  detail.files.forEach((file) => {
    const wrapper = document.createElement('div');
    wrapper.className = 'editor';
    const label = document.createElement('label');
    label.textContent = file.path;
    const textarea = document.createElement('textarea');
    textarea.dataset.path = file.path;
    textarea.spellcheck = false;
    textarea.value = file.proposed;
    textarea.addEventListener('keydown', (event) => {
      if (event.key === 'Tab') {
        event.preventDefault();
        const start = textarea.selectionStart;
        textarea.setRangeText('    ', start, textarea.selectionEnd, 'end');
      }
    });
    wrapper.append(label, textarea);
    container.append(wrapper);
  });
  $('#edit-dialog').showModal();
  container.querySelector('textarea')?.focus();
}

async function openFinish() {
  try {
    const result = await api('/api/final-diff');
    const count = result.files.length;
    $('#finish-copy').textContent = count
      ? `${result.summary.accepted} accepted · ${result.summary.rejected} rejected · ${result.summary.deferred + result.summary.stale} deferred/stale · ${count} file${count === 1 ? '' : 's'} will be updated.`
      : 'No source changes are queued. You can finish and close this review safely.';
    appendCode($('#final-diff'), result.diff || 'No changes queued.');
    $('#write-changes').textContent = count
      ? `Write ${count} file${count === 1 ? '' : 's'}`
      : 'Finish review';
    $('#finish-dialog').showModal();
  } catch (error) {
    toast(error.message, true);
  }
}

async function finish(write) {
  try {
    const result = await api('/api/finish', {
      method: 'POST', body: JSON.stringify({ write }),
    });
    $('#finish-dialog').close();
    document.body.replaceChildren();
    const done = document.createElement('main');
    done.className = 'done';
    const icon = document.createElement('span');
    icon.textContent = write ? '✓' : '×';
    const title = document.createElement('h1');
    title.textContent = write ? 'Changes written' : 'Review discarded';
    const copy = document.createElement('p');
    copy.textContent = write
      ? `${result.written.length} file${result.written.length === 1 ? '' : 's'} updated. Rerun clang-tidy to verify remaining diagnostics.`
      : 'No source files were changed.';
    const hint = document.createElement('small');
    hint.textContent = 'You can close this tab.';
    done.append(icon, title, copy, hint);
    document.body.append(done);
  } catch (error) {
    toast(error.message, true);
  }
}

$('#search').addEventListener('input', () => {
  const id = chooseDefaultSelection();
  renderQueue();
  if (id) select(id);
});
$('#filters').addEventListener('click', (event) => {
  const button = event.target.closest('button[data-filter]');
  if (!button) return;
  filter = button.dataset.filter;
  $$('#filters button').forEach((item) => item.classList.toggle('active', item === button));
  const id = chooseDefaultSelection();
  renderQueue();
  if (id) select(id);
});
$('#view-tabs').addEventListener('click', (event) => {
  const button = event.target.closest('button[data-view]');
  if (!button) return;
  view = button.dataset.view;
  $$('#view-tabs button').forEach((item) => item.classList.toggle('active', item === button));
  renderCode();
});
$('#previous').addEventListener('click', () => navigate(-1));
$('#next').addEventListener('click', () => navigate(1));
$('#accept').addEventListener('click', () => decide('accept'));
$('#reject').addEventListener('click', () => decide('reject'));
$('#defer').addEventListener('click', () => decide('defer'));
$('#edit').addEventListener('click', openEditor);
$('#accept-edits').addEventListener('click', (event) => {
  event.preventDefault();
  const edits = Object.fromEntries(
    $$('#editors textarea').map((item) => [item.dataset.path, item.value]),
  );
  $('#edit-dialog').close();
  decide('accept', edits);
});
[$('#finish-button'), $('#empty-finish')].forEach((button) => {
  button.addEventListener('click', openFinish);
});
$('#discard-all').addEventListener('click', (event) => {
  event.preventDefault();
  finish(false);
});
$('#write-changes').addEventListener('click', (event) => {
  event.preventDefault();
  finish(true);
});

document.addEventListener('keydown', (event) => {
  if (event.metaKey || event.ctrlKey || event.altKey
      || event.target.matches('input, textarea') || document.querySelector('dialog[open]')) return;
  const key = event.key.toLowerCase();
  if (key === 'j' || event.key === 'ArrowDown') navigate(1);
  else if (key === 'k' || event.key === 'ArrowUp') navigate(-1);
  else if (key === 'a' && !$('#accept').disabled) decide('accept');
  else if (key === 'r' && !$('#reject').disabled) decide('reject');
  else if (key === 'd' && !$('#defer').disabled) decide('defer');
  else if (key === 'e' && !$('#edit').disabled) openEditor();
  else if (key === 'f') openFinish();
});

refresh().catch((error) => toast(error.message, true));
