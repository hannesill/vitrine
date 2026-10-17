'use strict';

// ================================================================
// CARD PAGE — everything Vitrine keeps about one card
// ================================================================
// Opened from a card's details button or a #...&card=<id>&view=detail link.
// It replaces the feed until closed; the browser's back button closes it.

var HOST_NAMES = {
  lincei: 'Lincei',
  'claude-code': 'Claude Code',
  codex: 'Codex',
  unknown: 'Unknown host',
};

var TYPE_NAMES = {
  table: 'Table',
  markdown: 'Text',
  plotly: 'Chart',
  image: 'Figure',
  keyvalue: 'Values',
  decision: 'Decision',
  agent: 'Agent',
  section: 'Section',
};

var CARD_PAGE_PREVIEW_ROWS = 8;

state.cardPage = null;  // { cardId, pushed, scrollY, request }

// ----------------------------------------------------------------
// Opening and closing
// ----------------------------------------------------------------
function cardPageHash(cardId, withView) {
  var parts = [];
  if (state.activeStudyFilter) parts.push('study=' + encodeURIComponent(state.activeStudyFilter));
  parts.push('card=' + encodeURIComponent(cardId));
  if (withView) parts.push('view=detail');
  return '#' + parts.join('&');
}

function openCardPage(cardId) {
  var scrollY = window.scrollY;
  history.pushState(null, '', cardPageHash(cardId, true));
  showCardPage(cardId, { pushed: true, scrollY: scrollY });
}

function showCardPage(cardId, opts) {
  opts = opts || {};
  var prev = state.cardPage;
  if (prev && prev.cardId === cardId && !opts.pushed) return;
  state.cardPage = {
    cardId: cardId,
    pushed: opts.pushed || (prev ? prev.pushed : false),
    scrollY: opts.scrollY != null ? opts.scrollY : (prev ? prev.scrollY : window.scrollY),
    request: null,
  };
  if (parseHash().view !== 'detail') {
    history.replaceState(null, '', cardPageHash(cardId, true));
  }
  document.body.classList.add('card-page-open');
  window.scrollTo(0, 0);
  loadCardPage(cardId, false);
}

function hideCardPage() {
  if (!state.cardPage) return;
  var page = state.cardPage;
  state.cardPage = null;
  document.body.classList.remove('card-page-open');
  var pageEl = document.getElementById('card-page');
  if (pageEl) pageEl.innerHTML = '';
  if (page.pushed) {
    window.scrollTo(0, page.scrollY || 0);  // back to where the feed was
  } else {
    applyHashCard(page.cardId);  // opened from a link: show the card in the feed
  }
}

function closeCardPage() {
  if (!state.cardPage) return;
  if (state.cardPage.pushed) {
    history.back();  // the hashchange that follows hides the page
    return;
  }
  history.replaceState(null, '', cardPageHash(state.cardPage.cardId, false));
  hideCardPage();
}

// Called for every display.update so an open page follows its card.
function cardPageNotifyUpdate(cardId) {
  if (state.cardPage && state.cardPage.cardId === cardId) {
    loadCardPage(cardId, true);
  }
}

document.addEventListener('keydown', function(e) {
  if (e.key !== 'Escape' || !state.cardPage) return;
  var t = e.target;
  if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) return;
  if (document.querySelector('.action-palette-backdrop, .row-detail-overlay, .file-preview-overlay, .confirm-overlay')) return;
  closeCardPage();
});

