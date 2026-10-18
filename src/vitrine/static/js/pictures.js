'use strict';

// ================================================================
// PICTURES — image cards made by show(image) and show([images]).
// A single picture, or a gallery of thumbnails, opens in a viewer
// that zooms and pans, steps through the card's pictures, and shows
// or hides their overlays. Files come from /api/image/<card>/<name>
// and are named by their content, so the browser caches them.
// ================================================================

var PICTURE_MAX_ZOOM = 16;

function pictureUrl(cardId, name) {
  return '/api/image/' + encodeURIComponent(cardId) + '/' + encodeURIComponent(name);
}

function _pictureLegend(images) {
  var seen = {};
  var legend = [];
  images.forEach(function(entry) {
    (entry.legend || []).forEach(function(item) {
      if (!seen[item[0]]) {
        seen[item[0]] = true;
        legend.push(item);
      }
    });
  });
  return legend;
}

function _renderLegend(legend) {
  var el = document.createElement('div');
  el.className = 'picture-legend';
  legend.forEach(function(item) {
    var chip = document.createElement('span');
    chip.className = 'picture-legend-item';
    var swatch = document.createElement('span');
    swatch.className = 'picture-legend-swatch';
    swatch.style.background = item[1];
    chip.appendChild(swatch);
    chip.appendChild(document.createTextNode(item[0]));
    el.appendChild(chip);
  });
  return el;
}

function _renderPictureLabels(labels) {
  var el = document.createElement('div');
  el.className = 'picture-labels';
  Object.keys(labels).forEach(function(key) {
    var chip = document.createElement('span');
    chip.className = 'picture-label';
    chip.textContent = key + ': ' + labels[key];
    el.appendChild(chip);
  });
  return el;
}

function renderPictures(container, cardData) {
  var preview = cardData.preview || {};
  var images = preview.images || [];
  var single = preview.layout === 'single';
  var hasOverlay = images.some(function(entry) { return !!entry.overlay; });

  var root = document.createElement('div');
  root.className = 'pictures ' + (single ? 'pictures-single' : 'pictures-gallery');
  if (hasOverlay) root.classList.add('show-overlays');

  if (hasOverlay) {
    var toolbar = document.createElement('div');
    toolbar.className = 'pictures-toolbar';
    var toggle = document.createElement('label');
    toggle.className = 'pictures-toggle';
    var box = document.createElement('input');
    box.type = 'checkbox';
    box.checked = true;
    box.onchange = function() { root.classList.toggle('show-overlays', box.checked); };
    toggle.appendChild(box);
    toggle.appendChild(document.createTextNode(' Overlays'));
    toolbar.appendChild(toggle);
    var legend = _pictureLegend(images);
    if (legend.length) toolbar.appendChild(_renderLegend(legend));
    root.appendChild(toolbar);
  }

  var grid = document.createElement('div');
  grid.className = 'pictures-grid';
  images.forEach(function(entry, i) {
    var tile = document.createElement('div');
    tile.className = 'picture-tile';
    tile.dataset.caption = entry.caption || '';
    var main = document.createElement('button');
    main.type = 'button';
    main.className = 'picture-main';
    main.title = 'Open in the viewer';

    var frame = document.createElement('span');
    frame.className = 'picture-frame';
    var img = document.createElement('img');
    img.loading = 'lazy';
    img.decoding = 'async';
    img.src = pictureUrl(cardData.card_id, single ? entry.file : entry.thumb);
    img.alt = entry.caption || cardData.title || 'Image ' + (i + 1);
    if (single) {
      img.width = entry.width;
      img.height = entry.height;
    }
    frame.appendChild(img);
    if (entry.overlay) {
      var overlay = document.createElement('img');
      overlay.className = 'picture-overlay';
      overlay.loading = 'lazy';
      overlay.alt = '';
      overlay.src = pictureUrl(cardData.card_id, single ? entry.overlay : entry.overlay_thumb);
      frame.appendChild(overlay);
    }
    main.appendChild(frame);
    tile.appendChild(main);
    var zoom = document.createElement('button');
    zoom.type = 'button';
    zoom.className = 'picture-zoom';
    zoom.textContent = '\u2922';
    zoom.title = 'Open in the viewer';
    zoom.setAttribute('aria-label', 'Open ' + (entry.caption || 'image ' + (i + 1)) + ' in the viewer');
    zoom.onclick = function() { openPictureViewer(cardData, i, zoom); };
    tile.appendChild(zoom);

    if (entry.caption) {
      var caption = document.createElement('span');
      caption.className = 'picture-caption';
      caption.textContent = entry.caption;
      tile.appendChild(caption);
    }
    if (entry.labels) tile.appendChild(_renderPictureLabels(entry.labels));
    main.onclick = function() {
      var picker = root._picker;
      if (picker && picker.toggle(entry.caption)) return;
      openPictureViewer(cardData, i, main);
    };
    grid.appendChild(tile);
  });
  root.appendChild(grid);
  container.appendChild(root);

  // On a re-render (the card was updated), the question bar already exists.
  var cardEl = container.closest && container.closest('.card');
  if (cardEl && cardEl.querySelector('.card-controls-bar')) linkPictureQuestion(cardEl, cardData);
}

