(() => {
  let theme = "dark";
  try {
    if (localStorage.getItem("openperpdesk.theme") === "light") theme = "light";
  } catch {
    // Appearance remains usable when browser storage is unavailable.
  }
  document.documentElement.dataset.theme = theme;
})();
