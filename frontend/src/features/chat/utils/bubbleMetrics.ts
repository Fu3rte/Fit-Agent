import {
  layout,
  prepareWithSegments,
  walkLineRanges,
} from "@chenglou/pretext";
import type { PreparedTextWithSegments } from "@chenglou/pretext";
import { BUBBLE, bubbleContentMaxWidth } from "./bubbleContract";

export interface WrapMetrics {
  lineCount: number;
  height: number;
  maxLineWidth: number;
}

/** 文本 → 已测量句柄；同一文本复用同一次 prepare */
const preparedCache = new Map<string, PreparedTextWithSegments>();

export function prepareBubbleText(text: string): PreparedTextWithSegments {
  const cached = preparedCache.get(text);
  if (cached !== undefined) return cached;
  const prepared = prepareWithSegments(text, BUBBLE.font, {
    whiteSpace: BUBBLE.whiteSpace,
    letterSpacing: BUBBLE.letterSpacing,
  });
  preparedCache.set(text, prepared);
  return prepared;
}

/** webfont 晚到后必须丢弃按 fallback 字体量出的结果 */
export function clearPreparedBubbleTexts(): void {
  preparedCache.clear();
}

/** CSS 等价度量：一次行走取行数与最宽行 */
export function collectWrapMetrics(
  prepared: PreparedTextWithSegments,
  maxWidth: number,
): WrapMetrics {
  let maxLineWidth = 0;
  const lineCount = walkLineRanges(prepared, maxWidth, (line) => {
    if (line.width > maxLineWidth) maxLineWidth = line.width;
  });
  return { lineCount, height: lineCount * BUBBLE.lineHeight, maxLineWidth };
}

/** 最紧度量：二分出「行数不增加」的最窄宽度 */
export function findTightWrapMetrics(
  prepared: PreparedTextWithSegments,
  maxWidth: number,
): WrapMetrics {
  const initial = collectWrapMetrics(prepared, maxWidth);
  let lo = 1;
  let hi = Math.max(1, Math.ceil(maxWidth));
  while (lo < hi) {
    const mid = Math.floor((lo + hi) / 2);
    if (layout(prepared, mid, BUBBLE.lineHeight).lineCount <= initial.lineCount)
      hi = mid;
    else lo = mid + 1;
  }
  return collectWrapMetrics(prepared, lo);
}

/** 单条气泡外宽：最紧内容宽 + 左右内边距 */
export function bubbleWidth(
  prepared: PreparedTextWithSegments,
  bubbleMaxWidth: number,
): number {
  const contentMaxWidth = bubbleContentMaxWidth(bubbleMaxWidth);
  if (contentMaxWidth <= 0) return bubbleMaxWidth;
  const tight = findTightWrapMetrics(prepared, contentMaxWidth);
  return Math.min(
    bubbleMaxWidth,
    Math.ceil(tight.maxLineWidth) + BUBBLE.paddingX * 2,
  );
}
