const token = new URLSearchParams(location.hash.slice(1)).get('token') || '';
const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

let snapshot = { summary: {}, findings: [], outcome: 'reviewing' };
let detail = null;
let selectedId = null;
let filter = 'pending';
let toastTimer;
let editorGeneration = 0;
let modelSequence = 0;
let mainEditor = null;
let mainModels = [];
let mainFileIndex = 0;
let editEditor = null;
let editModels = [];
let editFileIndex = 0;

const monacoReady = new Promise((resolve, reject) => {
  if (typeof window.require !== 'function') {
    reject(new Error('Monaco loader is unavailable.'));
    return;
  }
  window.require.config({ paths: { vs: '/monaco/vs' } });
  window.require(
    ['vs/editor/editor.main', 'vs/basic-languages/monaco.contribution'],
    () => resolve(window.monaco),
    () => reject(new Error('Could not load the Monaco editor.')),
  );
});

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

function languageFor(path) {
  return /\.(?:c|cc|cp|cpp|cxx|c\+\+|h|hh|hpp|hxx|h\+\+|inl|ipp|tpp)$/i.test(path)
    ? 'cpp'
    : 'plaintext';
}

function modelUri(monaco, path, role) {
  modelSequence += 1;
  const safePath = path.replace(/^\/+/, '');
  return monaco.Uri.from({
    scheme: 'inmemory',
    authority: 'flexiv-tidy',
    path: `/${modelSequence}/${role}/${safePath}`,
  });
}

function editorOptions(readOnly = true) {
  return {
    automaticLayout: true,
    theme: 'vs-dark',
    readOnly,
    domReadOnly: readOnly,
    fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace',
    fontSize: 12,
    lineHeight: 20,
    lineNumbers: 'on',
    lineNumbersMinChars: 4,
    glyphMargin: true,
    minimap: { enabled: false },
    folding: false,
    overviewRulerLanes: 0,
    hideCursorInOverviewRuler: true,
    renderLineHighlight: 'all',
    scrollBeyondLastLine: false,
    smoothScrolling: true,
    wordWrap: 'off',
    fixedOverflowWidgets: true,
    padding: { top: 10, bottom: 10 },
  };
}

function disposeMainEditor() {
  if (mainEditor) mainEditor.dispose();
  mainEditor = null;
  mainModels.forEach((item) => {
    if (item.original) item.original.dispose();
    if (item.modified) item.modified.dispose();
    if (item.model) item.model.dispose();
  });
  mainModels = [];
  $('#monaco-editor').replaceChildren();
}

function disposeEditEditor() {
  if (editEditor) editEditor.dispose();
  editEditor = null;
  editModels.forEach((item) => item.model.dispose());
  editModels = [];
  $('#edit-monaco').replaceChildren();
}

function basename(path) {
  return path.split('/').pop() || path;
}

function renderFileTabs(container, files, activeIndex, onSelect) {
  container.replaceChildren();
  files.forEach((file, index) => {
    const button = document.createElement('button');
    button.type = 'button';
    button.role = 'tab';
    button.className = index === activeIndex ? 'active' : '';
    button.ariaSelected = String(index === activeIndex);
    button.title = file.path;
    button.textContent = basename(file.path);
    button.addEventListener('click', () => onSelect(index));
    container.append(button);
  });
}

function clampPosition(model, line, column) {
  const lineNumber = Math.min(Math.max(Number(line) || 1, 1), model.getLineCount());
  const maxColumn = model.getLineMaxColumn(lineNumber);
  return {
    lineNumber,
    column: Math.min(Math.max(Number(column) || 1, 1), maxColumn),
  };
}

function showMainFile(index) {
  if (!mainEditor || !mainModels.length) return;
  mainFileIndex = Math.min(Math.max(index, 0), mainModels.length - 1);
  const item = mainModels[mainFileIndex];
  renderFileTabs($('#file-tabs'), mainModels, mainFileIndex, showMainFile);

  if (item.modified) {
    mainEditor.setModel({ original: item.original, modified: item.modified });
    const target = item.path === detail.finding.path
      ? clampPosition(item.modified, detail.finding.line, detail.finding.column)
      : { lineNumber: 1, column: 1 };
    const editor = mainEditor.getModifiedEditor();
    const reveal = () => {
      if (editor.getModel() !== item.modified) return;
      editor.setPosition(target);
      editor.revealPositionInCenter(target);
    };
    reveal();
    requestAnimationFrame(reveal);
    let revealSubscription;
    revealSubscription = mainEditor.onDidUpdateDiff(() => {
      reveal();
      revealSubscription?.dispose();
    });
  } else {
    mainEditor.setModel(item.model);
    const target = item.path === detail.finding.path
      ? clampPosition(item.model, detail.finding.line, detail.finding.column)
      : { lineNumber: 1, column: 1 };
    mainEditor.setPosition(target);
    mainEditor.revealPositionInCenter(target);
  }
}

