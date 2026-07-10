// ── 主题切换（深色 ⇆ 浅色，localStorage 持久化，随时切换）──
(function initTheme() {
  // 默认深色；首次访问跟随系统偏好
  let saved = localStorage.getItem('automind_theme');
  if (!saved) {
    saved = (window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches)
      ? 'light' : 'dark';
  }
  if (saved === 'light') document.body.classList.add('light');
  document.addEventListener('DOMContentLoaded', updateThemeBtn);
})();

function toggleTheme() {
  const light = document.body.classList.toggle('light');
  localStorage.setItem('automind_theme', light ? 'light' : 'dark');
  updateThemeBtn();
  toast(light ? '已切换到浅色模式' : '已切换到深色模式', 'info');
}

function updateThemeBtn() {
  const light = document.body.classList.contains('light');
  // 设置菜单里的主题项（当前入口）
  const ico = document.getElementById('sm-theme-ico');
  const label = document.getElementById('sm-theme-label');
  if (ico) ico.textContent = light ? '🌙' : '☀️';
  if (label) label.textContent = light ? '切换到深色模式' : '切换到浅色模式';
  // 兼容旧头部按钮（若存在）
  const btn = document.getElementById('theme-btn');
  if (btn) {
    btn.textContent = light ? '🌙' : '☀️';
    btn.title = light ? '切换到深色模式' : '切换到浅色模式';
  }
}
