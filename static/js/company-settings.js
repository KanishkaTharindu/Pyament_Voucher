(() => {
  const palettes = {
    forest: ["#254C3B", "#19382C", "#6C9B72"],
    ocean: ["#176B87", "#10465A", "#53A9B7"],
    clay: ["#9A5034", "#63341F", "#D18A55"],
    slate: ["#46566B", "#2B3543", "#6F91B3"],
  };
  const theme = document.getElementById("id_theme_name");
  const primary = document.getElementById("id_primary_color");
  const secondary = document.getElementById("id_secondary_color");
  const accent = document.getElementById("id_accent_color");
  const preview = document.getElementById("brand-preview");
  const company = document.getElementById("id_company_name");
  const currency = document.getElementById("id_currency_code");
  const subtitle = document.getElementById("id_header_text");
  const previewCompany = document.getElementById("preview-company");
  const previewCurrency = document.getElementById("preview-currency");
  const previewSubtitle = document.getElementById("preview-subtitle");
  if (!theme || !primary || !secondary || !accent || !preview) return;

  const updatePreview = () => {
    preview.style.setProperty("--preview-primary", primary.value);
    preview.style.setProperty("--preview-secondary", secondary.value);
    preview.style.setProperty("--preview-accent", accent.value);
    document.querySelectorAll("[data-color-value]").forEach((label) => {
      const input = document.getElementById(`id_${label.dataset.colorValue}`);
      if (input) label.textContent = input.value.toUpperCase();
    });
    if (previewCompany && company) previewCompany.textContent = company.value;
    if (previewCurrency && currency) {
      previewCurrency.textContent = `${currency.options[currency.selectedIndex].text.split(" (")[1]?.replace(/\)$/, "") || currency.value} 1,250.00`;
    }
    if (previewSubtitle && subtitle) {
      previewSubtitle.textContent = subtitle.value || "Payment voucher";
    }
  };

  theme.addEventListener("change", () => {
    const palette = palettes[theme.value];
    if (palette) {
      [primary, secondary, accent].forEach((input, index) => {
        input.value = palette[index];
      });
    }
    updatePreview();
  });
  [primary, secondary, accent, company, subtitle, currency].forEach((input) => {
    if (input) input.addEventListener("input", updatePreview);
  });
  [primary, secondary, accent].forEach((input) => {
    input.addEventListener("input", () => {
      const palette = palettes[theme.value];
      const index = [primary, secondary, accent].indexOf(input);
      if (palette && palette[index] !== input.value.toUpperCase()) {
        theme.value = "custom";
      }
    });
  });
  updatePreview();
})();