// ----------------------------------------------------------------
// Loading
// ----------------------------------------------------------------
function loadCardPage(cardId, quiet) {
  var pageEl = document.getElementById('card-page');
  var token = {};
  state.cardPage.request = token;
  if (!quiet) {
    pageEl.innerHTML = '';
    pageEl.appendChild(cpEl('div', 'card-page-loading', 'Loading card…'));
  }
  fetch('/api/card/' + encodeURIComponent(cardId) + '/history')
    .then(function(resp) {
      if (!resp.ok) throw new Error(resp.status === 404 ? 'not-found' : 'failed');
      return resp.json();
    })
    .then(function(data) {
      if (!state.cardPage || state.cardPage.request !== token) return;
      renderCardPage(pageEl, data);
    })
    .catch(function(err) {
      if (!state.cardPage || state.cardPage.request !== token) return;
      if (quiet) return;  // keep showing what loaded before
      pageEl.innerHTML = '';
      pageEl.appendChild(buildCardPageBar(null));
      var msg = cpEl('div', 'card-page-missing');
      msg.appendChild(cpEl('div', 'card-page-missing-title',
        err.message === 'not-found' ? 'This card no longer exists' : 'This card could not be loaded'));
      msg.appendChild(cpEl('div', 'card-page-muted',
        err.message === 'not-found'
          ? 'It may belong to a study that was deleted.'
          : 'Check that the Vitrine server is still running, then reload.'));
      pageEl.appendChild(msg);
    });
}

// ----------------------------------------------------------------
// Page layout
// ----------------------------------------------------------------
function renderCardPage(pageEl, data) {
  var card = data.card;
  var code = data.code || {};
  var versions = buildCardVersions(card, data.revisions || []);
  var current = versions[versions.length - 1];

  pageEl.innerHTML = '';
  pageEl.appendChild(buildCardPageBar(card));
  pageEl.appendChild(buildCardPageHead(card, versions));

  // Why. A replace without a new reason keeps the old one, so say which
  // version the reason was written for when the card has changed since.
  var whyFrom = versions.length;
  while (whyFrom > 1 && card.why && versions[whyFrom - 2].card.why === card.why) whyFrom--;
  var why = cpSection('Why', card.why && whyFrom < versions.length
    ? 'Written for version ' + whyFrom + '; the card has changed since' : null);
  if (card.why) {
    why.appendChild(cpEl('p', 'card-page-why', card.why));
  } else {
    var none = cpEl('p', 'card-page-muted');
    none.appendChild(document.createTextNode('No reason recorded. An agent gives one with '));
    none.appendChild(cpEl('code', null, 'show(..., why="…")'));
    none.appendChild(document.createTextNode('.'));
    why.appendChild(none);
  }
  pageEl.appendChild(why);

  // Current version
  var cur = cpSection(versions.length > 1 ? 'Current version' : 'Content',
    versions.length > 1 ? 'Version ' + current.number + ' · ' + cpTime(current.createdAt) : null);
  var content = cpEl('div', 'card-page-content');
  renderVersionContent(content, card);
  cur.appendChild(content);
  cur.appendChild(buildVersionFacts(current, code));
  pageEl.appendChild(cur);

  // History
  if (versions.length > 1) {
    pageEl.appendChild(buildHistorySection(versions, code));
  }

  // Researcher
  var researcher = buildResearcherSection(card);
  if (researcher) pageEl.appendChild(researcher);

  // Source
  var source = buildSourceSection(card);
  if (source) pageEl.appendChild(source);
}

function buildCardPageBar(card) {
  var bar = cpEl('div', 'card-page-bar');
  var back = cpEl('button', 'btn card-page-back', '← Back to ' + (state.activeStudyFilter ? 'study' : 'cards'));
  back.onclick = closeCardPage;
  bar.appendChild(back);

  if (card) {
    var spacer = cpEl('span', 'card-page-bar-spacer');
    bar.appendChild(spacer);

    var idBtn = cpEl('button', 'btn card-page-id', card.card_id.slice(0, 8));
    idBtn.title = 'Copy card ID ' + card.card_id;
    idBtn.onclick = function() { cpCopy(card.card_id, 'Card ID copied'); };
    bar.appendChild(idBtn);

    var linkBtn = cpEl('button', 'btn', 'Copy link');
    linkBtn.onclick = function() { cpCopy(location.href, 'Link copied'); };
    bar.appendChild(linkBtn);
  }
  return bar;
}

