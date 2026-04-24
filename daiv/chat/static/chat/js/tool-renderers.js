// Per-tool UI strategies. Two exports on window:
//
//   toolSignature(name, argsStr, result, status)
//     -> { label, path, badges: [{text, tone}] }
//
//   toolBodyHTML(name, argsStr, result, status)
//     -> HTML string rendered inside <details> when the card is expanded.
//
// Every extraction is defensive: if JSON doesn't parse or expected keys are missing,
// we return a neutral signature/body rather than throwing. Unknown tools fall
// through to a generic "name + JSON-pretty args + raw result" view.

(() => {
  const escapeHtml = (s) =>
    String(s ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;");

  const parseArgs = (argsStr) => {
    if (!argsStr) return {};
    try {
      const parsed = JSON.parse(argsStr);
      return parsed && typeof parsed === "object" ? parsed : {};
    } catch {
      return {};
    }
  };

  const prettyJSON = (argsStr) => {
    try {
      return JSON.stringify(JSON.parse(argsStr), null, 2);
    } catch {
      return String(argsStr ?? "");
    }
  };

  const pickKey = (obj, keys) => {
    for (const k of keys) {
      if (obj && Object.hasOwn(obj, k) && obj[k] != null) return obj[k];
    }
    return null;
  };

  const truncate = (s, n) => {
    const str = String(s ?? "");
    return str.length > n ? str.slice(0, n) + "…" : str;
  };

  const badge = (text, tone) => ({ text: String(text), tone });

  // --- per-tool signature extractors -------------------------------------

  const sigReadFile = (args) => {
    const path = pickKey(args, ["path", "file_path"]) ?? "";
    const start = args.line_start ?? args.start_line ?? args.offset;
    const end = args.line_end ?? args.end_line;
    const badges = [];
    if (start != null || end != null) {
      badges.push(badge(`${start ?? "?"}–${end ?? "?"}`, "neutral"));
    }
    return { label: "read_file", path, badges };
  };

  const countDiffLines = (patch) => {
    let plus = 0;
    let minus = 0;
    for (const line of String(patch || "").split("\n")) {
      if (line.startsWith("+") && !line.startsWith("+++")) plus += 1;
      else if (line.startsWith("-") && !line.startsWith("---")) minus += 1;
    }
    return { plus, minus };
  };

  const sigEditFile = (args, result) => {
    const path = pickKey(args, ["path", "file_path"]) ?? "";
    const badges = [];
    const isDiff = /^@@|^(---|\+\+\+)\s/m.test(String(result ?? ""));
    if (isDiff) {
      const { plus, minus } = countDiffLines(result);
      if (plus) badges.push(badge(`+${plus}`, "success"));
      if (minus) badges.push(badge(`−${minus}`, "danger"));
    }
    return { label: "edit_file", path, badges };
  };

  const sigWriteFile = (args, result) => {
    const path = pickKey(args, ["path", "file_path"]) ?? "";
    const content = pickKey(args, ["content", "text"]) ?? "";
    const lines = String(content).split("\n").length;
    const badges = [];
    if (content) badges.push(badge(`new · ${lines} lines`, "info"));
    return { label: "write_file", path, badges };
  };

  const sigGrep = (args, result) => {
    const pattern = pickKey(args, ["pattern", "query", "regex"]) ?? "";
    const path = pickKey(args, ["path", "scope", "include"]) ?? "";
    const hitLines = String(result ?? "")
      .split("\n")
      .filter((l) => l.trim().length && !l.startsWith("#"));
    const hits = hitLines.length;
    const badges = [];
    if (hits) badges.push(badge(`${hits} hits`, "warn"));
    return { label: "grep", path: `"${truncate(pattern, 60)}" in ${path}`, badges };
  };

  const sigGlob = (args, result) => {
    const pattern = pickKey(args, ["pattern", "glob", "path"]) ?? "";
    const count = String(result ?? "").split("\n").filter((l) => l.trim().length).length;
    const badges = count ? [badge(`${count} files`, "neutral")] : [];
    return { label: "glob", path: pattern, badges };
  };

  const sigLs = (args, result) => {
    const path = pickKey(args, ["path", "dir"]) ?? "";
    const count = String(result ?? "").split("\n").filter((l) => l.trim().length).length;
    const badges = count ? [badge(`${count} entries`, "neutral")] : [];
    return { label: "ls", path, badges };
  };

  const sigBash = (args, result) => {
    const command = pickKey(args, ["command", "cmd", "script"]) ?? "";
    const badges = [];
    const exitMatch = String(result ?? "").match(/exit(?: code)?[:=\s]+(-?\d+)/i);
    if (exitMatch) {
      const code = Number(exitMatch[1]);
      badges.push(badge(`exit ${code}`, code === 0 ? "success" : "danger"));
    }
    return { label: "bash_tool", path: `$ ${truncate(command, 120)}`, badges };
  };

  const sigWriteTodos = (args) => {
    const todos = Array.isArray(args.todos) ? args.todos : [];
    return {
      label: "write_todos",
      path: "plan updated",
      badges: [badge(`${todos.length} items`, "violet")],
    };
  };

  const SIGNATURE_BY_TOOL = {
    read_file: sigReadFile,
    write_file: sigWriteFile,
    edit_file: sigEditFile,
    grep: sigGrep,
    glob: sigGlob,
    ls: sigLs,
    bash_tool: sigBash,
    write_todos: sigWriteTodos,
  };

  window.toolSignature = (name, argsStr, result, _status) => {
    const args = parseArgs(argsStr);
    const fn = SIGNATURE_BY_TOOL[name];
    if (fn) return fn(args, result);
    return {
      label: String(name || "tool"),
      path: truncate(String(argsStr ?? "").replace(/\s+/g, " "), 84),
      badges: [],
    };
  };

  // --- per-tool expanded-body builders -----------------------------------

  const pre = (text, cls = "") =>
    `<pre class="chat-tool__code${cls ? " " + cls : ""}">${escapeHtml(text)}</pre>`;

  const block = (label, html) =>
    `<div class="chat-tool__block"><div class="chat-tool__block-label">${escapeHtml(label)}</div>${html}</div>`;

  const genericBody = (argsStr, result) => {
    const parts = [];
    if (argsStr) parts.push(block("Arguments", pre(prettyJSON(argsStr))));
    if (result != null && String(result).length) parts.push(block("Result", pre(String(result))));
    return parts.join("");
  };

  const diffBody = (result) => {
    const lines = String(result ?? "").split("\n");
    const html = lines
      .map((line) => {
        const esc = escapeHtml(line);
        if (line.startsWith("@@")) return `<div class="diff-hunk">${esc}</div>`;
        if (line.startsWith("+++") || line.startsWith("---")) return `<div class="diff-meta">${esc}</div>`;
        if (line.startsWith("+")) return `<div class="diff-add">${esc}</div>`;
        if (line.startsWith("-")) return `<div class="diff-del">${esc}</div>`;
        return `<div class="diff-ctx">${esc}</div>`;
      })
      .join("");
    return block("Diff", `<div class="chat-diff">${html}</div>`);
  };

  const grepBody = (result) => {
    const groups = new Map();
    for (const line of String(result ?? "").split("\n")) {
      const match = line.match(/^([^:]+):(\d+):(.*)$/);
      if (!match) continue;
      const [, file, lineno, body] = match;
      if (!groups.has(file)) groups.set(file, []);
      groups.get(file).push({ lineno, body });
    }
    if (!groups.size) return block("Result", pre(String(result ?? "")));
    const html = [...groups.entries()]
      .map(([file, hits]) => {
        const rows = hits
          .slice(0, 100)
          .map(
            ({ lineno, body }) =>
              `<div class="chat-grep__hit"><span class="chat-grep__lineno">${escapeHtml(lineno)}</span>${escapeHtml(body)}</div>`,
          )
          .join("");
        return `<div class="chat-grep__group"><div class="chat-grep__file">${escapeHtml(file)}</div>${rows}</div>`;
      })
      .join("");
    return block("Matches", `<div class="chat-grep">${html}</div>`);
  };

  const bashBody = (argsStr, result) => {
    const args = parseArgs(argsStr);
    const command = pickKey(args, ["command", "cmd", "script"]) ?? "";
    const cmdLine = `<div class="chat-bash__cmd"><span class="chat-bash__prompt">$</span> ${escapeHtml(command)}</div>`;
    const output = pre(String(result ?? ""), "chat-tool__code--bash");
    return block("Shell", `<div class="chat-bash">${cmdLine}${output}</div>`);
  };

  const todosBody = (argsStr) => {
    const args = parseArgs(argsStr);
    const todos = Array.isArray(args.todos) ? args.todos : [];
    const rows = todos
      .map((t) => {
        const status = (t.status || "pending").toLowerCase();
        const content = escapeHtml(t.content || "");
        const icon =
          status === "completed" ? "☑" : status === "in_progress" ? "◐" : "☐";
        return `<div class="chat-todo chat-todo--${status}"><span class="chat-todo__icon">${icon}</span>${content}</div>`;
      })
      .join("");
    return block("Plan", `<div class="chat-todos">${rows || "—"}</div>`);
  };

  const BODY_BY_TOOL = {
    edit_file: (args, result) => diffBody(result) || genericBody(args, result),
    grep: (args, result) => grepBody(result),
    bash_tool: (args, result) => bashBody(args, result),
    write_todos: (args) => todosBody(args),
  };

  window.toolBodyHTML = (name, argsStr, result, status) => {
    if (status === "running" && !result && !argsStr) return "";
    const fn = BODY_BY_TOOL[name];
    if (fn) {
      try {
        const out = fn(argsStr, result);
        if (out) return out;
      } catch (err) {
        console.warn("chat-tool-renderers: body builder failed, falling back", err);
      }
    }
    return genericBody(argsStr, result);
  };
})();
