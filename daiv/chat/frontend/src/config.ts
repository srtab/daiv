export type MountConfig = {
  threadId: string;
  repoId: string;
  ref: string;
  csrf: string;
};

const REQUIRED = ["thread-id", "repo-id", "ref", "csrf"] as const;

export function readMountConfig(): MountConfig {
  const el = document.getElementById("copilot-root");
  if (!el) throw new Error("copilot-root mount node not found");
  for (const key of REQUIRED) {
    if (!el.dataset[toCamel(key)]) {
      throw new Error(`copilot-root missing required attribute: data-${key}`);
    }
  }
  return {
    threadId: el.dataset.threadId!,
    repoId: el.dataset.repoId!,
    ref: el.dataset.ref!,
    csrf: el.dataset.csrf!,
  };
}

function toCamel(kebab: string): string {
  return kebab.replace(/-([a-z])/g, (_, c: string) => c.toUpperCase());
}
