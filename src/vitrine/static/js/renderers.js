'use strict';

// ================================================================
// CONTENT RENDERERS — markdown, key-value, plotly, image
// ================================================================
function renderMarkdown(container, cardData) {
  var text = (cardData.preview && cardData.preview.text) || '';
  // Local images were copied beside the card; show() wrote vitrine-image:<file>.
  text = text.split('vitrine-image:').join('/api/image/' + encodeURIComponent(cardData.card_id) + '/');
  container.className += ' markdown-body';

  if (window.marked) {
    if (!state.markedConfigured) {
      state.markedConfigured = true;
      window.marked.setOptions({ breaks: true, gfm: true });
    }
    container.innerHTML = window.marked.parse(text);
  } else {
    // Fallback: marked.min.js should be loaded eagerly via index.html,
    // but handle the edge case gracefully.
    container.textContent = text;
  }
}

function renderKeyValue(container, cardData) {
  var items = (cardData.preview && cardData.preview.items) || {};
  var dl = document.createElement('div');
  dl.className = 'kv-list';

  Object.keys(items).forEach(function(key) {
    var keyEl = document.createElement('div');
    keyEl.className = 'kv-key';
    keyEl.textContent = key;

    var valEl = document.createElement('div');
    valEl.className = 'kv-value';
    valEl.textContent = items[key];

    dl.appendChild(keyEl);
    dl.appendChild(valEl);
  });

  container.appendChild(dl);
}

function renderImage(container, cardData) {
  var preview = cardData.preview || {};
  if (preview.images) {
    renderPictures(container, cardData);
    return;
  }
  var imgContainer = document.createElement('div');
  imgContainer.className = 'image-container';

  var img = document.createElement('img');

  if (preview.data && preview.format === 'svg') {
    img.src = 'data:image/svg+xml;base64,' + preview.data;
  } else if (preview.data && preview.format === 'png') {
    img.src = 'data:image/png;base64,' + preview.data;
  } else if (cardData.artifact_id) {
    // Fall back to artifact endpoint
    img.src = '/api/artifact/' + cardData.artifact_id;
  } else {
    container.textContent = 'No image data';
    return;
  }

  img.alt = cardData.title || 'Figure';
  img.style.maxWidth = '100%';
  imgContainer.appendChild(img);
  container.appendChild(imgContainer);
}
