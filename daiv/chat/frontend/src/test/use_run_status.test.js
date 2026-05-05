import { renderHook, waitFor } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { useRunStatus } from "../use_run_status";
describe("useRunStatus", () => {
    beforeEach(() => {
        globalThis.fetch = vi.fn();
    });
    it("returns active=true when endpoint reports active", async () => {
        globalThis.fetch.mockResolvedValue({
            ok: true,
            json: async () => ({ active: true }),
        });
        const { result } = renderHook(() => useRunStatus("/api/chat/threads/t1/status", { intervalMs: 50 }));
        await waitFor(() => expect(result.current.active).toBe(true));
    });
    it("flips to false when next poll reports inactive", async () => {
        let call = 0;
        globalThis.fetch.mockImplementation(async () => ({
            ok: true,
            json: async () => ({ active: ++call < 2 }),
        }));
        const { result } = renderHook(() => useRunStatus("/api/chat/threads/t1/status", { intervalMs: 20 }));
        await waitFor(() => expect(result.current.active).toBe(false), { timeout: 200 });
    });
});
