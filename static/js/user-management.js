(() => {
  const search = document.getElementById("permission-search");
  if (search) {
    search.addEventListener("input", () => {
      const query = search.value.trim().toLowerCase();
      document.querySelectorAll(".permission-checklist label").forEach((label) => {
        label.closest("div").hidden = !label.textContent.toLowerCase().includes(query);
      });
    });
  }
  document.querySelectorAll("[data-select-all], [data-clear-all]").forEach((button) => {
    button.addEventListener("click", () => {
      const container = document.getElementById(
        button.dataset.selectAll || button.dataset.clearAll
      );
      if (!container) return;
      const checked = Boolean(button.dataset.selectAll);
      container.querySelectorAll('input[type="checkbox"]').forEach((input) => {
        if (!input.closest("div").hidden) input.checked = checked;
      });
    });
  });
})();
