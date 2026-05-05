import { jsx as _jsx } from "react/jsx-runtime";
import ReactMarkdown from "react-markdown";
import { Prism as SyntaxHighlighter } from "react-syntax-highlighter";
import { oneDark } from "react-syntax-highlighter/dist/esm/styles/prism";
const components = {
    code({ className, children, ...props }) {
        const match = /language-(\w+)/.exec(className ?? "");
        const isBlock = match != null;
        if (!isBlock)
            return _jsx("code", { className: className, ...props, children: children });
        return (_jsx(SyntaxHighlighter, { language: match[1], style: oneDark, PreTag: "div", children: String(children).replace(/\n$/, "") }));
    },
};
export function Markdown({ source }) {
    return _jsx(ReactMarkdown, { components: components, children: source });
}
