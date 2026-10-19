// Apply the saved theme before first paint to avoid a flash (key shared with src/lib/theme.ts).
try {
  var t = localStorage.getItem("atlas-theme") || "system";
  var dark = t === "dark" || (t === "system" && matchMedia("(prefers-color-scheme: dark)").matches);
  document.documentElement.classList.toggle("dark", dark);
} catch (e) {}
