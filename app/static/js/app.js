(() => {
  const root = document.documentElement;
  const storageKey = "wai-intelligence-preferences";

  function readPreferences() {
    try {
      return JSON.parse(localStorage.getItem(storageKey) || "{}");
    } catch {
      return {};
    }
  }

  function savePreferences(preferences) {
    try {
      localStorage.setItem(storageKey, JSON.stringify(preferences));
    } catch {
      // Preferences still work for the current page if browser storage is unavailable.
    }
  }

  const preferences = readPreferences();
  const initialTheme = preferences.theme === "dark" ? "dark" : "light";
  const initialSize = ["small", "medium", "large"].includes(preferences.fontSize) ? preferences.fontSize : "medium";
  root.dataset.theme = initialTheme;
  root.dataset.fontSize = initialSize;

  function updateSizeButtons(size) {
    document.querySelectorAll("[data-font-size]").forEach((button) => {
      if (button.tagName === "BUTTON") {
        button.setAttribute("aria-pressed", String(button.dataset.fontSize === size));
      }
    });
  }

  updateSizeButtons(initialSize);

  document.querySelectorAll(".size-button[data-font-size]").forEach((button) => {
    button.addEventListener("click", () => {
      const fontSize = button.dataset.fontSize;
      root.dataset.fontSize = fontSize;
      const current = readPreferences();
      savePreferences({ ...current, fontSize });
      updateSizeButtons(fontSize);
    });
  });

  const languageSelect = document.getElementById("language-select");
  if (languageSelect) {
    languageSelect.addEventListener("change", () => languageSelect.form.requestSubmit());
  }

  const themeToggle = document.getElementById("theme-toggle");
  if (themeToggle) {
    themeToggle.setAttribute("aria-pressed", String(initialTheme === "dark"));
    themeToggle.addEventListener("click", () => {
      const theme = root.dataset.theme === "dark" ? "light" : "dark";
      root.dataset.theme = theme;
      themeToggle.setAttribute("aria-pressed", String(theme === "dark"));
      const current = readPreferences();
      savePreferences({ ...current, theme });
    });
  }
})();