async function renderMainEditor() {
  const generation = ++editorGeneration;
  disposeMainEditor();
  $('#editor-loading').hidden = false;
  if (!detail?.files.length) {
    $('#editor-loading').textContent = 'No source file is available.';
    $('#file-tabs').replaceChildren();
    return;
  }

  try {
    const monaco = await monacoReady;
    if (generation !== editorGeneration || !detail) return;
    const automaticFix = detail.finding.fixable
      && !detail.error
      && detail.files.some((file) => file.changed);

    if (automaticFix) {
      mainModels = detail.files.map((file) => ({
        path: file.path,
        original: monaco.editor.createModel(
          file.before,
          languageFor(file.path),
          modelUri(monaco, file.path, 'before'),
        ),
        modified: monaco.editor.createModel(
          file.proposed,
          languageFor(file.path),
          modelUri(monaco, file.path, 'suggested'),
        ),
      }));
      mainEditor = monaco.editor.createDiffEditor($('#monaco-editor'), {
        ...editorOptions(true),
        originalEditable: false,
        renderSideBySide: true,
        useInlineViewWhenSpaceIsLimited: true,
        renderMarginRevertIcon: false,
        enableSplitViewResizing: true,
      });
      $('#editor-mode').textContent = 'Suggested diff';
    } else {
      mainModels = detail.files.map((file) => ({
        path: file.path,
        model: monaco.editor.createModel(
          file.before,
          languageFor(file.path),
          modelUri(monaco, file.path, 'error'),
        ),
      }));
      mainModels.forEach((item) => {
        if (item.path !== detail.finding.path) return;
        const position = clampPosition(item.model, detail.finding.line, detail.finding.column);
        monaco.editor.setModelMarkers(item.model, 'clang-tidy', [{
          startLineNumber: position.lineNumber,
          startColumn: position.column,
          endLineNumber: position.lineNumber,
          endColumn: Math.min(position.column + 1, item.model.getLineMaxColumn(position.lineNumber)),
          severity: monaco.MarkerSeverity.Error,
          message: detail.finding.message,
          source: detail.finding.check,
        }]);
      });
      mainEditor = monaco.editor.create($('#monaco-editor'), editorOptions(true));
      $('#editor-mode').textContent = 'Error location';
    }

    const findingFile = mainModels.findIndex((item) => item.path === detail.finding.path);
    mainFileIndex = findingFile >= 0 ? findingFile : 0;
    showMainFile(mainFileIndex);
    if (!automaticFix) {
      const active = mainModels[mainFileIndex];
      if (active?.model && active.path === detail.finding.path) {
        const position = clampPosition(active.model, detail.finding.line, detail.finding.column);
        mainEditor.createDecorationsCollection([{
          range: new monaco.Range(position.lineNumber, 1, position.lineNumber, 1),
          options: {
            isWholeLine: true,
            className: 'diagnostic-whole-line',
            glyphMarginClassName: 'diagnostic-glyph',
          },
        }]);
      }
    }
    $('#editor-loading').hidden = true;
  } catch (error) {
    if (generation !== editorGeneration) return;
    $('#editor-loading').textContent = error.message;
    toast(error.message, true);
  }
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

function appendCode(target, contents) {
  target.replaceChildren();
  contents.split('\n').forEach((text) => {
    const line = document.createElement('span');
    line.className = 'code-line';
    if (text.startsWith('+++') || text.startsWith('---')) line.classList.add('meta');
    else if (text.startsWith('+')) line.classList.add('add');
    else if (text.startsWith('-')) line.classList.add('remove');
    else if (text.startsWith('@@')) line.classList.add('hunk');
    line.textContent = text || ' ';
    target.append(line);
  });
}

function renderDetail() {
  const finding = detail?.finding;
  const hasFinding = Boolean(finding);
  $('#finding-detail').hidden = !hasFinding;
  $('#empty-state').hidden = hasFinding;
  if (!finding) {
    editorGeneration += 1;
    disposeMainEditor();
    return;
  }

  $('#detail-check').textContent = finding.check;
  $('#detail-message').textContent = finding.message;
  $('#detail-location').textContent = finding.path;
  $('#detail-position').textContent = `:${finding.line}:${finding.column}`;
  const status = $('#detail-status');
  status.textContent = statusLabel(finding.status);
  status.className = `status-pill ${finding.status}`;

  const notice = $('#detail-notice');
  const noticeText = detail.error || (!finding.fixable
    ? 'No automatic fix is available. Showing the exact error location; choose Edit to correct it manually.'
    : '');
  notice.textContent = noticeText;
  notice.hidden = !noticeText;
  $('#finding-index').textContent = `${finding.id} of ${snapshot.findings.length}`;
  $('#file-count').textContent = `${detail.files.length} file${detail.files.length === 1 ? '' : 's'}`;

  const pending = finding.status === 'pending' && !detail.error;
  $('#reject').disabled = finding.status !== 'pending';
  $('#defer').disabled = finding.status !== 'pending';
  $('#edit').disabled = !(pending && detail.files.length > 0);
  $('#accept').disabled = !(pending && finding.fixable && detail.files.some((file) => file.changed));
  const visible = filteredFindings();
  const position = visible.findIndex((item) => item.id === finding.id);
  $('#previous').disabled = position <= 0;
  $('#next').disabled = position < 0 || position >= visible.length - 1;
  renderMainEditor();
}

async function select(id) {
  selectedId = id;
  renderQueue();
  try {
    const result = await api(`/api/findings/${id}`);
    if (selectedId !== id) return;
    detail = result;
    renderDetail();
  } catch (error) {
    if (selectedId === id) toast(error.message, true);
  }
}

function chooseDefaultSelection() {
  const visible = filteredFindings();
  if (visible.some((finding) => finding.id === selectedId)) return selectedId;
  return visible.find((finding) => finding.status === 'pending' && finding.fixable)?.id
    ?? visible[0]?.id
    ?? null;
}

async function refresh(preferredId) {
  snapshot = await api('/api/state');
  renderSummary();
  const nextId = preferredId ?? chooseDefaultSelection();
  renderQueue();
  if (nextId) await select(nextId);
  else {
    selectedId = null;
    detail = null;
    renderDetail();
  }
}

function nextPending(afterId) {
  const candidates = filteredFindings().filter((finding) => finding.status === 'pending');
  const laterFixable = candidates.find((finding) => finding.id > afterId && finding.fixable);
  const anyFixable = candidates.find((finding) => finding.fixable);
  const later = candidates.find((finding) => finding.id > afterId);
  const pending = laterFixable ?? anyFixable ?? later ?? candidates[0];
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
    else {
      selectedId = null;
      detail = null;
      renderDetail();
    }
  } catch (error) {
    toast(error.message, true);
    await refresh(current);
  }
}

