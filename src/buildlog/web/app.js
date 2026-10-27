/* Buildlog workspace (spec §10.2). Layout and behaviour follow the approved reference screen
   (reperence/buildlog-workbench.html) with the store's real data. Talks only to the local API; the one
   write here (body edit) carries expected_version. */
(() => {
  'use strict';

  const meta = name => document.querySelector(`meta[name="${name}"]`).content;
  const TOKEN = meta('buildlog-token');
  const STORE = meta('buildlog-store');
  const ICONS = '/static/vendor/lucide-0.460.0.svg';
  const TYPE_LABEL = { experience: '경험', concept: '개념', method: '방법', source: '문서', index: '목차', schema_table: '테이블', schema_column: '컬럼' };
  const OBJECT_LABEL = { term: '용어', rule: '규칙', join_path: '조인 경로', relation: '관계' };
  const STATUS_LABEL = { draft: '초안', active: '활성', deprecated: '폐기', superseded: '대체됨' };
  const SOURCE_LABEL = { conversation: '대화', web: '웹', markdown: '문서', text: '문서', document: '문서', sql_ddl: 'DDL', schema_json: '스키마', sample_json: '샘플', sample_csv: '샘플' };
  const EVIDENCE_LABEL = { observed: '관찰', reported: '전달받음', inferred: '추론' };
  const SPECIAL = { list: '전체 문서', graph: '전체 그래프', settings: '설정' };
  // Screens that exist in the reference but are connected to the store in a later step of plan.md Phase 5.
  const LATER = { list: '5-4', search: '5-4', session: '5-4', source: '5-4', graph: '5-5', settings: '5-5' };
  const TAB_RE = /^(?:(?:doc|session|source):[0-9A-Za-z-]{1,80}|list|graph|settings)$/;
  const STORAGE_KEY = `buildlog.workbench:${STORE}`;

  const $ = (sel, scope = document) => scope.querySelector(sel);
  const root = $('#workbench');
  const app = $('#ww-app');
  const side = $('#ww-side');
  const main = $('#ww-main');
  const overlay = $('#ww-overlay');
  const statusBox = $('[data-status]');

  const state = {
    tabs: [], active: null, left: true, context: true,
    navigator: 'sessions', editing: false, draft: null, editVersion: null,
    info: '정보', searching: false, run: null,
  };
  const details = new Map();   // object id -> /api/objects detail
  const failures = new Map();  // object id -> error message
  const graphs = new Map();    // page id -> /api/graph?focus= data
  let message = null;          // {text, kind}; cleared on navigation
  let localCy = null;

  // ------------------------------------------------------------ helpers

  function el(tag, attrs = {}, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs)) {
      if (value === undefined || value === null || value === false) continue;
      if (key === 'class') node.className = value;
      else if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
      else node.setAttribute(key, value === true ? '' : value);
    }
    for (const child of children.flat(Infinity)) {
      if (child === null || child === undefined || child === false) continue;
      node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return node;
  }

  function icon(name) {
    const ns = 'http://www.w3.org/2000/svg';
    const svg = document.createElementNS(ns, 'svg');
    svg.setAttribute('class', 'w-icon');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('aria-hidden', 'true');
    const use = document.createElementNS(ns, 'use');
    use.setAttribute('href', `${ICONS}#${name}`);
    svg.append(use);
    return svg;
  }

  function escapeHtml(text) {
    return String(text).replace(/[&<>"']/g, ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]));
  }

  // Raw HTML inside stored Markdown is shown as text, and the result is sanitized again.
  marked.use({ renderer: { html(token) { return escapeHtml(typeof token === 'string' ? token : token.text); } } });

  function markdown(text, cls) {
    const div = el('div', { class: cls });
    div.innerHTML = DOMPurify.sanitize(marked.parse(text || ''), { USE_PROFILES: { html: true } });
    div.querySelectorAll('a').forEach(a => { a.rel = 'noopener noreferrer'; a.target = '_blank'; });
    return div;
  }

  function when(ts) {
    const d = new Date(ts);
    if (Number.isNaN(d.getTime())) return String(ts || '');
    const p = n => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
  }

  function cut(text, n) {
    const s = String(text || '').replace(/\s+/g, ' ').trim();
    return s.length > n ? `${s.slice(0, n - 1)}…` : s;
  }

  async function api(method, path, body) {
    const options = { method, headers: {} };
    if (body !== undefined) {
      options.headers['Content-Type'] = 'application/json';
      options.body = JSON.stringify(body);
    }
    if (method !== 'GET') options.headers['X-Buildlog-Token'] = TOKEN;
    const response = await fetch(path, options);
    const data = await response.json().catch(() => ({ error: `HTTP ${response.status}` }));
    if (!response.ok) {
      const error = new Error(data.error || `HTTP ${response.status}`);
      error.status = response.status;
      throw error;
    }
    return data;
  }

  function say(text, kind = 'info') {
    message = { text, kind };
    updateStatus();
  }

  function report(error) {
    if (error.status === 409) {
      say(`${error.message} — 다른 곳에서 먼저 바뀌었습니다. 입력한 내용은 그대로 있습니다. 복사해 둔 뒤 [취소]를 누르면 최신 내용을 불러옵니다.`, 'error');
    } else {
      say(error.message, 'error');
    }
  }

  // ------------------------------------------------------------ tabs, URL, stored preferences

  function loadPrefs() {
    try {
      const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || 'null');
      if (saved && Array.isArray(saved.tabs)) {
        state.tabs = saved.tabs.filter(t => t && TAB_RE.test(t.id)).slice(0, 30).map(t => ({ id: t.id, title: t.title || null }));
        state.left = saved.left !== false;
        state.context = saved.context !== false;
        return TAB_RE.test(saved.active || '') ? saved.active : null;
      }
    } catch (e) { /* storage may be unavailable; the page works without it */ }
    return null;
  }

  function savePrefs() {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify({ tabs: state.tabs, active: state.active, left: state.left, context: state.context }));
    } catch (e) { /* see loadPrefs */ }
  }

  const screenOf = id => (!id ? 'empty' : { doc: 'document', session: 'session', source: 'source' }[id.split(':')[0]] || id);
  const objectOf = id => id.slice(id.indexOf(':') + 1);
  const tabIcon = id => ({ graph: 'network', source: 'file-input', session: 'messages-square' }[screenOf(id)] || 'file-text');
  const tabTitle = t => t.title || SPECIAL[t.id] || (screenOf(t.id) === 'document' ? '불러오는 중…' : objectOf(t.id).slice(0, 8));

  function urlFor(id) {
    // Tab ids are [0-9A-Za-z:-] only, so the link stays readable: ?tab=doc:<id>
    if (!id) return location.pathname;
    return `?tab=${id}${id === 'graph' && state.run ? `&run=${encodeURIComponent(state.run)}` : ''}`;
  }

  function tabFromUrl() {
    const params = new URLSearchParams(location.search);
    if (params.get('run')) state.run = params.get('run');
    const tab = params.get('tab');
    if (tab) return TAB_RE.test(tab) ? tab : null;
    const older = params.get('open') || params.get('focus');      // older review links
    if (older) return TAB_RE.test(`doc:${older}`) ? `doc:${older}` : null;
    if (params.get('run')) return 'graph';                         // the link a save hands out
    return null;
  }

  function setTabTitle(id, title) {
    const tab = state.tabs.find(t => t.id === id);
    if (tab && tab.title !== title) { tab.title = title; savePrefs(); }
  }

  async function openTab(id, { push = true } = {}) {
    if (!id || !TAB_RE.test(id)) { say(`열 수 없는 탭입니다: ${id}`, 'error'); return; }
    const same = state.active === id;
    state.active = id;
    state.editing = false;
    state.draft = null;
    state.searching = false;
    message = null;
    if (!state.tabs.some(t => t.id === id)) state.tabs.push({ id, title: SPECIAL[id] || null });
    if (push && !same) history.pushState({ tab: id }, '', urlFor(id));
    savePrefs();
    render();
    if (screenOf(id) === 'document') await loadDocument(objectOf(id));
  }

  function closeTab(id) {
    const index = state.tabs.findIndex(t => t.id === id);
    if (index < 0) return;
    state.tabs.splice(index, 1);
    if (state.active === id) {
      if (state.tabs.length) { openTab(state.tabs[Math.min(index, state.tabs.length - 1)].id); return; }
      state.active = null;
      state.editing = false;
      history.pushState({ tab: null }, '', urlFor(null));
    }
    savePrefs();
    render();
  }

  // ------------------------------------------------------------ data

  async function loadDocument(objectId, { force = false } = {}) {
    const tabId = `doc:${objectId}`;
    try {
      let detail = details.get(objectId);
      if (!detail || force) {
        detail = await api('GET', `/api/objects/${encodeURIComponent(objectId)}`);
        details.set(detail.id, detail);
        failures.delete(objectId);
        if (detail.id !== objectId) {   // a merged page leads to its representative
          details.set(objectId, detail);
          const tab = state.tabs.find(t => t.id === tabId);
          const target = `doc:${detail.id}`;
          if (tab && !state.tabs.some(t => t.id === target)) tab.id = target;
          else if (tab) state.tabs = state.tabs.filter(t => t !== tab);
          if (state.active === tabId) { state.active = target; history.replaceState({ tab: target }, '', urlFor(target)); }
        }
      }
      setTabTitle(`doc:${detail.id}`, titleOf(detail));
      if (state.active !== `doc:${detail.id}`) return;
      render();
      if (detail.object_type === 'page') await loadGraph(detail.id, force);
    } catch (error) {
      failures.set(objectId, error.status === 404 ? '이 기록을 찾을 수 없습니다. 탭을 닫아 주세요.' : error.message);
      if (state.active === tabId) render();
      report(error);
    }
  }

  async function loadGraph(pageId, force) {
    if (!graphs.has(pageId) || force) {
      try {
        graphs.set(pageId, await api('GET', `/api/graph?focus=${encodeURIComponent(pageId)}`));
      } catch (error) {
        report(error);
        return;
      }
    }
    if (state.active === `doc:${pageId}`) drawLocalGraph();
  }

  function currentDetail() {
    return screenOf(state.active) === 'document' ? details.get(objectOf(state.active)) || null : null;
  }

  function titleOf(detail) {
    const record = detail.record;
    if (detail.object_type === 'page') return record.page.title;
    if (detail.object_type === 'term') return record.term.expression;
    if (detail.object_type === 'join_path') return record.join_path.title;
    if (detail.object_type === 'relation') return `${record.relation.source_title} → ${record.relation.target_title}`;
    return OBJECT_LABEL[detail.object_type] || detail.object_type;
  }

  // Pages linked to this one: index parents, relations both ways, index children. One entry per page.
  function relatedPages(detail) {
    const record = detail.record;
    const seen = new Set([record.page.id]);
    const out = [];
    const add = (id, title) => { if (!seen.has(id)) { seen.add(id); out.push({ id, title }); } };
    for (const p of record.index.parents) add(p.id, p.title);
    for (const r of record.relations.outgoing) add(r.target_id, r.other_title);
    for (const r of record.relations.incoming) add(r.source_id, r.other_title);
    for (const c of record.index.children) add(c.id, c.title);
    return out;
  }

  // ------------------------------------------------------------ rendering

  function render() {
    const screen = screenOf(state.active);
    sidebar();
    const withContext = state.context && !['settings', 'empty', 'list'].includes(screen);
    main.replaceChildren(
      tabstrip(),
      toolbar(screen),
      el('div', { class: `w-content${withContext ? '' : ' no-context'}` }, content(screen), withContext ? contextPanel(screen) : null),
    );
    updateStatus();
    searchDialog();
    if (screen === 'document') drawLocalGraph(); else destroyLocalGraph();
  }

  function sidebar() {
    side.hidden = !state.left;
    app.classList.toggle('hide-left', !state.left);
    const nav = (key, label) => el('button', { type: 'button', class: state.navigator === key ? 'active' : '', 'data-navigator': key,
      onclick: () => { state.navigator = key; sidebar(); } }, label);
    const label = state.navigator === 'sessions' ? '세션' : '소스';
    side.replaceChildren(
      el('div', { class: 'l-pane-title' }, '탐색기'),
      el('div', { class: 'l-navtabs', 'aria-label': '탐색 대상' }, nav('sessions', '세션'), nav('sources', '소스')),
      el('div', { class: 'l-navfilter' }, el('input', { 'data-navigator-filter': true, 'aria-label': '탐색 항목 필터', placeholder: '목록에서 찾기', disabled: true })),
      el('div', { class: 'l-tree-body' }, el('p', { class: 'w-muted' }, `${label} 목록은 Phase ${LATER.session}에서 저장소와 연결합니다.`)),
      el('div', { class: 'l-nav-footer' }, state.navigator === 'sessions' ? '목차 묶음' : '저장된 출처'),
    );
  }

  function tabstrip() {
    return el('nav', { class: 'w-tabs', 'aria-label': '본 작업 영역의 열린 탭' },
      el('button', { type: 'button', class: 'l-graph-entry', 'data-graph': true, 'aria-label': '전체 그래프 열기', onclick: () => openTab('graph') }, icon('network')),
      state.tabs.map(t => el('div', { class: `w-tab${t.id === state.active ? ' active' : ''}`, 'data-tab-id': t.id },
        el('button', { type: 'button', class: 'w-tab-title', 'data-editor-tab': t.id, onclick: () => openTab(t.id) }, icon(tabIcon(t.id)), el('span', {}, tabTitle(t))),
        el('button', { type: 'button', class: 'w-close', 'data-close': t.id, 'aria-label': `${tabTitle(t)} 탭 닫기`, onclick: () => closeTab(t.id) }, '×'))));
  }

  function toolbar(screen) {
    const detail = currentDetail();
    const tab = state.tabs.find(t => t.id === state.active);
    const label = {
      document: `문서 / ${detail ? titleOf(detail) : '불러오는 중'}`,
      graph: '그래프 / 전체 지식', list: '전체 문서', settings: '설정',
      session: `세션 / ${tab ? tabTitle(tab) : ''}`, source: `소스 / ${tab ? tabTitle(tab) : ''}`,
    }[screen] || '작업 영역';
    const actions = [];
    if (detail && detail.object_type === 'page') {
      if (state.editing) {
        actions.push(el('button', { type: 'button', 'data-cancel-edit': true, onclick: cancelEdit }, '취소'),
          el('button', { type: 'button', class: 'w-solid', 'data-save': true, onclick: saveEdit }, '저장'));
      } else {
        actions.push(el('button', { type: 'button', 'data-edit': true, onclick: startEdit }, '본문 수정'));
      }
    }
    actions.push(el('button', { type: 'button', class: 'w-outline', 'data-toggle-context': true,
      onclick: () => { state.context = !state.context; savePrefs(); render(); } }, state.context ? '속성 접기' : '속성 열기'));
    return el('header', { class: 'w-toolbar' },
      el('div', { class: 'w-actions' },
        el('button', { type: 'button', class: 'w-outline', 'data-toggle-left': true, onclick: () => { state.left = !state.left; savePrefs(); render(); } }, '탐색기'),
        el('span', { class: 'w-breadcrumb' }, label)),
      el('div', { class: 'w-actions' }, actions));
  }

  function content(screen) {
    if (screen === 'document') {
      const id = objectOf(state.active);
      const detail = currentDetail();
      if (detail) return paper(detail);
      if (failures.has(id)) return el('div', { class: 'w-empty' }, failures.get(id));
      return el('div', { class: 'w-empty' }, '불러오는 중입니다.');
    }
    if (screen === 'empty') return el('div', { class: 'w-empty' }, '왼쪽에서 문서나 소스를 선택하세요.');
    const title = { list: '전체 문서', graph: '전체 그래프', settings: '설정', session: '세션', source: '소스' }[screen];
    return el('div', { class: 'w-empty' }, el('h1', {}, title), el('p', {}, `이 화면은 Phase ${LATER[screen]}에서 저장소 데이터와 연결합니다.`));
  }

  function paper(detail) {
    if (detail.object_type !== 'page') {
      return el('article', { class: 'w-paper' }, el('div', { class: 'w-kind' }, OBJECT_LABEL[detail.object_type] || detail.object_type),
        markdown(detail.markdown, 'w-body'));
    }
    const page = detail.record.page;
    const parts = [
      el('div', { class: 'w-kind' }, TYPE_LABEL[page.type] || page.type),
      el('h1', { id: 'doc-title' }, page.title),
      el('p', { class: 'w-summary' }, page.summary_md),
    ];
    if (page.summary_dirty) {
      parts.push(el('p', { class: 'w-muted w-dirty', 'data-summary-dirty': true }, '요약이 최근 본문 수정 전 내용입니다. 본문을 기준으로 읽고, 요약은 갱신이 필요합니다.'));
    }
    if (state.editing) {
      const editor = el('textarea', { class: 'w-edit', 'data-editor': true, 'aria-label': '문서 본문' });
      editor.value = state.draft ?? page.body_md;
      editor.addEventListener('input', () => { state.draft = editor.value; });
      parts.push(editor, el('div', { class: 'w-muted' },
        `저장하면 새 버전(v${state.editVersion + 1})으로 기록됩니다. 요약은 바뀌지 않으며 '요약 갱신 필요'로 표시됩니다.`));
    } else if (detail.document_md) {
      parts.push(markdown(detail.document_md, 'w-body'));
    } else {
      parts.push(el('p', { class: 'w-muted' }, '요약 외의 본문이 없습니다.'));
    }
    const related = relatedPages(detail);
    parts.push(el('div', { class: 'w-related' }, el('h3', {}, '관련 문서'),
      related.length
        ? related.map(r => el('button', { type: 'button', class: 'w-inline-link', 'data-open': r.id, onclick: () => openTab(`doc:${r.id}`) }, `${r.title} →`))
        : el('p', { class: 'w-muted' }, '연결된 문서가 없습니다.')));
    return el('article', { class: 'w-paper' }, parts);
  }

  function contextPanel(screen) {
    const detail = currentDetail();
    if (screen !== 'document' || !detail) {
      return el('aside', { class: 'w-context' }, el('div', { class: 'w-empty' }, screen === 'document' ? '문서를 불러오는 중입니다.' : '이 화면의 속성은 다음 단계에서 연결합니다.'));
    }
    return el('aside', { class: 'w-context' },
      el('div', { class: 'w-context-top' }, el('span', {}, '주변 연결'), el('button', { type: 'button', 'data-graph': true, onclick: () => openTab('graph') }, '전체 그래프')),
      el('div', { class: 'w-local w-graph', id: 'local-graph', 'aria-label': '현재 문서 주변 그래프' }),
      detailsPanel(detail));
  }

  function detailsPanel(detail) {
    const tabs = el('div', { class: 'w-detail-tabs' }, ['정보', '이력', '근거'].map(name =>
      el('button', { type: 'button', class: name === state.info ? 'active' : '', 'data-info': name, onclick: () => { state.info = name; render(); } }, name)));
    const body = [];
    const isPage = detail.object_type === 'page';
    const obj = isPage ? detail.record.page : detail.record[detail.object_type];
    if (state.info === '정보') {
      const rows = [['유형', isPage ? TYPE_LABEL[obj.type] || obj.type : OBJECT_LABEL[detail.object_type]]];
      if (isPage) rows.push(['상태', `${STATUS_LABEL[obj.status] || obj.status}${obj.summary_dirty ? ' · 요약 갱신 필요' : ''}`], ['연결', `${relatedPages(detail).length}개 문서`]);
      rows.push(['버전', `v${obj.version}`], ['수정일', when(obj.updated_at)]);
      body.push(el('h3', {}, isPage ? '문서 속성' : '속성'), el('dl', {}, rows.map(([k, v]) => [el('dt', {}, k), el('dd', {}, v)])));
    } else if (state.info === '이력') {
      body.push(el('h3', {}, '변경 이력'), detail.revisions.map(r =>
        el('div', { class: 'w-detail-line', 'data-version': r.version }, `v${r.version} · ${when(r.changed_at)} · ${r.reason}`)));
    } else {
      const evidence = (isPage ? detail.record.evidence : obj.evidence) || [];
      body.push(el('h3', {}, '연결된 소스'), evidence.length ? evidence.map(e => el('div', { class: 'w-evidence' },
        el('button', { type: 'button', class: 'w-inline-link', 'data-source': e.source_id, onclick: () => openSource(e) }, `${SOURCE_LABEL[e.kind] || e.kind} · ${sourceTitle(e)}`),
        el('div', { class: 'w-muted' }, `${EVIDENCE_LABEL[e.evidence_kind] || e.evidence_kind} · ${cut(e.excerpt, 180)}`)))
        : el('p', { class: 'w-muted' }, '연결된 근거가 없습니다.'));
    }
    return el('div', { class: 'w-details' }, tabs, body);
  }

  function sourceTitle(evidence) {
    if (evidence.kind === 'web') {
      try { const metaData = JSON.parse(evidence.source_meta || '{}'); if (metaData.title) return cut(metaData.title, 60); } catch (e) { /* plain locator */ }
    }
    const locator = String(evidence.locator || '');
    if (['markdown', 'text', 'sql_ddl', 'schema_json', 'sample_json', 'sample_csv'].includes(evidence.kind)) return locator.split(/[\\/]/).pop();
    return cut(locator, 60);
  }

  function openSource(evidence) {
    const id = `source:${evidence.source_id}`;
    if (!state.tabs.some(t => t.id === id)) state.tabs.push({ id, title: sourceTitle(evidence) });
    openTab(id);
  }

  function searchDialog() {
    if (!state.searching) { overlay.replaceChildren(); return; }
    const close = () => { state.searching = false; searchDialog(); };
    overlay.replaceChildren(el('div', { class: 'w-overlay', onkeydown: e => { if (e.key === 'Escape') close(); } },
      el('section', { class: 'w-search-dialog', role: 'dialog', 'aria-modal': 'true', 'aria-label': '전체 지식 검색' },
        el('div', { class: 'w-search-head' }, el('h3', {}, '전체 지식 검색'), el('button', { type: 'button', 'data-search-close': true, onclick: close }, '닫기')),
        el('input', { 'data-query': true, 'aria-label': '검색어', placeholder: '문서 제목이나 내용 검색', disabled: true }),
        el('p', { class: 'w-muted' }, `검색은 Phase ${LATER.search}에서 저장소와 연결합니다.`))));
    $('[data-search-close]', overlay).focus();
  }

  function updateStatus() {
    statusBox.className = '';
    if (message) {
      statusBox.textContent = message.text;
      if (message.kind === 'ok' || message.kind === 'error') statusBox.classList.add(`is-${message.kind}`);
      return;
    }
    const screen = screenOf(state.active);
    const detail = currentDetail();
    if (screen === 'document' && detail) {
      const obj = detail.object_type === 'page' ? detail.record.page : detail.record[detail.object_type];
      const kind = detail.object_type === 'page' ? TYPE_LABEL[obj.type] || obj.type : OBJECT_LABEL[detail.object_type];
      statusBox.textContent = `문서 · ${kind} · v${obj.version}${obj.summary_dirty ? ' · 요약 갱신 필요' : ''}`;
    } else if (LATER[screen]) {
      statusBox.textContent = `${SPECIAL[screen] || (screen === 'session' ? '세션' : '소스')} · Phase ${LATER[screen]}에서 연결 예정`;
    } else {
      statusBox.textContent = '작업 공간';
    }
  }

  // ------------------------------------------------------------ local graph

  function colorOf(variable) {
    const probe = el('span');
    probe.style.color = `var(${variable})`;
    root.append(probe);
    const value = getComputedStyle(probe).color;
    probe.remove();
    return value;
  }

  // As in the reference: the current page on the left, its neighbours fanned out on the right.
  // With many neighbours a fan gets crowded, so they go round the current page instead.
  function localPositions(currentId, ids) {
    const others = ids.filter(id => id !== currentId);
    const positions = { [currentId]: { x: 0, y: 0 } };
    const fan = others.length <= 7;
    const radius = fan ? 150 : Math.max(95, others.length * 10);
    others.forEach((id, i) => {
      const angle = fan
        ? (others.length === 1 ? 0 : (-60 + (120 * i) / (others.length - 1))) * Math.PI / 180
        : (2 * Math.PI * i) / others.length;
      positions[id] = { x: Math.cos(angle) * radius, y: Math.sin(angle) * radius };
    });
    return positions;
  }

  function destroyLocalGraph() {
    if (localCy) { localCy.destroy(); localCy = null; }
    window.buildlogLocalCy = null;
  }

  function drawLocalGraph() {
    destroyLocalGraph();
    const box = document.getElementById('local-graph');
    const detail = currentDetail();
    if (!box || !detail) return;
    box.replaceChildren();
    if (detail.object_type !== 'page') { box.append(el('p', { class: 'w-muted' }, '문서 페이지가 아닌 항목입니다.')); return; }
    const data = graphs.get(detail.id);
    if (!data) { box.append(el('p', { class: 'w-muted' }, '주변 연결을 불러오는 중입니다.')); return; }
    const color = { dot: colorOf('--w-dot'), accent: colorOf('--w-accent'), line: colorOf('--w-line'), muted: colorOf('--w-muted'), text: colorOf('--w-text') };
    const elements = [
      ...data.nodes.map(n => ({ group: 'nodes', data: { id: n.id, label: n.title, type: n.type },
        classes: `${n.id === detail.id ? 'current' : ''}${data.nodes.length > 8 ? ' crowded' : ''}` })),
      ...data.edges.map(e => ({ group: 'edges', data: { id: e.id, source: e.source, target: e.target, type: e.type }, classes: e.kind })),
    ];
    localCy = cytoscape({
      container: box,
      elements,
      minZoom: 0.3,
      maxZoom: 2.5,
      autoungrabify: true,
      style: [
        { selector: 'node', style: { 'background-color': color.dot, width: 11, height: 11, label: 'data(label)', 'font-size': 10,
          'font-family': '"Segoe UI", "Malgun Gothic", sans-serif', color: color.muted, 'text-valign': 'bottom', 'text-margin-y': 5,
          'text-wrap': 'ellipsis', 'text-max-width': 110 } },
        { selector: 'node.current', style: { 'background-color': color.accent, width: 15, height: 15, color: color.text } },
        { selector: 'node.crowded', style: { 'text-max-width': 72 } },
        { selector: 'edge', style: { width: 1.5, 'line-color': color.line, 'curve-style': 'straight' } },
      ],
      layout: { name: 'preset', positions: localPositions(detail.id, data.nodes.map(n => n.id)), fit: true, padding: 18, animate: false },
    });
    localCy.on('tap', 'node', event => {
      const id = event.target.id();
      if (id !== detail.id) openTab(`doc:${id}`);
    });
    window.buildlogLocalCy = localCy;   // exposed for inspection and browser tests
    const hidden = data.truncated.nodes;
    if (hidden) box.setAttribute('title', `표시 한도로 ${hidden}개 문서를 생략했습니다.`);
  }

  // ------------------------------------------------------------ body edit

  function startEdit() {
    const detail = currentDetail();
    if (!detail || detail.object_type !== 'page') return;
    state.editing = true;
    state.draft = detail.record.page.body_md;
    state.editVersion = detail.record.page.version;
    message = null;
    render();
    const editor = $('[data-editor]', main);
    if (editor) editor.focus();
  }

  async function cancelEdit() {
    const detail = currentDetail();
    state.editing = false;
    state.draft = null;
    message = null;
    render();
    if (detail) await loadDocument(detail.id, { force: true });   // show the latest stored version
  }

  async function saveEdit() {
    const detail = currentDetail();
    if (!detail) return;
    const page = detail.record.page;
    const body = state.draft ?? page.body_md;
    if (body === page.body_md && state.editVersion === page.version) { say('바뀐 내용이 없습니다.'); return; }
    try {
      const result = await api('PATCH', `/api/pages/${encodeURIComponent(page.id)}`,
        { expected_version: state.editVersion, patch: { body_md: body }, reason: '작업 공간에서 본문 수정' });
      state.editing = false;
      state.draft = null;
      await loadDocument(page.id, { force: true });
      say(`저장했습니다 (v${result.version}).${result.summary_dirty ? ' 본문만 바뀌어 요약 갱신이 필요합니다.' : ''}`, 'ok');
    } catch (error) {
      report(error);
    }
  }

  // ------------------------------------------------------------ start

  $('[data-list]').addEventListener('click', () => openTab('list'));
  $('[data-settings]').addEventListener('click', () => openTab('settings'));
  $('[data-search]').addEventListener('click', () => { state.searching = true; searchDialog(); });
  window.addEventListener('popstate', event => {
    const id = (event.state && event.state.tab) || tabFromUrl();
    if (id) openTab(id, { push: false });
    else { state.active = null; render(); }
  });

  async function start() {
    const remembered = loadPrefs();
    let id = tabFromUrl() || remembered;
    if (!id) {
      try {   // first visit: open the record the latest save touched
        const data = await api('GET', '/api/graph');
        const first = data.seeds[0] || (data.nodes[0] && data.nodes[0].id);
        if (first) id = `doc:${first}`;
      } catch (error) { report(error); }
    }
    if (id) {
      history.replaceState({ tab: id }, '', urlFor(id));
      await openTab(id, { push: false });
    } else {
      render();
    }
    document.body.dataset.ready = 'true';
  }

  start();
})();
