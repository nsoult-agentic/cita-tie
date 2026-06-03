// Runs at document_start in the content-script (isolated) world.
// To affect the PAGE world's navigator, inject an inline <script> that runs
// before any of the page's own scripts (incl. F5 BIG-IP ASM inline bot-defense).
(function () {
  const code = `
    (function () {
      // geckodriver forces navigator.webdriver = true; real Firefox reports false.
      // Override the prototype getter to report false on every page load.
      try {
        Object.defineProperty(Navigator.prototype, 'webdriver', {
          get: () => false, configurable: true
        });
      } catch (e) {}
      // Headless/automation Firefox reports navigator.plugins.length === 0.
      try {
        if (navigator.plugins && navigator.plugins.length === 0) {
          Object.defineProperty(navigator, 'plugins', {
            get: () => [1, 2, 3, 4, 5], configurable: true
          });
        }
      } catch (e) {}
    })();
  `;
  const s = document.createElement('script');
  s.textContent = code;
  (document.head || document.documentElement).appendChild(s);
  s.remove();
})();
