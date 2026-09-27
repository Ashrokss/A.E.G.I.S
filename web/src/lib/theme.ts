import { useCallback, useEffect, useState } from "react";

// "system" follows the OS (prefers-color-scheme); "light" / "dark" pin it by
// setting data-theme on <html>, which src/index.css already honours. The same
// logic runs early in index.html so the first paint is already right.
export type ThemeChoice = "system" | "light" | "dark";

const KEY = "aegis.theme";

function read(): ThemeChoice {
  try {
    const v = localStorage.getItem(KEY);
    return v === "light" || v === "dark" ? v : "system";
  } catch {
    return "system";
  }
}

function apply(choice: ThemeChoice) {
  const root = document.documentElement;
  if (choice === "system") root.removeAttribute("data-theme");
  else root.setAttribute("data-theme", choice);
  const dark = choice === "dark" || (choice === "system" && matchMedia("(prefers-color-scheme: dark)").matches);
  document.querySelector('meta[name="theme-color"]')?.setAttribute("content", dark ? "#131313" : "#fafafa");
}

export function useTheme() {
  const [theme, setThemeState] = useState<ThemeChoice>(read);

  useEffect(() => {
    apply(theme);
    if (theme !== "system") return;
    // Keep the browser chrome colour in step when the OS switches.
    const mq = matchMedia("(prefers-color-scheme: dark)");
    const onChange = () => apply("system");
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, [theme]);

  const setTheme = useCallback((next: ThemeChoice) => {
    try {
      if (next === "system") localStorage.removeItem(KEY);
      else localStorage.setItem(KEY, next);
    } catch {
      /* private mode: still applies for this page */
    }
    setThemeState(next);
  }, []);

  return { theme, setTheme };
}
