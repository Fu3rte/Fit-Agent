/**
 * Agent 文本 Markdown 渲染（A3）：白名单元素 + remark-gfm 表格支持。
 * 不渲染原始 HTML（未启用 rehype-raw，且 allowedElements 之外的元素一律移除）。
 */
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";

const components: Components = {
  h1: (props) => (
    <h3
      className="mt-3 text-sm font-semibold tracking-tight first:mt-0"
      {...props}
    />
  ),
  h2: (props) => (
    <h3
      className="mt-3 text-sm font-semibold tracking-tight first:mt-0"
      {...props}
    />
  ),
  h3: (props) => (
    <h4 className="mt-2 text-sm font-semibold first:mt-0" {...props} />
  ),
  p: (props) => (
    <p className="my-1.5 leading-relaxed first:mt-0 last:mb-0" {...props} />
  ),
  ul: (props) => <ul className="my-1.5 list-disc space-y-1 pl-5" {...props} />,
  ol: (props) => (
    <ol className="my-1.5 list-decimal space-y-1 pl-5" {...props} />
  ),
  li: (props) => <li className="leading-relaxed" {...props} />,
  table: (props) => (
    <div className="my-2 overflow-x-auto">
      <table className="w-full border-collapse text-xs" {...props} />
    </div>
  ),
  thead: (props) => <thead className="text-left" {...props} />,
  th: (props) => (
    <th className="border-b border-border px-2 py-1 font-medium" {...props} />
  ),
  td: (props) => (
    <td className="border-b border-border/60 px-2 py-1 align-top" {...props} />
  ),
  strong: (props) => <strong className="font-semibold" {...props} />,
  em: (props) => <em {...props} />,
  code: (props) => (
    <code className="rounded bg-muted px-1 py-0.5 text-xs" {...props} />
  ),
  pre: (props) => (
    <pre
      className="my-2 overflow-x-auto rounded-lg bg-muted p-3 text-xs leading-relaxed"
      {...props}
    />
  ),
  blockquote: (props) => (
    <blockquote
      className="my-2 border-l-2 border-border pl-3 text-muted-foreground"
      {...props}
    />
  ),
};

/** A3 白名单：仅渲染以下元素，其余（含图片、链接、原始 HTML）一律不渲染 */
const ALLOWED_ELEMENTS = [
  "p",
  "h1",
  "h2",
  "h3",
  "ul",
  "ol",
  "li",
  "table",
  "thead",
  "tbody",
  "tr",
  "th",
  "td",
  "strong",
  "em",
  "code",
  "pre",
  "blockquote",
  "br",
];

export function Markdown({ text }: { text: string }) {
  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      allowedElements={ALLOWED_ELEMENTS}
      components={components}
    >
      {text}
    </ReactMarkdown>
  );
}
