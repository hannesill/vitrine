'use strict';

// ================================================================
// CARD RENDERING — addCard, updateCard, toast
// ================================================================
function addCard(cardData) {
  // Remove empty state if present
  var empty = document.getElementById('empty-state');
  if (empty && empty.parentNode) {
    empty.remove();
  }

  // Deduplicate on reconnect replay
  var existing = document.getElementById('card-' + cardData.card_id);
  if (existing) return;

  state.cards.push(cardData);
  trackStudy(cardData.study);

  // Track active run and auto-switch
  if (cardData.study) {
    state.activeStudy = cardData.study;
    if (state.liveMode && state.activeStudyFilter !== cardData.study) {
      // Auto-switch to the new card's run
      state.activeStudyFilter = cardData.study;
      updateDropdownTrigger();
      // Defer full filter update to batch rapid arrivals
      if (!state._autoSelectPending) {
        state._autoSelectPending = true;
        requestAnimationFrame(function() {
          state._autoSelectPending = false;
          applyStudyFilter();
          updateStudyMetadataBar();
          // Refresh run list to get updated card counts
          loadStudies();
        });
      }
    }
  }

  var el = document.createElement('div');
  el.className = 'card';
  el.id = 'card-' + cardData.card_id;
  el.dataset.study = cardData.study || '';
  el.dataset.cardId = cardData.card_id;

  // Header
  var header = document.createElement('div');
  header.className = 'card-header';
  var headerType = (cardData.response_requested || cardData.response_action) ? 'decision' : cardData.card_type;
  header.setAttribute('data-type', headerType);

  // Collapse toggle
  var collapseBtn = document.createElement('button');
  collapseBtn.className = 'card-collapse-btn';
  collapseBtn.innerHTML = '&#9660;';
  collapseBtn.title = 'Collapse';
  collapseBtn.setAttribute('aria-label', 'Collapse card');
  collapseBtn.onclick = function() {
    var isCollapsed = collapseBtn.classList.toggle('collapsed');
    el.classList.toggle('card-collapsed', isCollapsed);
    collapseBtn.title = isCollapsed ? 'Expand' : 'Collapse';
    collapseBtn.setAttribute('aria-label', isCollapsed ? 'Expand card' : 'Collapse card');
  };
  header.appendChild(collapseBtn);

  // Type icon
  var typeIcon = document.createElement('div');
  typeIcon.className = 'card-type-icon';
  typeIcon.setAttribute('data-type', headerType);
  typeIcon.textContent = TYPE_LETTERS[headerType] || '?';
  header.appendChild(typeIcon);

  // Agent status mark (completed or failed run)
  if (cardData.card_type === 'agent' && cardData.preview) {
    var agentStatus = cardData.preview.status;
    if (agentStatus === 'completed' || agentStatus === 'failed') {
      var statusMark = document.createElement('span');
      statusMark.className = 'agent-status-dot ' + agentStatus;
      statusMark.textContent = agentStatus === 'completed' ? '\u2713' : '\u2717';
      header.appendChild(statusMark);
    }
  }

  // Title (click to rename)
  var title = document.createElement('span');
  title.className = 'card-title';
  title.textContent = cardData.title || cardData.card_type;
  title.addEventListener('click', function(e) {
    e.stopPropagation();
    startEditCardTitle(el, cardData.card_id);
  });
  header.appendChild(title);

  // Failure reason (agent cards only)
  if (cardData.card_type === 'agent' && cardData.preview
      && cardData.preview.status === 'failed' && cardData.preview.error) {
    var reasonBadge = document.createElement('span');
    reasonBadge.className = 'agent-reason-badge';
    reasonBadge.textContent = cardData.preview.error;
    header.appendChild(reasonBadge);
  }

  // Timestamp, or run duration for agent cards
  var meta = document.createElement('span');
  meta.className = 'card-meta';
  if (cardData.card_type === 'agent' && cardData.preview && cardData.preview.duration != null) {
    meta.textContent = formatAgentDuration(cardData.preview.duration);
  } else if (cardData.timestamp) {
    meta.textContent = new Date(cardData.timestamp).toLocaleTimeString();
  }
  header.appendChild(meta);

  // Action buttons
  var actions = document.createElement('div');
  actions.className = 'card-actions';

  // Card page button
  var detailsBtn = document.createElement('button');
  detailsBtn.className = 'card-action-btn card-details-btn';
  detailsBtn.title = 'Card details: history, session and code';
  detailsBtn.setAttribute('aria-label', 'Open card details');
  detailsBtn.innerHTML = INFO_SVG;
  detailsBtn.onclick = function(e) {
    e.stopPropagation();
    openCardPage(cardData.card_id);
  };
  actions.appendChild(detailsBtn);

  // Copy prompt button
  var promptBtn = document.createElement('button');
  promptBtn.className = 'card-action-btn copy-prompt-btn';
  promptBtn.title = 'Copy card content';
  promptBtn.setAttribute('aria-label', 'Copy card content');
  promptBtn.innerHTML = '<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="5" y="2" width="8" height="10" rx="1"/><path d="M3 6v7a1 1 0 001 1h7"/></svg>';
  promptBtn.onclick = function(e) {
    e.stopPropagation();
    var prompt = buildCardPrompt(cardData);
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(prompt).then(function() {
        showToast('Card copied');
      }, function() {
        showToast('Failed to copy', 'error');
      });
    }
  };
  actions.appendChild(promptBtn);

  // Export as PNG button
  var exportBtn = document.createElement('button');
  exportBtn.className = 'card-action-btn export-png-btn';
  exportBtn.title = 'Export as PNG';
  exportBtn.setAttribute('aria-label', 'Export as PNG');
  exportBtn.innerHTML = '<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M2 11v2a1 1 0 001 1h10a1 1 0 001-1v-2"/><polyline points="5 7 8 10 11 7"/><line x1="8" y1="3" x2="8" y2="10"/></svg>';
  exportBtn.onclick = function(e) {
    e.stopPropagation();
    exportCardAsPng(el, cardData);
  };
  actions.appendChild(exportBtn);

  // Annotate button
  var annotateBtn = document.createElement('button');
  annotateBtn.className = 'card-action-btn';
  annotateBtn.title = 'Add annotation';
  annotateBtn.setAttribute('aria-label', 'Add annotation');
  annotateBtn.innerHTML = '<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M2 14s1-2 1-3V4a2 2 0 012-2h6a2 2 0 012 2v7c0 1 1 3 1 3"/><path d="M6 6h4"/><path d="M6 9h2"/></svg>';
  annotateBtn.onclick = function(e) {
    e.stopPropagation();
    toggleAnnotationForm(el, cardData);
  };
  actions.appendChild(annotateBtn);

  // Dismiss button
  var dismissBtn = document.createElement('button');
  dismissBtn.className = 'card-action-btn dismiss-btn';
  dismissBtn.title = cardData.dismissed ? 'Show card' : 'Hide card';
  dismissBtn.setAttribute('aria-label', cardData.dismissed ? 'Show card' : 'Hide card');
  dismissBtn.innerHTML = cardData.dismissed ? EYE_OFF_SVG : EYE_SVG;
  dismissBtn.onclick = function(e) {
    e.stopPropagation();
    var currentlyDismissed = el.classList.contains('dismissed');
    sendDismissEvent(cardData.card_id, !currentlyDismissed);
  };
  actions.appendChild(dismissBtn);

  // Delete button
  var deleteBtn = document.createElement('button');
  deleteBtn.className = 'card-action-btn delete-btn';
  deleteBtn.title = 'Delete card';
  deleteBtn.setAttribute('aria-label', 'Delete card');
  deleteBtn.innerHTML = TRASH_SVG;
  deleteBtn.onclick = function(e) {
    e.stopPropagation();
    el.classList.add('card-deleting');
    var cleaned = false;
    var cleanup = function() {
      if (cleaned) return;
      cleaned = true;
      el.classList.remove('card-deleting');
      el.classList.add('deleted', 'hidden-by-delete');
      updateCardCount();
      if (typeof tocNotifyChange === 'function') tocNotifyChange();
    };
    el.addEventListener('animationend', cleanup, { once: true });
    setTimeout(cleanup, 400);
    sendDeleteEvent(cardData.card_id, true);
    showUndoSnackbar(cardData.card_id, cardData.title || cardData.card_type);
  };
  actions.appendChild(deleteBtn);

  header.appendChild(actions);
  el.appendChild(header);

  // Description (subtitle / context line below header)
  if (cardData.description) {
    var desc = document.createElement('div');
    desc.className = 'card-description';
    desc.textContent = cardData.description;
    el.appendChild(desc);
  }

  // Body
  var body = document.createElement('div');
  body.className = 'card-body';

  switch (cardData.card_type) {
    case 'table':
      renderTable(body, cardData);
      break;
    case 'plotly':
      renderPlotly(body, cardData);
      break;
    case 'image':
      renderImage(body, cardData);
      break;
    case 'markdown':
      renderMarkdown(body, cardData);
      break;
    case 'decision':
      if (cardData.response_action && cardData.response_values && Object.keys(cardData.response_values).length > 0) {
        renderFrozenForm(body, cardData.response_values, (cardData.preview && cardData.preview.fields) || []);
      } else {
        renderForm(body, cardData);
      }
      break;
    case 'keyvalue':
      renderKeyValue(body, cardData);
      break;
    case 'agent':
      renderAgentCard(body, cardData);
      break;
    case 'section':
      el.remove();
      addSection(cardData.title || (cardData.preview && cardData.preview.title) || '', cardData.study);
      return;
    default:
      body.textContent = JSON.stringify(cardData.preview);
  }

  el.appendChild(body);

  // Controls bar for hybrid data+controls cards (table/chart with controls)
  if (cardData.preview && cardData.preview.controls && cardData.preview.controls.length > 0
      && !cardData.preview.fields) {
    var controlsBar = document.createElement('div');
    controlsBar.className = 'card-controls-bar';
    renderFormFields(controlsBar, cardData.preview.controls);
    el.appendChild(controlsBar);
  }

  // Waiting card: response UI
  if (cardData.response_requested) {
    el.classList.add('waiting');
    var responseUI = buildResponseUI(cardData, el);
    el.appendChild(responseUI);
    notifyDecisionCard(cardData);
  } else if (cardData.response_action) {
    // Already-responded decision card (loaded from disk)
    el.classList.add('responded');
    typeIcon.textContent = '\u2713';
    var badge = document.createElement('span');
    badge.className = 'sent-badge';
    if (cardData.response_action === 'confirm') {
      badge.textContent = 'Confirmed';
    } else if (cardData.response_action === 'skip') {
      badge.textContent = 'Skipped';
    } else {
      badge.textContent = cardData.response_action;
    }
    header.appendChild(badge);
    // Show researcher's note if provided
    if (cardData.response_message && cardData.response_message.trim()) {
      var noteEl = document.createElement('div');
      noteEl.className = 'decision-note';
      noteEl.textContent = cardData.response_message.trim();
      var bodyEl = el.querySelector('.card-body');
      if (bodyEl) bodyEl.appendChild(noteEl);
    }
  }

  // Annotations
  var annotationsContainer = document.createElement('div');
  annotationsContainer.className = 'card-annotations';
  renderAnnotations(annotationsContainer, cardData);
  el.appendChild(annotationsContainer);

  // Provenance
  if (cardData.provenance) {
    var prov = document.createElement('div');
    prov.className = 'card-provenance';
    var parts = [];
    if (cardData.provenance.source) parts.push(cardData.provenance.source);
    if (cardData.provenance.dataset) parts.push(cardData.provenance.dataset);
    if (cardData.provenance.timestamp) {
      parts.push(new Date(cardData.provenance.timestamp).toLocaleString());
    }
    prov.textContent = parts.join(' \u00b7 ');
    if (parts.length > 0) el.appendChild(prov);
  }

  feed.appendChild(el);

  // Apply dismissed state
  if (cardData.dismissed) {
    el.classList.add('dismissed');
    if (!state.showDismissed) {
      el.classList.add('hidden-by-dismiss');
    }
    updateDismissToggleVisibility();
  }

  // Apply deleted state
  if (cardData.deleted) {
    el.classList.add('deleted');
    el.classList.add('hidden-by-delete');
  }

  // Hide card if it lands inside a collapsed section
  if (isInCollapsedSection(el)) {
    el.classList.add('hidden-by-section');
  }

  // Apply study filter to the new card
  if (state.activeStudyFilter) {
    var cardRun = cardData.study || '';
    if (cardRun !== state.activeStudyFilter) {
      el.classList.add('hidden-by-filter');
    }
  }

  updateCardCount();
  if (typeof tocNotifyChange === 'function') tocNotifyChange();

  // Check for pending deep-link scroll (prefix match)
  if (state.pendingCardScroll && cardData.card_id.indexOf(state.pendingCardScroll) === 0) {
    state.pendingCardScroll = null;
    setTimeout(function() { applyHashCard(cardData.card_id); }, 100);
  } else {
    scrollToBottom();
  }
}