// ----------------------------------------------------------------
// Questions answered on the pictures
// ----------------------------------------------------------------
// A question whose options are this card's captions is answered by
// clicking the pictures: a tile picks its option (a single choice moves
// the pick), and the option list folds into a line naming the picks.
// The question's own inputs stay in the form and carry the answer, so
// submitting and freezing work as for any other question.

function _pictureQuestion(cardData) {
  var captions = {};
  ((cardData.preview && cardData.preview.images) || []).forEach(function(entry) {
    if (entry.caption) captions[entry.caption] = true;
  });
  var controls = (cardData.preview && cardData.preview.controls) || [];
  for (var i = 0; i < controls.length; i++) {
    var options = controls[i].options || [];
    if (controls[i].type === 'question' && options.length && options.every(function(opt) {
      return captions[opt.label];
    })) {
      return controls[i];
    }
  }
  return null;
}

function _fieldPicks(fieldEl) {
  var select = fieldEl.querySelector('select[data-field-input]');
  if (select) return select.value && select.value !== '__other__' ? [select.value] : [];
  var picks = [];
  fieldEl.querySelectorAll('input[data-field-input]:checked').forEach(function(input) {
    if (input.value !== '__other__') picks.push(input.value);
  });
  return picks;
}

function _toggleFieldPick(fieldEl, label) {
  var select = fieldEl.querySelector('select[data-field-input]');
  if (select) {
    select.value = select.value === label ? '' : label;
    select.dispatchEvent(new Event('change', { bubbles: true }));
    return;
  }
  var input = null;
  fieldEl.querySelectorAll('input[data-field-input]').forEach(function(candidate) {
    if (candidate.value === label) input = candidate;
  });
  if (!input) return;
  input.checked = !input.checked;
  input.dispatchEvent(new Event('change', { bubbles: true }));
}

function _markPicks(root, picks) {
  var set = {};
  picks.forEach(function(label) { set[label] = true; });
  root.querySelectorAll('.picture-tile').forEach(function(tile) {
    var picked = !!set[tile.dataset.caption];
    tile.classList.toggle('picked', picked);
    tile.querySelector('.picture-main').setAttribute('aria-pressed', root._picker ? String(picked) : 'false');
  });
}

