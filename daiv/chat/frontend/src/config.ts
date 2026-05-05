export type MountConfig = {
  threadId: string;
  repoId: string;
  ref: string;
  csrfToken: string;
};

const REQUIRED = ["thread-id", "repo-id", "ref"] as const;

export function readMountConfig(): MountConfig {
  const el = document.getElementById("copilot-root");
  if (!el) throw new Error("copilot-root mount node not found");
  const missing: string[] = [];
  for (const key of REQUIRED) {
    if (!el.dataset[toCamel(key)]) missing.push(`data-${key}`);
  }
  if (missing.length) {
    const msg = `copilot-root missing required attribute(s): ${missing.join(", ")}`;
    console.error(msg);
    throw new Error(msg);
  }
  return {
    threadId: el.dataset.threadId!,
    repoId: el.dataset.repoId!,
    ref: el.dataset.ref!,
    csrfToken: el.dataset.csrfToken ?? readCsrfTokenFromCookie(),
  };
}

export function readCsrfToken(): string {
  const el = document.getElementById("copilot-root");
  return el?.dataset.csrfToken ?? readCsrfTokenFromCookie();
}

function readCsrfTokenFromCookie(): string {
  const match = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
  return match ? decodeURIComponent(match[1]!) : "";
}

function toCamel(kebab: string): string {
  return kebab.replace(/-([a-z])/g, (_, c: string) => c.toUpperCase());
}
