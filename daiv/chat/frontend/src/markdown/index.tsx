import ReactMarkdown from "react-markdown";
import { Prism as SyntaxHighlighter } from "react-syntax-highlighter";
import { oneDark } from "react-syntax-highlighter/dist/esm/styles/prism";
import type { Components } from "react-markdown";

const components: Components = {
  code({ className, children, ...props }) {
    const match = /language-(\w+)/.exec(className ?? "");
    const isBlock = match != null;
    if (!isBlock) return <code className={className} {...props}>{children}</code>;
    return (
      <SyntaxHighlighter language={match[1]} style={oneDark} PreTag="div">
        {String(children).replace(/\n$/, "")}
      </SyntaxHighlighter>
    );
  },
};

export function Markdown({ source }: { source: string }) {
  return <ReactMarkdown components={components}>{source}</ReactMarkdown>;
}