function showToast(msg, type) {
  copyToastEl.textContent = msg;
  copyToastEl.classList.remove('toast-error');
  if (type === 'error') copyToastEl.classList.add('toast-error');
  copyToastEl.classList.add('visible');
  setTimeout(function() {
    copyToastEl.classList.remove('visible');
  }, type === 'error' ? 3000 : 1500);
}

// ================================================================
// SECTIONS, CARD UPDATES & STUDY FILTERING
// ================================================================
function addSection(title, study) {
  var empty = document.getElementById('empty-state');
  if (empty && empty.parentNode) {
    empty.remove();
  }

  if (study) trackStudy(study);

  var div = document.createElement('div');
  div.className = 'section-divider';
  div.dataset.study = study || '';

  var chevron = document.createElement('span');
  chevron.className = 'section-chevron';
  chevron.innerHTML = '&#9660;';
  div.appendChild(chevron);

  var titleSpan = document.createElement('span');
  titleSpan.className = 'section-title';
  titleSpan.textContent = title;
  div.appendChild(titleSpan);

  div.addEventListener('click', function() {
    var isCollapsed = div.classList.toggle('section-collapsed');
    toggleSectionCards(div, isCollapsed);
    if (typeof tocNotifyChange === 'function') tocNotifyChange();
  });

  feed.appendChild(div);

  if (state.activeStudyFilter) {
    applyStudyFilter();
  }

  scrollToBottom();
  if (typeof tocNotifyChange === 'function') tocNotifyChange();
}

