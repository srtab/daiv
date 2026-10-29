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

  const COMMAND_CHARS = 80;
  const TASK_DETAIL_CHARS = 120;

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
        vars: { command: truncate(command.split("\n")[0], COMMAND_CHARS) },
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
      detail: truncate(pickKey(args, ["description", "title"]) ?? "", TASK_DETAIL_CHARS),
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

  const moreHTML = (count, html, summary = t("show_all", { count })) =>
    `<details class="body-more"><summary>${escapeHtml(summary)}</summary>${html}</details>`;

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

  const markdownHTML = (text, max = VISIBLE_LINES, summary = undefined) => {
    const { lines, total } = sliceText(text);
    const [head, tail] = splitMarkdown(lines, max);
    const more = tail.length ? moreHTML(lines.length, markdownBlock(tail.join("\n")), summary) : "";
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

  const tableMarkup = (headers, cells) => {
    const head = headers.map((h) => `<th>${escapeHtml(h)}</th>`).join("");
    const body = cells.map((row) => `<tr>${row.map((cell) => `<td>${cell}</td>`).join("")}</tr>`).join("");
    const table = `<table class="body-table"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
    return `<div class="body-table-wrap">${table}</div>`;
  };

  const tableHTML = (headers, rows) => tableMarkup(headers, rows.map((row) => row.map(escapeHtml)));

  const definitionsHTML = (items) => {
    const html = items.map(([term, value]) => `<dt>${escapeHtml(term)}</dt><dd>${value}</dd>`);
    return `<dl class="body-fields">${html.join("")}</dl>`;
  };

  const fieldsHTML = (pairs) => definitionsHTML(pairs.map(([key, value]) => [key, escapeHtml(value)]));

  const structuredDataHTML = (data) => {
    if (data.kind === "table") return tableHTML(data.headers, data.rows) + andMoreHTML(data.more);
    if (data.kind === "fields") return fieldsHTML(data.pairs) + andMoreHTML(data.more);
    return markdownHTML(data.text);
  };

  const structuredHTML = (text) => structuredDataHTML(structured(text));

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
      return { path: wrote[3].trim(), preview, bytes: Number(wrote[1]), lines: Number(wrote[2]), asked: true };
    }
    const evicted = source.match(EVICTED_RE);
    if (!evicted) return null;
    const rest = source.slice(evicted[0].length);
    const note = rest.match(PREVIEW_NOTE_RE);
    const preview = note ? rest.slice(note.index + note[0].length) : rest;
    return { path: evicted[1].trim(), preview: preview.replace(/^\n+/, "").trimEnd() };
  };

  const offloadHTML = ({ path, preview, bytes, lines, asked = false }) => {
    const size = bytes == null ? "" : ` <span class="body-size">${escapeHtml(formatSize(lines, bytes))}</span>`;
    const label = t(asked ? "saved_to_file" : "offloaded", { path });
    const notice = `<p class="body-offload">${escapeHtml(label)}${size}</p>`;
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

  const clampItems = (items, wrap, total = items.length) => {
    const kept = items.slice(0, LINE_CAP);
    const hidden = kept.slice(VISIBLE_LINES);
    const more = hidden.length ? moreHTML(kept.length, wrap(hidden)) : "";
    return wrap(kept.slice(0, VISIBLE_LINES)) + more + andMoreHTML(total - kept.length);
  };

  const notesHTML = (notes) => notes.map((note) => `<p class="body-note">${escapeHtml(note)}</p>`).join("");

  const succeeded = (seg) => seg.status !== "error" && seg.result != null && !isError(seg.result);

  // deepagents' read result: bracketed notices, then `@@ lines A-B … @@`, then the lines verbatim. Older
  // results numbered each line instead (`N  text`, `N.1` for a long line's continuation, or `cat -n`'s tab).
  const READ_HEADER_RE = /^@@ lines (\d+)-(\d+)\b.*@@$/;
  const READ_NOTICE_RE = /^\[.*\]$/;
  const GUTTER_START_RE = /^\s*\d+(?: {2}|\t)/;
  const GUTTER_RE = /^\s*(\d+(?:\.\d+)?)(?: {2}|\t|$)(.*)$/;

  const readRows = (lines) => {
    let start = 0;
    while (start < lines.length && READ_NOTICE_RE.test(lines[start])) start += 1;
    const header = lines[start]?.match(READ_HEADER_RE);
    if (header) {
      const [first, last] = [Number(header[1]), Number(header[2])];
      const rows = lines.slice(start + 1).map((code, i) => ({ ln: first + i <= last ? String(first + i) : "", code }));
      return { notes: lines.slice(0, start), skipped: start + 1, rows };
    }
    const gutter = GUTTER_START_RE.test(lines.find((line) => line.trim()) ?? "");
    const rows = lines.map((line) => {
      const numbered = gutter ? line.match(GUTTER_RE) : null;
      return numbered ? { ln: numbered[1], code: numbered[2] } : { ln: "", code: line };
    });
    return { notes: [], skipped: 0, rows };
  };

  const languageOf = (path) => {
    const name = basename(path).toLowerCase();
    const dot = name.lastIndexOf(".");
    return dot > 0 ? name.slice(dot + 1) : name;
  };

  // hljs spans can run across lines: each line closes the spans still open and the next reopens them.
  const splitHighlighted = (html) => {
    const lines = [];
    const open = [];
    let line = "";
    for (const [token] of html.matchAll(/<span[^>]*>|<\/span>|\n|<|[^<\n]+/g)) {
      if (token === "\n") {
        lines.push(line + "</span>".repeat(open.length));
        line = open.join("");
        continue;
      }
      if (token.startsWith("<span")) open.push(token);
      else if (token === "</span>") open.pop();
      line += token === "<" ? "&lt;" : token;
    }
    lines.push(line + "</span>".repeat(open.length));
    return lines;
  };

  const highlightLines = (codes, language) => {
    const hljs = window.hljs;
    if (!language || typeof hljs?.getLanguage !== "function" || !hljs.getLanguage(language)) return null;
    try {
      const lines = splitHighlighted(hljs.highlight(codes.join("\n"), { language, ignoreIllegals: true }).value);
      return lines.length === codes.length ? lines : null;
    } catch {
      return null;
    }
  };

  const codeHTML = (path) => (text) => {
    const { lines, total } = sliceText(text);
    const { notes, skipped, rows } = readRows(lines);
    const codes = rows.map((row) => row.code);
    const code = highlightLines(codes, languageOf(path)) ?? codes.map(escapeHtml);
    const html = rows.map((row, i) => (row.ln ? `<span class="ln">${escapeHtml(row.ln)}</span>` : "") + code[i]);
    return notesHTML(notes) + clampLines(html, VISIBLE_LINES, { total: total - skipped, cls: "body-code" });
  };

  const DIFF_CONTEXT = 2;
  const DIFF_KINDS = { "+": "diff-add", "-": "diff-del", " ": "diff-ctx" };
  const DIFF_GAP = "~";

  // Rows are unified-diff lines (sign + text); the sign is never "~", so that row marks a gap between changes.
  const diffRows = (oldStr, newStr) => {
    const rows = [];
    for (const part of window.Diff.diffLines(oldStr, newStr)) {
      if (!part.value) continue;
      const sign = part.added ? "+" : part.removed ? "-" : " ";
      for (const line of part.value.replace(/\n$/, "").split("\n")) rows.push(sign + line);
    }
    const near = rows.map(() => false);
    rows.forEach((row, i) => {
      if (row[0] === " ") return;
      const last = Math.min(rows.length - 1, i + DIFF_CONTEXT);
      for (let j = Math.max(0, i - DIFF_CONTEXT); j <= last; j += 1) near[j] = true;
    });
    const kept = [];
    rows.forEach((row, i) => {
      if (!near[i]) return;
      if (kept.length && !near[i - 1]) kept.push(DIFF_GAP);
      kept.push(row);
    });
    return kept;
  };

  const diffHTML = (rows) => {
    if (!rows.length) return "";
    const { lines, total } = sliceText(rows.join("\n"));
    const html = lines.map((row) => {
      if (row === DIFF_GAP) return '<div class="diff-gap">⋯</div>';
      return `<div class="${DIFF_KINDS[row[0]]}">${escapeHtml(row)}</div>`;
    });
    return clampItems(html, (part) => `<div class="chat-diff">${part.join("")}</div>`, total);
  };

  const GREP_INLINE_RE = /^([^:\s][^:]*):(\d+):(.*)$/;
  const GREP_FILE_RE = /^([^\s][^:]*):$/;
  const GREP_HIT_RE = /^\s+(\d+)([:-])\s?(.*)$/;
  const GREP_SEPARATOR_RE = /^\s+--$/;

  // Ripgrep's `file:line:text`, or deepagents' `file:` followed by `  line: text` (`line- text` for context).
  const parseGrep = (lines) => {
    const hits = [];
    const before = [];
    const after = [];
    let file = null;
    for (const line of lines) {
      const inline = line.match(GREP_INLINE_RE);
      const header = inline ? null : line.match(GREP_FILE_RE);
      const hit = inline || header || file == null ? null : line.match(GREP_HIT_RE);
      if (inline) hits.push({ file: inline[1], lineno: inline[2], text: inline[3], match: true });
      else if (header) file = header[1];
      else if (hit) hits.push({ file, lineno: hit[1], text: hit[3], match: hit[2] === ":" });
      else if (line.trim() && !GREP_SEPARATOR_RE.test(line)) (hits.length || file != null ? after : before).push(line);
    }
    return { hits, before, after };
  };

  const MARK_CHARS = 300;
  const QUANTIFIERS = new Set(["*", "+", "{"]);

  // A repeated group that holds a quantifier, as in `(\w+\s?)+`, backtracks exponentially in JS (ERE does not).
  const hasNestedQuantifier = (pattern) => {
    const quantified = [false];
    let opened = false;
    for (let i = 0; i < pattern.length; i += 1) {
      const ch = pattern[i];
      const quantifier = QUANTIFIERS.has(ch) || (ch === "?" && !opened);
      opened = ch === "(";
      if (ch === "\\") {
        i += 1;
      } else if (ch === "[") {
        while (i + 1 < pattern.length && pattern[i + 1] !== "]") i += pattern[i + 1] === "\\" ? 2 : 1;
        i += 1;
      } else if (ch === "(") {
        quantified.push(false);
      } else if (ch === ")" && quantified.length > 1) {
        const inner = quantified.pop();
        const repeated = QUANTIFIERS.has(pattern[i + 1]);
        if (inner && repeated) return true;
        if (inner || repeated) quantified[quantified.length - 1] = true;
      } else if (quantifier) {
        quantified[quantified.length - 1] = true;
      }
    }
    return false;
  };

  // `u` makes patterns JS would read differently from ERE (`\K`, `[[:alpha:]]`) throw instead of mismatching.
  const patternRE = (pattern) => {
    if (!pattern || hasNestedQuantifier(String(pattern))) return null;
    try {
      return new RegExp(pattern, "gu");
    } catch {
      return null;
    }
  };

  const markHTML = (text, re) => {
    if (!re) return escapeHtml(text);
    let html = "";
    let last = 0;
    for (const match of text.slice(0, MARK_CHARS).matchAll(re)) {
      if (!match[0]) continue;
      html += `${escapeHtml(text.slice(last, match.index))}<mark>${escapeHtml(match[0])}</mark>`;
      last = match.index + match[0].length;
    }
    return html + escapeHtml(text.slice(last));
  };

  const grepGroupsHTML = (re) => (hits) => {
    const groups = [];
    for (const hit of hits) {
      if (groups.at(-1)?.file !== hit.file) groups.push({ file: hit.file, rows: [] });
      const lineno = `<span class="ln">${escapeHtml(hit.lineno)}</span>`;
      const text = hit.match ? markHTML(hit.text, re) : escapeHtml(hit.text);
      groups.at(-1).rows.push(`<div class="body-grep-hit">${lineno}${text}</div>`);
    }
    const html = groups.map(({ file, rows }) => {
      const name = `<div class="body-grep-file">${escapeHtml(file)}</div>`;
      return `<div class="body-grep-group">${name}${rows.join("")}</div>`;
    });
    return `<div class="body-grep">${html.join("")}</div>`;
  };

  // `total` counts the hits of the whole result; the view parses only the lines sliceText keeps.
  const grepHTML = (pattern, total) => (text) => {
    const { hits, before, after } = parseGrep(sliceText(text).lines);
    if (!hits.length) return textHTML(text);
    return notesHTML(before) + clampItems(hits, grepGroupsHTML(patternRE(pattern)), total) + notesHTML(after);
  };

  // deepagents lists paths as a Python list repr (notes may follow it) and appends this item when it cut the list.
  const CUT_LIST_RE = /^\.\.\. \[results truncated\b/;

  const pathsHTML = (text) => {
    const source = String(text).trim();
    const [first, ...rest] = source.split("\n");
    if (first === "No files found") return textHTML(source);
    const repr = first.startsWith("[") && first.endsWith("]");
    const entries = parseLsEntries(repr ? first : source);
    const paths = entries.filter((entry) => !CUT_LIST_RE.test(entry));
    if (!paths.length) return textHTML(source);
    const notes = [...entries.filter((entry) => CUT_LIST_RE.test(entry)), ...(repr ? rest : [])];
    const icon = window.TranscriptLabels.iconHTML("document-text");
    const items = paths
      .slice(0, LINE_CAP)
      .map((path) => `<li>${icon}<span>${escapeHtml(truncate(repoRelative(path) || path, LINE_CHARS))}</span></li>`);
    const list = clampItems(items, (part) => `<ul class="body-files">${part.join("")}</ul>`, paths.length);
    return list + notesHTML(notes.filter((note) => note.trim()).slice(0, VISIBLE_LINES));
  };

  const FILE_OPS = { added: "A", modified: "M", deleted: "D", renamed: "R" };

  const changesHTML = (files) => {
    const items = files
      .slice(0, LINE_CAP)
      .filter(isRecord)
      .map((file) => {
        const path = String(file.path ?? "");
        const shown = file.op === "renamed" && file.from_path ? `${file.from_path} → ${path}` : path;
        const op = Object.hasOwn(FILE_OPS, file.op) ? FILE_OPS[file.op] : "M";
        return `<li><span class="body-op">${op}</span><span>${escapeHtml(shown)}</span></li>`;
      });
    if (!items.length) return "";
    return clampItems(items, (part) => `<ul class="body-files">${part.join("")}</ul>`, files.length);
  };

  // Shown as Input only when the row cut it: several lines, or longer than the row's COMMAND_CHARS.
  const commandHTML = (command) => {
    const { lines, total } = sliceText(command);
    const html = lines.map(escapeHtml);
    html[0] = `<span class="body-prompt">$</span> ${html[0]}`;
    return clampLines(html, VISIBLE_LINES, { total, cls: "body-term" });
  };

  const exitCode = (command) =>
    command.exit_code != null && Number(command.exit_code) !== 0 ? Number(command.exit_code) : null;

  // One command: the row already shows it and its exit code. Several: each gets its `$ command` line.
  const terminalHTML = (commands) => {
    if (commands.length === 1) return commands[0].output ? textHTML(String(commands[0].output), "body-term") : "";
    return commands
      .map((command) => {
        const line = escapeHtml(command.command ?? "");
        const prompt = `<div class="body-cmd"><span class="body-prompt">$</span> ${line}</div>`;
        const output = command.output ? textHTML(String(command.output), "body-term") : "";
        const code = exitCode(command);
        const exit = code == null ? "" : `<div class="body-exit">${escapeHtml(t("detail_exit", { code }))}</div>`;
        return `<div class="body-run">${prompt}${output}${exit}</div>`;
      })
      .join("");
  };

  const WEB_LINK_RE = /^https?:\/\//i;
  const LOCAL_LINK_RE = /^\/(?![/\\])[^\s\\\x00-\x1f]*$/;

  const linkable = (href, local) => WEB_LINK_RE.test(href) || (local && LOCAL_LINK_RE.test(href));

  // Open in a new tab so we never replace the chat tab; rel=noopener for the
  // usual reverse-tabnabbing reasons.
  const externalLink = (url, label, { local = false } = {}) => {
    const href = String(url ?? "");
    const text = escapeHtml(label != null ? String(label) : href);
    if (!linkable(href, local)) return text;
    const attrs = `class="chat-tool__link" href="${escapeAttr(href)}" target="_blank" rel="noopener noreferrer"`;
    return `<a ${attrs}>${text}</a>`;
  };

  const openHTML = (url, label, { local = false } = {}) =>
    linkable(String(url ?? ""), local) ? `<p class="body-open">${externalLink(url, label, { local })}</p>` : "";

  const noteHTML = (key) => `<p class="body-note">${escapeHtml(t(key))}</p>`;

  const webLink = (url) => externalLink(url, truncate(url, VALUE_CHARS));

  // Not the model's answer: no prompt (or no model) returns the page itself, a failed model call returns it too.
  const PAGE_DUMP_RE = /^(?:Contents of \S+:\n|Model processing failed \()/;

  const webFetchSize = (text) => {
    const summary = text.trim() && !PAGE_DUMP_RE.test(text) && !parseOffload(text);
    return summary ? escapeHtml(`${t("web_summary")} · ${contextSize(text)}`) : null;
  };

  const webSearchHTML = (text) => {
    const results = parseWebSearchResults(text);
    if (results == null) return textHTML(text);
    const items = results.slice(0, LINE_CAP).map((r) => {
      const link = (r && r.link) || "";
      const title = (r && r.title) || "";
      const content = truncate((r && r.content) || "", VALUE_CHARS);
      const answer = escapeHtml(title || "Suggested answer");
      const titleHtml = link
        ? externalLink(link, title || link)
        : `<span class="chat-search__title chat-search__title--answer">${answer}</span>`;
      const host = link ? `<div class="chat-search__host">${escapeHtml(summarizeUrl(link))}</div>` : "";
      const body = content ? `<div class="chat-search__snippet">${escapeHtml(content)}</div>` : "";
      return `<div class="chat-search__item">${titleHtml}${host}${body}</div>`;
    });
    if (!items.length) return "";
    return clampItems(items, (part) => `<div class="chat-search">${part.join("")}</div>`, results.length);
  };

  // shlex.split's POSIX rules, as the tool parses the subcommand: inside "…" a backslash escapes only `"` and `\`.
  const shellWords = (text) => {
    const source = String(text ?? "");
    const words = [];
    let word = null;
    let quote = "";
    for (let i = 0; i < source.length; i += 1) {
      const ch = source[i];
      if (quote === "'") {
        if (ch === "'") quote = "";
        else word += ch;
      } else if (quote === '"') {
        if (ch === '"') quote = "";
        else if (ch === "\\" && (source[i + 1] === '"' || source[i + 1] === "\\")) {
          i += 1;
          word += source[i];
        } else word += ch;
      } else if (/\s/.test(ch)) {
        if (word != null) words.push(word);
        word = null;
      } else {
        word ??= "";
        if (ch === "'" || ch === '"') quote = ch;
        else if (ch === "\\" && i + 1 < source.length) {
          i += 1;
          word += source[i];
        } else word += ch;
      }
    }
    if (word != null) words.push(word);
    return words;
  };

  const flagValue = (words, names) => {
    for (let i = 0; i < words.length; i += 1) {
      for (const name of names) {
        if (words[i] === name) return words[i + 1] ?? null;
        if (words[i].startsWith(`${name}=`)) return words[i].slice(name.length + 1);
      }
    }
    return null;
  };

  const platformCall = (args, argsStr) => {
    const words = shellWords(pickKeyOrPartial(args, ["subcommand"], argsStr) ?? "");
    return { resource: words[0] ?? "", action: words[1] ?? "", rest: words.slice(2) };
  };

  const isPlatformLog = (platform, { resource, action, rest }) =>
    platform === "gitlab"
      ? resource === "project-job" && action === "trace"
      : resource === "run" && action === "view" && rest.some((word) => word === "--log" || word === "--log-failed");

  // `error:` ends in a non-word character, so a closing \b would make it miss `error: x`.
  const LOG_ERROR_RE = /\b(?:FAILED|ERROR|Error)\b|\berror:/;

  const sliceTail = (text) => {
    const all = String(text ?? "").replace(/\n$/, "").split("\n");
    const lines = [];
    let budget = CHAR_CAP;
    for (let i = all.length - 1; i >= 0; i -= 1) {
      const line = all[i];
      const kept = line.length > LINE_CHARS ? `${line.slice(0, LINE_CHARS)}…` : line;
      if (lines.length === LINE_CAP || kept.length > budget) break;
      lines.push(kept);
      budget -= kept.length + 1;
    }
    return { lines: lines.reverse(), total: all.length };
  };

  const errorAnchor = (seg) => {
    const id = String(seg.id ?? "").replace(/[^A-Za-z0-9_-]/g, "");
    return id ? `log-err-${id}` : "";
  };

  // A log keeps its end; .body-log is a column-reverse scroller, so it opens scrolled to the last line.
  const logHTML = (anchor) => (text) => {
    const { lines, total } = sliceTail(text);
    let first = -1;
    const rows = lines.map((line, i) => {
      const error = LOG_ERROR_RE.test(line);
      if (error && first < 0) first = i;
      const id = error && i === first && anchor ? ` id="${anchor}"` : "";
      return `<div class="lgl${error ? " err" : ""}"${id}>${escapeHtml(line)}</div>`;
    });
    const jump =
      first >= 0 && anchor ? `<a class="body-jump" href="#${anchor}">${escapeHtml(t("jump_to_error"))}</a>` : "";
    const earlier = total - lines.length;
    const omitted = earlier > 0 ? `<p class="body-note">${escapeHtml(tn("log_earlier", earlier))}</p>` : "";
    return `${jump}${omitted}<div class="body-log"><div class="body-log-lines">${rows.join("")}</div></div>`;
  };

  const FIELD_RE = /^([a-z_][a-z0-9_-]*):(?:[ \t]+(.*))?$/;
  const CHILD_RE = /^(\s+)([a-z_][a-z0-9_-]*):(?:[ \t]+(.*))?$/;

  // python-gitlab prints `key: value` lines, a nested object as `key:` over indented lines, and a blank line between
  // objects (a value may itself run over several lines); gh prints `key:\tvalue` lines, then `--` and the body.
  const parseRecords = (lines, { body = false } = {}) => {
    const records = [];
    let field = null;
    let blank = false;
    for (let i = 0; i < lines.length; i += 1) {
      const line = lines[i];
      if (body && records.length && line === "--") return { records, body: lines.slice(i + 1).join("\n") };
      if (!line.trim()) {
        blank = true;
        continue;
      }
      const top = line.match(FIELD_RE);
      const child = top || !field?.nested ? null : line.match(CHILD_RE);
      if (top) {
        if (!records.length || (blank && top[1] === records[0][0].key)) records.push([]);
        field = { key: top[1], value: top[2] ?? "", nested: top[2] == null, children: [], indent: null };
        records.at(-1).push(field);
      } else if (child) {
        field.indent ??= child[1].length;
        if (child[1].length === field.indent) field.children.push([child[2], child[3] ?? ""]);
      } else if (!field) {
        return null;
      } else if (!field.nested) {
        field.value += `${blank ? "\n\n" : "\n"}${line}`;
      }
      blank = false;
    }
    return records.length ? { records, body: "" } : null;
  };

  const FIRST_KEYS = [
    "title", "state", "draft", "source_branch", "target_branch", "author", "status", "ref", "web_url",
  ];
  const LINK_KEYS = ["web_url", "url"];
  const NESTED_KEYS = ["username", "name", "title", "status"];

  const plainKey = (key) => key.replaceAll("-", "_");

  const keyRank = (key) => {
    const rank = FIRST_KEYS.indexOf(plainKey(key));
    return rank < 0 ? FIRST_KEYS.length : rank;
  };

  const byRank = (keys) => [...keys].sort((a, b) => keyRank(a) - keyRank(b));

  const fieldText = (field) => {
    if (!field.nested) return truncate(field.value, VALUE_CHARS);
    const children = new Map(field.children.map(([key, value]) => [plainKey(key), value]));
    const named = NESTED_KEYS.find((key) => children.get(key));
    const text = named ? children.get(named) : field.children.map((pair) => pair.join(": ")).join(", ");
    return truncate(text, VALUE_CHARS);
  };

  const recordLink = (record) => {
    for (const key of LINK_KEYS) {
      const field = record.find((one) => plainKey(one.key) === key && !one.nested);
      if (field && WEB_LINK_RE.test(field.value.trim())) return field;
    }
    return null;
  };

  // The link field becomes the link on the record's first shown field, unless it is the only field.
  const recordRow = (record) => {
    const link = recordLink(record);
    const fields = new Map(record.filter((one) => one !== link || record.length === 1).map((one) => [one.key, one]));
    return { fields, href: link ? link.value.trim() : "" };
  };

  const cellHTML = (field, href, first) => {
    const text = field ? fieldText(field) : "";
    return first && href ? externalLink(href, text) : escapeHtml(text);
  };

  const recordsHTML = (records) => {
    const rows = records.slice(0, STRUCTURED_ROWS).map(recordRow);
    if (rows.length === 1) {
      const { fields, href } = rows[0];
      const items = byRank(fields.keys()).map((key, i) => [key, cellHTML(fields.get(key), href, i === 0)]);
      return definitionsHTML(items);
    }
    const headers = byRank(new Set(rows.flatMap(({ fields }) => [...fields.keys()]))).slice(0, TABLE_COLUMNS);
    const cells = rows.map(({ fields, href }) => headers.map((key, i) => cellHTML(fields.get(key), href, i === 0)));
    return tableMarkup(headers, cells) + andMoreHTML(records.length - rows.length);
  };

  const platformHTML = (platform) => (text) => {
    const data = structured(text);
    if (data.kind !== "markdown") return structuredDataHTML(data);
    const { lines, total } = sliceText(text);
    const parsed = parseRecords(lines, { body: platform === "gh" });
    if (!parsed) return textHTML(text);
    const body = parsed.body.trim() ? markdownHTML(parsed.body) : "";
    return recordsHTML(parsed.records) + body + andMoreHTML(total - lines.length);
  };

  const READ_ACTIONS = new Set(["get", "list", "trace", "diff", "view"]);
  const POSTED_FLAGS = ["--body", "-b", "--description"];
  const GITLAB_NOTES = new Set(["project-merge-request-note", "project-merge-request-discussion-note"]);
  const GH_VALUE_FLAGS = new Set(["--body", "-b", "--body-file", "-F"]);

  const prNumber = (rest) => {
    for (let i = 0; i < rest.length; i += 1) {
      if (GH_VALUE_FLAGS.has(rest[i])) i += 1;
      else if (!rest[i].startsWith("-")) return rest[i].match(/^#?(\d+)$|\/pull\/(\d+)\/?$/)?.slice(1).find(Boolean);
    }
    return undefined;
  };

  // python-gitlab prints a created note with `id:` first and an updated one as sorted keys; nested ids are indented.
  const resultId = (result) => String(result ?? "").match(/^id: (\d+)[ \t]*$/m)?.[1];

  // The posted note's anchor on the session's MR/PR page, only when this call wrote a note to that MR/PR.
  const postedUrl = (platform, { resource, action, rest }, mergeRequestUrl, result) => {
    const url = String(mergeRequestUrl ?? "");
    if (platform === "gitlab") {
      const number = url.match(/\/merge_requests\/(\d+)\/?$/)?.[1];
      const id = resultId(result);
      const ours = number && GITLAB_NOTES.has(resource) && flagValue(rest, ["--mr-iid"]) === number;
      return ours && id ? `${url}#note_${id}` : "";
    }
    const number = url.match(/\/pull\/(\d+)\/?$/)?.[1];
    const id = String(result ?? "").match(/#issuecomment-(\d+)\b/)?.[1] ?? resultId(result);
    const ours = number && resource === "pr" && action === "comment" && prNumber(rest) === number;
    return ours && id ? `${url}#issuecomment-${id}` : "";
  };

  // Reads show what came back; any other action wrote something, shown as Changed once it succeeded.
  const platformBody = (platform) => ({ seg, ctx, args, argsStr, result }) => {
    const call = platformCall(args, argsStr);
    if (isPlatformLog(platform, call)) return contextSection(result, logHTML(errorAnchor(seg)));
    const reply = contextSection(result, platformHTML(platform));
    if (READ_ACTIONS.has(call.action)) return reply;
    const text = flagValue(call.rest, POSTED_FLAGS);
    const done = succeeded(seg);
    const link = done ? openHTML(postedUrl(platform, call, ctx.mergeRequestUrl, result), t("open_link")) : "";
    const posted = (text?.trim() ? markdownHTML(text) : "") + link;
    return section(done ? "body_changed" : "body_input", posted) + reply;
  };

  const PROMPT_LINES = 3;

  const BODY_BY_TOOL = {
    read_file: ({ args, argsStr, result }) => contextSection(result, codeHTML(toolPath(args, argsStr))),
    write_file: ({ seg, args, result }) => {
      const content = pickKey(args, ["content", "text"]);
      const lines = succeeded(seg) && content ? String(content).replace(/\n$/, "").split("\n") : [];
      const rows = lines.map((line) => `+${line}`);
      return section("body_changed", diffHTML(rows)) + contextSection(result, textHTML);
    },
    edit_file: ({ seg, args, result }) => {
      const oldStr = pickKey(args, ["old_string"]);
      const newStr = pickKey(args, ["new_string"]);
      const complete = succeeded(seg) && oldStr != null && newStr != null && Boolean(window.Diff?.diffLines);
      const changed = complete ? diffHTML(diffRows(String(oldStr), String(newStr))) : "";
      return section("body_changed", changed) + contextSection(result, textHTML);
    },
    grep: ({ args, result }) => {
      const total = parseGrep(String(result ?? "").split("\n")).hits.length;
      return contextSection(result, grepHTML(pickKey(args, ["pattern", "query", "regex"]), total));
    },
    glob: ({ result }) => contextSection(result, pathsHTML),
    ls: ({ result }) => contextSection(result, pathsHTML),
    bash: ({ args, argsStr, result }) => {
      const parsed = parseBashSuccess(result);
      const commands = parsed ? parsed.commands.filter(isRecord) : [];
      const command = String(pickKeyOrPartial(args, ["command", "cmd", "script"], argsStr) ?? "").trimEnd();
      const cut = commands.length < 2 && (command.includes("\n") || command.length > COMMAND_CHARS);
      const input = section("body_input", cut ? commandHTML(command) : "");
      if (!parsed) return input + contextSection(result, textHTML);
      const output = commands.map((one) => String(one.output ?? "")).filter(Boolean).join("\n");
      const size = output ? escapeHtml(contextSize(output)) : "";
      const context = contextSection(result, () => terminalHTML(commands), size);
      return section("body_changed", changesHTML(parsed.files_changed)) + input + context;
    },
    skill: ({ seg, args, result }) => {
      const name = pickKey(args, ["skill", "name"]) ?? "";
      const link = openHTML(seg.skill_url, t("open_in_skills", { name }), { local: true });
      return contextSection(result, () => noteHTML("skill_whole") + link);
    },
    task: ({ args, argsStr, result }) => {
      const description = String(pickKeyOrPartial(args, ["description"], argsStr) ?? "");
      const cut = description.includes("\n") || description.length > TASK_DETAIL_CHARS;
      const prompt = String(pickKey(args, ["prompt"]) ?? (cut ? description : "")).trim();
      const input = section("body_input", prompt ? markdownHTML(prompt, PROMPT_LINES, t("show_prompt")) : "");
      return input + contextSection(result, markdownHTML) + noteHTML("subagent_hidden");
    },
    web_fetch: ({ args, argsStr, result }) => {
      const url = String(pickKeyOrPartial(args, ["url"], argsStr) ?? "");
      const prompt = String(pickKeyOrPartial(args, ["prompt"], argsStr) ?? "");
      const items = [];
      if (url) items.push([t("web_page"), webLink(url)]);
      if (prompt.trim()) items.push([t("web_asked"), markdownHTML(prompt)]);
      const input = section("body_input", items.length ? definitionsHTML(items) : "");
      const text = String(result ?? "");
      const redirect = text.match(REDIRECT_TAG_RE)?.[1].trim();
      if (!redirect) return input + contextSection(text, markdownHTML, webFetchSize(text));
      return input + contextSection(text, () => definitionsHTML([[t("redirected_to"), webLink(redirect)]]));
    },
    web_search: ({ result }) => contextSection(result, webSearchHTML),
    gitlab: platformBody("gitlab"),
    gh: platformBody("gh"),
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
