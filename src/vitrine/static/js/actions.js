'use strict';

// ================================================================
// ACTION PALETTE
// ================================================================

var actionPaletteOpen = false;
var actionPaletteActiveIndex = -1;

var actionsBtn = document.getElementById('actions-btn');
if (actionsBtn) {
  actionsBtn.onclick = function(e) {
    e.stopPropagation();
    if (actionPaletteOpen) {
      closeActionPalette();
    } else {
      openActionPalette();
    }
  };
}

function openActionPalette() {
  if (actionPaletteOpen) return;
  actionPaletteOpen = true;
  actionPaletteActiveIndex = -1;
  renderActionPalette();
}

function closeActionPalette() {
  if (!actionPaletteOpen) return;
  actionPaletteOpen = false;
  var backdrop = document.querySelector('.action-palette-backdrop');
  if (backdrop) backdrop.remove();
}

function getActions() {
  var studyLabel = state.activeStudyFilter || null;
  var actions = [];

  // Export HTML
  actions.push({
    icon: '&#128196;',
    label: 'Export HTML',
    handler: function() {
      exportStudy(studyLabel, 'html');
    }
  });

  // Export JSON
  actions.push({
    icon: '&#128230;',
    label: 'Export JSON',
    handler: function() {
      exportStudy(studyLabel, 'json');
    }
  });

  // Download files (ZIP)
  actions.push({
    icon: '&#128451;',
    label: 'Download Files (ZIP)',
    handler: function() {
      var a = document.createElement('a');
      if (studyLabel) {
        a.href = '/api/studies/' + encodeURIComponent(studyLabel) + '/files-archive';
      } else {
        a.href = '/api/files-archive';
      }
      a.download = '';
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      showToast('Downloading files');
    }
  });

  // Print
  actions.push({
    icon: '&#128424;',
    label: 'Print',
    handler: function() {
      window.print();
    }
  });

  // Separator + study-only actions
  if (studyLabel) {
    actions.push({ separator: true });

    // Reproduce Study
    actions.push({
      icon: '&#9654;',
      label: 'Copy Prompt: Reproduce Study',
      handler: function() {
        copyTaskPrompt(studyLabel, 'reproduce');
      }
    });

    // Compile Report
    actions.push({
      icon: '&#128203;',
      label: 'Copy Prompt: Compile Report',
      handler: function() {
        copyTaskPrompt(studyLabel, 'report');
      }
    });

    // Draft Paper
    actions.push({
      icon: '&#128221;',
      label: 'Copy Prompt: Draft Paper',
      handler: function() {
        copyTaskPrompt(studyLabel, 'paper');
      }
    });

    // Create Presentation
    actions.push({
      icon: '&#127916;',
      label: 'Copy Prompt: Create Presentation',
      handler: function() {
        copyTaskPrompt(studyLabel, 'presentation');
      }
    });
  }

  return actions;
}

function renderActionPalette() {
  // Remove existing palette if any
  var existing = document.querySelector('.action-palette-backdrop');
  if (existing) existing.remove();

  var actions = getActions();

  // Backdrop
  var backdrop = document.createElement('div');
  backdrop.className = 'action-palette-backdrop';
  backdrop.addEventListener('mousedown', function(e) {
    if (e.target === backdrop) {
      closeActionPalette();
    }
  });

  // Panel
  var panel = document.createElement('div');
  panel.className = 'action-palette';

  // Header
  var header = document.createElement('div');
  header.className = 'action-palette-header';
  var headerLabel = document.createElement('span');
  headerLabel.textContent = 'Actions';
  header.appendChild(headerLabel);
  var kbd = document.createElement('kbd');
  var isMac = navigator.platform && navigator.platform.indexOf('Mac') !== -1;
  kbd.textContent = isMac ? '\u2318K' : 'Ctrl+K';
  header.appendChild(kbd);
  panel.appendChild(header);

  // Action list
  var list = document.createElement('div');
  list.className = 'action-palette-list';

  var itemIndex = 0;
  for (var i = 0; i < actions.length; i++) {
    var action = actions[i];

    if (action.separator) {
      var sep = document.createElement('div');
      sep.className = 'action-palette-sep';
      list.appendChild(sep);
      continue;
    }

    var item = document.createElement('button');
    item.className = 'action-palette-item';
    if (itemIndex === actionPaletteActiveIndex) {
      item.classList.add('active');
    }
    item.dataset.actionIndex = String(i);
    item.dataset.itemIndex = String(itemIndex);

    var iconSpan = document.createElement('span');
    iconSpan.className = 'action-icon';
    iconSpan.innerHTML = action.icon;
    item.appendChild(iconSpan);

    var labelSpan = document.createElement('span');
    labelSpan.className = 'action-label';
    labelSpan.textContent = action.label;
    item.appendChild(labelSpan);

    (function(act) {
      item.addEventListener('click', function(e) {
        e.stopPropagation();
        closeActionPalette();
        act.handler();
      });
    })(action);

    list.appendChild(item);
    itemIndex++;
  }

  panel.appendChild(list);
  backdrop.appendChild(panel);
  document.body.appendChild(backdrop);
}

