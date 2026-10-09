import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

/** 业务日期 ``YYYY-MM-DD`` → 本地 Date：带时间部分按本地时区解析，避免纯日期串按 UTC 解析挪一天 */
export function dateFromIso(iso: string): Date {
  return new Date(`${iso}T00:00:00`);
}

export function isoFromDate(date: Date): string {
  return date.toLocaleDateString("en-CA");
}

const TIMESTAMP_FORMAT = new Intl.DateTimeFormat("sv-SE", {
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
});

/** UTC 毫秒时间戳 → 本地可读时间（协议 §1：时间戳一律为 UTC 毫秒整数） */
export function formatTimestamp(millis: number): string {
  return TIMESTAMP_FORMAT.format(new Date(millis));
}
