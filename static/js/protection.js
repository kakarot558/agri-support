/**
 * AgriFortress Image Protection
 * Blocks right-click, drag, print-screen and mobile long-press on all pages.
 * Runs in capture phase so it fires before any element handler.
 */
(function () {
  'use strict';

  /* ── 1. Block ALL right-click menus site-wide ─────────────────────────── */
  document.addEventListener('contextmenu', function (e) {
    e.preventDefault();
    e.stopPropagation();
    return false;
  }, true);   /* true = capture phase */

  /* ── 2. Block image drag to desktop / other tab ───────────────────────── */
  document.addEventListener('dragstart', function (e) {
    if (e.target.tagName === 'IMG') {
      e.preventDefault(); return false;
    }
  });

  /* ── 3. Block Ctrl+S / Ctrl+P / Ctrl+U / PrintScreen ─────────────────── */
  document.addEventListener('keydown', function (e) {
    var k = (e.key || '').toLowerCase();
    if ((e.ctrlKey || e.metaKey) &&
        (k === 's' || k === 'p' || k === 'u' || k === 'a')) {
      e.preventDefault(); return false;
    }
    if (k === 'printscreen') { e.preventDefault(); return false; }
  });

  /* ── 4. Wipe clipboard after PrintScreen ─────────────────────────────── */
  document.addEventListener('keyup', function (e) {
    if (e.key === 'PrintScreen') {
      try { navigator.clipboard && navigator.clipboard.writeText(''); } catch (_) {}
    }
  });

  /* ── 5. Block mobile long-press "Save Image" popup ───────────────────── */
  var _lpt;
  document.addEventListener('touchstart', function (e) {
    var t = e.target;
    if (t.tagName === 'IMG' || t.closest('.prot-wrap, .product-img-wrap, .featured-img-wrap, .detail-img-card')) {
      _lpt = setTimeout(function () { /* do nothing, just consume */ }, 400);
      /* Prevent the native callout/popup */
      if (t.tagName === 'IMG') {
        t.style.webkitTouchCallout = 'none';
        t.style.webkitUserSelect   = 'none';
      }
    }
  }, { passive: true });

  /* ── 6. Hardcode pointer attributes on all product images ─────────────── */
  function lockImages() {
    var selectors = [
      '.prot-wrap img',
      '.product-img',
      '.featured-img',
      '.detail-main-img',
      '.related-img-wrap img',
      '.secure-img'
    ];
    document.querySelectorAll(selectors.join(',')).forEach(function (img) {
      img.style.pointerEvents        = 'none';
      img.style.webkitUserDrag       = 'none';
      img.style.webkitTouchCallout   = 'none';
      img.style.webkitUserSelect     = 'none';
      img.setAttribute('draggable',  'false');
      img.ondragstart   = function () { return false; };
      img.oncontextmenu = function () { return false; };
    });
  }

  /* Run immediately + after DOM ready */
  lockImages();
  document.addEventListener('DOMContentLoaded', lockImages);
  /* Re-run after any dynamic content loads */
  setTimeout(lockImages, 800);
  setTimeout(lockImages, 2000);
})();