function toggleSectionCards(sectionEl, collapsed) {
  var next = sectionEl.nextElementSibling;
  while (next && !next.classList.contains('section-divider')) {
    if (collapsed) {
      next.classList.add('hidden-by-section');
    } else {
      next.classList.remove('hidden-by-section');
    }
    next = next.nextElementSibling;
  }
}

function isInCollapsedSection(el) {
  var prev = el.previousElementSibling;
  while (prev) {
    if (prev.classList.contains('section-divider')) {
      return prev.classList.contains('section-collapsed');
    }
    prev = prev.previousElementSibling;
  }
  return false;
}

function startEditCardTitle(cardEl, cardId) {
  var titleEl = cardEl.querySelector('.card-title');
  if (!titleEl || titleEl.style.display === 'none') return;

  var originalTitle = titleEl.textContent;
  titleEl.style.display = 'none';

  var wrap = document.createElement('span');
  wrap.className = 'card-title-edit-wrap';

  var input = document.createElement('input');
  input.type = 'text';
  input.className = 'card-title-edit-input';
  input.value = originalTitle;
  wrap.appendChild(input);

  titleEl.parentNode.insertBefore(wrap, titleEl);
  input.focus();
  input.select();

  var committed = false;
  function commit() {
    if (committed) return;
    committed = true;
    var newTitle = input.value.trim();
    if (!newTitle || newTitle === originalTitle) { cleanup(); return; }

    if (ws && ws.readyState === 1) {
      ws.send(JSON.stringify({
        type: 'vitrine.event',
        event_type: 'rename',
        card_id: cardId,
        payload: { new_title: newTitle }
      }));
    }
    // Optimistic update
    titleEl.textContent = newTitle;
    for (var i = 0; i < state.cards.length; i++) {
      if (state.cards[i].card_id === cardId) {
        state.cards[i].title = newTitle;
        break;
      }
    }
    if (typeof tocNotifyChange === 'function') tocNotifyChange();
    cleanup();
  }

  function cleanup() {
    if (wrap.parentNode) wrap.parentNode.removeChild(wrap);
    titleEl.style.display = '';
  }

  var _blurTimeout = null;
  input.addEventListener('keydown', function(e) {
    if (e.key === 'Enter') { e.preventDefault(); if (_blurTimeout) { clearTimeout(_blurTimeout); _blurTimeout = null; } commit(); }
    if (e.key === 'Escape') { e.preventDefault(); if (_blurTimeout) { clearTimeout(_blurTimeout); _blurTimeout = null; } cleanup(); }
  });
  input.addEventListener('blur', function() { _blurTimeout = setTimeout(commit, 100); });
}

