'use strict';

// ================================================================
// PNG EXPORT — one-click card → presentation-ready PNG
// ================================================================

var _html2canvasPromise = null;

// Light-theme values forced on every export clone
var EXPORT_THEME = {
  '--bg': '#ffffff',
  '--bg-card': '#f8f9fa',
  '--bg-header': '#f0f1f3',
  '--text': '#1a1a2e',
  '--text-muted': '#6c757d',
  '--border': '#dee2e6',
  '--accent': '#4361ee',
  '--accent-hover': '#3a56d4',
  '--shadow': 'rgba(0,0,0,0.06)',
  '--bg-code': '#f1f3f5',
  '--text-code': '#e83e8c',
  '--bg-badge': '#e9ecef',
  '--text-badge': '#495057',
  '--bg-input': '#ffffff',
  '--bg-overlay': 'rgba(0,0,0,0.5)',
  '--bg-table-stripe': '#f8f9fa',
  '--bg-table-hover': '#e9ecef'
};

// Selectors stripped from export clone (interactive chrome)
var EXPORT_STRIP_SELECTORS = [
  '.card-actions',
  '.card-collapse-btn',
  '.response-actions',
  '.annotation-form',
  '.table-toolbar',
  '.table-pager',
  '.card-updating-overlay'
];

// Shared by concurrent exports; cleared on failure so the next export retries.
function _loadHtml2Canvas() {
  if (!_html2canvasPromise) {
    _html2canvasPromise = new Promise(function(resolve, reject) {
      var s = document.createElement('script');
      s.src = '/static/vendor/html2canvas.min.js';
      s.onload = function() { resolve(); };
      s.onerror = function() {
        _html2canvasPromise = null;
        s.remove();
        var err = new Error('Could not load ' + s.src);
        err.userMessage = 'Failed to load export library';
        reject(err);
      };
      document.head.appendChild(s);
    });
  }
  return _html2canvasPromise;
}

function _sanitizeFilename(title) {
  return (title || 'card').replace(/[^a-zA-Z0-9]+/g, '_').replace(/^_|_$/g, '').substring(0, 60);
}

function _forceTheme(el) {
  for (var v in EXPORT_THEME) {
    el.style.setProperty(v, EXPORT_THEME[v]);
  }
}

function _stripChrome(clone) {
  EXPORT_STRIP_SELECTORS.forEach(function(sel) {
    var els = clone.querySelectorAll(sel);
    for (var i = 0; i < els.length; i++) els[i].remove();
  });
  clone.classList.remove('card-collapsed', 'dismissed');
}

function _prepareClone(clone) {
  _forceTheme(clone);
  _stripChrome(clone);

  // Fixed width, no shadow, white bg
  clone.style.width = '800px';
  clone.style.maxWidth = '800px';
  clone.style.boxShadow = 'none';
  clone.style.margin = '0';
  clone.style.borderRadius = '8px';
  clone.style.border = '1px solid #dee2e6';

  // Uncap table scroll
  var scrollables = clone.querySelectorAll('.table-scroll-container');
  for (var i = 0; i < scrollables.length; i++) {
    scrollables[i].style.maxHeight = 'none';
    scrollables[i].style.overflow = 'visible';
  }
}

function _downloadBlob(blob, filename) {
  var url = URL.createObjectURL(blob);
  var a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  setTimeout(function() { URL.revokeObjectURL(url); }, 1000);
}

function _buildExportClone(cardEl) {
  // Capture a Plotly chart as a static image first; on failure, export
  // without the Plotly special handling.
  var plotlyDiv = cardEl.querySelector('.plotly-container');
  var chartImage = plotlyDiv && typeof Plotly !== 'undefined'
    ? Plotly.toImage(plotlyDiv, { format: 'png', width: 760, height: 450, scale: 2 })
        .catch(function() { return null; })
    : Promise.resolve(null);

  return chartImage.then(function(imgUrl) {
    var clone = cardEl.cloneNode(true);
    _prepareClone(clone);
    // Replace chart container with static image
    var chartClone = imgUrl && clone.querySelector('.plotly-container');
    if (chartClone) {
      var img = document.createElement('img');
      img.src = imgUrl;
      img.style.width = '100%';
      img.style.display = 'block';
      chartClone.parentNode.replaceChild(img, chartClone);
    }
    return clone;
  });
}

function _renderClone(clone, cardData) {
  // Offscreen container for rendering
  var container = document.createElement('div');
  container.style.position = 'fixed';
  container.style.left = '-9999px';
  container.style.top = '0';
  container.style.zIndex = '-1';
  document.body.appendChild(container);
  container.appendChild(clone);

  return html2canvas(clone, {
    scale: 2,
    useCORS: true,
    backgroundColor: '#ffffff',
    logging: false
  }).then(function(canvas) {
    return new Promise(function(resolve, reject) {
      canvas.toBlob(function(blob) {
        if (!blob) return reject(new Error('Canvas produced no PNG data'));
        var name = 'vitrine-' + _sanitizeFilename(cardData.title || cardData.card_type) + '.png';
        _downloadBlob(blob, name);
        resolve();
      }, 'image/png');
    });
  }).then(function() {
    container.remove();
  }, function(err) {
    container.remove();
    throw err;
  });
}

function exportCardAsPng(cardEl, cardData) {
  if (cardEl.classList.contains('card-exporting')) return;
  // card-exporting disables pointer events, so every path must clear it.
  cardEl.classList.add('card-exporting');
  function release() { cardEl.classList.remove('card-exporting'); }

  _loadHtml2Canvas()
    .then(function() { return _buildExportClone(cardEl); })
    .then(function(clone) { return _renderClone(clone, cardData); })
    .then(function() {
      showToast('PNG exported');
    }, function(err) {
      console.error('PNG export failed:', err);
      showToast((err && err.userMessage) || 'Export failed', 'error');
    })
    .then(release, release);
}
