/**
 * 气泡最紧宽度的可执行验证（Node 原生 ＋ 项目自带 Vite，无第三方测试框架）。
 *
 * 只验证 ``bubbleMetrics.ts`` 这一层：正文句子对 Pretext 是不透明的，Node 里没有 canvas，
 * 因此装一个确定性的 canvas 后端（pretext 自带 ``src/layout.test.ts`` 同款做法），
 * 再经 ``server.ssrLoadModule`` 载入真实源码——断言的是**二分算法的不变量**，
 * 不是真实字体下的像素值（那部分由浏览器验收）。
 *
 * 断言失败即进程非零退出。
 */
import assert from "node:assert/strict";
import { createServer } from "vite";

const WIDE = /[\u3000-\u9FFF\uAC00-\uD7AF\uFF00-\uFFEF]/u;

/** 确定性量宽：CJK／全角 = 一个字号，空格 = 0.33，其余 = 0.55 */
function measureWidth(text, font) {
  const fontSize = Number.parseFloat(/(\d+(?:\.\d+)?)\s*px/.exec(font)[1]);
  let width = 0;
  for (const character of text) {
    if (character === " ") width += fontSize * 0.33;
    else if (WIDE.test(character)) width += fontSize;
    else width += fontSize * 0.55;
  }
  return width;
}

class DeterministicContext {
  font = "";
  letterSpacing = "0px";
  measureText(text) {
    return { width: measureWidth(text, this.font) };
  }
}

Reflect.set(
  globalThis,
  "OffscreenCanvas",
  class {
    getContext() {
      return new DeterministicContext();
    }
  },
);

const server = await createServer({
  appType: "custom",
  logLevel: "silent",
  server: { middlewareMode: true },
});

try {
  const contract = await server.ssrLoadModule(
    "/src/features/chat/bubbleContract.ts",
  );
  const metrics = await server.ssrLoadModule(
    "/src/features/chat/bubbleMetrics.ts",
  );
  const { BUBBLE, bubbleContentMaxWidth, bubbleMaxWidthFor } = contract;
  const {
    bubbleWidth,
    clearPreparedBubbleTexts,
    collectWrapMetrics,
    findTightWrapMetrics,
    prepareBubbleText,
  } = metrics;

  /** 消息列 705px 下的气泡外宽上限（浏览器实测口径） */
  const bubbleMaxWidth = bubbleMaxWidthFor(705);
  const contentMaxWidth = bubbleContentMaxWidth(bubbleMaxWidth);
  assert.equal(bubbleMaxWidth, 599, "几何：705 * 0.85 向下取整");
  assert.equal(contentMaxWidth, 599 - BUBBLE.paddingX * 2, "几何：内容宽扣除内边距");

  /** 一条文本的全部不变量 */
  function checkText(label, text) {
    const prepared = prepareBubbleText(text);
    const contentWidth = bubbleContentMaxWidth(bubbleMaxWidth);
    const cssMetrics = collectWrapMetrics(prepared, contentWidth);
    const tightMetrics = findTightWrapMetrics(prepared, contentWidth);
    const width = bubbleWidth(prepared, bubbleMaxWidth);
    const tightContentWidth = width - BUBBLE.paddingX * 2;

    // 1. 行数不增加：最紧宽度必须还放得下同样的行数
    assert.equal(
      tightMetrics.lineCount,
      cssMetrics.lineCount,
      `${label}：最紧宽度不得多出一行`,
    );
    // 2. 最紧宽度不超过 CSS 等价宽度（外宽取整最多多 1px）
    const cssWidth = Math.ceil(cssMetrics.maxLineWidth) + BUBBLE.paddingX * 2;
    assert.ok(
      width <= cssWidth + 1,
      `${label}：最紧宽度 ${width} 超出 CSS 等价宽度 ${cssWidth}`,
    );
    // 3. 最小性：再窄 1px 就多一行
    const narrower = collectWrapMetrics(
      prepared,
      tightContentWidth - 1,
    ).lineCount;
    assert.ok(
      narrower > tightMetrics.lineCount,
      `${label}：再窄 1px 仍是 ${narrower} 行，说明还能更紧`,
    );
    // 4. 上限：不超出气泡外宽
    assert.ok(
      width <= bubbleMaxWidth,
      `${label}：最紧宽度 ${width} 超出上限 ${bubbleMaxWidth}`,
    );
    return { width, cssWidth, lineCount: tightMetrics.lineCount };
  }

  const single = checkText("单行短句", "今天休息");
  assert.equal(single.lineCount, 1, "单行短句应当只有一行");
  assert.ok(
    single.width < bubbleMaxWidth,
    "单行短句不应占满气泡宽度上限",
  );

  const cjk = checkText(
    "长 CJK",
    "记录今天杠铃卧推60kg四组五次，还做了哑铃飞鸟15kg三组十二次，以及绳索下压和杠铃弯举各三组",
  );
  assert.ok(cjk.lineCount > 1, "长 CJK 应当折行");
  assert.ok(
    cjk.width < cjk.cssWidth,
    `长 CJK 应当比 CSS 等价宽度更紧：${cjk.width} vs ${cjk.cssWidth}`,
  );

  const latin = checkText(
    "长拉丁",
    "did 5 sets of barbell bench press with 60kg then 3 sets of dumbbell flyes at 15kg plus cable pushdowns and barbell curls",
  );
  assert.ok(latin.lineCount > 1, "长拉丁应当折行");

  // 硬换行：换行符在 pre-wrap 下是硬行，两行各占一行，最紧宽度由最长的硬行决定
  const hardBreak = checkText("硬换行", "第一行\n第二行");
  assert.equal(hardBreak.lineCount, 2, "硬换行的每一行各占一行");
  assert.ok(
    hardBreak.width < contentMaxWidth,
    "硬换行的最紧宽度由最长硬行决定，不拉满内容宽",
  );

  // 硬换行 + 折行：不变量在混合路径下同样成立
  checkText(
    "硬换行 + 折行",
    "第一行很短\n第二行稍微长一点点，带一个硬换行并且再往后拖一段以便折行",
  );

  // 缓存不改变结果：清掉已 prepare 的文本后重算必须一致
  const repeated = prepareBubbleText("今天休息");
  clearPreparedBubbleTexts();
  assert.equal(
    bubbleWidth(repeated, bubbleMaxWidth),
    bubbleWidth(prepareBubbleText("今天休息"), bubbleMaxWidth),
    "清缓存后重算宽度必须一致",
  );

  // 无内容宽可用时退回上限
  assert.equal(
    bubbleWidth(prepareBubbleText("今天休息"), BUBBLE.paddingX * 2),
    BUBBLE.paddingX * 2,
    "内容宽 <= 0 时返回气泡外宽上限",
  );

  console.log(
    `气泡最紧宽度验证通过：705px 消息列 / 599px 上限 · 单行 ${single.width}px · ` +
      `长 CJK ${cjk.lineCount} 行 ${cjk.width}px（CSS 599px）· 长拉丁 ${latin.lineCount} 行 ${latin.width}px · ` +
      `硬换行 ${hardBreak.lineCount} 行 ${hardBreak.width}px`,
  );
} finally {
  await server.close();
}