function updateCard(cardId, newCardData) {
  var el = document.getElementById('card-' + cardId);
  if (!el) return;

  // Update state.cards entry (merge into existing, don't replace)
  if (newCardData) {
    for (var i = 0; i < state.cards.length; i++) {
      if (state.cards[i].card_id === cardId) {
        var existing = state.cards[i];
        // Merge top-level fields
        for (var key in newCardData) {
          if (key === 'preview' && existing.preview && newCardData.preview) {
            // Deep merge preview
            for (var pk in newCardData.preview) {
              existing.preview[pk] = newCardData.preview[pk];
            }
          } else {
            existing[key] = newCardData[key];
          }
        }
        newCardData = existing;
        break;
      }
    }

    // Update title
    var titleEl = el.querySelector('.card-title');
    if (titleEl) {
      titleEl.textContent = newCardData.title || newCardData.card_type;
    }

    // Update description
    var descEl = el.querySelector('.card-description');
    if (newCardData.description) {
      if (!descEl) {
        descEl = document.createElement('div');
        descEl.className = 'card-description';
        var headerEl = el.querySelector('.card-header');
        if (headerEl && headerEl.nextSibling) {
          el.insertBefore(descEl, headerEl.nextSibling);
        }
      }
      descEl.textContent = newCardData.description;
    } else if (descEl) {
      descEl.remove();
    }

    var header = el.querySelector('.card-header');
    if (header) {
      var headerType = (newCardData.response_requested || newCardData.response_action) ? 'decision' : newCardData.card_type;
      header.setAttribute('data-type', headerType);
      var typeIcon = header.querySelector('.card-type-icon');
      if (typeIcon) {
        typeIcon.setAttribute('data-type', headerType);
        typeIcon.textContent = newCardData.response_action ? '\u2713' : (TYPE_LETTERS[headerType] || '?');
      }
    }

    // Re-render body content
    var body = el.querySelector('.card-body');
    if (body) {
      body.innerHTML = '';
      switch (newCardData.card_type) {
        case 'table':
          renderTable(body, newCardData);
          break;
        case 'plotly':
          renderPlotly(body, newCardData);
          break;
        case 'image':
          renderImage(body, newCardData);
          break;
        case 'markdown':
          if (newCardData.preview && newCardData.preview.fields) {
            renderForm(body, newCardData);
          } else {
            renderMarkdown(body, newCardData);
          }
          break;
        case 'keyvalue':
          renderKeyValue(body, newCardData);
          break;
        case 'decision':
          if (newCardData.response_action && newCardData.response_values && Object.keys(newCardData.response_values).length > 0) {
            renderFrozenForm(body, newCardData.response_values, (newCardData.preview && newCardData.preview.fields) || []);
          } else {
            renderForm(body, newCardData);
          }
          break;
        case 'agent':
          renderAgentCard(body, newCardData);
          break;
        default:
          body.textContent = JSON.stringify(newCardData.preview);
      }
    }
  }

  // Handle response_requested toggling (e.g. wait_for() re-enabling after timeout)
  if (newCardData) {
    var existingResponseUI = el.querySelector('.card-response-ui');
    if (newCardData.response_requested && !existingResponseUI) {
      // Re-enable: remove responded state and old badges, add fresh response UI
      el.classList.remove('responded');
      el.classList.add('waiting');
      var oldBadge = el.querySelector('.sent-badge');
      if (oldBadge) oldBadge.remove();
      var responseUI = buildResponseUI(newCardData, el);
      el.appendChild(responseUI);
      notifyDecisionCard(newCardData);
    } else if (!newCardData.response_requested && existingResponseUI) {
      // Disable: remove response UI if no longer waiting
      if (existingResponseUI._timer) clearInterval(existingResponseUI._timer);
      existingResponseUI.remove();
      el.classList.remove('waiting');
    }
  }

  // Re-render annotations
  if (newCardData) {
    var annotationsContainer = el.querySelector('.card-annotations');
    if (annotationsContainer) {
      annotationsContainer.innerHTML = '';
      renderAnnotations(annotationsContainer, newCardData);
    }
  }

  // Handle dismissed state (with animation)
  if (newCardData) {
    var isDismissed = !!newCardData.dismissed;
    var wasDismissed = el.classList.contains('dismissed');
    var wasHiddenByDismiss = el.classList.contains('hidden-by-dismiss');

    if (isDismissed && !wasDismissed && !state.showDismissed) {
      // Newly dismissed and should be hidden — animate out
      el.classList.add('card-dismissing');
      var dCleaned = false;
      var dCleanup = function() {
        if (dCleaned) return;
        dCleaned = true;
        el.classList.remove('card-dismissing');
        el.classList.add('dismissed', 'hidden-by-dismiss');
        updateCardCount();
        if (typeof tocNotifyChange === 'function') tocNotifyChange();
      };
      el.addEventListener('animationend', dCleanup, { once: true });
      setTimeout(dCleanup, 350);
    } else if (!isDismissed && wasDismissed) {
      // Undismissed — animate in if was hidden
      el.classList.remove('dismissed');
      if (wasHiddenByDismiss) {
        el.classList.remove('hidden-by-dismiss');
        el.classList.add('card-undismissing');
        var uCleaned = false;
        var uCleanup = function() {
          if (uCleaned) return;
          uCleaned = true;
          el.classList.remove('card-undismissing');
        };
        el.addEventListener('animationend', uCleanup, { once: true });
        setTimeout(uCleanup, 350);
      }
    } else {
      // Sync state (no transition, e.g. initial load or showDismissed toggle)
      el.classList.toggle('dismissed', isDismissed);
      if (isDismissed && !state.showDismissed) {
        el.classList.add('hidden-by-dismiss');
      } else {
        el.classList.remove('hidden-by-dismiss');
      }
    }

    var dBtn = el.querySelector('.dismiss-btn');
    if (dBtn) {
      dBtn.title = isDismissed ? 'Show card' : 'Hide card';
      dBtn.setAttribute('aria-label', isDismissed ? 'Show card' : 'Hide card');
      dBtn.innerHTML = isDismissed ? EYE_OFF_SVG : EYE_SVG;
    }
    updateDismissToggleVisibility();
    updateCardCount();
  }

  // Handle deleted state (with animation)
  if (newCardData) {
    var isDeleted = !!newCardData.deleted;
    if (isDeleted) {
      // Card should be deleted — if already animating or hidden, don't interfere
      if (!el.classList.contains('card-deleting') && !el.classList.contains('hidden-by-delete')) {
        // Server-side delete (not initiated from this client's button)
        el.classList.add('deleted');
        el.classList.add('card-deleting');
        var sdCleaned = false;
        var sdCleanup = function() {
          if (sdCleaned) return;
          sdCleaned = true;
          el.classList.remove('card-deleting');
          el.classList.add('hidden-by-delete');
          updateCardCount();
          if (typeof tocNotifyChange === 'function') tocNotifyChange();
        };
        el.addEventListener('animationend', sdCleanup, { once: true });
        setTimeout(sdCleanup, 400);
      }
      el.classList.add('deleted');
    } else {
      // Restoring a deleted card
      var wasDeleted = el.classList.contains('deleted') || el.classList.contains('hidden-by-delete') || el.classList.contains('card-deleting');
      el.classList.remove('deleted', 'hidden-by-delete', 'card-deleting');
      if (wasDeleted) {
        el.classList.add('card-restoring');
        var rCleaned = false;
        var rCleanup = function() {
          if (rCleaned) return;
          rCleaned = true;
          el.classList.remove('card-restoring');
        };
        el.addEventListener('animationend', rCleanup, { once: true });
        setTimeout(rCleanup, 400);
        // Hide undo snackbar if this card was the one being undone
        if (_undoCardId === cardId) {
          hideUndoSnackbar();
        }
      }
    }
    updateCardCount();
    if (typeof tocNotifyChange === 'function') tocNotifyChange();
  }

  // Flash animation to highlight the update (skip during other animations)
  var isAnimating = el.classList.contains('card-deleting') || el.classList.contains('card-restoring')
    || el.classList.contains('card-dismissing') || el.classList.contains('card-undismissing')
    || el.classList.contains('hidden-by-delete');
  if (!isAnimating) {
    el.classList.remove('flash');
    void el.offsetWidth; // Force reflow
    el.classList.add('flash');
    setTimeout(function() { el.classList.remove('flash'); }, 600);
  }
  if (typeof tocNotifyChange === 'function') tocNotifyChange();
}

