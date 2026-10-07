import type { ReactNode } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { stabilizeStreamingTables } from "../utils/streamingMarkdown";

/** 表格自带滚动容器：窄屏横向滚动，段落仍按气泡宽度换行 */
function MarkdownTable({ children }: { children?: ReactNode }) {
  return (
    <div className="my-2 w-full overflow-x-auto">
      <table className="w-full border-collapse text-sm">{children}</table>
    </div>
  );
}

export default function MarkdownContent({
  text,
  streaming,
}: {
  text: string;
  streaming: boolean;
}) {
  return (
    <div className="min-w-0 max-w-full [&_p]:my-1 [&_ul]:list-disc [&_ol]:list-decimal [&_ul]:pl-5 [&_ol]:pl-5 [&_pre]:overflow-x-auto [&_pre]:rounded [&_pre]:bg-muted [&_pre]:p-3 [&_a]:underline">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          table: MarkdownTable,
          thead: ({ children }) => (
            <thead className="border-b border-border">{children}</thead>
          ),
          tr: ({ children }) => (
            <tr className="border-b border-border last:border-0">{children}</tr>
          ),
          th: ({ children }) => (
            <th className="px-2 py-1 text-left font-medium">{children}</th>
          ),
          td: ({ children }) => (
            <td className="px-2 py-1 align-top">{children}</td>
          ),
        }}
      >
        {streaming ? stabilizeStreamingTables(text) : text}
      </ReactMarkdown>
    </div>
  );
}
