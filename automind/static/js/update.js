// 🔄 版本检查与一键升级 —— 兼容版（经典原生 JS）界面专用。
//
// 为什么这个文件必须存在：/legacy 是老内核与各级白屏兜底的最终落点，被路由
// 到这里的正是"装了新版打不开、或内核过旧"的那批用户 —— 他们最需要升级提示，
// 而此前经典界面**完全没有版本检查**，等于把最该升级的人永久困在旧版本上。
//
// 为什么这个文件用 ES5 写法（var / function / 字符串拼接），与其余模块的
// ES6 风格不同：其余模块面向正常内核，而本文件要在"连 React 产物都跑不起来"
// 的老内核上工作 —— 用了模板字符串或箭头函数，本文件就会整体解析失败，
// 那正是它要解决的问题本身。这是有意的风格偏离。

(function () {
  var API = '/api';
  var NOTIFIED_KEY = 'automind_update_notified';
  var poll = null;
  var lastInfo = null;

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  function getJSON(url, opts, cb) {
    var xhr = new XMLHttpRequest();
    xhr.open((opts && opts.method) || 'GET', url, true);
    xhr.timeout = 20000;
    xhr.onreadystatechange = function () {
      if (xhr.readyState !== 4) return;
      if (xhr.status >= 200 && xhr.status < 300) {
        try { cb(null, JSON.parse(xhr.responseText || '{}')); }
        catch (e) { cb(e); }
      } else {
        cb(new Error('HTTP ' + xhr.status));
      }
    };
    xhr.ontimeout = function () { cb(new Error('timeout')); };
    xhr.onerror = function () { cb(new Error('network')); };
    xhr.send(null);
  }

  function fmtSize(n) {
    if (!n) return '';
    return n > 1048576 ? (n / 1048576).toFixed(1) + ' MB' : Math.round(n / 1024) + ' KB';
  }

  // ── 横幅（常驻，不像 toast 那样 2.5 秒就消失）──
  function banner() {
    var b = document.getElementById('update-banner');
    if (b) return b;
    b = el('div');
    b.id = 'update-banner';
    document.body.appendChild(b);
    return b;
  }

  function closeBanner() {
    var b = document.getElementById('update-banner');
    if (b && b.parentNode) b.parentNode.removeChild(b);
    if (poll) { clearInterval(poll); poll = null; }
  }

  function render(info) {
    var b = banner();
    b.innerHTML = '';
    var head = el('div', 'ub-head');
    head.appendChild(el('span', 'ub-title', '发现新版本 v' + info.latest));
    var x = el('button', 'ub-x', '×');
    x.title = '稍后再说';
    x.onclick = closeBanner;
    head.appendChild(x);
    b.appendChild(head);

    var sub = '当前 v' + info.current;
    if (info.asset_size) sub += ' · 安装包 ' + fmtSize(info.asset_size);
    b.appendChild(el('div', 'ub-sub', sub));

    if (info.notes) {
      var notes = el('div', 'ub-notes');
      notes.textContent = String(info.notes).slice(0, 600);
      b.appendChild(notes);
    }

    var acts = el('div', 'ub-acts');
    if (info.can_auto_install) {
      var go = el('button', 'ub-btn primary', '⬇ 立即更新（自动重启）');
      go.onclick = function () { apply(); };
      acts.appendChild(go);
    } else if (info.release_url) {
      var open = el('button', 'ub-btn primary', '⬇ 前往下载 ↗');
      open.onclick = function () { window.open(info.release_url, '_blank'); };
      acts.appendChild(open);
    }
    if (info.release_url && info.can_auto_install) {
      var page = el('button', 'ub-btn', '发布页 ↗');
      page.onclick = function () { window.open(info.release_url, '_blank'); };
      acts.appendChild(page);
    }
    var later = el('button', 'ub-btn', '稍后');
    later.onclick = closeBanner;
    acts.appendChild(later);
    b.appendChild(acts);
  }

  function renderProgress(s) {
    var b = document.getElementById('update-banner');
    if (!b) return;
    b.innerHTML = '';
    b.appendChild(el('div', 'ub-head')).appendChild(
      el('span', 'ub-title', s.status === 'verifying' ? '正在校验安装包…'
        : s.status === 'installing' || s.status === 'restarting' ? '正在安装，应用即将重启…'
          : '正在下载更新…'));

    var pct = Math.max(0, Math.min(100, s.progress || 0));
    var bar = el('div', 'ub-bar');
    var fill = el('i');
    fill.style.width = pct + '%';
    bar.appendChild(fill);
    b.appendChild(bar);

    // 弱网下 30MB 要下好几分钟，没有速度/线路反馈用户只会以为卡死
    var bits = [];
    if (s.total) bits.push(fmtSize(s.downloaded || 0) + ' / ' + fmtSize(s.total));
    if (s.speed) bits.push(s.speed + ' MB/s');
    if (s.mirror) bits.push('线路 ' + s.mirror);
    if (s.attempt > 1) bits.push('第 ' + s.attempt + ' 次尝试（自动换线路续传）');
    b.appendChild(el('div', 'ub-sub', bits.join(' · ') || (pct + '%')));
  }

  function renderError(msg) {
    var b = banner();
    b.innerHTML = '';
    var head = el('div', 'ub-head');
    head.appendChild(el('span', 'ub-title err', '更新失败'));
    var x = el('button', 'ub-x', '×');
    x.onclick = closeBanner;
    head.appendChild(x);
    b.appendChild(head);
    b.appendChild(el('div', 'ub-sub', msg || '未知错误'));
    var acts = el('div', 'ub-acts');
    var retry = el('button', 'ub-btn primary', '重试');
    retry.onclick = function () { apply(); };
    acts.appendChild(retry);
    if (lastInfo && lastInfo.release_url) {
      var page = el('button', 'ub-btn', '手动下载 ↗');
      page.onclick = function () { window.open(lastInfo.release_url, '_blank'); };
      acts.appendChild(page);
    }
    b.appendChild(acts);
  }

  function apply() {
    renderProgress({ status: 'downloading', progress: 0 });
    getJSON(API + '/update/apply', { method: 'POST' }, function (err, r) {
      if (err || (r && r.error)) { renderError((r && r.error) || '请求失败'); return; }
      if (poll) clearInterval(poll);
      poll = setInterval(function () {
        getJSON(API + '/update/state', null, function (e2, s) {
          if (e2) {
            // 服务已退出 → 安装器接管，应用即将自动重启
            clearInterval(poll); poll = null;
            renderProgress({ status: 'restarting', progress: 100 });
            return;
          }
          if (s.status === 'error') {
            clearInterval(poll); poll = null;
            renderError(s.error);
            return;
          }
          renderProgress(s);
        });
      }, 700);
    });
  }

  // ── 对外入口 ──
  // manual=true：设置菜单点进来的，强制刷新且"已是最新"也要给回执，
  // 否则用户点了没反应会以为坏了。
  function checkUpdate(manual) {
    getJSON(API + '/update/check' + (manual ? '?force=true' : ''), null, function (err, info) {
      if (err) {
        if (manual && window.toast) toast('检查更新失败：' + err.message, 'error');
        return;
      }
      lastInfo = info;
      if (info.error) {
        if (manual && window.toast) toast(info.error, 'error');
        return;
      }
      if (!info.available) {
        if (manual && window.toast) toast('已是最新版本 v' + info.current, 'success');
        return;
      }
      if (!manual) {
        // 自动提示每会话只弹一次，别打扰
        try {
          if (sessionStorage.getItem(NOTIFIED_KEY)) return;
          sessionStorage.setItem(NOTIFIED_KEY, '1');
        } catch (e) { /* 无 sessionStorage 时照常提示一次 */ }
      }
      render(info);
    });
  }

  // 设置菜单的 onclick 由此拿到函数（本文件整体包在 IIFE 里，不显式挂就取不到）
  window.checkUpdate = checkUpdate;

  // 启动后延迟检查，别和首屏的其它请求抢
  function boot() { setTimeout(function () { checkUpdate(false); }, 3000); }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
