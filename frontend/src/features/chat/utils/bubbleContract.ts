export const BUBBLE = {
  /** canvas font 简写；首选项与 index.css 的 --font-sans 一致 */
  font: '400 14px "Noto Sans SC"',
  lineHeight: 20,
  paddingX: 16,
  paddingY: 12,
  /** index.css 里 body 的 letter-spacing */
  letterSpacing: 0.15,
  /** 与 .bubble 的 white-space 同值，也用作 prepare 的 whiteSpace 选项 */
  whiteSpace: "pre-wrap",
} as const;

/** 气泡外宽占消息列宽的比例 */
const BUBBLE_MAX_RATIO = 0.85;

/** 几何：消息列宽 → 气泡外宽上限 */
export function bubbleMaxWidthFor(laneWidth: number): number {
  return Math.floor(laneWidth * BUBBLE_MAX_RATIO);
}

/** 内容区上限：气泡外宽减去左右内边距 */
export function bubbleContentMaxWidth(bubbleMaxWidth: number): number {
  return bubbleMaxWidth - BUBBLE.paddingX * 2;
}

/** 把契约值与几何写进根 CSS 变量，返回气泡外宽上限 */
export function paintBubbleGeometry(laneWidth: number): number {
  const bubbleMaxWidth = bubbleMaxWidthFor(laneWidth);
  const root = document.documentElement.style;
  root.setProperty("--bubble-font", BUBBLE.font);
  root.setProperty("--bubble-line-height", `${BUBBLE.lineHeight}px`);
  root.setProperty("--bubble-letter-spacing", `${BUBBLE.letterSpacing}px`);
  root.setProperty(
    "--bubble-padding",
    `${BUBBLE.paddingY}px ${BUBBLE.paddingX}px`,
  );
  root.setProperty("--bubble-white-space", BUBBLE.whiteSpace);
  root.setProperty("--bubble-max-width", `${bubbleMaxWidth}px`);
  return bubbleMaxWidth;
}
