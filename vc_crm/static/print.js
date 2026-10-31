// Print button on statement and notice pages (inline scripts are blocked by the CSP).
document.querySelector("[data-print]")?.addEventListener("click", () => window.print());
