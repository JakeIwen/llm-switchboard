// First paint only: apply the saved or system color scheme before the app
// loads, so the page doesn't flash light. Mantine takes over after render.
// The two hex values mirror --bg in src/styles/palette.css; keep them in step.
(function () {
  var saved = 'auto';
  try { saved = localStorage.getItem('mantine-color-scheme-value') || 'auto'; } catch (e) {}
  var dark = saved === 'dark' || (saved === 'auto' && window.matchMedia('(prefers-color-scheme: dark)').matches);
  document.documentElement.setAttribute('data-mantine-color-scheme', dark ? 'dark' : 'light');
  var meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute('content', dark ? '#171614' : '#f6f5f2');
})();
