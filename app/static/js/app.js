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

  // Long operational workflows use tabs to keep related steps together and reduce page length.
  const tabbedPaths = new Set(["/app/funding", "/app/reporting", "/app/partnerships", "/app/grants"]);
  const path = window.location.pathname.replace(/\/$/, "");
  const main = document.getElementById("main-content");
  if (main && tabbedPaths.has(path)) {
    const panels = Array.from(main.children).filter((node) => node.tagName === "SECTION" && node.querySelector("h2"));
    if (panels.length > 1) {
      const tabs = document.createElement("div");
      tabs.className = "workflow-tabs";
      tabs.setAttribute("role", "tablist");
      tabs.setAttribute("aria-label", "Workflow sections");
      panels.forEach((panel, index) => {
        const heading = panel.querySelector("h2");
        const button = document.createElement("button");
        button.type = "button";
        button.className = "workflow-tab";
        button.id = `workflow-tab-${index}`;
        button.setAttribute("role", "tab");
        button.setAttribute("aria-controls", `workflow-panel-${index}`);
        button.setAttribute("aria-selected", String(index === 0));
        button.tabIndex = index === 0 ? 0 : -1;
        button.textContent = heading ? heading.textContent.trim() : `Section ${index + 1}`;
        panel.id = `workflow-panel-${index}`;
        panel.setAttribute("role", "tabpanel");
        panel.setAttribute("aria-labelledby", button.id);
        panel.hidden = index !== 0;
        button.addEventListener("click", () => {
          panels.forEach((candidate, candidateIndex) => {
            const active = candidateIndex === index;
            candidate.hidden = !active;
            tabs.children[candidateIndex].setAttribute("aria-selected", String(active));
            tabs.children[candidateIndex].tabIndex = active ? 0 : -1;
          });
        });
        tabs.appendChild(button);
      });
      const firstPanel = panels[0];
      main.insertBefore(tabs, firstPanel);
    }
  }


  // Each partner case separates its summary, research work and decision step.
  document.querySelectorAll(".assessment-card").forEach((card, caseIndex) => {
    const overviewNodes = [card.querySelector(":scope > .panel-heading"), card.querySelector(":scope > .summary-list"), card.querySelector(":scope > .table-wrap.content-section")].filter(Boolean);
    const researchNode = card.querySelector(":scope > .content-section.two-column-grid");
    const decisionNode = card.querySelector(":scope > .decision-panel");
    const groups = [
      { label: document.documentElement.lang === "fr-CA" ? "Aperçu" : "Overview", nodes: overviewNodes },
      { label: document.documentElement.lang === "fr-CA" ? "Recherche" : "Research", nodes: researchNode ? [researchNode] : [] },
      { label: document.documentElement.lang === "fr-CA" ? "Décision" : "Decision", nodes: decisionNode ? [decisionNode] : [] }
    ].filter((group) => group.nodes.length);
    if (groups.length < 2) return;
    const nav = document.createElement("div");
    nav.className = "workflow-tabs assessment-tabs";
    nav.setAttribute("role", "tablist");
    const panels = document.createElement("div");
    panels.className = "assessment-tab-panels";
    groups.forEach((group, index) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "workflow-tab";
      button.id = `assessment-${caseIndex}-tab-${index}`;
      button.setAttribute("role", "tab");
      button.setAttribute("aria-selected", String(index === 0));
      button.tabIndex = index === 0 ? 0 : -1;
      button.textContent = group.label;
      const panel = document.createElement("div");
      panel.className = "assessment-tab-panel";
      panel.id = `assessment-${caseIndex}-panel-${index}`;
      panel.setAttribute("role", "tabpanel");
      panel.setAttribute("aria-labelledby", button.id);
      panel.hidden = index !== 0;
      group.nodes.forEach((node) => panel.appendChild(node));
      button.setAttribute("aria-controls", panel.id);
      button.addEventListener("click", () => {
        Array.from(nav.children).forEach((tab, tabIndex) => {
          const active = tabIndex === index;
          tab.setAttribute("aria-selected", String(active));
          tab.tabIndex = active ? 0 : -1;
          panels.children[tabIndex].hidden = !active;
        });
      });
      nav.appendChild(button);
      panels.appendChild(panel);
    });
    card.insertBefore(nav, card.firstChild);
    card.appendChild(panels);
  });

})();
