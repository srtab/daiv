import { renderHook, waitFor } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { useRunStatus } from "../use_run_status";

describe("useRunStatus", () => {
  let errSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    globalThis.fetch = vi.fn();
    errSpy = vi.spyOn(console, "error").mockImplementation(() => {});
  });

  afterEach(() => {
    errSpy.mockRestore();
  });

  it("returns active=true when endpoint reports active", async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: true,
      json: async () => ({ active: true }),
    });
    const { result } = renderHook(() => useRunStatus("/api/chat/threads/t1/status", { intervalMs: 50 }));
    await waitFor(() => expect(result.current.active).toBe(true));
  });

  it("flips to false when next poll reports inactive", async () => {
    let call = 0;
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockImplementation(async () => ({
      ok: true,
      json: async () => ({ active: ++call < 2 }),
    }));
    const { result } = renderHook(() => useRunStatus("/api/chat/threads/t1/status", { intervalMs: 20 }));
    await waitFor(() => expect(result.current.active).toBe(false), { timeout: 200 });
  });

  it("clears the interval on unmount", async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: true,
      json: async () => ({ active: false }),
    });
    const clearSpy = vi.spyOn(globalThis, "clearInterval");
    const { unmount } = renderHook(() => useRunStatus("/api/chat/threads/t1/status", { intervalMs: 50 }));
    unmount();
    expect(clearSpy).toHaveBeenCalled();
    clearSpy.mockRestore();
  });

  it("logs and skips state update on a non-ok response", async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: false,
      status: 500,
      json: async () => ({}),
    });
    const { result } = renderHook(() => useRunStatus("/api/chat/threads/t1/status", { intervalMs: 50 }));
    await new Promise((r) => setTimeout(r, 60));
    expect(result.current.active).toBe(false);
    expect(errSpy).toHaveBeenCalled();
  });

  it("stops polling on 401/403/404", async () => {
    const fetchMock = globalThis.fetch as ReturnType<typeof vi.fn>;
    fetchMock.mockResolvedValue({ ok: false, status: 401, json: async () => ({}) });
    renderHook(() => useRunStatus("/api/chat/threads/t1/status", { intervalMs: 30 }));
    await new Promise((r) => setTimeout(r, 100));
    const callsAfterStop = fetchMock.mock.calls.length;
    await new Promise((r) => setTimeout(r, 100));
    expect(fetchMock.mock.calls.length).toBe(callsAfterStop);
  });

  it("ignores rejected fetches without crashing", async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockRejectedValue(new Error("network down"));
    const { result } = renderHook(() => useRunStatus("/api/chat/threads/t1/status", { intervalMs: 50 }));
    await new Promise((r) => setTimeout(r, 60));
    expect(result.current.active).toBe(false);
    expect(errSpy).toHaveBeenCalled();
  });

  it("does not log AbortError when unmounted mid-flight", async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockImplementation(
      (_url: string, init: RequestInit) =>
        new Promise((_resolve, reject) => {
          init.signal?.addEventListener("abort", () => {
            const err = new Error("aborted");
            err.name = "AbortError";
            reject(err);
          });
        }),
    );
    const { unmount } = renderHook(() => useRunStatus("/api/chat/threads/t1/status", { intervalMs: 1000 }));
    unmount();
    await new Promise((r) => setTimeout(r, 30));
    const aborts = errSpy.mock.calls.filter((c) => /poll failed/.test(String(c[0])));
    expect(aborts).toHaveLength(0);
  });
});