function rebuildStudyFilter() {
  // Rebuild studyNames from cards (used as fallback)
  var names = [];
  state.cards.forEach(function(c) {
    if (c.study && names.indexOf(c.study) === -1) names.push(c.study);
  });
  state.studyNames = names;

  // If the current filter no longer exists, switch to "All studies"
  if (state.activeStudyFilter && names.indexOf(state.activeStudyFilter) === -1) {
    state.activeStudyFilter = '';
    updateDropdownTrigger();
  }
}

// ================================================================
// ANNOTATIONS
// ================================================================
function renderAnnotations(container, cardData) {
  var annotations = cardData.annotations || [];
  if (annotations.length === 0) {
    container.style.display = 'none';
    return;
  }
  container.style.display = '';
  annotations.forEach(function(ann) {
    var annEl = document.createElement('div');
    annEl.className = 'card-annotation';
    annEl.dataset.annotationId = ann.id;

    var textEl = document.createElement('div');
    textEl.className = 'annotation-text';
    textEl.textContent = ann.text;
    annEl.appendChild(textEl);

    var metaEl = document.createElement('div');
    metaEl.className = 'annotation-meta';

    var ts = '';
    if (ann.timestamp) {
      try { ts = new Date(ann.timestamp).toLocaleString(); } catch(e) { ts = ann.timestamp; }
    }
    var metaText = document.createElement('span');
    metaText.textContent = ts;
    metaEl.appendChild(metaText);

    var editBtn = document.createElement('button');
    editBtn.className = 'annotation-action-btn';
    editBtn.textContent = 'edit';
    editBtn.onclick = function(e) {
      e.stopPropagation();
      startEditAnnotation(annEl, cardData, ann);
    };
    metaEl.appendChild(editBtn);

    var deleteBtn = document.createElement('button');
    deleteBtn.className = 'annotation-action-btn';
    deleteBtn.textContent = 'delete';
    deleteBtn.onclick = function(e) {
      e.stopPropagation();
      sendAnnotationEvent(cardData.card_id, 'delete', ann.id, '');
    };
    metaEl.appendChild(deleteBtn);

    annEl.appendChild(metaEl);
    container.appendChild(annEl);
  });
}