function linkPictureQuestion(cardEl, cardData) {
  var root = cardEl.querySelector('.pictures');
  var field = _pictureQuestion(cardData);
  if (!root || !field) return;

  // Answered: show the picks, and let the tiles open the viewer again.
  var answered = cardData.response_action && cardData.response_values;
  if (answered) {
    var value = cardData.response_values[field.name];
    _markPicks(root, value == null ? [] : (Array.isArray(value) ? value : [value]));
    return;
  }

  var fieldEl = null;
  cardEl.querySelectorAll('.card-controls-bar .form-field').forEach(function(el) {
    if (el.dataset.fieldName === field.name) fieldEl = el;
  });
  if (!fieldEl) return;

  fieldEl.classList.add('question-on-pictures');
  // An updated card re-renders its pictures but keeps its question bar.
  var summary = fieldEl.querySelector('.picture-question-summary');
  if (!summary) {
    summary = document.createElement('div');
    summary.className = 'picture-question-summary';
    var questionText = fieldEl.querySelector('.question-text');
    if (questionText) questionText.after(summary);
    else fieldEl.prepend(summary);
  }

  function sync() {
    var picks = _fieldPicks(fieldEl);
    _markPicks(root, picks);
    summary.textContent = (field.multiple ? 'Click the pictures to choose. ' : 'Click a picture to choose it. ')
      + (picks.length ? 'Chosen: ' + picks.join(', ') : 'None chosen yet.');
  }

  root.classList.add('pictures-picking');
  root._picker = {
    isPicked: function(label) { return _fieldPicks(fieldEl).indexOf(label) !== -1; },
    // False once the answer is sent and the form is gone: the tile opens the viewer then.
    toggle: function(label) {
      if (!fieldEl.isConnected) {
        root._picker = null;
        root.classList.remove('pictures-picking');
        return false;
      }
      _toggleFieldPick(fieldEl, label);
      return true;
    },
  };
  if (fieldEl._pictureSync) fieldEl.removeEventListener('change', fieldEl._pictureSync);
  fieldEl._pictureSync = sync;
  fieldEl.addEventListener('change', sync);
  sync();
}

// ----------------------------------------------------------------
// Viewer
// ----------------------------------------------------------------

var _pictureViewer = null;

function closePictureViewer() {
  var v = _pictureViewer;
  if (!v) return;
  _pictureViewer = null;
  document.removeEventListener('keydown', v.onKey);
  window.removeEventListener('resize', v.onResize);
  v.root.remove();
  if (v.returnFocus && document.contains(v.returnFocus)) v.returnFocus.focus();
}

function _viewerButton(label, title, onClick) {
  var btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'picture-viewer-btn';
  btn.textContent = label;
  btn.title = title;
  btn.setAttribute('aria-label', title);
  btn.onclick = onClick;
  return btn;
}

