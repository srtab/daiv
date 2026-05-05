import { render, screen } from "@testing-library/react";
import { describe, it, expect } from "vitest";
import ReactMarkdown from "react-markdown";
import { markdownComponents } from "../markdown";

function Md({ source }: { source: string }) {
  return <ReactMarkdown components={markdownComponents}>{source}</ReactMarkdown>;
}

describe("markdownComponents", () => {
  it("renders bold and links via default react-markdown", () => {
    render(<Md source="**hi** [there](https://example)" />);
    expect(screen.getByText("hi").tagName).toBe("STRONG");
    expect(screen.getByRole("link")).toHaveAttribute("href", "https://example");
  });
  it("renders fenced code through the syntax-highlight wrapper", () => {
    render(<Md source={"```python\nprint('hi')\n```"} />);
    expect(screen.getByText(/print/)).toBeInTheDocument();
  });
  it("leaves inline code alone", () => {
    render(<Md source={"some `inline` code"} />);
    expect(screen.getByText("inline").tagName).toBe("CODE");
  });
});