function startEditAnnotation(annEl, cardData, ann) {
  // Replace annotation content with edit form
  annEl.innerHTML = '';
  var textarea = document.createElement('textarea');
  textarea.className = 'annotation-textarea';
  textarea.value = ann.text;
  textarea.rows = 2;
  annEl.appendChild(textarea);

  var btns = document.createElement('div');
  btns.className = 'annotation-form-buttons';

  var saveBtn = document.createElement('button');
  saveBtn.className = 'response-btn response-btn-confirm';
  saveBtn.textContent = 'Save';
  saveBtn.onclick = function(e) {
    e.stopPropagation();
    var text = textarea.value.trim();
    if (text) {
      sendAnnotationEvent(cardData.card_id, 'edit', ann.id, text);
    }
  };
  btns.appendChild(saveBtn);

  var cancelBtn = document.createElement('button');
  cancelBtn.className = 'response-btn response-btn-skip';
  cancelBtn.textContent = 'Cancel';
  cancelBtn.onclick = function(e) {
    e.stopPropagation();
    // Re-render annotations to restore original state
    var container = annEl.parentElement;
    if (container) {
      container.innerHTML = '';
      renderAnnotations(container, cardData);
    }
  };
  btns.appendChild(cancelBtn);
  annEl.appendChild(btns);

  textarea.focus();
}

function toggleAnnotationForm(cardEl, cardData) {
  var existing = cardEl.querySelector('.annotation-form');
  if (existing) {
    existing.remove();
    return;
  }

  var form = document.createElement('div');
  form.className = 'annotation-form';

  var textarea = document.createElement('textarea');
  textarea.className = 'annotation-textarea';
  textarea.placeholder = 'Add a note...';
  textarea.rows = 2;
  form.appendChild(textarea);

  var btns = document.createElement('div');
  btns.className = 'annotation-form-buttons';

  var saveBtn = document.createElement('button');
  saveBtn.className = 'response-btn response-btn-confirm';
  saveBtn.textContent = 'Save';
  saveBtn.onclick = function(e) {
    e.stopPropagation();
    var text = textarea.value.trim();
    if (text) {
      sendAnnotationEvent(cardData.card_id, 'add', '', text);
      form.remove();
    }
  };
  btns.appendChild(saveBtn);

  var cancelBtn = document.createElement('button');
  cancelBtn.className = 'response-btn response-btn-skip';
  cancelBtn.textContent = 'Cancel';
  cancelBtn.onclick = function(e) {
    e.stopPropagation();
    form.remove();
  };
  btns.appendChild(cancelBtn);
  form.appendChild(btns);

  // Insert after annotations container
  var annotationsContainer = cardEl.querySelector('.card-annotations');
  if (annotationsContainer) {
    annotationsContainer.parentNode.insertBefore(form, annotationsContainer.nextSibling);
  } else {
    cardEl.appendChild(form);
  }

  textarea.focus();
}

function sendAnnotationEvent(cardId, action, annotationId, text) {
  if (!state.ws || !state.connected) return;
  var payload = { action: action };
  if (annotationId) payload.annotation_id = annotationId;
  if (text) payload.text = text;
  state.ws.send(JSON.stringify({
    type: 'vitrine.event',
    event_type: 'annotation',
    card_id: cardId,
    payload: payload
  }));
}

function showEmptyState() {
  var existing = document.getElementById('empty-state');
  if (existing) return;

  var empty = document.createElement('div');
  empty.className = 'empty-state';
  empty.id = 'empty-state';
  empty.innerHTML = '<div style="font-size: 32px; opacity: 0.3;">&#9671;</div>'
    + '<div style="font-size: 15px; font-weight: 500;">No studies yet</div>'
    + '<div style="font-size: 13px;">Run an agent to get started</div>'
    + '<div><code>from vitrine import show</code></div>';
  feed.appendChild(empty);
}

// ================================================================
// DISMISS / HIDE
// ================================================================
function sendDismissEvent(cardId, dismissed) {
  if (!state.ws || !state.connected) return;
  state.ws.send(JSON.stringify({
    type: 'vitrine.event',
    event_type: 'dismiss',
    card_id: cardId,
    payload: { dismissed: dismissed }
  }));
}

function sendDeleteEvent(cardId, deleted) {
  // Use HTTP POST with keepalive so the request survives page reload.
  fetch('/api/card/' + encodeURIComponent(cardId) + '/delete', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ deleted: deleted }),
    keepalive: true
  }).catch(function() {});
}

function applyDismissFilter() {
  var cards = feed.querySelectorAll('.card.dismissed');
  cards.forEach(function(el) {
    if (state.showDismissed) {
      el.classList.remove('hidden-by-dismiss');
    } else {
      el.classList.add('hidden-by-dismiss');
    }
  });
  updateCardCount();
}

function updateDismissToggleVisibility() {
  var toggle = document.getElementById('dismiss-toggle');
  if (!toggle) return;
  var hasDismissed = feed.querySelector('.card.dismissed') !== null;
  toggle.style.display = hasDismissed ? '' : 'none';
}

