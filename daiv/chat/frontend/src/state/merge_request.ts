export type MergeRequest = {
  merge_request_id: number;
  web_url: string;
  title: string;
  draft?: boolean;
  source_branch?: string;
  target_branch?: string;
};
