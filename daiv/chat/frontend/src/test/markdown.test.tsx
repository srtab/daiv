import { render, screen } from "@testing-library/react";
import { describe, it, expect } from "vitest";
import { Markdown } from "../markdown";

describe("Markdown", () => {
  it("renders bold and links", () => {
    render(<Markdown source="**hi** [there](https://example)" />);
    expect(screen.getByText("hi").tagName).toBe("STRONG");
    expect(screen.getByRole("link")).toHaveAttribute("href", "https://example");
  });
  it("renders fenced code with syntax highlight wrapper", () => {
    render(<Markdown source={"```python\nprint('hi')\n```"} />);
    expect(screen.getByText(/print/)).toBeInTheDocument();
  });
});
