// Per-tool UI strategies. Exports on window:
//
//   toolRow(seg, ctx?)
//     -> one compact transcript row for a tool_call or thinking segment (see toolRow below)
//
//   toolBodyHTML(seg, ctx)
//     -> an opened row's body: What went wrong, the tool's own sections, then Raw (debug mode only);
//        ctx = { mcpServers, mcpRows, latestArtifactsById, mergeRequestUrl }
//
//   artifactItem(seg, latestById)
//     -> one publish_artifact row for the Artifacts card: { state: "running" | "published" | "error", ... },
//        showing the newest result `latestById` holds for its artifact id
//
//   parseArtifactResult(result)
//     -> the parsed publish_artifact success result, or null
//
// Every extraction is defensive: if JSON doesn't parse or expected keys are missing,
// we return a neutral row/body rather than throwing. Unknown tools fall
// through to the default body: the result, formatted (table, field list or markdown).

(() => {
  const escapeHtml = (s) =>
    String(s ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;");

  const escapeAttr = (s) => escapeHtml(s).replaceAll('"', "&quot;");

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
      .replace(/\\n/g, "\n")
      .replace(/\\t/g, "\t")
      .replace(/\\\\/g, "\\");
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

  // --- per-tool row details ---------------------------------------------

  const readRange = (args) => {
    const start = args.line_start ?? args.start_line ?? args.offset;
    let end = args.line_end ?? args.end_line;
    if (end == null && start != null && args.limit != null) {
      end = Number(start) + Number(args.limit) - 1;
    }
    if (start != null && end != null) return `${start}–${end}`;
    if (start != null) return `from ${start}`;
    if (end != null) return `to ${end}`;
    return "";
  };

  const isUnifiedDiff = (result) => /^@@|^(---|\+\+\+)\s/m.test(String(result ?? ""));

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

  const grepBadges = (args, result, argsStr) => {
    const mode = pickKeyOrPartial(args, ["output_mode"], argsStr) ?? "files_with_matches";
    const text = String(result ?? "").trim();
    if (text === "No matches found") return [badge("no matches", "warn")];
    if (!text || text.toLowerCase().startsWith("invalid regex")) return [];
    const lines = text.split("\n").filter((l) => l.trim().length);
    let hits = lines.length;
    let label = "files";
    if (mode === "count") {
      hits = lines.reduce((acc, l) => {
        const m = l.match(/:\s*(\d+)\s*$/);
        return m ? acc + Number(m[1]) : acc;
      }, 0);
      label = "matches";
    } else if (mode === "content") {
      hits = lines.filter((l) => /^\s+\d+:/.test(l)).length;
      label = "matches";
    }
    return hits ? [badge(`${hits} ${label}`, "warn")] : [];
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

  const countBadges = (result, noun) => {
    const count = parseLsEntries(result).length;
    return count ? [badge(`${count} ${noun}`, "neutral")] : [];
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

  const skillDetail = (args) => {
    const name = pickKey(args, ["skill", "name"]) ?? "";
    const skillArgs = pickKey(args, ["skill_args", "args"]) ?? "";
    return name ? `${name}${skillArgs ? " " + truncate(String(skillArgs), 60) : ""}` : "";
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
  // Legacy line-truncation sentinel; presence drives the "truncated" badge. Git platform tools
  // no longer truncate (large results are offloaded to a file by the agent harness), so this only
  // matches messages persisted before that change.
  const TRUNCATED_SENTINEL_RE = /\.{3} \(truncated, (\d+) lines omitted\)/;
  // First identifying flag to surface in the gitlab/gh summary row so adjacent
  // calls are distinguishable without expanding.
  const CLI_ID_FLAGS = new Set(["--iid", "--id", "--mr-iid", "--issue-iid", "--pipeline-id", "--discussion-id"]);

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

  const webSearchBadges = (result) => {
    const text = String(result ?? "").trim();
    if (!text) return [];
    const parsed = parseWebSearchResults(text);
    if (parsed != null) {
      // Subtract the synthetic "Suggested answer" entry (link="") so the
      // count reflects real hits the user might click.
      const hits = parsed.filter((r) => r && r.link).length;
      return [hits ? badge(`${hits} results`, "info") : badge("no results", "warn")];
    }
    // Pre-JSON transcripts.
    return /^no relevant results found/i.test(text) ? [badge("no results", "warn")] : [];
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

  // publish_artifact returns JSON ({"status":"published"|"updated","url":...}) on success and an
  // "Error publishing artifact..." string otherwise.
  const parseArtifactResult = (result) => {
    const parsed = parseArgs(result);
    return parsed.url ? parsed : null;
  };

  const formatBytes = (n) => {
    const size = Number(n);
    if (!Number.isFinite(size)) return "";
    if (size < 1024) return `${size} B`;
    if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
    return `${(size / (1024 * 1024)).toFixed(1)} MB`;
  };

  const basename = (p) => {
    const str = String(p ?? "");
    const idx = str.lastIndexOf("/");
    return idx >= 0 ? str.slice(idx + 1) : str;
  };

  const ARTIFACT_KIND_LABELS = { markdown: "Markdown", html: "HTML", image: "Image", text: "Text", other: "File" };

  window.parseArtifactResult = parseArtifactResult;

  // A server-built segment reads `done` with a null result until its ToolMessage is
  // checkpointed, so a missing result means still publishing unless RUN_ERROR marked it.
  window.artifactItem = (seg, latestById) => {
    const argsStr = seg.args;
    const args = parseArgs(argsStr);
    const pathArg = pickKeyOrPartial(args, ["path"], argsStr) ?? "";

    if (seg.status === "running" || (seg.result == null && seg.status !== "error")) {
      return {
        state: "running",
        title: pickKeyOrPartial(args, ["title"], argsStr) || pathArg,
        updating: Boolean(pickKeyOrPartial(args, ["artifact_id"], argsStr)),
      };
    }

    const parsed = parseArtifactResult(seg.result);
    if (parsed) {
      const current = (parsed.id && latestById?.get(parsed.id)) || parsed;
      return {
        state: "published",
        id: parsed.id || "",
        updated: current.status === "updated",
        title: current.title || basename(pathArg),
        filename: current.filename || "",
        kindLabel: ARTIFACT_KIND_LABELS[current.kind] || "",
        sizeLabel: current.size != null ? formatBytes(current.size) : "",
        url: current.url,
        download_url: current.download_url || "",
      };
    }

    return { state: "error", title: pathArg, message: String(seg.result ?? "").trim() || "Publishing failed." };
  };

  // --- one compact row per tool call ---------------------------------------

  const repoRelative = (p) => String(p ?? "").replace(/^\/workspace\/repo(?:\/|$)/, "");

  const repoDir = (p) => {
    const rel = repoRelative(p);
    const idx = rel.lastIndexOf("/");
    return idx > 0 ? rel.slice(0, idx) : "";
  };

  const countLines = (text) => {
    const str = String(text ?? "");
    if (!str) return 0;
    return str.split("\n").length - (str.endsWith("\n") ? 1 : 0);
  };

  const joinDetail = (...parts) => parts.filter(Boolean).join(" · ");

  const toolPath = (args, argsStr) => pickKeyOrPartial(args, ["path", "file_path"], argsStr) ?? "";

  const bashExit = (result) => {
    const commands = parseBashSuccess(result)?.commands;
    if (!commands) return { known: false, code: null };
    const bad = commands.find((c) => c.exit_code != null && Number(c.exit_code) !== 0);
    return { known: true, code: bad ? Number(bad.exit_code) : null };
  };

  // gitlab and gh rows: the brand names the row, and the subcommand is its detail.
  const platformRow = (name, icon) => ({ args, result, argsStr }) => {
    const subcommand = pickKeyOrPartial(args, ["subcommand"], argsStr) ?? "";
    // "simplified" is the default; only the costlier "detailed" mode rates a chip.
    const badges = pickKey(args, ["output_mode"]) === "detailed" ? [badge("detailed", "info")] : [];
    if (TRUNCATED_SENTINEL_RE.test(String(result ?? ""))) badges.push(badge("truncated", "warn"));
    return {
      icon,
      phrase: name,
      detail: truncate(summarizeSubcommand(subcommand), 120),
      badges,
      category: { key: "platform", name },
    };
  };

  const jobsRow = (key) => ({ args }) => ({
    icon: "share",
    key,
    detail: String(pickKey(args, ["batch_id"]) ?? ""),
    category: { key: "jobs" },
  });

  // Each builder returns { icon, key | phrase, vars?, detail?, diff?, badges?, failed?, category }.
  const ROW_BY_TOOL = {
    read_file: ({ args, argsStr }) => {
      const path = toolPath(args, argsStr);
      return {
        icon: "document-text",
        key: "read",
        vars: { name: basename(path) },
        detail: joinDetail(repoDir(path), readRange(args)),
        category: { key: "read" },
      };
    },
    edit_file: ({ args, argsStr, result }) => {
      const path = toolPath(args, argsStr);
      const counts = editDiffCounts(args) ?? (isUnifiedDiff(result) ? countDiffLines(result) : null);
      return {
        icon: "pencil",
        key: "edit",
        vars: { name: basename(path) },
        detail: repoDir(path),
        diff: counts ? { added: counts.plus, removed: counts.minus } : null,
        category: { key: "edit" },
      };
    },
    write_file: ({ args, argsStr }) => {
      const path = toolPath(args, argsStr);
      const content = pickKeyOrPartial(args, ["content", "text"], argsStr);
      return {
        icon: "document-plus",
        key: "create",
        vars: { name: basename(path) },
        detail: repoDir(path),
        diff: content == null ? null : { added: countLines(content), removed: 0 },
        category: { key: "create" },
      };
    },
    grep: ({ args, result, argsStr }) => {
      const pattern = pickKeyOrPartial(args, ["pattern", "query", "regex"], argsStr) ?? "";
      const path = pickKeyOrPartial(args, ["path", "scope", "include"], argsStr) ?? "";
      const glob = pickKeyOrPartial(args, ["glob"], argsStr) ?? "";
      return {
        icon: "magnifying-glass",
        key: "grep",
        vars: { pattern: truncate(pattern, 60) },
        detail: [repoRelative(path), glob].filter(Boolean).join(" / "),
        badges: grepBadges(args, result, argsStr),
        category: { key: "search" },
      };
    },
    glob: ({ args, result, argsStr }) => ({
      icon: "document-magnifying-glass",
      key: "glob",
      vars: { pattern: truncate(pickKeyOrPartial(args, ["pattern", "glob", "path"], argsStr) ?? "", 60) },
      badges: countBadges(result, "files"),
      category: { key: "search" },
    }),
    ls: ({ args, result, argsStr }) => ({
      icon: "folder",
      key: "ls",
      vars: { name: repoRelative(pickKeyOrPartial(args, ["path", "dir"], argsStr) ?? "") || "." },
      badges: countBadges(result, "entries"),
      category: { key: "list" },
    }),
    bash: ({ args, result, argsStr }) => {
      const command = String(pickKeyOrPartial(args, ["command", "cmd", "script"], argsStr) ?? "");
      const T = window.TranscriptLabels;
      const exit = bashExit(result);
      let detail = "";
      if (exit.code != null) detail = T.t("detail_exit", { code: exit.code });
      else if (exit.known) detail = T.t("detail_passed");
      return {
        icon: "command-line",
        key: "run",
        vars: { command: truncate(command.split("\n")[0], 80) },
        detail,
        failed: exit.code != null,
        category: { key: "run" },
      };
    },
    skill: ({ args }) => ({ icon: "beaker", key: "skill", detail: skillDetail(args), category: { key: "skill" } }),
    task: ({ args }) => ({
      icon: "agent",
      key: "subagent",
      vars: { name: pickKey(args, ["subagent_type", "agent", "type"]) ?? "general-purpose" },
      detail: truncate(pickKey(args, ["description", "title"]) ?? "", 120),
      category: { key: "subagent" },
    }),
    web_search: ({ args, result, argsStr }) => {
      const query = pickKeyOrPartial(args, ["query"], argsStr) ?? "";
      return {
        icon: "web-search",
        key: "web",
        detail: query ? `"${truncate(query, 80)}"` : "",
        badges: webSearchBadges(result),
        category: { key: "web" },
      };
    },
    web_fetch: ({ args, result, argsStr }) => ({
      icon: "web-fetch",
      key: "fetch",
      detail: summarizeUrl(pickKeyOrPartial(args, ["url"], argsStr) ?? ""),
      badges: REDIRECT_TAG_RE.test(String(result ?? "")) ? [badge("redirect", "warn")] : [],
      category: { key: "fetch" },
    }),
    gitlab: platformRow("GitLab", "gitlab"),
    gh: platformRow("GitHub", "github"),
    tool_search: ({ args }) => {
      const select = pickKey(args, ["select"]);
      const query = pickKey(args, ["query"]);
      return {
        icon: "squares-plus",
        key: "tools",
        detail: Array.isArray(select) ? select.join(", ") : String(query ?? ""),
        category: { key: "tools" },
      };
    },
    fetch_artifact: ({ args, argsStr }) => ({
      icon: "paper-clip",
      key: "artifact_open",
      detail: pickKeyOrPartial(args, ["path"], argsStr) || pickKeyOrPartial(args, ["artifact_id"], argsStr) || "",
      category: { key: "artifact" },
    }),
    publish_artifact: ({ args, result, argsStr }) => ({
      icon: "paper-clip",
      key: "published",
      detail:
        parseArtifactResult(result)?.title ||
        pickKeyOrPartial(args, ["title"], argsStr) ||
        basename(pickKeyOrPartial(args, ["path"], argsStr) ?? ""),
      category: { key: "artifact" },
    }),
    ask_user_question: ({ args, argsStr }) => {
      const questions = pickKey(args, ["questions"]);
      const text = Array.isArray(questions) ? questions[0]?.question : partialStringArg(argsStr, "question");
      return {
        icon: "chat-bubble",
        key: "question",
        detail: truncate(String(text ?? "").replace(/\s+/g, " "), 80),
        category: { key: "question" },
      };
    },
    delegate_jobs: () => ({ icon: "share", key: "delegate", category: { key: "jobs" } }),
    check_delegated_jobs: jobsRow("check_jobs"),
    cancel_delegated_jobs: jobsRow("cancel_jobs"),
  };

  const mcpRow = (name, { args }, servers) => {
    const server = servers
      .filter((s) => name.startsWith(`${s}_`))
      .sort((a, b) => b.length - a.length)[0];
    if (!server) {
      return { icon: "puzzle-piece", phrase: name, category: { key: "other" } };
    }
    const first = Object.values(args).find((v) => ["string", "number"].includes(typeof v) && String(v));
    return {
      icon: "puzzle-piece",
      key: "mcp",
      vars: { server, tool: name.slice(server.length + 1).replaceAll("_", " ") },
      detail: first == null ? "" : truncate(String(first).replace(/\s+/g, " "), 80),
      category: { key: "mcp", name: server },
    };
  };

  const thinkingRow = (seg) => {
    const T = window.TranscriptLabels;
    const timed = seg.startedAt != null && seg.endedAt != null;
    const secs = timed ? Math.max(1, Math.round((seg.endedAt - seg.startedAt) / 1000)) : null;
    const phrase = timed ? T.t("thought_for", { duration: T.duration(secs) }) : T.t("thought");
    return {
      icon: "light-bulb",
      phrase,
      livePhrase: seg.status === "running" ? T.t("thinking") : phrase,
      detail: "",
      diff: null,
      badges: [],
      failed: false,
      category: { key: "thinking" },
      thoughtSecs: secs,
    };
  };

  const rowFailed = (name, seg, part) => {
    if (seg.status === "error") return true;
    if (name === "delegate_jobs") return false;
    return Boolean(part.failed) || (seg.result != null && ERROR_PREFIX_RE.test(String(seg.result).trim()));
  };

  window.toolRow = (seg, ctx) => {
    if (seg.type === "thinking") return thinkingRow(seg);
    const T = window.TranscriptLabels;
    const name = String(seg.name ?? "");
    const input = { args: parseArgs(seg.args), argsStr: seg.args, result: seg.result, status: seg.status };
    const build = Object.hasOwn(ROW_BY_TOOL, name) ? ROW_BY_TOOL[name] : null;
    const part = build ? build(input) : mcpRow(name, input, ctx?.mcpServers ?? []);
    const phrase = part.phrase ?? T.t(`row_${part.key}`, part.vars);
    const liveKey = `live_${part.key}`;
    const livePhrase = part.phrase ?? (liveKey in T.defaults ? T.t(liveKey, part.vars) : phrase);
    return {
      icon: part.icon,
      phrase,
      livePhrase,
      detail: part.detail ?? "",
      diff: part.diff ?? null,
      badges: part.badges ?? [],
      failed: rowFailed(name, seg, part),
      category: part.category,
    };
  };

  // --- expanded bodies ---------------------------------------------------

  const VISIBLE_LINES = 12;
  const LINE_CAP = 500;
  const LINE_CHARS = 2000;
  const CHAR_CAP = 50000;
  const STRUCTURED_ROWS = 50;
  const TABLE_COLUMNS = 6;
  const VALUE_CHARS = 1000;

  const t = (key, vars) => window.TranscriptLabels.t(key, vars);
  const tn = (key, count, vars) => window.TranscriptLabels.tn(key, count, vars);

  const isError = (text) => ERROR_PREFIX_RE.test(String(text ?? "").trim());

  const byteLength = (s) => (typeof TextEncoder === "function" ? new TextEncoder().encode(s).length : s.length);

  const formatSize = (lines, bytes) => {
    const kb = bytes / 1024;
    const size = kb < 10 ? Math.max(kb, 0.1).toFixed(1) : String(Math.round(kb));
    return `${tn("size_lines", lines)} · ${t("size_kb", { size })}`;
  };

  const contextSize = (text) => {
    const source = String(text ?? "");
    return source ? formatSize(countLines(source), byteLength(source)) : "";
  };

  const sliceText = (text) => {
    const all = String(text ?? "").replace(/\n$/, "").split("\n");
    const lines = [];
    let budget = CHAR_CAP;
    for (const line of all) {
      const kept = line.length > LINE_CHARS ? `${line.slice(0, LINE_CHARS)}…` : line;
      if (lines.length === LINE_CAP || kept.length > budget) break;
      lines.push(kept);
      budget -= kept.length + 1;
    }
    return { lines, total: all.length };
  };

  const andMoreHTML = (count) =>
    count > 0 ? `<p class="body-and-more">${escapeHtml(t("and_more", { count }))}</p>` : "";

  const moreHTML = (count, html) =>
    `<details class="body-more"><summary>${escapeHtml(t("show_all", { count }))}</summary>${html}</details>`;

  // `total` is the line count before the caller sliced `lines`: "Show all" counts the lines it holds,
  // and the lines never kept are counted in the "and N more" note.
  const clampLines = (lines, max = VISIBLE_LINES, { total = lines.length, cls = "" } = {}) => {
    const kept = lines.slice(0, LINE_CAP);
    const pre = (part) => `<pre class="body-pre${cls ? ` ${cls}` : ""}">${part.join("\n")}</pre>`;
    const hidden = kept.slice(max);
    const more = hidden.length ? moreHTML(kept.length, pre(hidden)) : "";
    return pre(kept.slice(0, max)) + more + andMoreHTML(total - kept.length);
  };

  const textHTML = (text, cls = "") => {
    const { lines, total } = sliceText(text);
    return clampLines(lines.map(escapeHtml), VISIBLE_LINES, { total, cls });
  };

  const markdownBlock = (source) =>
    window.renderMarkdown
      ? `<div class="body-md chat-text">${window.renderMarkdown(source)}</div>`
      : `<pre class="body-pre">${escapeHtml(source)}</pre>`;

  const FENCE_RE = /^ {0,3}(`{3,}|~{3,})/;

  // Past line `max`, cut at a blank line or before a fence within the next `max` lines, else at `max`;
  // a fence still open at `max` is closed in the head and reopened in the tail.
  const splitMarkdown = (lines, max) => {
    if (lines.length <= max) return [lines, []];
    let fence = "";
    let opener = "";
    for (const line of lines.slice(0, max)) {
      const marker = line.match(FENCE_RE)?.[1];
      if (!marker) continue;
      if (!fence) [fence, opener] = [marker, line];
      else if (marker[0] === fence[0] && marker.length >= fence.length) fence = "";
    }
    if (fence) return [[...lines.slice(0, max), fence], [opener, ...lines.slice(max)]];
    for (let i = max; i < Math.min(lines.length, 2 * max); i += 1) {
      if (!lines[i].trim()) return [lines.slice(0, i), lines.slice(i + 1)];
      if (FENCE_RE.test(lines[i])) return [lines.slice(0, i), lines.slice(i)];
    }
    return [lines.slice(0, max), lines.slice(max)];
  };

  const markdownHTML = (text) => {
    const { lines, total } = sliceText(text);
    const [head, tail] = splitMarkdown(lines, VISIBLE_LINES);
    const more = tail.length ? moreHTML(lines.length, markdownBlock(tail.join("\n"))) : "";
    return markdownBlock(head.join("\n")) + more + andMoreHTML(total - lines.length);
  };

  const isRecord = (value) => value != null && typeof value === "object" && !Array.isArray(value);

  const valueText = (value) => {
    if (value === undefined) return "";
    return truncate(typeof value === "string" ? value : JSON.stringify(value), VALUE_CHARS);
  };

  const parseJSON = (text) => {
    try {
      return JSON.parse(text);
    } catch {
      return undefined;
    }
  };

  const structured = (text) => {
    const source = String(text ?? "");
    const trimmed = source.trim();
    const parsed = /^[[{]/.test(trimmed) ? parseJSON(trimmed) : undefined;
    if (Array.isArray(parsed) && parsed.length && parsed.every(isRecord)) {
      const headers = Object.keys(parsed[0]).slice(0, TABLE_COLUMNS);
      if (headers.length) {
        const rows = parsed.slice(0, STRUCTURED_ROWS).map((item) => headers.map((key) => valueText(item[key])));
        return { kind: "table", headers, rows, more: parsed.length - rows.length };
      }
    }
    if (isRecord(parsed) && Object.keys(parsed).length) {
      const entries = Object.entries(parsed);
      const pairs = entries.slice(0, STRUCTURED_ROWS).map(([key, value]) => [key, valueText(value)]);
      return { kind: "fields", pairs, more: entries.length - pairs.length };
    }
    return { kind: "markdown", text: source };
  };

  const tableHTML = (headers, rows) => {
    const head = headers.map((h) => `<th>${escapeHtml(h)}</th>`).join("");
    const body = rows.map((row) => `<tr>${row.map((cell) => `<td>${escapeHtml(cell)}</td>`).join("")}</tr>`).join("");
    const table = `<table class="body-table"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
    return `<div class="body-table-wrap">${table}</div>`;
  };

  const fieldsHTML = (pairs) => {
    const items = pairs.map(([key, value]) => `<dt>${escapeHtml(key)}</dt><dd>${escapeHtml(value)}</dd>`);
    return `<dl class="body-fields">${items.join("")}</dl>`;
  };

  const structuredHTML = (text) => {
    const data = structured(text);
    if (data.kind === "table") return tableHTML(data.headers, data.rows) + andMoreHTML(data.more);
    if (data.kind === "fields") return fieldsHTML(data.pairs) + andMoreHTML(data.more);
    return markdownHTML(data.text);
  };

  // deepagents' eviction stub (no size), and DAIV's confirmation for a result written to a file on request.
  const EVICTED_RE =
    /^Tool result too large, the result of this tool call \S* was saved in the filesystem at this path: ([^\n]+)/;
  const WROTE_RE = /^Wrote (\d+) bytes \((\d+) lines\) to ([^\n]+)\nPreview \(first \d+ lines\):\n?/;
  const PREVIEW_NOTE_RE = /^Here is a preview\b[^\n]*\n/m;

  const parseOffload = (text) => {
    const source = String(text ?? "");
    const wrote = source.match(WROTE_RE);
    if (wrote) {
      const preview = source.slice(wrote[0].length);
      return { path: wrote[3].trim(), preview, bytes: Number(wrote[1]), lines: Number(wrote[2]) };
    }
    const evicted = source.match(EVICTED_RE);
    if (!evicted) return null;
    const rest = source.slice(evicted[0].length);
    const note = rest.match(PREVIEW_NOTE_RE);
    const preview = note ? rest.slice(note.index + note[0].length) : rest;
    return { path: evicted[1].trim(), preview: preview.replace(/^\n+/, "").trimEnd() };
  };

  const offloadHTML = ({ path, preview, bytes, lines }) => {
    const size = bytes == null ? "" : ` <span class="body-size">${escapeHtml(formatSize(lines, bytes))}</span>`;
    const notice = `<p class="body-offload">${escapeHtml(t("offloaded", { path }))}${size}</p>`;
    return notice + (preview.trim() ? textHTML(preview) : "");
  };

  const section = (labelKey, html, sizeHTML = "") => {
    if (!html) return "";
    const size = sizeHTML ? ` <span class="body-size">${sizeHTML}</span>` : "";
    return `<div class="body-section"><div class="body-label">${escapeHtml(t(labelKey))}${size}</div>${html}</div>`;
  };

  // Every builder shows its result through here, so an evicted result gets the offload notice whatever the tool.
  const contextSection = (text, render = structuredHTML, sizeHTML = null) => {
    const source = String(text ?? "");
    if (!source.trim() || isError(source)) return "";
    const offload = parseOffload(source);
    const body = offload ? offloadHTML(offload) : render(source);
    return section("body_context", body, sizeHTML ?? escapeHtml(contextSize(source)));
  };

  const wentWrong = (seg) => {
    const result = seg.result == null ? "" : String(seg.result).trim();
    if (isError(result)) return result.replace(/^error:\s*/i, "") || result;
    return seg.status === "error" && !result ? t("unanswered") : "";
  };

  const wrongHTML = (text) =>
    `<div class="body-error"><div class="body-label">${escapeHtml(t("body_wrong"))}</div>${textHTML(text)}</div>`;

  const rawHTML = (seg) => {
    if (!window.TranscriptLabels.showRaw) return "";
    const pre = (text) => `<pre class="body-pre">${escapeHtml(text)}</pre>`;
    const args = seg.args ? section("body_args", pre(prettyJSON(seg.args))) : "";
    const result = seg.result == null ? "" : section("body_result", pre(String(seg.result)));
    if (!args && !result) return "";
    return `<details class="body-raw"><summary>${escapeHtml(t("body_raw"))}</summary>${args}${result}</details>`;
  };

  // --- per-tool bodies (each returns its middle sections, or { wrong, html }) ---

  const diffHTML = (patch) => {
    const lines = String(patch ?? "").split("\n");
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
    return `<div class="chat-diff">${html}</div>`;
  };

  // Returns null when jsdiff isn't loaded so callers can fall back to text.
  const jsdiffHTML = (path, oldStr, newStr, context) => {
    if (!window.Diff?.createPatch) return null;
    const patch = window.Diff.createPatch(path, oldStr, newStr, "", "", { context });
    return diffHTML(patch.split("\n").filter((l) => !l.startsWith("Index:") && !/^=+$/.test(l)).join("\n"));
  };

  const grepHTML = (text) => {
    const groups = new Map();
    let currentFile = null;
    for (const line of sliceText(text).lines) {
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
    if (!groups.size) return textHTML(text);
    const html = [...groups.entries()]
      .map(([file, hits]) => {
        const rows = hits
          .map(
            ({ lineno, body }) =>
              `<div class="chat-grep__hit"><span class="chat-grep__lineno">${escapeHtml(lineno)}</span>${escapeHtml(body)}</div>`,
          )
          .join("");
        return `<div class="chat-grep__group"><div class="chat-grep__file">${escapeHtml(file)}</div>${rows}</div>`;
      })
      .join("");
    return `<div class="chat-grep">${html}</div>`;
  };

  const entriesHTML = (text) => {
    const entries = parseLsEntries(text);
    return textHTML(entries.length ? entries.join("\n") : text);
  };

  const bashRunHTML = (command, output, exitCode) => {
    const cmdLine = command
      ? `<div class="chat-bash__cmd"><span class="chat-bash__prompt">$</span> ${escapeHtml(command)}</div>`
      : "";
    const outBlock = output ? textHTML(output, "chat-tool__code--bash") : "";
    const exitLine =
      exitCode != null && Number(exitCode) !== 0
        ? `<div class="chat-bash__exit chat-bash__exit--err">exit ${Number(exitCode)}</div>`
        : "";
    return `<div class="chat-bash">${cmdLine}${outBlock}${exitLine}</div>`;
  };

  const bashHTML = (text) => {
    const entries = parseBashResult(text);
    if (!entries) return textHTML(text, "chat-tool__code--bash");
    return entries.map((e) => bashRunHTML(e.command ?? "", e.output ?? "", e.exit_code)).join("");
  };

  // Open in a new tab so we never replace the chat tab; rel=noopener for the
  // usual reverse-tabnabbing reasons.
  const externalLink = (url, label) => {
    const text = escapeHtml(label != null ? String(label) : String(url));
    if (!/^https?:\/\//i.test(String(url))) return text;
    return `<a class="chat-tool__link" href="${escapeAttr(url)}" target="_blank" rel="noopener noreferrer">${text}</a>`;
  };

  const linkLine = (url) => `<div class="chat-webfetch__url">${externalLink(url, summarizeUrl(url))}</div>`;

  const webFetchHTML = (text) => {
    const redirect = text.match(REDIRECT_TAG_RE);
    return redirect ? linkLine(redirect[1]) : markdownHTML(text);
  };

  const webSearchHTML = (text) => {
    const results = parseWebSearchResults(text);
    if (results == null) return textHTML(text);
    if (!results.length) return textHTML("No relevant results found for the given search query.");
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
    return `<div class="chat-search">${items}</div>`;
  };

  const BODY_BY_TOOL = {
    read_file: ({ result }) => contextSection(result, textHTML),
    write_file: ({ args, result }) => {
      const content = pickKey(args, ["content", "text"]);
      const path = pickKey(args, ["path", "file_path"]) ?? "";
      const changed =
        content && !isError(result) ? jsdiffHTML(path, "", String(content), 0) ?? textHTML(String(content)) : "";
      return section("body_changed", changed) + contextSection(result, textHTML);
    },
    edit_file: ({ args, result }) => {
      const oldStr = pickKey(args, ["old_string"]);
      const newStr = pickKey(args, ["new_string"]);
      const path = pickKey(args, ["path", "file_path"]) ?? "";
      // Partial streaming args would produce misleading hunks, so require both.
      const complete = oldStr != null && newStr != null && !isError(result);
      const changed = complete ? jsdiffHTML(path, String(oldStr), String(newStr), 2) : null;
      return section("body_changed", changed ?? "") + contextSection(result, textHTML);
    },
    grep: ({ result }) => contextSection(result, grepHTML),
    glob: ({ result }) => contextSection(result, entriesHTML),
    ls: ({ result }) => contextSection(result, entriesHTML),
    bash: ({ result }) => contextSection(result, bashHTML),
    skill: ({ result }) => contextSection(result, markdownHTML),
    task: ({ args, result }) => {
      const prompt = pickKey(args, ["prompt", "input", "task"]) ?? "";
      return section("body_input", prompt ? textHTML(String(prompt)) : "") + contextSection(result, markdownHTML);
    },
    web_fetch: ({ args, argsStr, result }) => {
      const url = pickKeyOrPartial(args, ["url"], argsStr) ?? "";
      const prompt = pickKeyOrPartial(args, ["prompt"], argsStr) ?? "";
      const input = (url ? linkLine(url) : "") + (prompt ? textHTML(prompt) : "");
      return section("body_input", input) + contextSection(result, webFetchHTML);
    },
    web_search: ({ result }) => contextSection(result, webSearchHTML),
    gitlab: ({ result }) => contextSection(result, textHTML),
    gh: ({ result }) => contextSection(result, textHTML),
  };

  const defaultBody = ({ result }) => contextSection(result);

  window.toolBodyHTML = (seg, ctx) => {
    if (!seg) return "";
    const name = String(seg.name ?? "");
    const input = { seg, ctx: ctx ?? {}, args: parseArgs(seg.args), argsStr: seg.args, result: seg.result };
    let body;
    try {
      body = (Object.hasOwn(BODY_BY_TOOL, name) ? BODY_BY_TOOL[name] : defaultBody)(input);
    } catch (err) {
      console.warn("chat-tool-renderers: body builder failed, falling back", err);
      body = contextSection(seg.result, textHTML);
    }
    const { wrong = wentWrong(seg), html = "" } = typeof body === "string" ? { html: body } : body ?? {};
    return (wrong ? wrongHTML(wrong) : "") + html + rawHTML(seg);
  };
})();
