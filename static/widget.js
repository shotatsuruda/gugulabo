(function () {
  'use strict';

  var el = document.getElementById('gugulabo-widget');
  if (!el) return;

  var slug = el.getAttribute('data-shop');
  if (!slug) return;

  // Google Fonts の読み込み
  var link = document.createElement('link');
  link.rel = 'stylesheet';
  link.href = 'https://fonts.googleapis.com/css2?family=Noto+Sans+JP:wght@400;500;700&display=swap';
  document.head.appendChild(link);

  // スタイル注入
  var style = document.createElement('style');
  style.textContent = [
    '.glb-widget{font-family:"Noto Sans JP",sans-serif;max-width:640px;margin:0 auto;padding:0;}',
    '.glb-carousel{position:relative;overflow:hidden;border-radius:16px;background:#fff;box-shadow:0 2px 16px rgba(0,0,0,0.08);padding:32px 40px;}',
    '.glb-slide{display:none;animation:glbFadeIn 0.35s ease;}',
    '.glb-slide.active{display:block;}',
    '@keyframes glbFadeIn{from{opacity:0;transform:translateY(6px);}to{opacity:1;transform:translateY(0);}}',
    '.glb-stars{color:#f5a623;font-size:1.2rem;letter-spacing:2px;margin-bottom:12px;}',
    '.glb-comment{font-size:0.95rem;color:#374151;line-height:1.8;min-height:64px;}',
    '.glb-date{font-size:0.75rem;color:#9ca3af;margin-top:12px;}',
    '.glb-nav{display:flex;align-items:center;justify-content:center;gap:10px;margin-top:24px;}',
    '.glb-dot{width:8px;height:8px;border-radius:50%;background:#e5e7eb;cursor:pointer;border:none;padding:0;transition:background 0.2s;}',
    '.glb-dot.active{background:#8C7B6B;}',
    '.glb-arrow{background:none;border:1px solid #e5e7eb;border-radius:50%;width:32px;height:32px;cursor:pointer;',
    '  display:flex;align-items:center;justify-content:center;color:#6b7280;transition:border-color 0.2s,color 0.2s;flex-shrink:0;}',
    '.glb-arrow:hover{border-color:#8C7B6B;color:#8C7B6B;}',
    '.glb-empty{text-align:center;color:#9ca3af;font-size:0.9rem;padding:32px;}',
    '.glb-badge{display:inline-block;font-size:0.7rem;color:#8C7B6B;border:1px solid #e5e7eb;',
    '  border-radius:20px;padding:2px 10px;margin-bottom:16px;letter-spacing:.05em;}',
  ].join('');
  document.head.appendChild(style);

  function stars(n) {
    return '★'.repeat(n) + '☆'.repeat(5 - n);
  }

  function render(reviews) {
    if (!reviews.length) {
      el.innerHTML = '<div class="glb-widget"><div class="glb-empty">レビューがありません</div></div>';
      return;
    }

    var current = 0;
    var total = reviews.length;

    function slidesHtml() {
      return reviews.map(function (r, i) {
        return [
          '<div class="glb-slide' + (i === 0 ? ' active' : '') + '" data-idx="' + i + '">',
          '  <span class="glb-stars">' + stars(r.rating) + '</span>',
          '  <div class="glb-comment">' + escHtml(r.comment || '') + '</div>',
          '  <div class="glb-date">' + escHtml(r.date || '') + '</div>',
          '</div>',
        ].join('');
      }).join('');
    }

    function dotsHtml() {
      return reviews.map(function (_, i) {
        return '<button class="glb-dot' + (i === 0 ? ' active' : '') + '" data-idx="' + i + '" aria-label="' + (i + 1) + '枚目"></button>';
      }).join('');
    }

    el.innerHTML = [
      '<div class="glb-widget">',
      '  <div class="glb-carousel">',
      '    <span class="glb-badge">お客様の声</span>',
      slidesHtml(),
      '  </div>',
      '  <div class="glb-nav">',
      '    <button class="glb-arrow" id="glb-prev" aria-label="前へ">',
      '      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="15 18 9 12 15 6"/></svg>',
      '    </button>',
      dotsHtml(),
      '    <button class="glb-arrow" id="glb-next" aria-label="次へ">',
      '      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 18 15 12 9 6"/></svg>',
      '    </button>',
      '  </div>',
      '</div>',
    ].join('');

    function goTo(idx) {
      var slides = el.querySelectorAll('.glb-slide');
      var dots = el.querySelectorAll('.glb-dot');
      slides[current].classList.remove('active');
      dots[current].classList.remove('active');
      current = (idx + total) % total;
      slides[current].classList.add('active');
      dots[current].classList.add('active');
    }

    el.querySelector('#glb-prev').addEventListener('click', function () { goTo(current - 1); });
    el.querySelector('#glb-next').addEventListener('click', function () { goTo(current + 1); });
    el.querySelectorAll('.glb-dot').forEach(function (dot) {
      dot.addEventListener('click', function () { goTo(parseInt(dot.getAttribute('data-idx'), 10)); });
    });
  }

  function escHtml(str) {
    return str.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  // データ取得（origin 基準で絶対URLを組み立て）
  var base = el.getAttribute('data-api-base') || 'https://gugulabo.com';
  fetch(base + '/widget/' + encodeURIComponent(slug))
    .then(function (res) { return res.json(); })
    .then(function (data) { render(data); })
    .catch(function () {
      el.innerHTML = '<div class="glb-widget"><div class="glb-empty">レビューを読み込めませんでした</div></div>';
    });
})();
