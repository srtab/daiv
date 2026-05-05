import type { MergeRequest } from "./state/merge_request";

export function MergeRequestCard({ mr }: { mr: MergeRequest | null }) {
  if (!mr) return null;
  return (
    <aside className="chat-mr-card" data-state={mr.draft ? "draft" : "opened"}>
      <a href={mr.web_url}>{mr.title}</a>
    </aside>
  );
}