function openPictureViewer(cardData, index, returnFocus) {
  closePictureViewer();
  var images = (cardData.preview && cardData.preview.images) || [];
  if (!images.length) return;

  var v = {
    images: images,
    index: index,
    scale: 1, x: 0, y: 0, fitW: 0, fitH: 0,
    overlayOn: true,
    returnFocus: returnFocus,
  };

  var root = document.createElement('div');
  root.className = 'picture-viewer';
  root.setAttribute('role', 'dialog');
  root.setAttribute('aria-modal', 'true');
  root.setAttribute('aria-label', cardData.title || 'Image viewer');

  var header = document.createElement('div');
  header.className = 'picture-viewer-header';
  var title = document.createElement('span');
  title.className = 'picture-viewer-title';
  var counter = document.createElement('span');
  counter.className = 'picture-viewer-counter';
  header.appendChild(title);
  header.appendChild(counter);

  var tools = document.createElement('div');
  tools.className = 'picture-viewer-tools';
  var overlayToggle = document.createElement('label');
  overlayToggle.className = 'pictures-toggle';
  var overlayBox = document.createElement('input');
  overlayBox.type = 'checkbox';
  overlayBox.checked = true;
  overlayToggle.appendChild(overlayBox);
  overlayToggle.appendChild(document.createTextNode(' Overlay'));
  var opacity = document.createElement('input');
  opacity.type = 'range';
  opacity.min = '0';
  opacity.max = '100';
  opacity.value = '100';
  opacity.className = 'picture-viewer-opacity';
  opacity.setAttribute('aria-label', 'Overlay opacity');
  tools.appendChild(overlayToggle);
  tools.appendChild(opacity);
  var pickRoot = returnFocus && returnFocus.closest ? returnFocus.closest('.pictures') : null;
  var pickBtn = _viewerButton('Select', 'Select this picture (s)', function() { togglePick(); });
  pickBtn.classList.add('picture-viewer-pick');
  tools.appendChild(pickBtn);
  tools.appendChild(_viewerButton('Fit', 'Fit to window (0)', function() { fit(); }));
  var closeBtn = _viewerButton('×', 'Close (Esc)', closePictureViewer);
  tools.appendChild(closeBtn);
  header.appendChild(tools);
  root.appendChild(header);

  var stage = document.createElement('div');
  stage.className = 'picture-viewer-stage';
  var layer = document.createElement('div');
  layer.className = 'picture-viewer-layer';
  var img = document.createElement('img');
  img.className = 'picture-viewer-image';
  img.alt = '';
  img.draggable = false;
  var overlayImg = document.createElement('img');
  overlayImg.className = 'picture-viewer-overlay';
  overlayImg.alt = '';
  overlayImg.draggable = false;
  layer.appendChild(img);
  layer.appendChild(overlayImg);
  stage.appendChild(layer);

  var prev = _viewerButton('‹', 'Previous (←)', function() { go(-1); });
  prev.classList.add('picture-viewer-prev');
  var next = _viewerButton('›', 'Next (→)', function() { go(1); });
  next.classList.add('picture-viewer-next');
  stage.appendChild(prev);
  stage.appendChild(next);
  root.appendChild(stage);

  var footer = document.createElement('div');
  footer.className = 'picture-viewer-footer';
  root.appendChild(footer);

  function apply() {
    layer.style.width = v.fitW + 'px';
    layer.style.height = v.fitH + 'px';
    layer.style.transform = 'translate(' + v.x + 'px,' + v.y + 'px) scale(' + v.scale + ')';
  }

  function fit() {
    var entry = v.images[v.index];
    var w = stage.clientWidth;
    var h = stage.clientHeight;
    var ratio = Math.min(w / entry.width, h / entry.height, 1);
    v.fitW = entry.width * ratio;
    v.fitH = entry.height * ratio;
    v.scale = 1;
    v.x = (w - v.fitW) / 2;
    v.y = (h - v.fitH) / 2;
    apply();
  }

  function zoomAt(px, py, factor) {
    var s = Math.min(PICTURE_MAX_ZOOM, Math.max(1, v.scale * factor));
    v.x = px - (px - v.x) * (s / v.scale);
    v.y = py - (py - v.y) * (s / v.scale);
    v.scale = s;
    if (s === 1) { fit(); return; }
    apply();
  }

  function showOverlay() {
    var entry = v.images[v.index];
    var on = !!entry.overlay && v.overlayOn;
    overlayImg.hidden = !on;
    overlayImg.style.opacity = String(Number(opacity.value) / 100);
    overlayToggle.hidden = !entry.overlay;
    opacity.hidden = !entry.overlay;
  }

  function show(i) {
    v.index = (i + v.images.length) % v.images.length;
    var entry = v.images[v.index];
    var shown = v.index;
    title.textContent = entry.caption || cardData.title || 'Image';
    counter.textContent = v.images.length > 1 ? (v.index + 1) + ' / ' + v.images.length : '';
    prev.hidden = next.hidden = v.images.length < 2;

    // The thumbnail stands in until the full picture has loaded.
    img.src = pictureUrl(cardData.card_id, entry.thumb);
    var full = new Image();
    full.onload = function() { if (v.index === shown) img.src = full.src; };
    full.src = pictureUrl(cardData.card_id, entry.file);
    if (entry.overlay) overlayImg.src = pictureUrl(cardData.card_id, entry.overlay);
    else overlayImg.removeAttribute('src');
    showOverlay();

    showPick();
    footer.innerHTML = '';
    if (entry.labels) footer.appendChild(_renderPictureLabels(entry.labels));
    if (entry.legend) footer.appendChild(_renderLegend(entry.legend));
    fit();
  }

  function showPick() {
    var picker = pickRoot && pickRoot._picker;
    var caption = v.images[v.index].caption;
    pickBtn.hidden = !picker || !caption;
    if (pickBtn.hidden) return;
    var picked = picker.isPicked(caption);
    pickBtn.textContent = picked ? 'Selected \u2713' : 'Select';
    pickBtn.setAttribute('aria-pressed', String(picked));
  }

  function togglePick() {
    var picker = pickRoot && pickRoot._picker;
    var caption = v.images[v.index].caption;
    if (picker && caption) picker.toggle(caption);
    showPick();
  }

  function go(step) { show(v.index + step); }

  overlayBox.onchange = function() { v.overlayOn = overlayBox.checked; showOverlay(); };
  opacity.oninput = showOverlay;

  stage.addEventListener('wheel', function(e) {
    e.preventDefault();
    var rect = stage.getBoundingClientRect();
    zoomAt(e.clientX - rect.left, e.clientY - rect.top, Math.exp(-e.deltaY * 0.0015));
  }, { passive: false });

  var drag = null;
  stage.addEventListener('pointerdown', function(e) {
    if (e.button !== 0 || e.target.closest('button')) return;
    drag = { id: e.pointerId, x: e.clientX, y: e.clientY };
    stage.setPointerCapture(e.pointerId);
    stage.classList.add('dragging');
  });
  stage.addEventListener('pointermove', function(e) {
    if (!drag || drag.id !== e.pointerId) return;
    v.x += e.clientX - drag.x;
    v.y += e.clientY - drag.y;
    drag.x = e.clientX;
    drag.y = e.clientY;
    apply();
  });
  function endDrag(e) {
    if (!drag || drag.id !== e.pointerId) return;
    drag = null;
    stage.classList.remove('dragging');
  }
  stage.addEventListener('pointerup', endDrag);
  stage.addEventListener('pointercancel', endDrag);
  stage.addEventListener('dblclick', function(e) {
    if (e.target.closest('button')) return;
    if (v.scale > 1) { fit(); return; }
    var rect = stage.getBoundingClientRect();
    zoomAt(e.clientX - rect.left, e.clientY - rect.top, 3);
  });

  v.onKey = function(e) {
    if (e.target && e.target.tagName === 'INPUT' && e.target.type !== 'checkbox' && e.target.type !== 'range') return;
    var rect = stage.getBoundingClientRect();
    if (e.key === 'Escape') { closePictureViewer(); }
    else if (e.key === 'ArrowLeft') { go(-1); }
    else if (e.key === 'ArrowRight') { go(1); }
    else if (e.key === '0') { fit(); }
    else if (e.key === '+' || e.key === '=') { zoomAt(rect.width / 2, rect.height / 2, 1.5); }
    else if (e.key === '-') { zoomAt(rect.width / 2, rect.height / 2, 1 / 1.5); }
    else if (e.key === 's' && !pickBtn.hidden) { togglePick(); }
    else if (e.key === 'o' && v.images[v.index].overlay) {
      overlayBox.checked = !overlayBox.checked;
      overlayBox.onchange();
    } else { return; }
    e.preventDefault();
  };
  v.onResize = fit;
  document.addEventListener('keydown', v.onKey);
  window.addEventListener('resize', v.onResize);

  v.root = root;
  _pictureViewer = v;
  document.body.appendChild(root);
  show(index);
  closeBtn.focus();
}