function buildCardPageHead(card, versions) {
  var head = cpEl('header', 'card-page-head');
  var type = (card.response_requested || card.response_action) ? 'decision' : card.card_type;
  head.setAttribute('data-type', type);

  var icon = cpEl('div', 'card-type-icon', TYPE_LETTERS[type] || '?');
  icon.setAttribute('data-type', type);
  head.appendChild(icon);

  var typeName = TYPE_NAMES[type] || type;
  var text = cpEl('div', 'card-page-head-text');
  text.appendChild(cpEl('h2', 'card-page-title', cardPageTitle(card) || typeName));
  if (card.description) text.appendChild(cpEl('div', 'card-page-description', card.description));

  var meta = [typeName];
  if (card.study) meta.push(card.study);
  meta.push('Created ' + cpTime(versions[0].createdAt));
  if (versions.length > 1) meta.push(versions.length + ' versions');
  if (card.deleted) meta.push('Deleted');
  else if (card.dismissed) meta.push('Hidden');
  text.appendChild(cpEl('div', 'card-page-meta', meta.join(' · ')));
  head.appendChild(text);
  return head;
}

// Cards made by confirm() or ask() have no title; their question names them.
function cardPageTitle(card) {
  if (card.title) return card.title;
  var text = card.prompt || (card.preview && card.preview.text) || '';
  var first = text.split('\n').map(function(l) { return l.replace(/^#+\s*/, '').trim(); })
    .filter(Boolean)[0] || '';
  return first.length > 90 ? first.slice(0, 89) + '\u2026' : first;
}

// Oldest first; the last entry is the card as it is now.
function buildCardVersions(card, revisions) {
  var versions = revisions.map(function(rev, i) {
    var c = rev.card || {};
    var created = (c.agent && c.agent.recorded_at)
      || (i === 0 ? c.timestamp : revisions[i - 1].replaced_at);
    return { number: rev.revision, card: c, createdAt: created, current: false };
  });
  versions.push({
    number: revisions.length + 1,
    card: card,
    createdAt: (card.agent && card.agent.recorded_at)
      || (revisions.length ? revisions[revisions.length - 1].replaced_at : card.timestamp),
    current: true,
  });
  return versions;
}

// ----------------------------------------------------------------
// A version: who made it and with which code
// ----------------------------------------------------------------
function buildVersionFacts(version, code) {
  var wrap = cpEl('div', 'card-page-facts-wrap');
  var dl = cpEl('dl', 'card-page-facts');
  var agent = version.card.agent;

  if (agent) {
    var host = HOST_NAMES[agent.host] || agent.host;
    var sessionDd = cpEl('dd');
    if (agent.session_id) {
      sessionDd.appendChild(document.createTextNode(host + ' '));
      var sid = cpEl('code', 'card-page-session', cpShortId(agent.session_id));
      sid.title = agent.session_id;
      sessionDd.appendChild(sid);
      sessionDd.appendChild(cpSmallButton('Copy ID', function() {
        cpCopy(agent.session_id, 'Session ID copied');
      }));
      if (agent.host === 'claude-code') {
        sessionDd.appendChild(cpSmallButton('Copy resume command', function() {
          cpCopy('claude --resume ' + agent.session_id, 'Resume command copied');
        }));
      }
    } else {
      sessionDd.textContent = host + ', session not known';
    }
    cpFact(dl, 'Session', sessionDd);
    if (agent.turn_id) cpFact(dl, 'Turn', cpEl('dd', null, agent.turn_id));
  } else {
    cpFact(dl, 'Session', cpEl('dd', 'card-page-muted',
      'Not recorded. This version was made before Vitrine recorded agent sessions.'));
  }
  cpFact(dl, 'Made', cpEl('dd', null, cpTime(version.createdAt)));
  wrap.appendChild(dl);
  wrap.appendChild(buildCodeBlock(version.card.code, code));
  return wrap;
}

function buildCodeBlock(origin, code) {
  var block = cpEl('div', 'card-page-code');
  var head = cpEl('div', 'card-page-code-head');
  head.appendChild(cpEl('span', 'card-page-code-label', 'Code'));
  block.appendChild(head);

  if (!origin) {
    block.appendChild(cpEl('div', 'card-page-muted',
      'Not recorded. This version was made before Vitrine recorded code.'));
    return block;
  }

  head.appendChild(cpEl('code', 'card-page-code-path',
    origin.path + (origin.line ? ':' + origin.line : '')));
  if (origin.sha256) {
    var sha = cpEl('span', 'card-page-code-sha', origin.sha256.slice(0, 8));
    sha.title = 'SHA-256 ' + origin.sha256;
    head.appendChild(sha);
  }

  var source = origin.sha256 ? code[origin.sha256] : null;
  if (source != null) {
    block.appendChild(buildCodeViewer(source, origin.line));
  } else if (origin.path.charAt(0) === '<') {
    block.appendChild(cpEl('div', 'card-page-muted',
      'Run from ' + origin.path + ', so there is no script file to keep. '
      + 'Code saved as a script in the study folder is kept here.'));
  } else {
    block.appendChild(cpEl('div', 'card-page-muted',
      'Vitrine did not keep a copy of this script.'));
  }
  return block;
}

function buildCodeViewer(source, line) {
  var pre = cpEl('pre', 'card-page-code-view');
  var lines = source.replace(/\n$/, '').split('\n');
  var target = null;
  lines.forEach(function(text, i) {
    var row = cpEl('span', 'code-line');
    row.setAttribute('data-n', String(i + 1));
    row.textContent = text + '\n';
    if (line && i + 1 === line) {
      row.classList.add('code-line-target');
      target = row;
    }
    pre.appendChild(row);
  });
  if (target) {
    // Scroll the viewer (not the page) so the calling line sits near the top.
    setTimeout(function() {
      pre.scrollTop = Math.max(0, target.offsetTop - pre.clientHeight / 3);
    }, 0);
  }
  return pre;
}

// ----------------------------------------------------------------
// History
// ----------------------------------------------------------------
function buildHistorySection(versions, code) {
  var section = cpSection('History', versions.length + ' versions, newest first');
  var list = cpEl('ol', 'card-page-history');

  for (var i = versions.length - 1; i >= 0; i--) {
    var v = versions[i];
    var item = cpEl('li', 'card-page-version' + (v.current ? ' current' : ''));
    var changes = i === 0 ? ['First version'] : describeChange(versions[i - 1].card, v.card);

    var summaryParent;
    if (v.current) {
      summaryParent = item;
    } else {
      var details = cpEl('details');
      summaryParent = cpEl('summary');
      details.appendChild(summaryParent);
      item.appendChild(details);
    }

    var line = cpEl('div', 'card-page-version-line');
    line.appendChild(cpEl('span', 'card-page-version-number', 'Version ' + v.number));
    if (v.current) line.appendChild(cpEl('span', 'card-page-badge', 'Current'));
    line.appendChild(cpEl('span', 'card-page-version-time', cpTime(v.createdAt)));
    var who = versionWho(v.card);
    if (who) line.appendChild(cpEl('span', 'card-page-version-who', who));
    summaryParent.appendChild(line);

    var changeList = cpEl('ul', 'card-page-changes');
    changes.forEach(function(c) { changeList.appendChild(cpEl('li', null, c)); });
    summaryParent.appendChild(changeList);

    if (!v.current) fillVersionOnOpen(details, v, code);
    list.appendChild(item);
  }
  section.appendChild(list);
  return section;
}

// An earlier version's content and code are built when it is first opened.
function fillVersionOnOpen(details, version, code) {
  details.addEventListener('toggle', function() {
    if (!details.open || details.querySelector('.card-page-version-body')) return;
    var body = cpEl('div', 'card-page-version-body');
    var content = cpEl('div', 'card-page-content');
    body.appendChild(content);
    body.appendChild(buildVersionFacts(version, code));
    details.appendChild(body);
    renderVersionContent(content, version.card);
  });
}

function versionWho(card) {
  var parts = [];
  if (card.agent) {
    var host = HOST_NAMES[card.agent.host] || card.agent.host;
    parts.push(card.agent.session_id ? host + ' ' + cpShortId(card.agent.session_id) : host);
  }
  if (card.code) parts.push(card.code.path + (card.code.line ? ':' + card.code.line : ''));
  return parts.join(' · ');
}

// What changed from one version to the next, as short phrases.
function describeChange(a, b) {
  var changes = [];
  var pa = a.preview || {};
  var pb = b.preview || {};

  if (a.card_type !== b.card_type) {
    changes.push((TYPE_NAMES[a.card_type] || a.card_type) + ' became ' + (TYPE_NAMES[b.card_type] || b.card_type).toLowerCase());
  }
  if ((a.title || '') !== (b.title || '')) {
    changes.push('Title “' + (a.title || '') + '” → “' + (b.title || '') + '”');
  }
  if (a.card_type === b.card_type) {
    if (b.card_type === 'table') {
      var sa = pa.shape || [];
      var sb = pb.shape || [];
      if (sa[0] !== sb[0]) changes.push('Rows ' + cpNum(sa[0]) + ' → ' + cpNum(sb[0]));
      var ca = pa.columns || [];
      var cb = pb.columns || [];
      var added = cb.filter(function(c) { return ca.indexOf(c) === -1; });
      var removed = ca.filter(function(c) { return cb.indexOf(c) === -1; });
      if (added.length) changes.push('Columns added: ' + added.join(', '));
      if (removed.length) changes.push('Columns removed: ' + removed.join(', '));
      if (sa[0] === sb[0] && !added.length && !removed.length
          && JSON.stringify(pa.preview_rows) !== JSON.stringify(pb.preview_rows)) {
        changes.push('Values changed');
      }
    } else if (b.card_type === 'keyvalue') {
      var ia = pa.items || {};
      var ib = pb.items || {};
      var keys = Object.keys(ia).concat(Object.keys(ib).filter(function(k) { return !(k in ia); }));
      var changed = keys.filter(function(k) { return String(ia[k]) !== String(ib[k]); });
      changed.slice(0, 4).forEach(function(k) {
        if (!(k in ib)) changes.push(k + ' removed');
        else if (!(k in ia)) changes.push(k + ' added: ' + ib[k]);
        else changes.push(k + ' ' + ia[k] + ' → ' + ib[k]);
      });
      if (changed.length > 4) changes.push((changed.length - 4) + ' more values changed');
    } else if (b.card_type === 'markdown') {
      if (pa.text !== pb.text) changes.push('Text changed');
    } else if (JSON.stringify(pa) !== JSON.stringify(pb)) {
      changes.push(b.card_type === 'plotly' ? 'Chart changed'
        : b.card_type === 'image' ? 'Figure changed' : 'Content changed');
    }
  }

  var codeA = a.code || {};
  var codeB = b.code || {};
  if (b.code && codeA.path !== codeB.path) {
    changes.push('Made by ' + codeB.path);
  } else if (codeA.sha256 && codeB.sha256 && codeA.sha256 !== codeB.sha256) {
    changes.push('Script edited');
  }
  if (b.why && a.why !== b.why) changes.push('New reason');
  return changes.length ? changes : ['No visible change'];
}

// ----------------------------------------------------------------
// Content of a version, from what its index entry keeps inline
// ----------------------------------------------------------------
function renderVersionContent(container, card) {
  var preview = card.preview || {};
  switch (card.card_type) {
    case 'markdown':
      renderMarkdown(container, card);
      break;
    case 'keyvalue':
      renderKeyValue(container, card);
      break;
    case 'table':
      renderTablePreview(container, preview);
      break;
    case 'plotly':
      renderStaticChart(container, preview);
      break;
    case 'image':
      if (preview.data) renderImage(container, card);
      else container.appendChild(cpEl('div', 'card-page-muted', 'No image kept for this version.'));
      break;
    case 'decision':
      renderDecisionPreview(container, card);
      break;
    default:
      var pre = cpEl('pre', 'card-page-json');
      pre.textContent = JSON.stringify(preview, null, 2);
      container.appendChild(pre);
  }
}

function renderTablePreview(container, preview) {
  var columns = preview.columns || [];
  var rows = (preview.preview_rows || []).slice(0, CARD_PAGE_PREVIEW_ROWS);
  var shape = preview.shape || [rows.length, columns.length];

  var caption = cpNum(shape[0]) + ' rows × ' + cpNum(shape[1]) + ' columns';
  if (shape[0] > rows.length) caption += ', first ' + rows.length + ' shown';
  container.appendChild(cpEl('div', 'card-page-table-caption', caption));

  var wrap = cpEl('div', 'card-page-table-wrap');
  var table = cpEl('table', 'card-page-table');
  var thead = cpEl('thead');
  var hr = cpEl('tr');
  columns.forEach(function(c) { hr.appendChild(cpEl('th', null, String(c))); });
  thead.appendChild(hr);
  table.appendChild(thead);
  var tbody = cpEl('tbody');
  rows.forEach(function(row) {
    var tr = cpEl('tr');
    row.forEach(function(v) { tr.appendChild(cpEl('td', null, v == null ? '' : String(v))); });
    tbody.appendChild(tr);
  });
  table.appendChild(tbody);
  wrap.appendChild(table);
  container.appendChild(wrap);
}

function renderStaticChart(container, preview) {
  var spec = preview.spec;
  if (!spec) {
    container.appendChild(cpEl('div', 'card-page-muted', 'No chart kept for this version.'));
    return;
  }
  var div = cpEl('div', 'card-page-chart');
  container.appendChild(div);
  loadPlotly(function() {
    // The page is assembled before it is attached; draw once the chart has a width.
    // setTimeout, not requestAnimationFrame: frames stop while an embedding panel is hidden.
    setTimeout(function() { drawStaticChart(div, spec); }, 0);
  });
}

function drawStaticChart(div, spec) {
  if (!window.Plotly || !div.isConnected) return;
  var layout = Object.assign({}, spec.layout || {}, {
    autosize: true,
    height: 320,
    margin: { l: 50, r: 20, t: 30, b: 40 },
    paper_bgcolor: 'transparent',
    plot_bgcolor: 'transparent',
    font: { color: getComputedStyle(document.documentElement).getPropertyValue('--text').trim() },
  });
  window.Plotly.newPlot(div, spec.data || [], layout, { staticPlot: true, responsive: true });
}

function renderDecisionPreview(container, card) {
  if (card.prompt) container.appendChild(cpEl('p', 'card-page-prompt', card.prompt));
  var fields = (card.preview && card.preview.fields) || [];
  if (fields.length) {
    var ul = cpEl('ul', 'card-page-fields');
    fields.forEach(function(f) { ul.appendChild(cpEl('li', null, f.question || f.name)); });
    container.appendChild(ul);
  } else if (card.preview && card.preview.text) {
    renderMarkdown(container, card);
  }
}

// ----------------------------------------------------------------
// The researcher's side, and the declared source
// ----------------------------------------------------------------
function buildResearcherSection(card) {
  var annotations = card.annotations || [];
  var hasDecision = card.response_action || card.response_requested;
  if (!hasDecision && !annotations.length) return null;

  var section = cpSection('Researcher');
  if (hasDecision) {
    var dl = cpEl('dl', 'card-page-facts');
    if (card.response_action) {
      var action = card.response_action === 'confirm' ? 'Confirmed'
        : card.response_action === 'skip' ? 'Skipped'
        : card.response_action === 'timeout' ? 'No answer before the timeout'
        : card.response_action;
      cpFact(dl, 'Answer', cpEl('dd', 'card-page-answer', action));
      if (card.response_timestamp) cpFact(dl, 'Answered', cpEl('dd', null, cpTime(card.response_timestamp)));
      if (card.response_message && card.response_message.trim()) {
        cpFact(dl, 'Note', cpEl('dd', null, card.response_message.trim()));
      }
      var values = card.response_values || {};
      Object.keys(values).forEach(function(k) {
        var v = values[k];
        cpFact(dl, k, cpEl('dd', null, Array.isArray(v) ? v.join(', ') : String(v)));
      });
      if (card.response_summary) cpFact(dl, 'Selection', cpEl('dd', null, card.response_summary));
    } else {
      cpFact(dl, 'Answer', cpEl('dd', 'card-page-muted', 'Waiting for the researcher.'));
    }
    section.appendChild(dl);
  }
  if (annotations.length) {
    var list = cpEl('ul', 'card-page-annotations');
    annotations.forEach(function(ann) {
      var li = cpEl('li');
      li.appendChild(cpEl('div', null, ann.text));
      if (ann.timestamp) li.appendChild(cpEl('div', 'card-page-muted', cpTime(ann.timestamp)));
      list.appendChild(li);
    });
    section.appendChild(cpEl('h4', 'card-page-subhead', 'Annotations'));
    section.appendChild(list);
  }
  return section;
}

function buildSourceSection(card) {
  var p = card.provenance;
  if (!p || !(p.source || p.dataset || p.query)) return null;
  var section = cpSection('Source');
  var dl = cpEl('dl', 'card-page-facts');
  if (p.source) cpFact(dl, 'Source', cpEl('dd', null, p.source));
  if (p.dataset) cpFact(dl, 'Dataset', cpEl('dd', null, p.dataset));
  if (p.timestamp) cpFact(dl, 'Recorded', cpEl('dd', null, cpTime(p.timestamp)));
  section.appendChild(dl);
  if (p.query) {
    var pre = cpEl('pre', 'card-page-json');
    pre.textContent = p.query;
    section.appendChild(pre);
  }
  return section;
}

// ----------------------------------------------------------------
// Small helpers
// ----------------------------------------------------------------
function cpEl(tag, className, text) {
  var el = document.createElement(tag);
  if (className) el.className = className;
  if (text != null) el.textContent = text;
  return el;
}

function cpSection(title, note) {
  var section = cpEl('section', 'card-page-section');
  var head = cpEl('div', 'card-page-section-head');
  head.appendChild(cpEl('h3', null, title));
  if (note) head.appendChild(cpEl('span', 'card-page-section-note', note));
  section.appendChild(head);
  return section;
}

function cpFact(dl, label, dd) {
  dl.appendChild(cpEl('dt', null, label));
  dl.appendChild(dd);
}

function cpSmallButton(label, onClick) {
  var btn = cpEl('button', 'card-page-small-btn', label);
  btn.type = 'button';
  btn.onclick = function(e) { e.preventDefault(); onClick(); };
  return btn;
}

function cpCopy(text, message) {
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(function() {
      showToast(message);
    }, function() {
      showToast('Failed to copy', 'error');
    });
  }
}

function cpTime(iso) {
  if (!iso) return 'unknown time';
  var d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  return d.toLocaleString(undefined, {
    year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
  });
}

// Long IDs (UUIDs) read better shortened; the full ID is in the tooltip and copy button.
function cpShortId(id) {
  return id.length > 20 ? id.slice(0, 8) + '\u2026' : id;
}

function cpNum(n) {
  return typeof n === 'number' ? n.toLocaleString() : String(n == null ? '?' : n);
}
