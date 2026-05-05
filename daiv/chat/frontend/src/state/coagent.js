import { useCoAgent } from "@copilotkit/react-core";
export function useDaivState() {
    return useCoAgent({ name: "DAIV", initialState: { merge_request: null } });
}
