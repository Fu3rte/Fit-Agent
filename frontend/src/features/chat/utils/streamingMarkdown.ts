const FENCE = /^ {0,3}(`{3,}|~{3,})/;
const UNESCAPED_PIPE = /(?<!\\)\|/;
const DELIMITER_BODY = /^[\s:|-]*-[\s:|-]*$/;

function isPipeRow(line: string): boolean {
  return UNESCAPED_PIPE.test(line);
}

function isDelimiterRow(line: string): boolean {
  return DELIMITER_BODY.test(line.trim());
}

/** 行内单元格，忽略首尾管道 */
function cells(line: string): string[] {
  let body = line.trim();
  if (body.startsWith("|")) body = body.slice(1);
  if (body.endsWith("|")) body = body.slice(0, -1);
  return body.split(UNESCAPED_PIPE).map((cell) => cell.trim());
}

function delimiterRow(columns: number): string {
  return `| ${Array.from({ length: columns }, () => "---").join(" | ")} |`;
}

/** 该行之前是否仍有未闭合的代码围栏 */
function insideFence(lines: string[], index: number): boolean {
  let fence: string | null = null;
  for (let line = 0; line < index; line++) {
    const match = FENCE.exec(lines[line] ?? "");
    if (match === null) continue;
    const marker = match[1]!;
    if (fence === null) fence = marker;
    else if (marker[0] === fence[0] && marker.length >= fence.length)
      fence = null;
  }
  return fence !== null;
}

/**
 * GFM 只在分隔行完整时认定表格，表头先到会被渲染成段落，分隔行补齐后整块突变为表格。
 * 流式期间补全分隔行并按表头列数补齐在途末行，使表格从第一帧起就是表格并逐行生长。
 * 仅作用于文本末尾的表格区；终态渲染走原始文本。
 */
export function stabilizeStreamingTables(markdown: string): string {
  if (!markdown.includes("|")) return markdown;
  const lines = markdown.split("\n");
  const end = lines.at(-1) === "" ? lines.length - 1 : lines.length;
  let start = end;
  while (start > 0 && isPipeRow(lines[start - 1]!)) start--;

  const header = lines[start];
  const delimiter = lines[start + 1];
  if (start === end || header === undefined || delimiter === undefined)
    return markdown;
  if (isDelimiterRow(header) || !isDelimiterRow(delimiter)) return markdown;
  if (insideFence(lines, start)) return markdown;

  const columns = cells(header).length;
  if (columns === 0) return markdown;
  lines[start + 1] = delimiterRow(columns);

  const lastIndex = end - 1;
  if (lastIndex > start + 1) {
    const row = cells(lines[lastIndex]!);
    if (row.length < columns) {
      while (row.length < columns) row.push("");
      lines[lastIndex] = `| ${row.join(" | ")} |`;
    }
  }
  return lines.join("\n");
}
