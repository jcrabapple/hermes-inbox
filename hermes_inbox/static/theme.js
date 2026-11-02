// Runs in <head> before first paint so there is no light/dark flash. Dark is the default.
(function () {
  var KEY = 'hermes-inbox-theme';
  var COLORS = { dark: '#1a1c21', light: '#f7f6f3' };
  function apply(theme) {
    document.documentElement.dataset.theme = theme;
    var meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute('content', COLORS[theme]);
  }
  var saved = null;
  try { saved = localStorage.getItem(KEY); } catch (e) { /* private mode */ }
  apply(saved === 'light' ? 'light' : 'dark');
  window.hermesTheme = {
    get: function () { return document.documentElement.dataset.theme; },
    set: function (t) { apply(t); try { localStorage.setItem(KEY, t); } catch (e) { /* ignore */ } },
    toggle: function () { this.set(this.get() === 'dark' ? 'light' : 'dark'); return this.get(); },
  };
})();
