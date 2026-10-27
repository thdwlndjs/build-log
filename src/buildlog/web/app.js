/* Buildlog review UI. Talks only to the local API; every change carries expected_version. */
(() => {
  'use strict';

  const TOKEN = document.querySelector('meta[name="buildlog-token"]').content;
  const PAGE_STATUSES = ['draft', 'active', 'deprecated', 'superseded'];
  const KNOWLEDGE_STATUSES = ['extracted', 'inferred', 'verified', 'rejected'];
  const RELATION_TYPES = ['USES', 'DEPENDS_ON', 'SUPPORTS', 'TESTED_BY', 'DERIVED_FROM', 'SUPERSEDES', 'CONTRADICTS', 'RELATED_TO', 'PART_OF', 'JOINS_TO'];
  const STATUS_LABEL = { extracted: '원문 추출', inferred: '추정', verified: '검증됨', rejected: '기각' };
  const TYPE_COLOR = { experience: '#2f5fb3', concept: '#7b4fb3', method: '#4f8fb3', source: '#6b6b66', index: '#333',
    schema_table: '#2e7d4f', schema_column: '#7fae8e' };

  let cy = null;
  let params = {};
  let current = null;

  // ------------------------------------------------------------ helpers

  const $ = (sel, root = document) => root.querySelector(sel);

  function el(tag, attrs = {}, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs)) {
      if (value === undefined || value === null || value === false) continue;
      if (key === 'text') node.textContent = value;
      else if (key === 'class') node.className = value;
      else if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
      else node.setAttribute(key, value === true ? '' : value);
    }
    for (const child of children.flat()) {
      if (child === null || child === undefined || child === false) continue;
      node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return node;
  }

  function escapeHtml(text) {
    return String(text).replace(/[&<>"']/g, ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]));
  }

  // Raw HTML inside stored Markdown is shown as text, and the result is sanitized again.
  marked.use({ renderer: { html(token) { return escapeHtml(typeof token === 'string' ? token : token.text); } } });

  function renderMd(markdown) {
    const div = el('div', { class: 'md' });
    div.innerHTML = DOMPurify.sanitize(marked.parse(markdown || ''), { USE_PROFILES: { html: true } });
    div.querySelectorAll('a').forEach(a => { a.rel = 'noopener noreferrer'; a.target = '_blank'; });
    return div;
  }

  function status(message, kind = 'info') {
    const box = $('#status');
    box.textContent = message;
    box.className = `status ${kind}`;
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
      error.kind = data.kind;
      error.status = response.status;
      throw error;
    }
    return data;
  }

  function reportError(error) {
    if (error.status === 409) {
      status(`${error.message} — 다른 곳에서 먼저 바뀌었습니다. '새로 고침'으로 최신 내용을 불러온 뒤 다시 고치세요.`, 'error');
    } else {
      status(error.message, 'error');
    }
  }

  function field(label, input) {
    return el('label', {}, label, input);
  }

  function select(options, value, attrs = {}) {
    const s = el('select', attrs);
    for (const option of options) {
      const [v, text] = Array.isArray(option) ? option : [option, STATUS_LABEL[option] || option];
      s.append(el('option', { value: v, selected: v === value, text }));
    }
    return s;
  }

  function changed(original, values) {
    const patch = {};
    for (const [key, value] of Object.entries(values)) {
      if ((original[key] ?? '') !== value) patch[key] = value;
    }
    return patch;
  }

  // ------------------------------------------------------------ graph

  function query(next) {
    const search = new URLSearchParams();
    for (const [k, v] of Object.entries(next)) if (v) search.set(k, v);
    return search;
  }

  async function loadGraph(next = params, { push = true } = {}) {
    params = { ...next };
    if ($('#columns-toggle').checked) params.columns = '1'; else delete params.columns;
    const search = query(params);
    if (push) history.replaceState(null, '', `?${search}`);
    try {
      const data = await api('GET', `/api/graph?${search}`);
      renderGraph(data);
      return data;
    } catch (error) {
      reportError(error);
      return null;
    }
  }

  function renderGraph(data) {
    const elements = [];
    for (const n of data.nodes) {
      const classes = [n.flag || '', n.summary_dirty ? 'dirty' : '', n.seed ? 'seed' : '', n.status === 'superseded' || n.status === 'deprecated' ? 'old' : ''];
      elements.push({ group: 'nodes', data: { id: n.id, label: n.title, type: n.type, color: TYPE_COLOR[n.type] || '#555' }, classes: classes.join(' ') });
    }
    for (const e of data.edges) {
      elements.push({ group: 'edges', data: { id: e.id, source: e.source, target: e.target, label: e.type, kind: e.kind },
        classes: `${e.status === 'inferred' ? 'inferred' : ''} ${e.kind}` });
    }
    if (cy) cy.destroy();
    cy = cytoscape({
      container: $('#graph'),
      elements,
      minZoom: 0.2,
      maxZoom: 3,
      style: [
        { selector: 'node', style: { 'background-color': 'data(color)', label: 'data(label)', 'font-size': 10, 'text-wrap': 'ellipsis',
          'text-max-width': 140, 'text-valign': 'bottom', 'text-margin-y': 4, width: 18, height: 18, 'border-width': 0 } },
        { selector: 'node.seed', style: { width: 24, height: 24 } },
        { selector: 'node.created', style: { 'border-width': 4, 'border-color': '#2e7d4f' } },
        { selector: 'node.updated', style: { 'border-width': 4, 'border-color': '#b26a00' } },
        { selector: 'node.dirty', style: { 'border-width': 3, 'border-style': 'dashed', 'border-color': '#b3261e' } },
        { selector: 'node.old', style: { opacity: 0.5 } },
        { selector: 'node:selected', style: { 'overlay-color': '#2f5fb3', 'overlay-opacity': 0.25 } },
        { selector: 'edge', style: { width: 1.5, 'line-color': '#9a9a94', 'target-arrow-color': '#9a9a94', 'target-arrow-shape': 'triangle',
          'curve-style': 'bezier', label: 'data(label)', 'font-size': 8, color: '#6b6b66', 'text-rotation': 'autorotate' } },
        { selector: 'edge.inferred', style: { 'line-style': 'dashed' } },
        { selector: 'edge.index', style: { 'line-color': '#cfcfc8', 'target-arrow-color': '#cfcfc8', label: '' } },
        { selector: 'node.hl', style: { 'background-color': '#d81b60', 'border-width': 3, 'border-color': '#d81b60' } },
        { selector: 'edge.hl', style: { 'line-color': '#d81b60', 'target-arrow-color': '#d81b60', width: 3 } },
      ],
      layout: { name: 'cose', animate: false, randomize: false, nodeDimensionsIncludeLabels: true, padding: 20 },
    });
    cy.on('tap', 'node', event => openObject(event.target.id()));
    cy.on('tap', 'edge', event => { if (event.target.data('kind') === 'relation') openObject(event.target.id()); });
    window.buildlogCy = cy;  // exposed for inspection and browser tests

    const list = $('#node-list');
    list.replaceChildren(...data.nodes.map(n => el('li', {},
      el('button', { type: 'button', 'data-node-id': n.id, onclick: () => openObject(n.id) },
        n.title, el('span', { class: 't', text: `${n.type}${n.flag ? ' · ' + flagLabel(n.flag) : ''}${n.summary_dirty ? ' · 요약 갱신 필요' : ''}` })))));
    const modeText = { run: '저장 실행', focus: '주변', search: '검색', recent: '최근 수정' }[data.mode];
    const truncated = data.truncated.nodes || data.truncated.edges
      ? ` · 표시 한도로 노드 ${data.truncated.nodes}개, 연결 ${data.truncated.edges}개를 생략했습니다. 노드를 골라 '주변 펼치기'로 보세요.` : '';
    $('#graph-info').textContent = `${modeText}: 노드 ${data.nodes.length}개, 연결 ${data.edges.length}개${truncated}`;
    if (!data.nodes.length) status(data.mode === 'search' ? '검색 결과가 없습니다.' : '표시할 기록이 없습니다.', 'info');
  }

  function flagLabel(flag) {
    return { created: '새로 저장', updated: '갱신', unchanged: '변경 없음', researched: '조사 대상' }[flag] || flag;
  }

  function highlightPath(path) {
    if (!cy) return;
    cy.elements().removeClass('hl');
    const missing = [];
    const ids = new Set();
    for (const step of path.steps) { ids.add(step.from_table_id); ids.add(step.to_table_id); }
    ids.forEach(id => { const node = cy.getElementById(id); if (node.nonempty()) node.addClass('hl'); else missing.push(id); });
    for (const step of path.steps) {
      cy.edges().filter(e => (e.data('source') === step.from_table_id && e.data('target') === step.to_table_id)
        || (e.data('source') === step.to_table_id && e.data('target') === step.from_table_id)).addClass('hl');
    }
    status(missing.length ? `경로의 테이블 ${missing.length}개는 지금 그래프에 없습니다. 테이블 노드에서 '주변 펼치기'를 쓰세요.` : `조인 경로 '${path.title}'를 강조했습니다.`, 'info');
  }

  // ------------------------------------------------------------ panel

  async function openObject(id) {
    try {
      const detail = await api('GET', `/api/objects/${encodeURIComponent(id)}`);
      current = detail;
      if (cy) { cy.elements().unselect(); cy.getElementById(detail.id).select(); }
      renderPanel(detail);
      return detail;
    } catch (error) {
      reportError(error);
      return null;
    }
  }

  async function refresh(message) {
    if (current) await openObject(current.id);
    await loadGraph(params, { push: false });
    if (message) status(message, 'ok');
  }

  function renderPanel(detail) {
    const panel = $('#panel');
    panel.replaceChildren();
    if (detail.object_type === 'page') panel.append(...pageView(detail));
    else if (detail.object_type === 'relation') panel.append(...relationView(detail));
    else panel.append(...genericView(detail));
  }

  function tabs(defs) {
    const bar = el('div', { class: 'tabs', role: 'tablist' });
    const body = el('div', { class: 'tab' });
    const show = (index) => {
      [...bar.children].forEach((b, i) => b.setAttribute('aria-selected', i === index ? 'true' : 'false'));
      body.replaceChildren(defs[index].render());
    };
    defs.forEach((def, i) => bar.append(el('button', { type: 'button', role: 'tab', 'data-tab': def.key, onclick: () => show(i) }, def.label)));
    show(0);
    return [bar, body];
  }

  function pageView(detail) {
    const p = detail.record.page;
    const rec = detail.record;
    const isSchema = p.type === 'schema_table' || p.type === 'schema_column';
    const rels = [...rec.relations.outgoing.map(r => ({ ...r, dir: 'out' })), ...rec.relations.incoming.map(r => ({ ...r, dir: 'in' }))];
    const head = [
      el('h2', { id: 'panel-title', text: p.title }),
      el('div', { class: 'meta' }, `${p.type} · ${p.status} · v${p.version} · `, el('code', { text: p.id })),
      el('div', {},
        p.summary_dirty ? el('span', { class: 'badge err', text: '요약 갱신 필요' }) : null,
        rec.redirected_from ? el('span', { class: 'badge warn', text: `병합된 ${rec.redirected_from}에서 이동` }) : null,
        detail.merged_from.length ? el('span', { class: 'badge', text: `병합됨 ${detail.merged_from.length}건` }) : null),
      el('div', { class: 'actions' },
        el('button', { type: 'button', onclick: () => loadGraph({ focus: p.id }) }, '주변 펼치기'),
        el('button', { type: 'button', onclick: () => refresh('새로 불러왔습니다.') }, '새로 고침')),
    ];
    return [...head, ...tabs([
      { key: 'body', label: '본문', render: () => renderMd(detail.markdown) },
      { key: 'edit', label: '편집', render: () => pageForm(p, isSchema, detail.feedback) },
      { key: 'relations', label: `관계 ${rels.length}`, render: () => relationsList(rels) },
      { key: 'knowledge', label: `용어·규칙·경로 ${rec.terms.length + rec.rules.length + rec.join_paths.length}`, render: () => knowledgeList(rec) },
      { key: 'feedback', label: `피드백 ${detail.feedback.length}`, render: () => feedbackView(p, detail.feedback) },
      { key: 'merge', label: '병합', render: () => mergeView(p, isSchema) },
      { key: 'history', label: '이력', render: () => historyView(detail) },
    ])];
  }

  function feedbackPicker(feedback) {
    const open = feedback.filter(f => f.status === 'open');
    if (!open.length) return null;
    return field('이 수정으로 해결하는 피드백(선택)', select([['', '없음'], ...open.map(f => [f.id, f.feedback_text.slice(0, 60)])], '', { name: 'feedback' }));
  }

  function pageForm(p, isSchema, feedback) {
    const title = el('input', { type: 'text', name: 'title', value: p.title, disabled: isSchema });
    const summary = el('textarea', { name: 'summary_md' }); summary.value = p.summary_md;
    const body = el('textarea', { name: 'body_md', rows: 14 }); body.value = p.body_md;
    const pageStatus = select(PAGE_STATUSES, p.status, { name: 'status' });
    const reason = el('input', { type: 'text', name: 'reason', value: '검토 화면에서 수정' });
    const picker = feedbackPicker(feedback);
    const save = async () => {
      const values = { title: title.value, summary_md: summary.value, body_md: body.value, status: pageStatus.value };
      if (isSchema) delete values.title;
      const patch = changed(p, values);
      if (!Object.keys(patch).length) { status('바뀐 내용이 없습니다.', 'info'); return; }
      try {
        const result = await api('PATCH', `/api/pages/${p.id}`, { expected_version: p.version, patch, reason: reason.value,
          feedback_id: picker ? $('select', picker).value || undefined : undefined });
        await refresh(`저장했습니다 (v${result.version}).${result.summary_dirty ? ' 본문만 바뀌어 요약 갱신이 필요합니다.' : ''}`);
      } catch (error) { reportError(error); }
    };
    return el('div', { class: 'form' },
      isSchema ? el('p', { class: 'note', text: '테이블·컬럼 제목은 스키마에서 오므로 여기서 바꾸지 않습니다.' }) : null,
      field('제목', title), field('요약', summary), field('본문 (Markdown)', body), field('상태', pageStatus),
      el('p', { class: 'note', text: '본문만 고치고 요약을 그대로 두면 요약에 "갱신 필요" 표시가 붙습니다. 검색에는 바로 새 본문이 반영됩니다.' }),
      field('수정 이유', reason), picker,
      el('div', { class: 'row' }, el('button', { type: 'button', class: 'primary', 'data-action': 'save-page', onclick: save }, '저장')));
  }

  function relationText(r) {
    return r.dir === 'out' ? `이 페이지 —${r.type}→ ${r.other_title}` : `${r.other_title} —${r.type}→ 이 페이지`;
  }

  function relationsList(rels) {
    if (!rels.length) return el('p', { class: 'note', text: '연결된 관계가 없습니다.' });
    return el('div', {}, rels.map(r => {
      const slot = el('div');
      return el('div', { class: 'item', 'data-relation-id': r.id },
        el('div', { class: 'head', text: relationText(r) }),
        el('div', { class: 'meta', text: `${STATUS_LABEL[r.status] || r.status} · v${r.version}` }),
        el('div', { text: r.explanation_md }),
        el('div', { class: 'actions' },
          el('button', { type: 'button', onclick: () => openObject(r.id) }, '열기'),
          el('button', { type: 'button', 'data-action': 'edit-relation', onclick: () => slot.replaceChildren(relationForm(r)) }, '수정'),
          el('button', { type: 'button', class: 'danger', 'data-action': 'unlink-relation', onclick: () => slot.replaceChildren(unlinkForm(r)) }, '연결 해제')),
        slot);
    }));
  }

  function relationForm(r) {
    const type = select(RELATION_TYPES.map(t => [t, t]), r.type, { name: 'type' });
    const explanation = el('textarea', { name: 'explanation_md' }); explanation.value = r.explanation_md;
    const relStatus = select(KNOWLEDGE_STATUSES, r.status, { name: 'status' });
    const reverse = el('input', { type: 'checkbox', name: 'reverse' });
    const reason = el('input', { type: 'text', name: 'reason', value: '검토 화면에서 관계 수정' });
    const save = async () => {
      const patch = changed(r, { type: type.value, explanation_md: explanation.value, status: relStatus.value });
      if (reverse.checked) patch.reverse = true;
      if (!Object.keys(patch).length) { status('바뀐 내용이 없습니다.', 'info'); return; }
      try {
        const result = await api('PATCH', `/api/relations/${r.id}`, { expected_version: r.version, patch, reason: reason.value });
        await refresh(`관계를 저장했습니다 (v${result.version}).`);
      } catch (error) { reportError(error); }
    };
    return el('div', { class: 'form' }, field('관계 종류', type), field('설명', explanation), field('상태', relStatus),
      el('label', { class: 'row' }, reverse, '방향 뒤집기'), field('수정 이유', reason),
      el('div', { class: 'row' }, el('button', { type: 'button', class: 'primary', 'data-action': 'save-relation', onclick: save }, '관계 저장')));
  }

  function unlinkForm(r) {
    const reason = el('input', { type: 'text', name: 'reason', placeholder: '해제하는 이유 (필수)' });
    const go = async () => {
      if (!reason.value.trim()) { status('해제 이유를 적어 주세요.', 'error'); return; }
      try {
        await api('POST', `/api/relations/${r.id}/unlink`, { expected_version: r.version, reason: reason.value });
        await refresh('관계를 해제했습니다. 기록과 근거, 이력은 남아 있습니다.');
      } catch (error) { reportError(error); }
    };
    return el('div', { class: 'form' }, el('p', { class: 'note', text: '해제한 관계는 조회와 그래프에서 빠지지만 기록·근거·이력은 남습니다.' }),
      field('이유', reason), el('div', { class: 'row' }, el('button', { type: 'button', class: 'danger', 'data-action': 'confirm-unlink', onclick: go }, '해제 확인')));
  }

  function editButton(label, build) {
    const slot = el('div');
    return [el('div', { class: 'actions' }, el('button', { type: 'button', 'data-action': 'edit', onclick: () => slot.replaceChildren(build()) }, label)), slot];
  }

  function knowledgeList(rec) {
    const box = el('div');
    if (rec.terms.length) box.append(el('h3', { text: '용어' }));
    for (const t of rec.terms) {
      box.append(el('div', { class: 'item', 'data-term-id': t.id },
        el('div', { class: 'head', text: `${t.expression} (맥락: ${t.scope})` }),
        el('div', { class: 'meta', text: `${STATUS_LABEL[t.status] || t.status} · v${t.version}${t.aliases.length ? ' · 별칭: ' + t.aliases.join(', ') : ''}` }),
        renderMd(t.definition_md), ...editButton('용어 수정', () => termForm(t))));
    }
    if (rec.rules.length) box.append(el('h3', { text: '필수 조건·금칙·미지원 연산' }));
    for (const r of rec.rules) {
      box.append(el('div', { class: 'item', 'data-rule-id': r.id },
        el('div', { class: 'head', text: `${{ requirement: '필수 조건', prohibition: '금칙', unsupported_operation: '미지원 연산' }[r.kind]} — ${r.instruction_md}` }),
        el('div', { class: 'meta', text: `대상: ${r.scope_title} · ${STATUS_LABEL[r.status] || r.status} · v${r.version}` }),
        ...editButton('규칙 수정', () => ruleForm(r))));
    }
    if (rec.join_paths.length) box.append(el('h3', { text: '조인 경로' }));
    for (const j of rec.join_paths) {
      box.append(el('div', { class: 'item', 'data-path-id': j.id },
        el('div', { class: 'head', text: j.title }),
        el('div', { class: 'meta', text: `${STATUS_LABEL[j.status] || j.status} · v${j.version} · ${j.purpose}` }),
        el('ol', {}, j.steps.map(s => el('li', { text: `${s.from_table}.${s.from_columns.join(',')} → ${s.to_table}.${s.to_columns.join(',')} · 관계 수 ${s.cardinality}` }))),
        j.caveats_md ? el('div', { class: 'note', text: `주의: ${j.caveats_md}` }) : null,
        el('div', { class: 'actions' }, el('button', { type: 'button', 'data-action': 'highlight-path', onclick: () => highlightPath(j) }, '그래프에서 강조')),
        ...editButton('경로 수정', () => pathForm(j))));
    }
    if (!box.children.length) box.append(el('p', { class: 'note', text: '이 페이지에 연결된 용어·규칙·조인 경로가 없습니다.' }));
    return box;
  }

  function simpleForm(obj, route, fields, extras = {}) {
    const inputs = {};
    const rows = fields.map(([key, label, kind, options]) => {
      let input;
      if (kind === 'select') input = select(options, obj[key], { name: key });
      else if (kind === 'textarea') { input = el('textarea', { name: key }); input.value = obj[key] ?? ''; }
      else input = el('input', { type: 'text', name: key, value: obj[key] ?? '' });
      inputs[key] = input;
      return field(label, input);
    });
    const reason = el('input', { type: 'text', name: 'reason', value: '검토 화면에서 수정' });
    const save = async () => {
      const values = Object.fromEntries(Object.entries(inputs).map(([k, i]) => [k, i.value]));
      const patch = changed(obj, values);
      if (extras.toPatch) extras.toPatch(patch, values);
      if (!Object.keys(patch).length) { status('바뀐 내용이 없습니다.', 'info'); return; }
      try {
        const result = await api('PATCH', `/api/${route}/${obj.id}`, { expected_version: obj.version, patch, reason: reason.value });
        await refresh(`저장했습니다 (v${result.version}).`);
      } catch (error) { reportError(error); }
    };
    return el('div', { class: 'form' }, rows, field('수정 이유', reason),
      el('div', { class: 'row' }, el('button', { type: 'button', class: 'primary', 'data-action': 'save', onclick: save }, '저장')));
  }

  function termForm(t) {
    const obj = { ...t, aliases_text: t.aliases.join(', ') };
    return simpleForm(obj, 'terms', [
      ['definition_md', '정의', 'textarea'], ['scope', '맥락(scope)', 'text'], ['status', '상태', 'select', KNOWLEDGE_STATUSES],
      ['aliases_text', '별칭 (쉼표로 구분)', 'text'],
    ], { toPatch: (patch, values) => {
      delete patch.aliases_text;
      if (values.aliases_text !== obj.aliases_text) patch.aliases = values.aliases_text.split(',').map(s => s.trim()).filter(Boolean);
    } });
  }

  function ruleForm(r) {
    const fields = [['instruction_md', '지시', 'textarea'], ['condition_md', '적용 조건', 'textarea'], ['reason_md', '이유', 'textarea'],
      ['alternative_md', '대안', 'textarea'], ['status', '상태', 'select', KNOWLEDGE_STATUSES]];
    if (r.kind === 'unsupported_operation') fields.push(['confirmation', '부재 확인', 'select', [['confirmed', '부재 확인됨'], ['unconfirmed', '미확인']]]);
    return simpleForm(r, 'rules', fields);
  }

  function pathForm(j) {
    return simpleForm(j, 'paths', [['title', '제목', 'text'], ['purpose', '목적', 'textarea'], ['conditions_md', '조건', 'textarea'],
      ['caveats_md', '주의점', 'textarea'], ['status', '상태', 'select', KNOWLEDGE_STATUSES]]);
  }

  function feedbackView(p, feedback) {
    const text = el('textarea', { name: 'feedback', placeholder: '무엇이 틀렸거나 빠졌는지 적어 주세요.' });
    const send = async () => {
      if (!text.value.trim()) { status('피드백 내용을 적어 주세요.', 'error'); return; }
      try {
        await api('POST', '/api/feedback', { target_id: p.id, target_version: p.version, text: text.value });
        await refresh('피드백을 기록했습니다. 수정할 때 이 피드백을 골라 해결로 표시할 수 있습니다.');
      } catch (error) { reportError(error); }
    };
    const setStatus = async (f, value) => {
      try { await api('POST', `/api/feedback/${f.id}/status`, { status: value }); await refresh('피드백 상태를 바꿨습니다.'); }
      catch (error) { reportError(error); }
    };
    return el('div', {},
      el('div', { class: 'form' }, field(`피드백 (v${p.version} 기준)`, text),
        el('div', { class: 'row' }, el('button', { type: 'button', class: 'primary', 'data-action': 'send-feedback', onclick: send }, '피드백 남기기'))),
      feedback.map(f => el('div', { class: 'item', 'data-feedback-id': f.id },
        el('div', { class: 'meta', text: `${f.status} · v${f.target_version} · ${f.created_at}${f.resulting_revision ? ' · 해결한 수정 ' + f.resulting_revision : ''}` }),
        el('div', { text: f.feedback_text }),
        f.status === 'open' ? el('div', { class: 'actions' },
          el('button', { type: 'button', onclick: () => setStatus(f, 'addressed') }, '해결됨'),
          el('button', { type: 'button', onclick: () => setStatus(f, 'dismissed') }, '기각')) : null)));
  }

  function mergeView(p, isSchema) {
    if (isSchema) return el('p', { class: 'note', text: '테이블·컬럼 페이지는 스키마를 다시 넣어 정리합니다. 병합하지 않습니다.' });
    const input = el('input', { type: 'search', name: 'merge-search', placeholder: '중복된 페이지를 찾을 검색어' });
    const results = el('div');
    const reason = el('input', { type: 'text', name: 'reason', placeholder: '병합 이유 (필수)' });
    const chosen = new Set();
    const find = async () => {
      try {
        const found = await api('GET', `/api/search?q=${encodeURIComponent(input.value)}`);
        const items = [...found.results, ...found.out_of_scope].filter(r => r.id !== p.id && !String(r.type || '').startsWith('schema_'));
        results.replaceChildren(...(items.length ? items : []).map(r => el('label', { class: 'row', 'data-candidate-id': r.id },
          el('input', { type: 'checkbox', onchange: e => e.target.checked ? chosen.add(r.id) : chosen.delete(r.id) }), `${r.title} (${r.id.slice(0, 8)})`)));
        if (!items.length) results.replaceChildren(el('p', { class: 'note', text: '후보가 없습니다.' }));
      } catch (error) { reportError(error); }
    };
    const merge = async () => {
      if (!chosen.size) { status('합칠 중복 페이지를 고르세요.', 'error'); return; }
      if (!reason.value.trim()) { status('병합 이유를 적어 주세요.', 'error'); return; }
      try {
        const versions = { [p.id]: p.version };
        for (const id of chosen) versions[id] = (await api('GET', `/api/objects/${id}`)).record.page.version;
        const report = await api('POST', '/api/merge', { source_ids: [...chosen], target_id: p.id, expected_versions: versions, reason: reason.value });
        await refresh(`병합했습니다: ${report.merged.map(m => m.title).join(', ')} → 이 페이지. 관계 ${report.relations_moved}개 이동, ${report.relations_folded}개 합침. 본문은 필요하면 직접 고치세요.`);
      } catch (error) { reportError(error); }
    };
    return el('div', { class: 'form' },
      el('p', { class: 'note', text: '이 페이지를 대표로 두고 고른 중복 페이지를 합칩니다. 중복 페이지는 "대체됨"이 되고 그 ID는 이 페이지로 연결됩니다. 관계·근거·용어·규칙은 이 페이지로 옮겨집니다.' }),
      el('div', { class: 'row' }, input, el('button', { type: 'button', 'data-action': 'merge-search', onclick: find }, '찾기')),
      results, field('이유', reason),
      el('div', { class: 'row' }, el('button', { type: 'button', class: 'danger', 'data-action': 'confirm-merge', onclick: merge }, '이 페이지로 병합')));
  }

  function historyView(detail) {
    return el('div', {},
      detail.merged_from.length ? el('div', { class: 'warnbox' }, '병합된 페이지: ', detail.merged_from.map(m => `${m.title} (${m.id.slice(0, 8)})`).join(', ')) : null,
      el('ol', {}, detail.revisions.map(r => el('li', { text: `v${r.version} · ${r.changed_at} · ${r.reason}` }))));
  }

  function relationView(detail) {
    const r = detail.record.relation;
    const rel = { ...r, dir: 'out', other_title: r.target_title };
    return [
      el('h2', { id: 'panel-title', text: `${r.source_title} —${r.type}→ ${r.target_title}` }),
      el('div', { class: 'meta' }, `관계 · ${STATUS_LABEL[r.status] || r.status} · v${r.version} · `, el('code', { text: r.id })),
      r.removed_at ? el('div', { class: 'warnbox', text: `연결 해제됨 (${r.removed_at}): ${r.removed_reason}` }) : null,
      el('div', { class: 'actions' },
        el('button', { type: 'button', onclick: () => openObject(r.source_id) }, '출발 페이지'),
        el('button', { type: 'button', onclick: () => openObject(r.target_id) }, '도착 페이지')),
      renderMd(detail.markdown),
      r.removed_at ? null : el('h3', { text: '수정' }), r.removed_at ? null : relationForm(rel),
      r.removed_at ? null : el('h3', { text: '연결 해제' }), r.removed_at ? null : unlinkForm(rel),
    ];
  }

  function genericView(detail) {
    const kind = detail.object_type;
    const obj = detail.record[kind];
    const title = { term: `용어: ${obj.expression}`, rule: '규칙', join_path: `조인 경로: ${obj.title}` }[kind];
    const form = kind === 'term' ? termForm(obj) : kind === 'rule' ? ruleForm(obj) : pathForm(obj);
    return [el('h2', { id: 'panel-title', text: title }), el('div', { class: 'meta' }, `${kind} · v${obj.version} · `, el('code', { text: obj.id })),
      kind === 'join_path' ? el('div', { class: 'actions' }, el('button', { type: 'button', onclick: () => highlightPath(obj) }, '그래프에서 강조')) : null,
      renderMd(detail.markdown), el('h3', { text: '수정' }), form];
  }

  // ------------------------------------------------------------ start

  async function loadRuns() {
    try {
      const data = await api('GET', '/api/runs');
      const s = $('#run-select');
      s.replaceChildren(el('option', { value: '', text: '최근 저장' }),
        ...data.runs.filter(r => r.status === 'succeeded').map(r => el('option', { value: r.id, selected: r.id === params.run,
          text: `${r.updated_at.slice(0, 16)} · ${r.first_title || r.idempotency_key}` })));
    } catch (error) { reportError(error); }
  }

  $('#search-form').addEventListener('submit', event => {
    event.preventDefault();
    const q = $('#search-input').value.trim();
    if (q) loadGraph({ q });
  });
  $('#run-select').addEventListener('change', event => loadGraph(event.target.value ? { run: event.target.value } : {}));
  $('#columns-toggle').addEventListener('change', () => loadGraph(params));

  const initial = Object.fromEntries(new URLSearchParams(location.search));
  $('#columns-toggle').checked = initial.columns === '1';
  if (initial.q) $('#search-input').value = initial.q;
  params = initial;
  loadRuns();
  loadGraph(initial, { push: false }).then(() => {
    if (initial.open) openObject(initial.open);
    document.body.dataset.ready = 'true';
  });
})();
