// The theme is one attribute on <html> and one key in local storage.
//
// The initial value is applied by an inline script in index.html, before the
// first paint, so this module only has to read what is already there and keep
// it in step when the operator flips the switch.

const KEY = "tf-theme";

export function currentTheme() {
  const set = document.documentElement.dataset.theme;
  if (set === "dark" || set === "light") return set;
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

export function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  try {
    localStorage.setItem(KEY, theme);
  } catch {
    // A blocked storage backend is not a reason to refuse to change theme.
  }
}

export function toggleTheme(theme) {
  const next = theme === "dark" ? "light" : "dark";
  applyTheme(next);
  return next;
}

/// Follow the system while the operator has not made a choice of their own.
export function followSystem(onChange) {
  const query = window.matchMedia("(prefers-color-scheme: dark)");
  const listener = (event) => {
    try {
      if (localStorage.getItem(KEY)) return;
    } catch {
      return;
    }
    onChange(event.matches ? "dark" : "light");
  };
  query.addEventListener("change", listener);
  return () => query.removeEventListener("change", listener);
}