function navigate(delta) {
  if (!selectedId) return;
  const visible = filteredFindings();
  const index = visible.findIndex((finding) => finding.id === selectedId);
  const next = visible[index + delta];
  if (next) select(next.id);
}

function showEditFile(index) {
  if (!editEditor || !editModels.length) return;
  editFileIndex = Math.min(Math.max(index, 0), editModels.length - 1);
  const item = editModels[editFileIndex];
  editEditor.setModel(item.model);
  renderFileTabs($('#edit-file-tabs'), editModels, editFileIndex, showEditFile);
  renderEditDiagnostics(item);
  if (item.focusPosition) {
    editEditor.setPosition(item.focusPosition);
    editEditor.revealPositionInCenter(item.focusPosition);
  } else {
    editEditor.setPosition({ lineNumber: 1, column: 1 });
    editEditor.revealLine(1);
  }
  editEditor.focus();
}

function focusEditDiagnostic(item, diagnostic) {
  editEditor.setPosition(diagnostic.position);
  editEditor.revealPositionInCenter(diagnostic.position);
  editEditor.focus();
  $$('#edit-diagnostic-list .edit-diagnostic-item').forEach((button) => {
    button.classList.toggle('active', Number(button.dataset.findingId) === diagnostic.id);
  });
}

function renderEditDiagnostics(item) {
  const count = item.diagnostics.length;
  $('#edit-diagnostic-count').textContent = `${count} ERROR${count === 1 ? '' : 'S'}`;
  $('#edit-diagnostic-path').textContent = item.path;
  const list = $('#edit-diagnostic-list');
  list.replaceChildren();
  if (!count) {
    const empty = document.createElement('p');
    empty.className = 'edit-diagnostic-empty';
    empty.textContent = 'No diagnostics are located in this file.';
    list.append(empty);
    return;
  }
  item.diagnostics.forEach((diagnostic) => {
    const button = document.createElement('button');
    button.type = 'button';
    button.dataset.findingId = diagnostic.id;
    button.className = `edit-diagnostic-item${diagnostic.id === detail.finding.id ? ' active' : ''}`;
    button.title = diagnostic.message;

    const location = document.createElement('span');
    location.textContent = `${diagnostic.line}:${diagnostic.column}`;
    const copy = document.createElement('div');
    const message = document.createElement('strong');
    message.textContent = diagnostic.message;
    const check = document.createElement('code');
    check.textContent = diagnostic.check;
    copy.append(message, check);
    const status = document.createElement('i');
    status.className = diagnostic.status;
    status.title = statusLabel(diagnostic.status);
    button.append(location, copy, status);
    button.addEventListener('click', () => focusEditDiagnostic(item, diagnostic));
    list.append(button);
  });
}

