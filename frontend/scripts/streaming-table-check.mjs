import assert from "node:assert/strict";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { createServer } from "vite";

const server = await createServer({
  appType: "custom",
  logLevel: "silent",
  server: { middlewareMode: true },
});

function render(markdown) {
  return renderToStaticMarkup(
    createElement(ReactMarkdown, { remarkPlugins: [remarkGfm] }, markdown),
  );
}

const PLAN = [
  "| 动作 | 组数 | 次数/时间 | 备注 |",
  "| --- | --- | --- | --- |",
  "| 引体向上（标准/正握） | 4组 | 6-12次 | 力竭停止 |",
  "| 反握引体向上 | 3组 | 6-10次 | 侧重二头肌 |",
  "",
].join("\n");

try {
  const { stabilizeStreamingTables } = await server.ssrLoadModule(
    "/src/features/chat/utils/streamingMarkdown.ts",
  );

  /** 1. 无管道与已完整的表格零改写 */
  assert.equal(stabilizeStreamingTables("普通段落文字"), "普通段落文字");
  assert.equal(stabilizeStreamingTables(PLAN), PLAN);

  /** 2. 仅表头时无法判定，保持原样 */
  const headerOnly = "| 动作 | 组数 | 次数/时间 | 备注 |\n";
  assert.equal(stabilizeStreamingTables(headerOnly), headerOnly);

  /** 3. 分隔行一开始流入就补全为表头列数 */
  const partialDelimiter = "| 动作 | 组数 | 次数/时间 | 备注 |\n|-";
  assert.equal(
    stabilizeStreamingTables(partialDelimiter),
    "| 动作 | 组数 | 次数/时间 | 备注 |\n| --- | --- | --- | --- |",
  );
  assert.match(render(stabilizeStreamingTables(partialDelimiter)), /<table/);

  /** 4. 逐前缀：分隔行出现首个 "-" 后必须是表格，行数单调增长，且无裸管道泄漏 */
  const delimiterStart = PLAN.indexOf("\n|");
  /** 分隔行仅到达 "|" 或 "| " 时语法上仍可能是数据行，此窗口为 3 字符 */
  assert.doesNotMatch(
    render(stabilizeStreamingTables(PLAN.slice(0, delimiterStart + 3))),
    /<table/,
  );
  let rows = 0;
  for (let length = delimiterStart + 4; length <= PLAN.length; length++) {
    const prefix = PLAN.slice(0, length);
    const html = render(stabilizeStreamingTables(prefix));
    assert.match(html, /<table/, `前缀未渲染为表格：${JSON.stringify(prefix)}`);
    assert.doesNotMatch(html, /\|/, `裸管道泄漏：${JSON.stringify(prefix)}`);
    const counted = (html.match(/<tr/g) ?? []).length;
    assert.ok(counted >= rows, `行数回退：${counted} < ${rows}`);
    rows = counted;
  }
  assert.equal(rows, 3, "表头一行 + 数据两行");

  /** 5. 在途末行补齐到表头列数，避免列宽抖动 */
  const partialRow = `${PLAN.split("\n").slice(0, 3).join("\n")}\n| 反握引体向上 | 3组`;
  const html = render(stabilizeStreamingTables(partialRow));
  const lastRowCells = (html.match(/<tr[^>]*>(?:(?!<\/tr>).)*<\/tr>/gs) ?? []).at(-1);
  assert.equal((lastRowCells.match(/<td/g) ?? []).length, 4);

  /** 6. 代码围栏内的管道行不被改写 */
  const fenced = "```text\n| a | b |\n|-";
  assert.equal(stabilizeStreamingTables(fenced), fenced);

  /** 7. 关闭 remark-gfm 时表格不成立，证明依赖已生效 */
  assert.doesNotMatch(
    renderToStaticMarkup(createElement(ReactMarkdown, {}, PLAN)),
    /<table/,
  );

  console.log("streaming table check passed");
} finally {
  await server.close();
}