// Toggle click handler — wired up in init.js-compatible IIFE
(function() {
  var toggle = document.getElementById('dismiss-toggle');
  if (toggle) {
    toggle.addEventListener('click', function() {
      state.showDismissed = !state.showDismissed;
      toggle.classList.toggle('active', state.showDismissed);
      toggle.title = state.showDismissed ? 'Hide hidden cards' : 'Show hidden cards';
      toggle.innerHTML = state.showDismissed ? EYE_SVG : EYE_OFF_SVG;
      applyDismissFilter();
    });
  }
})();

// ================================================================
// UNDO SNACKBAR
// ================================================================
var _undoTimer = null;
var _undoCardId = null;

function showUndoSnackbar(cardId, title) {
  var snackbar = document.getElementById('undo-snackbar');
  if (!snackbar) return;
  var textEl = snackbar.querySelector('.undo-snackbar-text');
  if (_undoTimer) clearTimeout(_undoTimer);
  _undoCardId = cardId;
  var displayTitle = title || 'Card';
  if (displayTitle.length > 30) displayTitle = displayTitle.substring(0, 30) + '\u2026';
  textEl.textContent = '\u201c' + displayTitle + '\u201d deleted';
  snackbar.classList.add('visible');
  _undoTimer = setTimeout(function() { hideUndoSnackbar(); }, 5000);
}

function hideUndoSnackbar() {
  var snackbar = document.getElementById('undo-snackbar');
  if (snackbar) snackbar.classList.remove('visible');
  if (_undoTimer) { clearTimeout(_undoTimer); _undoTimer = null; }
  _undoCardId = null;
}

(function() {
  var btn = document.getElementById('undo-snackbar-btn');
  if (btn) {
    btn.addEventListener('click', function() {
      if (_undoCardId) {
        sendDeleteEvent(_undoCardId, false);
        hideUndoSnackbar();
      }
    });
  }
})();

// ================================================================
// AGENT CARD RENDERING
// ================================================================
// Agent cards record runs of the agent runner that earlier versions built
// into the board. They are read-only: the run's output and its usage.
function renderAgentCard(container, cardData) {
  var preview = cardData.preview || {};
  var outputText = preview.output || '';
  if (preview.status !== 'completed' && preview.status !== 'failed') {
    outputText += (outputText ? '\n\n---\n' : '') + '*This agent run did not finish.*';
  }

  container.innerHTML = '';

  // Terminal box — compact height with gradient fade
  var terminal = document.createElement('div');
  terminal.className = 'agent-terminal agent-terminal-compact';

  var termContent = document.createElement('div');
  termContent.className = 'agent-terminal-content markdown-body';
  if (typeof marked !== 'undefined') {
    termContent.innerHTML = marked.parse(outputText);
  } else {
    termContent.textContent = outputText;
  }
  terminal.appendChild(termContent);
  container.appendChild(terminal);

  // Bottom bar with usage info (left) + expand toggle (right)
  var bottomBar = document.createElement('div');
  bottomBar.className = 'agent-bottom-bar agent-bottom-bar-completed';

  var usageStr = formatAgentUsage(preview);
  if (usageStr) {
    var usageEl = document.createElement('span');
    usageEl.className = 'agent-bottom-usage';
    usageEl.textContent = usageStr;
    bottomBar.appendChild(usageEl);
  }

  var expandBtn = document.createElement('button');
  expandBtn.className = 'agent-expand-btn';
  expandBtn.textContent = 'Expand';
  expandBtn.onclick = function() {
    var isExpanded = terminal.classList.toggle('expanded');
    terminal.classList.toggle('agent-terminal-compact', !isExpanded);
    expandBtn.textContent = isExpanded ? 'Collapse' : 'Expand';
  };
  bottomBar.appendChild(expandBtn);

  container.appendChild(bottomBar);
}

function formatAgentDuration(seconds) {
  if (seconds == null) return '';
  var secs = Math.round(seconds);
  if (secs >= 60) {
    return Math.floor(secs / 60) + 'm ' + (secs % 60) + 's';
  }
  return secs + 's';
}

function formatTokenCount(n) {
  if (n == null || n === 0) return '0';
  if (n >= 1000000) return (n / 1000000).toFixed(1) + 'M';
  if (n >= 1000) return (n / 1000).toFixed(1) + 'k';
  return String(n);
}

function formatAgentUsage(preview) {
  var usage = preview.usage;
  if (!usage) return '';
  var parts = [];
  var totalTokens = (usage.input_tokens || 0) + (usage.output_tokens || 0);
  if (totalTokens > 0) {
    parts.push(formatTokenCount(totalTokens) + ' tokens');
  }
  if (usage.input_tokens > 0 && usage.context_window > 0) {
    var pct = Math.round((usage.input_tokens / usage.context_window) * 100);
    parts.push(pct + '% ctx');
  }
  if (usage.cost_usd != null) {
    var cost = usage.cost_usd;
    parts.push(cost < 0.01 ? '<$0.01' : '$' + cost.toFixed(2));
  }
  return parts.join(' \u00b7 ');
}

function buildCardPrompt(card) {
  var header = '## ' + (card.title || 'Untitled') + '  [card:' + card.card_id + (card.study ? ', study:' + card.study : '') + ']\n\n';
  // confirm()/ask() cards stay markdown and controls cards stay table/plotly,
  // so the researcher's response is appended for every card type.
  return header + _buildCardBody(card) + _buildResponsePrompt(card);
}

