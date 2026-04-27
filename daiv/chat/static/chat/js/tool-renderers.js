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

  // Best-effort extraction for streaming: tool args stream in as partial JSON,
  // so parseArgs returns {} until the final brace arrives. This scans the raw
  // string for `"key": "..."` pairs, which lets the summary row (path, command)
  // update live as the model writes args.
  const partialStringArg = (argsStr, key) => {
    if (!argsStr) return null;
    const re = new RegExp(`"${key}"\\s*:\\s*"((?:\\\\.|[^"\\\\])*)`);
    const m = String(argsStr).match(re);
    if (!m) return null;
    return m[1]
      .replace(/\\"/g, '"')
      .replace(/\\\\/g, "\\")
      .replace(/\\n/g, "\n")
      .replace(/\\t/g, "\t");
  };

  const pickKeyOrPartial = (args, keys, argsStr) => {
    const direct = pickKey(args, keys);
    if (direct != null) return direct;
    for (const k of keys) {
      const v = partialStringArg(argsStr, k);
      if (v != null) return v;
    }
    return null;
  };

  const truncate = (s, n) => {
    const str = String(s ?? "");
    return str.length > n ? str.slice(0, n) + "…" : str;
  };

  const badge = (text, tone) => ({ text: String(text), tone });

  // --- per-tool signature extractors -------------------------------------

  const sigReadFile = (args, _result, argsStr) => {
    const path = pickKeyOrPartial(args, ["path", "file_path"], argsStr) ?? "";
    const start = args.line_start ?? args.start_line ?? args.offset;
    let end = args.line_end ?? args.end_line;
    if (end == null && start != null && args.limit != null) {
      end = Number(start) + Number(args.limit) - 1;
    }
    const badges = [];
    if (start != null && end != null) {
      badges.push(badge(`${start}–${end}`, "neutral"));
    } else if (start != null) {
      badges.push(badge(`from ${start}`, "neutral"));
    } else if (end != null) {
      badges.push(badge(`to ${end}`, "neutral"));
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

  // The edit_file tool returns a textual ack ("Successfully replaced N..."), so
  // we lean on jsdiff to synthesize a real unified diff from old_string /
  // new_string on the client — proper LCS, correct hunk headers, context lines.
  const editDiffCounts = (args) => {
    const oldStr = pickKey(args, ["old_string"]);
    const newStr = pickKey(args, ["new_string"]);
    if (oldStr == null && newStr == null) return null;
    if (!window.Diff) return null;
    let plus = 0;
    let minus = 0;
    for (const part of window.Diff.diffLines(String(oldStr ?? ""), String(newStr ?? ""))) {
      if (part.added) plus += part.count ?? 0;
      else if (part.removed) minus += part.count ?? 0;
    }
    return { plus, minus };
  };

  const sigEditFile = (args, result, argsStr) => {
    const path = pickKeyOrPartial(args, ["path", "file_path"], argsStr) ?? "";
    const badges = [];
    const counts = editDiffCounts(args);
    if (counts) {
      if (counts.plus) badges.push(badge(`+${counts.plus}`, "success"));
      if (counts.minus) badges.push(badge(`−${counts.minus}`, "danger"));
    } else {
      // Fallback for legacy runs where result happens to be a unified diff.
      const isDiff = /^@@|^(---|\+\+\+)\s/m.test(String(result ?? ""));
      if (isDiff) {
        const { plus, minus } = countDiffLines(result);
        if (plus) badges.push(badge(`+${plus}`, "success"));
        if (minus) badges.push(badge(`−${minus}`, "danger"));
      }
    }
    if (result && String(result).toLowerCase().startsWith("error")) {
      badges.push(badge("error", "danger"));
    }
    return { label: "edit_file", path, badges };
  };

  const sigWriteFile = (args, _result, argsStr) => {
    const path = pickKeyOrPartial(args, ["path", "file_path"], argsStr) ?? "";
    const content = pickKeyOrPartial(args, ["content", "text"], argsStr) ?? "";
    const lines = String(content).split("\n").length;
    const badges = [];
    if (content) badges.push(badge(`new · ${lines} lines`, "info"));
    return { label: "write_file", path, badges };
  };

  const sigGrep = (args, result, argsStr) => {
    const pattern = pickKeyOrPartial(args, ["pattern", "query", "regex"], argsStr) ?? "";
    const path = pickKeyOrPartial(args, ["path", "scope", "include"], argsStr) ?? "";
    const mode = pickKeyOrPartial(args, ["output_mode"], argsStr) ?? "files_with_matches";
    const text = String(result ?? "").trim();
    const badges = [];
    if (text && text !== "No matches found" && !text.toLowerCase().startsWith("invalid regex")) {
      const lines = text.split("\n").filter((l) => l.trim().length);
      let hits = 0;
      let label = "hits";
      if (mode === "count") {
        hits = lines.reduce((acc, l) => {
          const m = l.match(/:\s*(\d+)\s*$/);
          return m ? acc + Number(m[1]) : acc;
        }, 0);
        label = "matches";
      } else if (mode === "content") {
        hits = lines.filter((l) => /^\s+\d+:/.test(l)).length;
        label = "matches";
      } else {
        hits = lines.length;
        label = "files";
      }
      if (hits) badges.push(badge(`${hits} ${label}`, "warn"));
    }
    const glob = pickKeyOrPartial(args, ["glob"], argsStr) ?? "";
    const scope = [path, glob].filter(Boolean).join(" / ");
    const target = scope ? ` in ${scope}` : "";
    return { label: "grep", path: `"${truncate(pattern, 60)}"${target}`, badges };
  };

  const sigGlob = (args, result, argsStr) => {
    const pattern = pickKeyOrPartial(args, ["pattern", "glob", "path"], argsStr) ?? "";
    const count = parseLsEntries(result).length;
    const badges = count ? [badge(`${count} files`, "neutral")] : [];
    return { label: "glob", path: pattern, badges };
  };

  // The `ls` and `glob` tools serialize their result with `str(list_of_paths)`,
  // producing a Python list repr like "['/repo/a.py', '/repo/b.py']" — a single
  // line of text with quoted entries. Pull entries out of either that shape or
  // a plain newline-separated fallback. Returns [] for anything we can't parse.
  const parseLsEntries = (result) => {
    const text = String(result ?? "").trim();
    if (!text) return [];
    if (text.startsWith("[") && text.endsWith("]")) {
      const entries = [];
      const re = /'((?:\\.|[^'\\])*)'|"((?:\\.|[^"\\])*)"/g;
      let m;
      while ((m = re.exec(text)) !== null) {
        const raw = m[1] ?? m[2] ?? "";
        entries.push(raw.replace(/\\'/g, "'").replace(/\\"/g, '"').replace(/\\\\/g, "\\"));
      }
      return entries;
    }
    return text.split("\n").map((l) => l.trim()).filter(Boolean);
  };

  const sigLs = (args, result, argsStr) => {
    const path = pickKeyOrPartial(args, ["path", "dir"], argsStr) ?? "";
    const count = parseLsEntries(result).length;
    const badges = count ? [badge(`${count} entries`, "neutral")] : [];
    return { label: "ls", path, badges };
  };

  // Bash success result comes in two shapes: the current
  // `{commands: [...], files_changed: [...]}` object and a legacy array of
  // per-command entries. Normalize to `{commands, files_changed}` with empty
  // defaults. Returns null for non-JSON results (`error: ...` or streaming),
  // so callers can fall back cleanly.
  const parseBashSuccess = (result) => {
    if (!result) return null;
    try {
      const parsed = JSON.parse(result);
      if (Array.isArray(parsed) && parsed.every((e) => e && typeof e === "object")) {
        return { commands: parsed, files_changed: [] };
      }
      if (parsed && typeof parsed === "object" && Array.isArray(parsed.commands)) {
        return {
          commands: parsed.commands,
          files_changed: Array.isArray(parsed.files_changed) ? parsed.files_changed : [],
        };
      }
    } catch {
      /* not JSON */
    }
    return null;
  };
  window.parseBashSuccess = parseBashSuccess;

  const parseBashResult = (result) => parseBashSuccess(result)?.commands ?? null;

  const aggregateBashExit = (entries) => {
    if (!entries || !entries.length) return null;
    const failed = entries.find((e) => Number(e.exit_code) !== 0);
    return failed ? Number(failed.exit_code) : Number(entries[entries.length - 1].exit_code);
  };

  const sigBash = (args, result, argsStr) => {
    const command = pickKeyOrPartial(args, ["command", "cmd", "script"], argsStr) ?? "";
    const badges = [];
    const entries = parseBashResult(result);
    let code = aggregateBashExit(entries);
    if (code == null) {
      const m = String(result ?? "").match(/exit(?: code)?[:=\s]+(-?\d+)/i);
      if (m) code = Number(m[1]);
    }
    if (code != null) {
      badges.push(badge(code === 0 ? "ok" : `exit ${code}`, code === 0 ? "success" : "danger"));
    }
    return { label: "bash", path: `$ ${truncate(command, 120)}`, badges };
  };

  const sigSkill = (args) => {
    const name = pickKey(args, ["skill", "name"]) ?? "";
    const skillArgs = pickKey(args, ["skill_args", "args"]) ?? "";
    return {
      label: "skill",
      path: name ? `${name}${skillArgs ? " " + truncate(String(skillArgs), 60) : ""}` : "",
      badges: [],
    };
  };

  const BUILTIN_SUBAGENTS = new Set(["general-purpose", "explore"]);

  const sigTask = (args) => {
    const subagent = pickKey(args, ["subagent_type", "agent", "type"]) ?? "";
    const description = pickKey(args, ["description", "title"]) ?? "";
    const badges = [];
    if (subagent) {
      badges.push(badge(subagent, BUILTIN_SUBAGENTS.has(subagent) ? "violet" : "info"));
    }
    return { label: "task", path: truncate(description, 120), badges };
  };

  // `new URL` throws on partial streaming values; fall back to the raw string.
  const summarizeUrl = (url) => {
    if (!url) return "";
    try {
      const u = new URL(String(url));
      const tail = (u.pathname || "") + (u.search || "");
      return tail && tail !== "/" ? `${u.host}${tail}` : u.host;
    } catch {
      return String(url);
    }
  };

  // Tools signal failures with `error: ...` (web_fetch / gitlab / gh). Soft
  // fallbacks like "Model processing failed (...). Contents of <url>:..."
  // intentionally do NOT use the prefix — they still carry useful content.
  const ERROR_PREFIX_RE = /^error\b/i;
  const REDIRECT_TAG_RE = /<redirect_url>([^<]+)<\/redirect_url>/;
  // Sentinel from _truncate_cli_output; presence drives the "truncated" badge.
  const TRUNCATED_SENTINEL_RE = /\.{3} \(truncated, (\d+) lines omitted\)/;
  // First identifying flag to surface in the gitlab/gh summary row so adjacent
  // calls are distinguishable without expanding.
  const CLI_ID_FLAGS = new Set(["--iid", "--id", "--mr-iid", "--issue-iid", "--pipeline-id", "--discussion-id"]);

  const sigWebFetch = (args, result, argsStr) => {
    const url = pickKeyOrPartial(args, ["url"], argsStr) ?? "";
    const badges = [];
    const text = String(result ?? "");
    if (REDIRECT_TAG_RE.test(text)) {
      badges.push(badge("redirect", "warn"));
    } else if (text && ERROR_PREFIX_RE.test(text.trim())) {
      badges.push(badge("error", "danger"));
    }
    return { label: "web_fetch", path: summarizeUrl(url), badges };
  };

  // web_search returns a JSON array of `{title, link, content}` objects.
  // Returns null if the result isn't (yet) parseable JSON — caller falls back
  // to a generic body. We also accept the legacy "no relevant results found…"
  // sentinel so old transcripts keep rendering correctly.
  const parseWebSearchResults = (result) => {
    const text = String(result ?? "").trim();
    if (!text) return null;
    try {
      const parsed = JSON.parse(text);
      return Array.isArray(parsed) ? parsed : null;
    } catch {
      return null;
    }
  };

  const sigWebSearch = (args, result, argsStr) => {
    const query = pickKeyOrPartial(args, ["query"], argsStr) ?? "";
    const text = String(result ?? "").trim();
    const badges = [];
    if (text) {
      const parsed = parseWebSearchResults(text);
      if (parsed != null) {
        // Subtract the synthetic "Suggested answer" entry (link="") so the
        // count reflects real hits the user might click.
        const hits = parsed.filter((r) => r && r.link).length;
        badges.push(hits ? badge(`${hits} results`, "info") : badge("no results", "warn"));
      } else if (/^no relevant results found/i.test(text)) {
        // Pre-JSON transcripts.
        badges.push(badge("no results", "warn"));
      }
    }
    return { label: "web_search", path: query ? `"${truncate(query, 80)}"` : "", badges };
  };

  // Server runs the real shlex parse; whitespace split is enough for the row.
  const summarizeSubcommand = (subcommand) => {
    const tokens = String(subcommand ?? "").trim().split(/\s+/).filter(Boolean);
    if (!tokens.length) return "";
    const [resource, action, ...rest] = tokens;
    const head = action ? `${resource} ${action}` : resource;
    for (let i = 0; i < rest.length; i += 1) {
      const tok = rest[i];
      const eq = tok.indexOf("=");
      const name = eq > -1 ? tok.slice(0, eq) : tok;
      if (CLI_ID_FLAGS.has(name)) {
        const value = eq > -1 ? tok.slice(eq + 1) : rest[i + 1] ?? "";
        return value ? `${head} ${name} ${value}` : `${head} ${name}`;
      }
    }
    return head;
  };

  const cliResultBadges = (text) => {
    const badges = [];
    if (text && ERROR_PREFIX_RE.test(text)) badges.push(badge("error", "danger"));
    if (TRUNCATED_SENTINEL_RE.test(text)) badges.push(badge("truncated", "warn"));
    return badges;
  };

  const sigGitlab = (args, result, argsStr) => {
    const subcommand = pickKeyOrPartial(args, ["subcommand"], argsStr) ?? "";
    const text = String(result ?? "").trim();
    const badges = [];
    // "simplified" is the default; only the costlier "detailed" mode rates a chip.
    if (pickKey(args, ["output_mode"]) === "detailed") badges.push(badge("detailed", "info"));
    badges.push(...cliResultBadges(text));
    return { label: "gitlab", path: truncate(summarizeSubcommand(subcommand), 120), badges };
  };

  const sigGh = (args, result, argsStr) => {
    const subcommand = pickKeyOrPartial(args, ["subcommand"], argsStr) ?? "";
    const text = String(result ?? "").trim();
    return {
      label: "gh",
      path: truncate(summarizeSubcommand(subcommand), 120),
      badges: cliResultBadges(text),
    };
  };

  const SIGNATURE_BY_TOOL = {
    read_file: sigReadFile,
    write_file: sigWriteFile,
    edit_file: sigEditFile,
    grep: sigGrep,
    glob: sigGlob,
    ls: sigLs,
    bash: sigBash,
    skill: sigSkill,
    task: sigTask,
    web_fetch: sigWebFetch,
    web_search: sigWebSearch,
    gitlab: sigGitlab,
    gh: sigGh,
  };

  window.toolSignature = (name, argsStr, result, _status) => {
    const args = parseArgs(argsStr);
    const fn = SIGNATURE_BY_TOOL[name];
    if (fn) return fn(args, result, argsStr);
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

  const renderMarkdownBlock = (text) =>
    window.renderMarkdown
      ? `<div class="chat-text">${window.renderMarkdown(text)}</div>`
      : pre(String(text));

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
    let currentFile = null;
    for (const line of String(result ?? "").split("\n")) {
      // Ripgrep-style "file:lineno:body" (single line per hit).
      const inline = line.match(/^([^:\s][^:]*):(\d+):(.*)$/);
      if (inline) {
        const [, file, lineno, body] = inline;
        if (!groups.has(file)) groups.set(file, []);
        groups.get(file).push({ lineno, body });
        continue;
      }
      // Deepagents "content" mode: "<file>:" header followed by indented "  <lineno>: <body>" rows.
      const header = line.match(/^([^\s][^:]*):$/);
      if (header) {
        currentFile = header[1];
        if (!groups.has(currentFile)) groups.set(currentFile, []);
        continue;
      }
      const hit = line.match(/^\s+(\d+):\s?(.*)$/);
      if (hit && currentFile) {
        groups.get(currentFile).push({ lineno: hit[1], body: hit[2] });
      }
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

  const bashRunBlock = (command, output, exitCode) => {
    const cmdLine = command
      ? `<div class="chat-bash__cmd"><span class="chat-bash__prompt">$</span> ${escapeHtml(command)}</div>`
      : "";
    const outBlock = output ? pre(output, "chat-tool__code--bash") : "";
    const exitLine =
      exitCode != null && Number(exitCode) !== 0
        ? `<div class="chat-bash__exit chat-bash__exit--err">exit ${Number(exitCode)}</div>`
        : "";
    return `<div class="chat-bash">${cmdLine}${outBlock}${exitLine}</div>`;
  };

  const editFileBody = (argsStr, result) => {
    const args = parseArgs(argsStr);
    const path = pickKey(args, ["path", "file_path"]) ?? "";
    const oldStr = pickKey(args, ["old_string"]);
    const newStr = pickKey(args, ["new_string"]);

    // Both strings fully parsed + jsdiff loaded → real unified diff. Partial
    // streaming values would produce misleading hunks, so we require full args.
    if (oldStr != null && newStr != null && window.Diff) {
      const patch = window.Diff.createPatch(path, String(oldStr), String(newStr), "", "", { context: 2 });
      // Strip the "Index:" / "===" header jsdiff emits — diffBody only needs
      // the ---/+++/@@/+/- lines.
      const lines = patch.split("\n").filter(
        (l) => !l.startsWith("Index:") && !/^=+$/.test(l),
      );
      const parts = [diffBody(lines.join("\n"))];
      if (result && String(result).toLowerCase().startsWith("error")) {
        parts.push(block("Error", pre(String(result))));
      }
      return parts.join("");
    }

    // Streaming / unparseable args — show what the tool returned for context.
    if (result) return block("Result", pre(String(result)));
    return genericBody(argsStr, result);
  };

  const bashBody = (argsStr, result) => {
    const args = parseArgs(argsStr);
    const streamedCommand = pickKeyOrPartial(args, ["command", "cmd", "script"], argsStr) ?? "";
    const entries = parseBashResult(result);

    if (entries) {
      // Render a block per entry so multi-command runs read cleanly; drop the
      // surrounding JSON entirely.
      const html = entries
        .map((e) => bashRunBlock(e.command ?? "", e.output ?? "", e.exit_code))
        .join("");
      return block("Shell", html);
    }

    // Either still streaming (no result yet) or raw-text result from an older
    // tool. Show whatever command we have, plus raw output if present.
    return block(
      "Shell",
      bashRunBlock(streamedCommand, result == null ? "" : String(result), null),
    );
  };

  const taskBody = (argsStr, result) => {
    const args = parseArgs(argsStr);
    const description = pickKey(args, ["description", "title"]) ?? "";
    const subagent = pickKey(args, ["subagent_type", "agent", "type"]) ?? "";
    const prompt = pickKey(args, ["prompt", "input", "task"]) ?? "";
    const parts = [];
    if (description || subagent) {
      const meta = `<div class="chat-task__meta">` +
        (subagent ? `<span class="chat-task__agent">${escapeHtml(subagent)}</span>` : "") +
        (description ? `<span class="chat-task__desc">${escapeHtml(description)}</span>` : "") +
        `</div>`;
      parts.push(block("Task", meta));
    }
    if (prompt) parts.push(block("Prompt", pre(String(prompt))));
    if (result != null && String(result).length) {
      parts.push(block("Result", renderMarkdownBlock(result)));
    }
    return parts.join("");
  };

  const resultOnlyBody = (label, result) => {
    const text = String(result ?? "");
    return text.length ? block(label, pre(text)) : "";
  };

  const skillBody = (argsStr, result) => {
    const body = String(result ?? "");
    if (!body.trim()) return genericBody(argsStr, result);
    return block("Skill", renderMarkdownBlock(body));
  };

  // Open in a new tab so we never replace the chat tab; rel=noopener for the
  // usual reverse-tabnabbing reasons.
  const externalLink = (url, label) => {
    const safeHref = escapeHtml(String(url));
    const safeLabel = escapeHtml(label != null ? String(label) : String(url));
    return `<a class="chat-tool__link" href="${safeHref}" target="_blank" rel="noopener noreferrer">${safeLabel}</a>`;
  };

  const webFetchBody = (argsStr, result) => {
    const args = parseArgs(argsStr);
    const url = pickKeyOrPartial(args, ["url"], argsStr) ?? "";
    const prompt = pickKeyOrPartial(args, ["prompt"], argsStr) ?? "";
    const text = String(result ?? "");
    const parts = [];

    if (url) {
      parts.push(block("URL", `<div class="chat-webfetch__url">${externalLink(url, summarizeUrl(url))}</div>`));
    }
    if (prompt) parts.push(block("Prompt", pre(prompt)));

    if (text) {
      const redirect = text.match(REDIRECT_TAG_RE);
      if (redirect) {
        parts.push(
          block(
            "Redirected to",
            `<div class="chat-webfetch__url">${externalLink(redirect[1], summarizeUrl(redirect[1]))}</div>`,
          ),
        );
      } else if (ERROR_PREFIX_RE.test(text.trim())) {
        parts.push(block("Error", pre(text)));
      } else {
        parts.push(block("Result", renderMarkdownBlock(text)));
      }
    }
    return parts.join("") || genericBody(argsStr, result);
  };

  const webSearchBody = (argsStr, result) => {
    const args = parseArgs(argsStr);
    const query = pickKeyOrPartial(args, ["query"], argsStr) ?? "";
    const text = String(result ?? "");
    const parts = [];
    if (query) parts.push(block("Query", pre(String(query))));

    const trimmed = text.trim();
    if (!trimmed) return parts.join("") || genericBody(argsStr, result);

    const results = parseWebSearchResults(trimmed);
    if (results == null) {
      // Pre-JSON transcripts or unparseable output — fall back gracefully.
      parts.push(block("Result", pre(text)));
      return parts.join("");
    }
    if (!results.length) {
      parts.push(block("Result", pre("No relevant results found for the given search query.")));
      return parts.join("");
    }

    const items = results
      .map((r) => {
        const link = (r && r.link) || "";
        const title = (r && r.title) || "";
        const content = (r && r.content) || "";
        const titleHtml = link
          ? externalLink(link, title || link)
          : `<span class="chat-search__title chat-search__title--answer">${escapeHtml(title || "Suggested answer")}</span>`;
        const host = link ? `<div class="chat-search__host">${escapeHtml(summarizeUrl(link))}</div>` : "";
        const body = content ? `<div class="chat-search__snippet">${escapeHtml(content)}</div>` : "";
        return `<div class="chat-search__item">${titleHtml}${host}${body}</div>`;
      })
      .join("");
    parts.push(block("Results", `<div class="chat-search">${items}</div>`));
    return parts.join("");
  };

  const cliBody = (tool, argsStr, result) => {
    const args = parseArgs(argsStr);
    const subcommand = pickKeyOrPartial(args, ["subcommand"], argsStr) ?? "";
    const command = `${tool}${subcommand ? " " + subcommand : ""}`;
    const text = String(result ?? "");
    return block("Shell", bashRunBlock(command, text, null));
  };

  const BODY_BY_TOOL = {
    read_file: (_args, result) => resultOnlyBody("Contents", result),
    write_file: (_args, result) => resultOnlyBody("Result", result),
    edit_file: (args, result) => editFileBody(args, result),
    grep: (args, result) => grepBody(result),
    glob: (_args, result) => {
      const entries = parseLsEntries(result);
      if (!entries.length) return resultOnlyBody("Matches", result);
      return block("Matches", pre(entries.join("\n")));
    },
    ls: (_args, result) => {
      const entries = parseLsEntries(result);
      if (!entries.length) return resultOnlyBody("Entries", result);
      return block("Entries", pre(entries.join("\n")));
    },
    bash: (args, result) => bashBody(args, result),
    skill: (args, result) => skillBody(args, result),
    task: (args, result) => taskBody(args, result),
    web_fetch: (args, result) => webFetchBody(args, result),
    web_search: (args, result) => webSearchBody(args, result),
    gitlab: (args, result) => cliBody("gitlab", args, result),
    gh: (args, result) => cliBody("gh", args, result),
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
