/**
 * 帧层（对齐 pretext pages/demos/bubbles.ts）：事件只排帧，一帧里读一次宽度 → 计算 → 写 DOM。
 *
 * - 只写 maxWidth 与 width 两个属性，几何以外不碰 DOM；
 * - resize 只重跑算术（prepare 结果按文本缓存，不重跑）；
 * - webfont 晚到：clearCache() 后按新字体重新 prepare，再同步重画一次；
 * - 首次同步画一遍（bubbles.html 的 parse-time paint），首帧不会以未就位的变量渲染。
 */
import { clearCache } from "@chenglou/pretext";
import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useState,
  type RefObject,
} from "react";
import { paintBubbleGeometry } from "./bubbleContract";
import {
  bubbleWidth,
  clearPreparedBubbleTexts,
  prepareBubbleText,
} from "./bubbleMetrics";

export function useBubbleShrinkwrap(
  texts: readonly string[],
  laneRef: RefObject<HTMLElement | null>,
  nodesRef: RefObject<(HTMLElement | null)[]>,
): void {
  /** 字体就位代次：晚到的 webfont 让已量出的宽度作废，+1 触发重新 prepare */
  const [fontEpoch, setFontEpoch] = useState(0);
  const prepared = useMemo(
    () => texts.map(prepareBubbleText),
    [texts, fontEpoch],
  );

  /** 一帧的全部工作：读一次列宽 → 写契约变量与几何 → 写每个气泡的 maxWidth / width */
  const render = useCallback(() => {
    const lane = laneRef.current;
    if (lane === null) return;
    const laneWidth = lane.clientWidth;
    if (laneWidth === 0) return;
    const bubbleMaxWidth = paintBubbleGeometry(laneWidth);
    const nodes = nodesRef.current;
    for (let index = 0; index < prepared.length; index++) {
      const node = nodes[index];
      if (node === null || node === undefined) continue;
      node.style.maxWidth = `${bubbleMaxWidth}px`;
      node.style.width = `${bubbleWidth(prepared[index]!, bubbleMaxWidth)}px`;
    }
  }, [laneRef, nodesRef, prepared]);

  useLayoutEffect(render, [render]);

  useEffect(() => {
    let scheduledFrame: number | null = null;
    const scheduleRender = () => {
      if (scheduledFrame !== null) return;
      scheduledFrame = requestAnimationFrame(() => {
        scheduledFrame = null;
        render();
      });
    };
    window.addEventListener("resize", scheduleRender);
    // 消息列自身的尺寸变化（侧栏折叠、主区留白）不走 resize 事件，直接观察它
    const laneObserver = new ResizeObserver(scheduleRender);
    const lane = laneRef.current;
    if (lane !== null) laneObserver.observe(lane);
    return () => {
      if (scheduledFrame !== null) cancelAnimationFrame(scheduledFrame);
      window.removeEventListener("resize", scheduleRender);
      laneObserver.disconnect();
    };
  }, [laneRef, render]);

  /**
   * 字体就位后重测一次：订阅只装一次（`[]`），否则 `setFontEpoch` 换掉 `render`
   * 会重新注册 `document.fonts.ready.then(...)`，而已 resolve 的 promise 立即回调，
   * 形成每秒数千次 `clearCache()` ＋ 重测的自激循环。
   */
  useEffect(() => {
    const reprepare = () => {
      clearCache();
      clearPreparedBubbleTexts();
      setFontEpoch((epoch) => epoch + 1);
    };
    void document.fonts.ready.then(reprepare);
    document.fonts.addEventListener("loadingdone", reprepare);
    return () => document.fonts.removeEventListener("loadingdone", reprepare);
  }, []);
}