function _buildCardBody(card) {
  if (card.card_type === 'table' && card.preview && card.preview.columns) {
    return _buildTablePrompt(card.preview);
  }
  if (card.card_type === 'markdown' && card.preview && card.preview.text) {
    return card.preview.text + '\n';
  }
  if (card.card_type === 'keyvalue' && card.preview && card.preview.items) {
    return _buildKeyValuePrompt(card.preview.items);
  }
  if (card.card_type === 'plotly' && card.preview && card.preview.spec) {
    return _buildPlotlyPrompt(card.preview.spec);
  }
  if (card.card_type === 'decision' && card.preview) {
    return _buildDecisionPrompt(card);
  }
  if (card.card_type === 'agent' && card.preview) {
    return _buildAgentPrompt(card.preview);
  }
  if (card.card_type === 'image') {
    return '*(image — not copyable as text)*\n';
  }
  return '';
}

function _buildTablePrompt(preview) {
  var shape = preview.shape || [0, 0];
  var totalRows = shape[0];
  var totalCols = shape[1];
  var rows = preview.preview_rows || [];
  var cols = preview.columns || [];
  var maxPreviewRows = 20;
  var maxCols = 20;

  var displayCols = cols.length > maxCols ? cols.slice(0, maxCols) : cols;
  var colsTruncated = cols.length > maxCols;

  var lines = [];
  lines.push(totalRows.toLocaleString() + ' rows \u00d7 ' + totalCols + ' columns\n');

  // Header row
  lines.push('| ' + displayCols.join(' | ') + (colsTruncated ? ' | \u2026' : '') + ' |');
  // Separator
  var sep = displayCols.map(function(c) {
    var dtype = preview.dtypes && preview.dtypes[c] || '';
    return dtype.match(/int|float|num/i) ? '---:' : '---';
  });
  lines.push('| ' + sep.join(' | ') + (colsTruncated ? ' | ---' : '') + ' |');

  // Data rows
  var displayRows = rows.length > maxPreviewRows ? rows.slice(0, maxPreviewRows) : rows;
  displayRows.forEach(function(row) {
    var vals = [];
    for (var i = 0; i < displayCols.length; i++) {
      var v = i < row.length ? row[i] : '';
      vals.push(v === null ? '\u2014' : String(v));
    }
    lines.push('| ' + vals.join(' | ') + (colsTruncated ? ' | \u2026' : '') + ' |');
  });

  if (totalRows > displayRows.length) {
    lines.push('\n*(' + displayRows.length + ' of ' + totalRows.toLocaleString() + ' rows shown)*');
  }

  return lines.join('\n') + '\n';
}

function _buildKeyValuePrompt(items) {
  var lines = [];
  Object.keys(items).forEach(function(k) {
    lines.push('- **' + k + '**: ' + items[k]);
  });
  return lines.join('\n') + '\n';
}

function _buildPlotlyPrompt(spec) {
  var layout = spec.layout || {};
  var data = spec.data || [];
  var lines = [];

  // Title
  var title = layout.title;
  if (title) {
    lines.push('**' + (typeof title === 'object' ? title.text || '' : title) + '**\n');
  }

  // Axes
  var xLabel = layout.xaxis && layout.xaxis.title;
  var yLabel = layout.yaxis && layout.yaxis.title;
  if (xLabel || yLabel) {
    var axParts = [];
    if (xLabel) axParts.push('X: ' + (typeof xLabel === 'object' ? xLabel.text || '' : xLabel));
    if (yLabel) axParts.push('Y: ' + (typeof yLabel === 'object' ? yLabel.text || '' : yLabel));
    lines.push(axParts.join('  |  '));
  }

  // Traces summary
  if (data.length > 0) {
    lines.push('');
    data.forEach(function(trace, i) {
      var name = trace.name || ('Trace ' + (i + 1));
      var type = trace.type || 'scatter';
      var n = 0;
      if (trace.x && trace.x.length) n = trace.x.length;
      else if (trace.y && trace.y.length) n = trace.y.length;
      else if (trace.values && trace.values.length) n = trace.values.length;
      lines.push('- ' + name + ' (' + type + ', ' + n + ' points)');
    });
  }

  return lines.join('\n') + '\n';
}

function _buildDecisionPrompt(card) {
  var lines = [];
  var fields = (card.preview && card.preview.fields) || [];
  fields.forEach(function(f) {
    if (f.question) lines.push('**Q:** ' + f.question);
    if (f.options && f.options.length > 0) {
      f.options.forEach(function(opt) {
        var label = typeof opt === 'object' ? opt.label : opt;
        lines.push('  - ' + label);
      });
    }
  });
  return lines.join('\n') + '\n';
}

function _buildResponsePrompt(card) {
  if (!card.response_action) return '';
  var lines = ['\n**Response:** ' + card.response_action];
  if (card.response_values) {
    Object.keys(card.response_values).forEach(function(k) {
      lines.push('- ' + k + ': ' + card.response_values[k]);
    });
  }
  // The server stores a selection artifact only when rows or points were
  // selected; otherwise the summary is just "from '<title>'".
  if (card.response_artifact_id && card.response_summary) {
    lines.push('*Selection:* ' + card.response_summary);
  }
  if (card.response_message) {
    lines.push('*Note:* ' + card.response_message);
  }
  return lines.join('\n') + '\n';
}

function _buildAgentPrompt(preview) {
  var lines = [];
  var status = preview.status || 'pending';
  lines.push('Agent: ' + status + (preview.model ? ' (' + preview.model + ')' : ''));
  if (preview.task) lines.push('Task: ' + preview.task);
  if (preview.duration != null) lines.push('Duration: ' + formatAgentDuration(preview.duration));
  if (preview.error) lines.push('Error: ' + preview.error);
  if ((status === 'completed' || status === 'failed') && preview.output) {
    lines.push('', preview.output);
  }
  return lines.join('\n') + '\n';
}
