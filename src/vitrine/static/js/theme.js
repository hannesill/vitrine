'use strict';

// ================================================================
// THEME
// ================================================================
var _sunIcon = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2"/><path d="M12 20v2"/><path d="m4.93 4.93 1.41 1.41"/><path d="m17.66 17.66 1.41 1.41"/><path d="M2 12h2"/><path d="M20 12h2"/><path d="m6.34 17.66-1.41 1.41"/><path d="m19.07 4.93-1.41 1.41"/></svg>';
var _moonIcon = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z"/></svg>';

// The board follows the system's colour scheme, which is also what a host
// application's embedded browser reports, until the reader picks a side with
// the toggle. Picking the side the system is already on hands control back.
var _systemDark = window.matchMedia ? window.matchMedia('(prefers-color-scheme: dark)') : null;

function _savedTheme() {
  try {
    var saved = localStorage.getItem('vitrine-theme');
    return saved === 'dark' || saved === 'light' ? saved : null;
  } catch (e) {
    return null;
  }
}

function _applyTheme(dark) {
  if (dark) {
    document.documentElement.setAttribute('data-theme', 'dark');
  } else {
    document.documentElement.removeAttribute('data-theme');
  }
  themeToggleEl.innerHTML = dark ? _sunIcon : _moonIcon;
  _swapHljsTheme(dark);
  // Whatever bakes the theme's colours in (Plotly charts) listens for this,
  // whether the reader clicked the toggle or the system changed its scheme.
  document.dispatchEvent(new CustomEvent('vitrine:themechange', { detail: { dark: dark } }));
}

function initTheme() {
  var saved = _savedTheme();
  _applyTheme(saved ? saved === 'dark' : !!(_systemDark && _systemDark.matches));
}

if (_systemDark && _systemDark.addEventListener) {
  _systemDark.addEventListener('change', function(event) {
    if (!_savedTheme()) _applyTheme(event.matches);
  });
}

themeToggleEl.onclick = function() {
  var dark = document.documentElement.getAttribute('data-theme') !== 'dark';
  try {
    if (_systemDark && _systemDark.matches === dark) {
      localStorage.removeItem('vitrine-theme');
    } else {
      localStorage.setItem('vitrine-theme', dark ? 'dark' : 'light');
    }
  } catch (e) {
    // Without storage the choice still holds for this page.
  }
  _applyTheme(dark);
};

// Swap highlight.js theme if loaded
function _swapHljsTheme(dark) {
  var el = document.getElementById('hljs-theme');
  if (!el) return;
  el.href = dark
    ? 'https://cdn.jsdelivr.net/gh/highlightjs/cdn-release@11.9.0/build/styles/github-dark.min.css'
    : 'https://cdn.jsdelivr.net/gh/highlightjs/cdn-release@11.9.0/build/styles/github.min.css';
}

initTheme();