async function openEditor() {
  if (!detail?.files.length || $('#edit').disabled) return;
  disposeEditEditor();
  $('#edit-dialog').showModal();
  try {
    const monaco = await monacoReady;
    if (!$('#edit-dialog').open) return;
    editModels = detail.files.map((file) => {
      const model = monaco.editor.createModel(
        file.proposed,
        languageFor(file.path),
        modelUri(monaco, file.path, 'manual-edit'),
      );
      const diagnostics = snapshot.findings
        .filter((finding) => finding.path === file.path)
        .sort((left, right) => left.line - right.line || left.column - right.column)
        .map((finding) => ({
          ...finding,
          position: clampPosition(model, finding.line, finding.column),
        }));
      monaco.editor.setModelMarkers(model, 'clang-tidy-manual', diagnostics.map((finding) => {
        const word = model.getWordAtPosition(finding.position);
        return {
          startLineNumber: finding.position.lineNumber,
          startColumn: finding.position.column,
          endLineNumber: finding.position.lineNumber,
          endColumn: word?.endColumn ?? Math.min(
            finding.position.column + 1,
            model.getLineMaxColumn(finding.position.lineNumber),
          ),
          severity: monaco.MarkerSeverity.Error,
          message: finding.message,
          source: finding.check,
        };
      }));
      const highlightedLines = new Map();
      diagnostics.forEach((finding) => {
        const line = finding.position.lineNumber;
        highlightedLines.set(line, highlightedLines.get(line) || finding.id === detail.finding.id);
      });
      model.deltaDecorations([], [...highlightedLines].map(([line, current]) => ({
        range: new monaco.Range(line, 1, line, model.getLineMaxColumn(line)),
        options: {
          isWholeLine: true,
          className: current ? 'manual-diagnostic-current-line' : 'manual-diagnostic-line',
          glyphMarginClassName: 'manual-diagnostic-glyph',
          overviewRuler: {
            color: '#e05645',
            position: monaco.editor.OverviewRulerLane.Right,
          },
        },
      })));
      const selected = diagnostics.find((finding) => finding.id === detail.finding.id);
      return {
        path: file.path,
        model,
        diagnostics,
        focusPosition: selected?.position ?? diagnostics[0]?.position,
      };
    });
    editEditor = monaco.editor.create($('#edit-monaco'), {
      ...editorOptions(false),
      tabSize: 4,
      insertSpaces: true,
    });
    showEditFile(Math.min(mainFileIndex, editModels.length - 1));
  } catch (error) {
    $('#edit-dialog').close();
    toast(error.message, true);
  }
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
    disposeMainEditor();
    disposeEditEditor();
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
  selectedId = null;
  const id = chooseDefaultSelection();
  renderQueue();
  if (id) select(id);
  else {
    detail = null;
    renderDetail();
  }
});
$('#filters').addEventListener('click', (event) => {
  const button = event.target.closest('button[data-filter]');
  if (!button) return;
  filter = button.dataset.filter;
  selectedId = null;
  $$('#filters button').forEach((item) => item.classList.toggle('active', item === button));
  const id = chooseDefaultSelection();
  renderQueue();
  if (id) select(id);
  else {
    detail = null;
    renderDetail();
  }
});
$('#previous').addEventListener('click', () => navigate(-1));
$('#next').addEventListener('click', () => navigate(1));
$('#accept').addEventListener('click', () => decide('accept'));
$('#reject').addEventListener('click', () => decide('reject'));
$('#defer').addEventListener('click', () => decide('defer'));
$('#edit').addEventListener('click', openEditor);
$('#edit-dialog').addEventListener('close', disposeEditEditor);
$('#accept-edits').addEventListener('click', (event) => {
  event.preventDefault();
  const edits = Object.fromEntries(editModels.map((item) => [item.path, item.model.getValue()]));
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
