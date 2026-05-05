import { useCoAgent } from "@copilotkit/react-core";
import type { MergeRequest } from "./merge_request";

export type DaivState = { merge_request: MergeRequest | null };

export function useDaivState() {
  return useCoAgent<DaivState>({ name: "DAIV", initialState: { merge_request: null } });
}