// ================================================================
// TASK PROMPTS
// ================================================================

// Copy the prompt that runs a study task (reproduce, report, paper,
// presentation) in the researcher's own coding agent.
function copyTaskPrompt(studyLabel, task) {
  var url = '/api/studies/' + encodeURIComponent(studyLabel)
    + '/prompts/' + encodeURIComponent(task);
  var text = fetch(url).then(function(r) {
    return r.json().catch(function() { return {}; }).then(function(d) {
      if (!r.ok || typeof d.prompt !== 'string') {
        throw new Error(d.error || 'Server returned ' + r.status);
      }
      return d.prompt;
    });
  });

  // Safari only lets a click write to the clipboard while the click is being
  // handled, so hand it the pending text instead of awaiting the fetch first.
  var written;
  if (window.ClipboardItem && navigator.clipboard && navigator.clipboard.write) {
    written = navigator.clipboard.write([new ClipboardItem({
      'text/plain': text.then(function(t) {
        return new Blob([t], { type: 'text/plain' });
      })
    })]);
  } else if (navigator.clipboard && navigator.clipboard.writeText) {
    written = text.then(function(t) { return navigator.clipboard.writeText(t); });
  } else {
    written = Promise.reject(new Error('Clipboard is not available'));
  }

  written.then(function() {
    showToast('Prompt copied \u2014 paste it into your agent');
  }, function(err) {
    // Prefer the server's error over the clipboard's generic one
    text.then(function() {
      showToast((err && err.message) || 'Failed to copy prompt', 'error');
    }, function(fetchErr) {
      showToast(fetchErr.message || 'Failed to copy prompt', 'error');
    });
  });
}

// ================================================================
// KEYBOARD
// ================================================================

document.addEventListener('keydown', function(e) {
  var isMod = e.metaKey || e.ctrlKey;

  // Cmd+K / Ctrl+K — toggle palette
  if (isMod && e.key === 'k') {
    e.preventDefault();
    if (actionPaletteOpen) {
      closeActionPalette();
    } else {
      openActionPalette();
    }
    return;
  }

  // Palette-specific keys
  if (!actionPaletteOpen) return;

  if (e.key === 'Escape') {
    e.preventDefault();
    closeActionPalette();
    return;
  }

  var items = document.querySelectorAll('.action-palette-item');
  if (!items.length) return;

  if (e.key === 'ArrowDown') {
    e.preventDefault();
    actionPaletteActiveIndex = (actionPaletteActiveIndex + 1) % items.length;
    _updateActiveItem(items);
    return;
  }

  if (e.key === 'ArrowUp') {
    e.preventDefault();
    actionPaletteActiveIndex = actionPaletteActiveIndex <= 0
      ? items.length - 1
      : actionPaletteActiveIndex - 1;
    _updateActiveItem(items);
    return;
  }

  if (e.key === 'Enter') {
    e.preventDefault();
    if (actionPaletteActiveIndex < 0) return;
    var activeItem = items[actionPaletteActiveIndex];
    if (activeItem) activeItem.click();
    return;
  }
});

function _updateActiveItem(items) {
  for (var i = 0; i < items.length; i++) {
    if (i === actionPaletteActiveIndex) {
      items[i].classList.add('active');
      items[i].scrollIntoView({ block: 'nearest' });
    } else {
      items[i].classList.remove('active');
    }
  }
}
