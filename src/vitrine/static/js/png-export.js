'use strict';

// ================================================================
// PNG EXPORT — one-click card → presentation-ready PNG
// ================================================================

var _html2canvasLoaded = false;
var _html2canvasLoading = false;

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

function _loadHtml2Canvas(cb) {
  if (_html2canvasLoaded) return cb();
  if (_html2canvasLoading) {
    // Another load in progress — poll
    var poll = setInterval(function() {
      if (_html2canvasLoaded) { clearInterval(poll); cb(); }
    }, 50);
    return;
  }
  _html2canvasLoading = true;
  var s = document.createElement('script');
  s.src = '/static/vendor/html2canvas.min.js';
  s.onload = function() { _html2canvasLoaded = true; _html2canvasLoading = false; cb(); };
  s.onerror = function() { _html2canvasLoading = false; showToast('Failed to load export library', 'error'); };
  document.head.appendChild(s);
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

function _renderClone(clone, cardEl, cardData) {
  // Offscreen container for rendering
  var container = document.createElement('div');
  container.style.position = 'fixed';
  container.style.left = '-9999px';
  container.style.top = '0';
  container.style.zIndex = '-1';
  document.body.appendChild(container);
  container.appendChild(clone);

  html2canvas(clone, {
    scale: 2,
    useCORS: true,
    backgroundColor: '#ffffff',
    logging: false
  }).then(function(canvas) {
    canvas.toBlob(function(blob) {
      var name = 'vitrine-' + _sanitizeFilename(cardData.title || cardData.card_type) + '.png';
      _downloadBlob(blob, name);
      cardEl.classList.remove('card-exporting');
      showToast('PNG exported');
      document.body.removeChild(container);
    }, 'image/png');
  }).catch(function(err) {
    console.error('PNG export failed:', err);
    cardEl.classList.remove('card-exporting');
    showToast('Export failed', 'error');
    document.body.removeChild(container);
  });
}

function exportCardAsPng(cardEl, cardData) {
  if (cardEl.classList.contains('card-exporting')) return;
  cardEl.classList.add('card-exporting');

  _loadHtml2Canvas(function() {
    // Check for Plotly chart
    var plotlyDiv = cardEl.querySelector('.plotly-container');
    if (plotlyDiv && typeof Plotly !== 'undefined') {
      // Capture chart as static image first
      Plotly.toImage(plotlyDiv, { format: 'png', width: 760, height: 450, scale: 2 }).then(function(imgUrl) {
        var clone = cardEl.cloneNode(true);
        _prepareClone(clone);
        // Replace chart container with static image
        var chartClone = clone.querySelector('.plotly-container');
        if (chartClone) {
          var img = document.createElement('img');
          img.src = imgUrl;
          img.style.width = '100%';
          img.style.display = 'block';
          chartClone.parentNode.replaceChild(img, chartClone);
        }
        _renderClone(clone, cardEl, cardData);
      }).catch(function() {
        // Fallback: export without Plotly special handling
        var clone = cardEl.cloneNode(true);
        _prepareClone(clone);
        _renderClone(clone, cardEl, cardData);
      });
    } else {
      var clone = cardEl.cloneNode(true);
      _prepareClone(clone);
      _renderClone(clone, cardEl, cardData);
    }
  });
}
