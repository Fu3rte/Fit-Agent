/**
 * 主题切换（C3B）：浅色默认，暗色第二主题。
 * localStorage 键 fitagent-theme；class 策略挂在 documentElement 上，
 * 配合 index.css 的 @custom-variant dark。
 */
export type Theme = "light" | "dark";

const STORAGE_KEY = "fitagent-theme";

export function getTheme(): Theme {
 const saved = localStorage.getItem(STORAGE_KEY);
 return saved === "dark" ? "dark" : "light";
}

/** 模块加载时执行一次，避免首帧闪烁 */
export function initTheme(): void {
 document.documentElement.classList.toggle("dark", getTheme() === "dark");
}

export function setTheme(theme: Theme): void {
 localStorage.setItem(STORAGE_KEY, theme);
 document.documentElement.classList.toggle("dark", theme === "dark");
}

export function toggleTheme(): Theme {
 const next: Theme = getTheme() === "dark" ? "light" : "dark";
 setTheme(next);
 return next;
}

initTheme();
