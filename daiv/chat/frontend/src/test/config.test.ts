import { describe, it, expect, beforeEach } from "vitest";
import { readMountConfig } from "../config";

describe("readMountConfig", () => {
  beforeEach(() => {
    document.body.innerHTML = "";
  });

  it("reads thread_id, repo_id, ref, csrf from data-* attributes", () => {
    document.body.innerHTML =
      '<div id="copilot-root" data-thread-id="t1" data-repo-id="r1" data-ref="main" data-csrf="abc"></div>';
    const cfg = readMountConfig();
    expect(cfg).toEqual({ threadId: "t1", repoId: "r1", ref: "main", csrf: "abc" });
  });

  it("throws when mount node missing", () => {
    expect(() => readMountConfig()).toThrow(/copilot-root/);
  });

  it("throws when any required data-* attr missing", () => {
    document.body.innerHTML = '<div id="copilot-root" data-thread-id="t1"></div>';
    expect(() => readMountConfig()).toThrow(/data-repo-id/);
  });
});
