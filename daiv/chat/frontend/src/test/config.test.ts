import { describe, it, expect, beforeEach, vi } from "vitest";
import { readCsrfToken, readMountConfig } from "../config";

describe("readMountConfig", () => {
  beforeEach(() => {
    document.body.innerHTML = "";
  });

  it("reads thread_id, repo_id, ref from data-* attributes", () => {
    document.body.innerHTML =
      '<div id="copilot-root" data-thread-id="t1" data-repo-id="r1" data-ref="main"></div>';
    const cfg = readMountConfig();
    expect(cfg).toEqual({ threadId: "t1", repoId: "r1", ref: "main" });
  });

  it("throws when mount node missing", () => {
    expect(() => readMountConfig()).toThrow(/copilot-root/);
  });

  it("throws and logs when any required data-* attr missing", () => {
    document.body.innerHTML = '<div id="copilot-root" data-thread-id="t1"></div>';
    const err = vi.spyOn(console, "error").mockImplementation(() => {});
    expect(() => readMountConfig()).toThrow(/data-repo-id.*data-ref/);
    expect(err).toHaveBeenCalled();
    err.mockRestore();
  });
});

describe("readCsrfToken", () => {
  it("extracts csrftoken from document.cookie", () => {
    Object.defineProperty(document, "cookie", {
      configurable: true,
      get: () => "other=x; csrftoken=abc123; another=y",
    });
    expect(readCsrfToken()).toBe("abc123");
  });

  it("returns empty string when cookie missing", () => {
    Object.defineProperty(document, "cookie", {
      configurable: true,
      get: () => "other=x",
    });
    expect(readCsrfToken()).toBe("");
  });
});
