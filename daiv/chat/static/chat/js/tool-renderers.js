// Per-tool UI strategies. Exports on window:
//
//   toolRow(seg, ctx?)
//     -> one compact transcript row for a tool_call or thinking segment (see toolRow below)
//
//   toolBodyHTML(seg, ctx)
//     -> an opened row's body: What went wrong, the tool's own sections, then Raw (debug mode only);
//        ctx = { mcpRows, latestArtifactsById, mergeRequestUrl }
//
//   artifactItem(seg, latestById)
//     -> one publish_artifact row for the Artifacts card: { state: "running" | "published" | "error", ... },
//        showing the newest result `latestById` holds for its artifact id
//
//   delegatedItem(seg)
//     -> one delegate_jobs call for the Delegated jobs card: { legs, goal, error }; `legs` are the server's
//        `seg.legs` when present, else what the tool result says; `error` is set when the whole call failed
//
//   mergeRequestLabel(url)
//     -> "!N" / "#N" for a merge or pull request URL, "merge request" for another web URL, else ""
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

  const summarizeSubcommand = (subcommand) => {
    const [resource, action, ...rest] = shellWords(subcommand);
    if (!resource) return "";
    const head = action ? `${resource} ${action}` : resource;
    const name = rest.map((word) => word.split("=", 1)[0]).find((word) => CLI_ID_FLAGS.has(word));
    if (!name) return head;
    const value = flagValue(rest, [name]);
    return value ? `${head} ${name} ${value}` : `${head} ${name}`;
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

  const exitCode = (command) =>
    command.exit_code != null && Number(command.exit_code) !== 0 ? Number(command.exit_code) : null;

  const bashExit = (result) => {
    const commands = parseBashSuccess(result)?.commands;
    if (!commands) return { known: false, code: null };
    return { known: true, code: commands.map(exitCode).find((code) => code != null) ?? null };
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

  const jobsRow = (key, category) => ({ args }) => ({
    icon: "share",
    key,
    detail: String(pickKey(args, ["batch_id"]) ?? ""),
    category: { key: category },
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
    delegate_jobs: ({ result }) => {
      const started = recordsOf(jobsResult(result)?.started).length;
      const row = { icon: "share", key: "delegate", category: { key: "jobs" } };
      return started ? { ...row, phrase: tn("delegated_count", started) } : row;
    },
    check_delegated_jobs: jobsRow("check_jobs", "jobs"),
    cancel_delegated_jobs: jobsRow("cancel_jobs", "cancel_jobs"),
  };

  const mcpServerOf = (name, ctx) =>
    (ctx?.mcpRows ?? [])
      .filter((row) => row?.name && name.startsWith(`${row.name}_`))
      .sort((a, b) => b.name.length - a.name.length)[0] ?? null;

  // The server already keeps only these; an `<img>` never runs script, so any https or image data URI is safe.
  const ICON_SRC_RE = /^(?:https:\/\/|data:image\/)/i;

  const iconSrcOf = (row) => {
    const src = String(row?.icon ?? "");
    return ICON_SRC_RE.test(src) ? src : "";
  };

  const MCP_DETAIL_CHARS = 80;

  // The argument an MCP row shows: its first non-empty string or number, as `[key, value]`.
  const mcpDetailArg = (args) =>
    Object.entries(args).find(([, v]) => ["string", "number"].includes(typeof v) && String(v)) ?? null;

  const mcpRow = (name, { args }, ctx) => {
    const server = mcpServerOf(name, ctx);
    if (!server) {
      return { icon: "puzzle-piece", phrase: name, category: { key: "other" } };
    }
    const first = mcpDetailArg(args)?.[1];
    return {
      icon: "puzzle-piece",
      iconSrc: iconSrcOf(server),
      key: "mcp",
      vars: { server: server.name, tool: name.slice(server.name.length + 1).replaceAll("_", " ") },
      detail: first == null ? "" : truncate(String(first).replace(/\s+/g, " "), MCP_DETAIL_CHARS),
      category: { key: "mcp", name: server.name },
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

  // A sandbox write failure reads `Failed to write file '…': …`; every other file-tool failure starts with Error.
  const FAILURE_RE_BY_TOOL = new Map([["write_file", /^Failed to write file\b/]]);

  const failedResult = (name, result) => {
    const text = result == null ? "" : String(result).trim();
    return ERROR_PREFIX_RE.test(text) || Boolean(FAILURE_RE_BY_TOOL.get(name)?.test(text));
  };

  const rowFailed = (seg, part) => seg.status === "error" || Boolean(part.failed) || Boolean(wentWrong(seg));

  window.toolRow = (seg, ctx) => {
    if (seg.type === "thinking") return thinkingRow(seg);
    const T = window.TranscriptLabels;
    const name = String(seg.name ?? "");
    const input = { args: parseArgs(seg.args), argsStr: seg.args, result: seg.result, status: seg.status };
    const build = Object.hasOwn(ROW_BY_TOOL, name) ? ROW_BY_TOOL[name] : null;
    const part = build ? build(input) : mcpRow(name, input, ctx);
    const phrase = part.phrase ?? T.t(`row_${part.key}`, part.vars);
    const liveKey = `live_${part.key}`;
    const livePhrase = part.phrase ?? (liveKey in T.defaults ? T.t(liveKey, part.vars) : phrase);
    return {
      icon: part.icon,
      iconSrc: part.iconSrc ?? "",
      phrase,
      livePhrase,
      detail: part.detail ?? "",
      diff: part.diff ?? null,
      badges: part.badges ?? [],
      failed: rowFailed(seg, part),
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

  const kbSize = (bytes) => {
    const kb = bytes / 1024;
    return t("size_kb", { size: kb < 10 ? Math.max(kb, 0.1).toFixed(1) : String(Math.round(kb)) });
  };

  const formatSize = (lines, bytes) => `${tn("size_lines", lines)} · ${kbSize(bytes)}`;

  const contextSize = (text) => {
    const source = String(text ?? "");
    return source ? formatSize(countLines(source), byteLength(source)) : "";
  };

  // `fromEnd` keeps the last lines instead of the first.
  const sliceText = (text, { fromEnd = false } = {}) => {
    const all = String(text ?? "").replace(/\n$/, "").split("\n");
    const lines = [];
    let budget = CHAR_CAP;
    for (const line of fromEnd ? [...all].reverse() : all) {
      const kept = line.length > LINE_CHARS ? `${line.slice(0, LINE_CHARS)}…` : line;
      if (lines.length === LINE_CAP || kept.length > budget) break;
      lines.push(kept);
      budget -= kept.length + 1;
    }
    return { lines: fromEnd ? lines.reverse() : lines, total: all.length };
  };

  const andMoreHTML = (count) =>
    count > 0 ? `<p class="body-and-more">${escapeHtml(t("and_more", { count }))}</p>` : "";

  const moreHTML = (count, html, summary = t("show_all", { count })) =>
    `<details class="body-more"><summary>${escapeHtml(summary)}</summary>${html}</details>`;

  // `total` is the count before the caller sliced `items`: "Show all" counts the items it holds,
  // and the items never kept are counted in the "and N more" note.
  const clampItems = (items, wrap, total = items.length) => {
    const kept = items.slice(0, LINE_CAP);
    const hidden = kept.slice(VISIBLE_LINES);
    const more = hidden.length ? moreHTML(kept.length, wrap(hidden)) : "";
    return wrap(kept.slice(0, VISIBLE_LINES)) + more + andMoreHTML(total - kept.length);
  };

  const clampLines = (lines, { total = lines.length, cls = "" } = {}) =>
    clampItems(lines, (part) => `<pre class="body-pre${cls ? ` ${cls}` : ""}">${part.join("\n")}</pre>`, total);

  const textHTML = (text, cls = "") => {
    const { lines, total } = sliceText(text);
    return clampLines(lines.map(escapeHtml), { total, cls });
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

  const wholeValueText = (value) => {
    if (value === undefined) return "";
    return typeof value === "string" ? value : JSON.stringify(value);
  };

  const valueText = (value) => truncate(wholeValueText(value), VALUE_CHARS);

  const fits = (text) => text.length <= VALUE_CHARS;

  const parseJSON = (text) => {
    try {
      return JSON.parse(text);
    } catch {
      return undefined;
    }
  };

  const fieldsView = (record) => {
    const entries = Object.entries(record);
    const values = entries.slice(0, STRUCTURED_ROWS).map(([key, value]) => [key, wholeValueText(value)]);
    const more = entries.length - values.length;
    const whole = !more && values.every(([, value]) => fits(value));
    const pairs = values.map(([key, value]) => [key, truncate(value, VALUE_CHARS)]);
    return { kind: "fields", pairs, more, whole, data: record };
  };

  // `whole` is false when the view leaves something out: a column, a row or pair past the fiftieth, a cut value.
  const structured = (text) => {
    const source = String(text ?? "");
    const trimmed = source.trim();
    const parsed = /^[[{]/.test(trimmed) ? parseJSON(trimmed) : undefined;
    if (Array.isArray(parsed) && parsed.length && parsed.every(isRecord)) {
      const headers = Object.keys(parsed[0]).slice(0, TABLE_COLUMNS);
      if (headers.length) {
        const cells = parsed.slice(0, STRUCTURED_ROWS).map((item) => headers.map((key) => wholeValueText(item[key])));
        const more = parsed.length - cells.length;
        const columns = parsed.every((item) => Object.keys(item).every((key) => headers.includes(key)));
        const whole = columns && !more && cells.flat().every(fits);
        const rows = cells.map((row) => row.map((cell) => truncate(cell, VALUE_CHARS)));
        return { kind: "table", headers, rows, more, whole, data: parsed };
      }
    }
    if (isRecord(parsed) && Object.keys(parsed).length) return fieldsView(parsed);
    return { kind: "markdown", text: source, whole: true };
  };

  const tableMarkup = (headers, cells, rowClasses = []) => {
    const head = headers.map((h) => `<th>${escapeHtml(h)}</th>`).join("");
    const tr = (i) => (rowClasses[i] ? `<tr class="${rowClasses[i]}">` : "<tr>");
    const body = cells.map((row, i) => `${tr(i)}${row.map((cell) => `<td>${cell}</td>`).join("")}</tr>`).join("");
    const table = `<table class="body-table"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
    return `<div class="body-table-wrap">${table}</div>`;
  };

  const tableHTML = (headers, rows) => tableMarkup(headers, rows.map((row) => row.map(escapeHtml)));

  const definitionsHTML = (items) => {
    const html = items.map(([term, value]) => `<dt>${escapeHtml(term)}</dt><dd>${value}</dd>`);
    return `<dl class="body-fields">${html.join("")}</dl>`;
  };

  const fieldsHTML = (pairs) => definitionsHTML(pairs.map(([key, value]) => [key, escapeHtml(value)]));

  const showAllTextHTML = (text) => {
    const { lines, total } = sliceText(text);
    const full = `<pre class="body-pre">${lines.map(escapeHtml).join("\n")}</pre>`;
    return moreHTML(lines.length, full) + andMoreHTML(total - lines.length);
  };

  const structuredDataHTML = (data) => {
    const all = data.whole ? "" : showAllTextHTML(JSON.stringify(data.data, null, 2));
    if (data.kind === "table") return tableHTML(data.headers, data.rows) + andMoreHTML(data.more) + all;
    if (data.kind === "fields") return fieldsHTML(data.pairs) + andMoreHTML(data.more) + all;
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

  const section = (labelKey, html, sizeHTML = "", tagHTML = "") => {
    if (!html) return "";
    const size = sizeHTML ? ` <span class="body-size">${sizeHTML}</span>` : "";
    const label = `${escapeHtml(t(labelKey))}${tagHTML ? ` ${tagHTML}` : ""}${size}`;
    return `<div class="body-section"><div class="body-label">${label}</div>${html}</div>`;
  };

  // Every builder shows its result through here, so an evicted result gets the offload notice whatever the tool.
  // `size` is HTML: null works it out from the text, "" shows none.
  const resultSection = (labelKey, text, { render = structuredHTML, size = null, tag = "" } = {}) => {
    const source = String(text ?? "");
    if (!source.trim() || isError(source)) return "";
    const offload = parseOffload(source);
    const body = offload ? offloadHTML(offload) : render(source);
    return section(labelKey, body, size ?? escapeHtml(contextSize(source)), tag);
  };

  const contextSection = (text, render, size) => resultSection("body_context", text, { render, size });

  const wentWrong = (seg) => {
    const data = Object.hasOwn(JOBS_WRONG, seg.name) ? jobsResult(seg.result) : null;
    if (data) return JOBS_WRONG[seg.name](data, seg.result);
    const result = seg.result == null ? "" : String(seg.result).trim();
    if (failedResult(seg.name, result)) return result.replace(/^error:\s*/i, "") || result;
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

  // --- per-tool bodies (each returns its middle sections) ---

  const noteHTML = (text) => (text ? `<p class="body-note">${escapeHtml(text)}</p>` : "");

  const notesHTML = (notes) => notes.map(noteHTML).join("");

  const succeeded = (seg) => seg.status !== "error" && seg.result != null && !failedResult(seg.name, seg.result);

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
    return notesHTML(notes) + clampLines(html, { total: total - skipped, cls: "body-code" });
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
  const UNBOUNDED_RE = /[*+]|\{\d+,\}/y;
  const UNBOUNDED_LIMIT = 3;

  // JS backtracks exponentially (ERE does not) on a repeated group holding a quantifier or an alternation, as in
  // `(\w+\s?)+` or `(a|aa)*`, and polynomially on several unbounded quantifiers, as in `\s*\s*\s*y`.
  const backtracksBadly = (pattern) => {
    const quantified = [false];
    let opened = false;
    let unbounded = 0;
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
      } else if (ch === "|") {
        quantified[quantified.length - 1] = true;
      } else if (quantifier) {
        quantified[quantified.length - 1] = true;
        UNBOUNDED_RE.lastIndex = i;
        if (UNBOUNDED_RE.test(pattern)) unbounded += 1;
        if (unbounded >= UNBOUNDED_LIMIT) return true;
      }
    }
    return false;
  };

  // `u` makes patterns JS would read differently from ERE (`\K`, `[[:alpha:]]`) throw instead of mismatching.
  const patternRE = (pattern) => {
    if (!pattern || backtracksBadly(String(pattern))) return null;
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

  // The lines parseGrep reads as hits: ripgrep's `file:line:`, or an indented `line:` / `line-`.
  const GREP_HIT_LINE_RE = /^(?:[^:\s][^:\n]*:\d+:|[ \t]+\d+[:-])/gm;

  // The view parses only the lines sliceText keeps; the hits past them are counted, not parsed.
  const grepHTML = (pattern) => (text) => {
    const sliced = sliceText(text);
    const { hits, before, after } = parseGrep(sliced.lines);
    if (!hits.length) return textHTML(text);
    const cut = sliced.lines.length < sliced.total;
    const total = cut ? Math.max(hits.length, String(text).match(GREP_HIT_LINE_RE)?.length ?? 0) : hits.length;
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
    const records = files.filter(isRecord);
    const items = records
      .slice(0, LINE_CAP)
      .map((file) => {
        const path = String(file.path ?? "");
        const shown = file.op === "renamed" && file.from_path ? `${file.from_path} → ${path}` : path;
        const op = Object.hasOwn(FILE_OPS, file.op) ? FILE_OPS[file.op] : "M";
        return `<li><span class="body-op">${op}</span><span>${escapeHtml(shown)}</span></li>`;
      });
    if (!items.length) return "";
    return clampItems(items, (part) => `<ul class="body-files">${part.join("")}</ul>`, records.length);
  };

  // Shown as Input only when the row cut it: several lines, or longer than the row's COMMAND_CHARS.
  const commandHTML = (command) => {
    const { lines, total } = sliceText(command);
    const html = lines.map(escapeHtml);
    html[0] = `<span class="body-prompt">$</span> ${html[0]}`;
    return clampLines(html, { total, cls: "body-term" });
  };

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
  const externalLink = (url, label, { local = false, cls = "chat-tool__link" } = {}) => {
    const href = String(url ?? "");
    const text = escapeHtml(label != null ? String(label) : href);
    if (!linkable(href, local)) return text;
    const attrs = `class="${cls}" href="${escapeAttr(href)}" target="_blank" rel="noopener noreferrer"`;
    return `<a ${attrs}>${text}</a>`;
  };

  const openHTML = (url, label, { local = false } = {}) =>
    linkable(String(url ?? ""), local) ? `<p class="body-open">${externalLink(url, label, { local })}</p>` : "";

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
      const answer = escapeHtml(title || t("suggested_answer"));
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

  const errorAnchor = (seg) => {
    const id = String(seg.id ?? "").replace(/[^A-Za-z0-9_-]/g, "");
    return id ? `log-err-${id}` : "";
  };

  // A log keeps its end; .body-log is a column-reverse scroller, so it opens scrolled to the last line.
  const logHTML = (anchor) => (text) => {
    const { lines, total } = sliceText(text, { fromEnd: true });
    let first = -1;
    const rows = lines.map((line, i) => {
      const error = LOG_ERROR_RE.test(line);
      if (error && first < 0) first = i;
      const id = error && i === first && anchor ? ` id="${anchor}"` : "";
      return `<div class="lgl${error ? " err" : ""}"${id}>${escapeHtml(line)}</div>`;
    });
    const jumpLabel = escapeHtml(t("jump_to_error"));
    const jump = first >= 0 && anchor ? `<a class="body-jump chat-tool__link" href="#${anchor}">${jumpLabel}</a>` : "";
    const earlier = total - lines.length;
    const omitted = earlier > 0 ? noteHTML(tn("log_earlier", earlier)) : "";
    return `${jump}${omitted}<div class="body-log"><div class="body-log-lines">${rows.join("")}</div></div>`;
  };

  const FIELD_RE = /^([a-z_][a-z0-9_-]*):(?:[ \t]+(.*))?$/s;

  // python-gitlab: `key: value` lines (a value may run on), a nested `key:` over indented lines, a blank line between
  // objects; gh: `key:\tvalue` lines, `--`, then the body. A line no field takes fails the parse (null).
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
      const indent = line.length - line.trimStart().length;
      if (top) {
        if (!records.length || (blank && top[1] === records[0][0].key)) records.push([]);
        field = { key: top[1], value: top[2] ?? "", nested: top[2] == null, block: [], indent: 0 };
        records.at(-1).push(field);
      } else if (!field) {
        return null;
      } else if (!field.nested) {
        field.value += `${blank ? "\n\n" : "\n"}${line}`;
      } else if (indent > 0 && indent >= (field.indent || indent)) {
        field.indent ||= indent;
        field.block.push(line.slice(field.indent));
      } else {
        return null;
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

  // A nested object reads as its username (or name, …) or as its `key: value` children; `whole` is false when the
  // text shown leaves any of the field's lines or characters out.
  const fieldView = (field) => {
    if (!field.nested) return { text: truncate(field.value, VALUE_CHARS), whole: field.value.length <= VALUE_CHARS };
    const children = field.block.map((line) => line.match(FIELD_RE)).filter(Boolean);
    const named = NESTED_KEYS.map((key) => children.find((m) => plainKey(m[1]) === key && m[2])).find(Boolean);
    const text = named ? named[2] : children.map((m) => `${m[1]}: ${m[2] ?? ""}`).join(", ");
    const everyLine = named ? field.block.length === 1 : children.length === field.block.length;
    return { text: truncate(text, VALUE_CHARS), whole: everyLine && text.length <= VALUE_CHARS };
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
    const shown = record.filter((one) => one !== link || record.length === 1);
    const fields = new Map(shown.map((one) => [one.key, one]));
    return { fields, href: link ? link.value.trim() : "", complete: fields.size === shown.length };
  };

  const cellHTML = (view, href, first) =>
    first && href ? externalLink(href, view.text || href) : escapeHtml(view.text);

  const viewOf = (fields, key) => (fields.has(key) ? fieldView(fields.get(key)) : { text: "", whole: true });

  // `whole` is false when the view leaves a line out: a nested object's other lines, a repeated key, columns past the
  // sixth, rows past the fiftieth.
  const recordsHTML = (records) => {
    const rows = records.slice(0, STRUCTURED_ROWS).map(recordRow);
    const keys = byRank(new Set(rows.flatMap(({ fields }) => [...fields.keys()])));
    const headers = rows.length === 1 ? keys : keys.slice(0, TABLE_COLUMNS);
    const views = rows.map(({ fields }) => headers.map((key) => viewOf(fields, key)));
    const cells = views.map((row, r) => row.map((view, i) => cellHTML(view, rows[r].href, i === 0)));
    const html =
      rows.length === 1
        ? definitionsHTML(headers.map((key, i) => [key, cells[0][i]]))
        : tableMarkup(headers, cells) + andMoreHTML(records.length - rows.length);
    const complete = headers.length === keys.length && rows.length === records.length && rows.every((r) => r.complete);
    return { html, whole: complete && views.flat().every((view) => view.whole) };
  };

  const platformHTML = (platform) => (text) => {
    const data = structured(text);
    if (data.kind !== "markdown") return structuredDataHTML(data);
    const sliced = sliceText(text);
    const lines = sliced.lines.map((line) => line.replace(/\r$/, ""));
    const parsed = parseRecords(lines, { body: platform === "gh" });
    if (!parsed) return textHTML(text);
    const view = recordsHTML(parsed.records);
    const full = view.whole ? "" : `<pre class="body-pre">${lines.map(escapeHtml).join("\n")}</pre>`;
    const all = full && moreHTML(lines.length, full);
    const body = parsed.body.trim() ? markdownHTML(parsed.body) : "";
    return view.html + all + body + andMoreHTML(sliced.total - lines.length);
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

  const MR_URL_RE = /\/merge_requests\/(\d+)\/?(?:[?#]|$)/;
  const PR_URL_RE = /\/pull\/(\d+)\/?(?:[?#]|$)/;

  // The posted note's anchor on the session's MR/PR page, only when this call wrote a note to that MR/PR.
  const postedUrl = (platform, { resource, action, rest }, mergeRequestUrl, result) => {
    const url = String(mergeRequestUrl ?? "");
    if (platform === "gitlab") {
      const number = url.match(MR_URL_RE)?.[1];
      const id = resultId(result);
      const ours = number && GITLAB_NOTES.has(resource) && flagValue(rest, ["--mr-iid"]) === number;
      return ours && id ? `${url}#note_${id}` : "";
    }
    const number = url.match(PR_URL_RE)?.[1];
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

  // --- MCP tools ---------------------------------------------------------

  // `readOnly` is null when the server's tool data is unknown (never synced).
  const mcpTool = (name, ctx) => {
    const server = mcpServerOf(name, ctx);
    if (!server) return null;
    const known = server.synced !== false && Array.isArray(server.read_only_tools);
    return { readOnly: known ? server.read_only_tools.includes(name.slice(server.name.length + 1)) : null };
  };

  const tagHTML = (key, modifier) => `<span class="body-tag body-tag--${modifier}">${escapeHtml(t(key))}</span>`;

  // A value the field list cuts is whole in the arguments as sent, under Show all.
  const argsHTML = (args, argsStr) => {
    if (!Object.keys(args).length) return "";
    const view = fieldsView(args);
    const list = fieldsHTML(view.pairs) + andMoreHTML(view.more);
    return view.whole ? list : list + showAllTextHTML(prettyJSON(argsStr));
  };

  const URL_RE = /https?:\/\/[^\s<>"'`\\)\]]+/gi;
  const LINK_BUTTONS = 10;

  const linkButtonsHTML = (text) => {
    const urls = new Set();
    for (const match of text.slice(0, CHAR_CAP).matchAll(URL_RE)) urls.add(match[0].replace(/[.,;:!?]+$/, ""));
    const all = [...urls];
    const buttons = all
      .slice(0, LINK_BUTTONS)
      .map((url) => externalLink(url, `${truncate(summarizeUrl(url), 80)} ↗`, { cls: "body-button" }));
    if (!buttons.length) return "";
    return `<p class="body-buttons">${buttons.join("")}</p>${andMoreHTML(all.length - buttons.length)}`;
  };

  const mcpTag = (readOnly) => {
    if (readOnly == null) return "";
    return readOnly ? tagHTML("read_only", "read-only") : tagHTML("can_change", "can-change");
  };

  const rowShowsWhole = (value) => {
    const text = String(value);
    return text.length <= MCP_DETAIL_CHARS && text.replace(/\s+/g, " ") === text;
  };

  const changedHTML = (text) => structuredHTML(text) + linkButtonsHTML(text);

  // Input leaves out the argument the row shows whole; with nothing left, the tag goes on the result's label.
  const mcpBody = (readOnly) => ({ args, argsStr, result }) => {
    const shown = mcpDetailArg(args);
    const others = shown && rowShowsWhole(shown[1]) ? Object.entries(args).filter(([key]) => key !== shown[0]) : null;
    const fields = argsHTML(others ? Object.fromEntries(others) : args, argsStr);
    const tag = mcpTag(readOnly);
    const input = section("body_input", fields, "", tag);
    const resultTag = fields ? "" : tag;
    if (readOnly !== false) return input + resultSection("body_context", result, { tag: resultTag });
    return resultSection("body_changed", result, { render: changedHTML, tag: resultTag }) + input;
  };

  // --- tool_search -------------------------------------------------------

  // With schemas embedded: `<functions>` holding `<function>{openai schema}</function>` or
  // `<function-summary>name: summary</function-summary>`; without: `- name: summary` lines.
  const TOOL_BLOCK_RE = /<function>([\s\S]*?)<\/function>|<function-summary>([\s\S]*?)<\/function-summary>/g;
  const TOOL_LINE_RE = /^- (\S+?):(?: (.*))?$/;
  const TOOL_SUMMARY_RE = /^\s*([^:\s]+):\s?([\s\S]*)$/;
  const LOADED_HEADER_RE = /^Loaded \d+ tool\(s\)[.:]/;
  const FUNCTIONS_TAG_RE = /^<\/?functions>$/;
  const TOOL_CHIPS = 20;

  const firstSentence = (text) => {
    const line = String(text ?? "").trim().split("\n")[0];
    return line.match(/^.*?[.!?](?=\s|$)/)?.[0] ?? line;
  };

  const schemaEntry = (json) => {
    const parsed = parseJSON(json);
    const fn = isRecord(parsed?.function) ? parsed.function : parsed;
    if (!isRecord(fn) || typeof fn.name !== "string") return null;
    const properties = fn.parameters?.properties ?? fn.input_schema?.properties;
    const params = isRecord(properties) ? Object.keys(properties) : [];
    return { name: fn.name, about: firstSentence(fn.description), params };
  };

  const summaryEntry = (text) => {
    const match = String(text).match(TOOL_SUMMARY_RE);
    const [name, about] = match ? [match[1], match[2].trim()] : [String(text).trim(), ""];
    return { name, about, params: [] };
  };

  // null when the text lists no tool, or a schema does not parse: the result then shows as it came.
  const loadedTools = (text) => {
    const entries = [];
    let schemas = 0;
    let broken = false;
    const blocks = text.includes("<functions>");
    const rest = !blocks
      ? text
      : text.replace(TOOL_BLOCK_RE, (_, schema, summary) => {
          const entry = schema == null ? summaryEntry(summary) : schemaEntry(schema);
          if (schema != null) schemas += 1;
          if (entry) entries.push(entry);
          else broken = true;
          return "";
        });
    const notes = [];
    for (const line of rest.split("\n")) {
      const listed = blocks ? null : line.match(TOOL_LINE_RE);
      if (listed) entries.push({ name: listed[1], about: listed[2] ?? "", params: [] });
      else if (line.trim() && !LOADED_HEADER_RE.test(line) && !FUNCTIONS_TAG_RE.test(line.trim())) notes.push(line);
    }
    return broken || !entries.length ? null : { entries, schemas, notes };
  };

  const toolEntryHTML = ({ name, about, params }) => {
    const shown = params.slice(0, TOOL_CHIPS);
    const chips = shown.map((param) => `<span class="body-chip">${escapeHtml(param)}</span>`);
    if (params.length > shown.length) {
      const more = t("and_more", { count: params.length - shown.length });
      chips.push(`<span class="body-chip body-chip--more">${escapeHtml(more)}</span>`);
    }
    const aboutHTML = about ? `<span class="body-tool-about">${escapeHtml(truncate(about, VALUE_CHARS))}</span>` : "";
    const chipsHTML = chips.length ? `<span class="body-chips">${chips.join("")}</span>` : "";
    return `<li><code class="body-tool-name">${escapeHtml(name)}</code>${aboutHTML}${chipsHTML}</li>`;
  };

  const toolsHTML = ({ entries, notes }) => {
    const items = entries.slice(0, LINE_CAP).map(toolEntryHTML);
    const list = clampItems(items, (part) => `<ul class="body-tools">${part.join("")}</ul>`, entries.length);
    const shownNotes = notes.slice(0, VISIBLE_LINES).map((note) => truncate(note.trim(), LINE_CHARS));
    return list + notesHTML(shownNotes) + andMoreHTML(notes.length - shownNotes.length);
  };

  const toolSearchBody = ({ result }) => {
    const text = String(result ?? "");
    const loaded = parseOffload(text) ? null : loadedTools(text);
    const schemas = loaded?.schemas ? `${tn("size_tool_schemas", loaded.schemas)} · ${kbSize(byteLength(text))}` : "";
    const size = schemas ? escapeHtml(schemas) : null;
    return contextSection(text, loaded ? () => toolsHTML(loaded) : structuredHTML, size);
  };

  // --- artifacts ---------------------------------------------------------

  const artifactHTML = (item) => {
    const state = t(item.updated ? "artifact_updated" : "artifact_new");
    const meta = [item.kindLabel, item.sizeLabel, state].filter(Boolean);
    const metaHTML = meta.map((text) => `<span class="body-meta">${escapeHtml(text)}</span>`).join("");
    const link = externalLink(item.url, item.title || item.filename || item.url, { local: true });
    const download = item.download_url
      ? `<span class="body-download">${externalLink(item.download_url, t("artifact_download"), { local: true })}</span>`
      : "";
    return `<p class="body-artifact">${link}${metaHTML}${download}</p>`;
  };

  const publishedHTML = (text) => {
    const parsed = parseArtifactResult(text);
    if (!parsed) return structuredHTML(text);
    const status = escapeHtml(valueText(parsed.status));
    const line = [status, externalLink(parsed.url, null, { local: true })].filter(Boolean).join(" · ");
    const warning = parsed.warning ? noteHTML(valueText(parsed.warning)) : "";
    return `<p class="body-line">${line}</p>${warning}`;
  };

  // "Fetched artifact '<id>' (<filename>, <bytes> bytes) to <path>. Edit it, then call …"
  const FETCHED_RE = /^Fetched artifact '([^'\n]*)' \(([^\n]*), (\d+) bytes\) to ([^\n]+?)\.(?: |$)/;

  const fetchedHTML = (text, latestById) => {
    const match = text.match(FETCHED_RE);
    if (!match) return "";
    const [, id, filename, bytes, path] = match;
    const known = latestById?.get?.(id);
    const parts = [`<span class="body-op">A</span><span>${escapeHtml(path)}</span>`];
    parts.push(`<span class="body-size">${escapeHtml(formatBytes(bytes))}</span>`);
    if (known) parts.push(externalLink(known.url, known.title || known.filename || filename, { local: true }));
    return `<ul class="body-files"><li>${parts.join(" · ")}</li></ul>`;
  };

  // --- delegated jobs ----------------------------------------------------

  const SLUG_RE = /^[A-Za-z0-9_-]+$/;

  const jobsResult = (result) => {
    const parsed = parseJSON(String(result ?? "").trim());
    return isRecord(parsed) ? parsed : null;
  };

  const recordsOf = (value) => (Array.isArray(value) ? value.filter(isRecord) : []);

  const jobsError = (data, result) => valueText(data.error).trim() || String(result).trim();

  const jobsFailure = (data, result) => (data.error != null ? jobsError(data, result) : "");

  const firstLine = (value) => valueText(value).trim().split("\n")[0];

  const branchText = (ref) => valueText(ref).trim() || t("jobs_default_branch");

  const delegateWrong = (data, result) => {
    if (data.error != null) return jobsError(data, result);
    if (recordsOf(data.started).length) return "";
    const reason = (leg) => `${valueText(leg.repo_id)} · ${branchText(leg.ref)}: ${valueText(leg.error)}`;
    return recordsOf(data.failed).map(reason).join("\n") || t("jobs_none_started");
  };

  // What went wrong, as a jobs tool's JSON result tells it; "" when nothing did.
  const JOBS_WRONG = {
    delegate_jobs: delegateWrong,
    check_delegated_jobs: jobsFailure,
    cancel_delegated_jobs: jobsFailure,
  };

  // The page's own session URL for a thread id; the absolute `session_url` carries the configured site's host.
  const sessionHref = (leg) => {
    const threadId = valueText(leg.thread_id);
    const path = window.TranscriptLabels.sessionPath;
    const own = path && SLUG_RE.test(threadId) ? window.TranscriptLabels.fill(path, { thread_id: threadId }) : "";
    const href = own || valueText(leg.session_url);
    return linkable(href, true) ? href : "";
  };

  const sessionLink = (leg) => {
    const href = sessionHref(leg);
    return href ? externalLink(href, t("jobs_open_session"), { local: true }) : "";
  };

  const wordLabel = (word) => {
    const states = window.TranscriptLabels.legStates;
    return Object.hasOwn(states, word) ? t(`leg_${states[word]}`) : word;
  };

  const legText = (leg, key) => valueText(leg[key]).trim();

  const legKey = (leg) => `${legText(leg, "repo_id")}\n${legText(leg, "ref")}`;

  const claimIndex = (list, used, same) => {
    const index = list.findIndex((item, i) => !used.has(i) && same(item));
    if (index >= 0) used.add(index);
    return index;
  };

  const cardLeg = (leg, state, error = "") => ({
    repo_id: legText(leg, "repo_id"),
    ref: legText(leg, "ref"),
    state,
    merge_request_url: legText(leg, "merge_request_url"),
    session_url: legText(leg, "session_url"),
    error,
  });

  const LOST_OR_FAILED = new Set(["failed", "lost"]);

  const resultLegs = (data) => [
    ...recordsOf(data?.started).map((leg) => ({
      ...cardLeg(leg, leg.starts_after_this_turn === true ? "waits" : "queued"),
      session_url: sessionHref(leg),
    })),
    ...recordsOf(data?.failed).map((leg) =>
      cardLeg(leg, leg.still_runs === true ? "lost" : "failed", legText(leg, "error")),
    ),
  ];

  // A leg that failed to start has a row only when DAIV made one; one that still runs may have moved on since.
  const mergedLegs = (enriched, data) => {
    const legs = enriched.filter(isRecord).map((leg) => cardLeg(leg, legText(leg, "state")));
    const claimed = new Set();
    for (const failed of recordsOf(data?.failed)) {
      const error = legText(failed, "error");
      const mine = (leg) => legKey(leg) === legKey(failed);
      const index = claimIndex(legs, claimed, (leg) => mine(leg) && LOST_OR_FAILED.has(leg.state));
      if (index >= 0) legs[index].error = error;
      else if (failed.still_runs !== true || claimIndex(legs, claimed, mine) < 0) {
        legs.push(cardLeg(failed, failed.still_runs === true ? "lost" : "failed", error));
      }
    }
    return legs;
  };

  window.delegatedItem = (seg) => {
    const goal = valueText(parseArgs(seg.args).goal).trim();
    const text = seg.result == null ? "" : String(seg.result).trim();
    const data = jobsResult(text);
    let error = "";
    if (data) {
      const noBatch = data.batch_id == null && !recordsOf(data.started).length;
      error = data.error != null || noBatch ? delegateWrong(data, text) : "";
    } else if (text && text !== "null") {
      error = text.replace(/^error:\s*/i, "") || text;
    } else if (!text && seg.status === "error") {
      error = t("delegated_unfinished");
    }
    if (error) return { legs: [], goal, error };
    return { legs: Array.isArray(seg.legs) ? mergedLegs(seg.legs, data) : resultLegs(data), goal, error };
  };

  const noteOf = (data) => noteHTML(valueText(data.note).trim());

  // A leg takes the first target left with its repository and branch, else with its repository (`ref` may differ).
  const claimTarget = (targets, used, leg) => {
    const repo = legText(leg, "repo_id");
    let index = claimIndex(targets, used, (target) => legKey(target) === legKey(leg));
    if (index < 0) index = claimIndex(targets, used, (target) => legText(target, "repo_id") === repo);
    return index < 0 ? { index: targets.length, target: {} } : { index, target: targets[index] };
  };

  const jobsHeaders = (...keys) => keys.map((key) => t(key));

  const delegatedTable = (targets, started, failed) => {
    const used = new Set();
    const legs = [...started.map((leg) => ({ leg, failed: false })), ...failed.map((leg) => ({ leg, failed: true }))];
    const rows = legs.slice(0, STRUCTURED_ROWS).map(({ leg, failed: legFailed }) => {
      const { index, target } = claimTarget(targets, used, leg);
      const after = leg.starts_after_this_turn === true ? ` · ${escapeHtml(t("jobs_after_turn"))}` : "";
      const last = legFailed ? escapeHtml(valueText(leg.error)) : sessionLink(leg) + after;
      const cells = [valueText(leg.repo_id), branchText(leg.ref), firstLine(target.prompt)].map(escapeHtml);
      return { index, failed: legFailed, cells: [...cells, last] };
    });
    rows.sort((a, b) => a.index - b.index);
    const headers = jobsHeaders("jobs_repository", "jobs_branch", "jobs_job", "jobs_session");
    const classes = rows.map((row) => (row.failed ? "body-row-failed" : ""));
    return tableMarkup(headers, rows.map((row) => row.cells), classes) + andMoreHTML(legs.length - rows.length);
  };

  const targetsHTML = (targets) => {
    if (!targets.length) return "";
    const rows = targets.slice(0, STRUCTURED_ROWS).map((target) => [
      valueText(target.repo_id),
      branchText(target.ref),
      firstLine(target.prompt),
    ]);
    const headers = jobsHeaders("jobs_repository", "jobs_branch", "jobs_job");
    return tableHTML(headers, rows) + andMoreHTML(targets.length - rows.length);
  };

  // The targets show as Input until the call started a leg; then the Changed table holds them.
  const delegateInput = (args, targets, withTargets) => {
    const goal = valueText(args.goal).trim();
    const fields = goal ? definitionsHTML([[t("jobs_goal"), escapeHtml(goal)]]) : "";
    return section("body_input", fields + (withTargets ? targetsHTML(targets) : ""));
  };

  const delegateBody = ({ args, result }) => {
    const targets = recordsOf(args.targets);
    const data = jobsResult(result);
    if (!data) return delegateInput(args, targets, true) + contextSection(result);
    if (data.error != null) return delegateInput(args, targets, true);
    const started = recordsOf(data.started);
    if (!started.length) return delegateInput(args, targets, true) + contextSection(result, () => noteOf(data));
    const batch = valueText(data.batch_id);
    const line = tn("jobs_started", started.length, { id: batch });
    const context = contextSection(result, () => `<p class="body-line">${escapeHtml(line)}</p>${noteOf(data)}`);
    const changed = section("body_changed", delegatedTable(targets, started, recordsOf(data.failed)));
    return changed + delegateInput(args, targets, false) + context;
  };

  const MR_NUMBER_RES = [
    [MR_URL_RE, "!"],
    [PR_URL_RE, "#"],
  ];

  const mergeRequestLabel = (url) => {
    const href = String(url ?? "");
    if (!WEB_LINK_RE.test(href)) return "";
    for (const [re, mark] of MR_NUMBER_RES) {
      const number = href.match(re)?.[1];
      if (number) return `${mark}${number}`;
    }
    return t("jobs_mr");
  };

  window.mergeRequestLabel = mergeRequestLabel;

  const mergeRequestLink = (url) => {
    const label = mergeRequestLabel(url);
    return label ? externalLink(url, label) : "";
  };

  const legResultHTML = (leg) => {
    const reply = valueText(leg.reply).trim();
    const first = reply.split("\n")[0];
    const parts = [mergeRequestLink(valueText(leg.merge_request_url)), escapeHtml(first)].filter(Boolean);
    const whole = reply !== first ? moreHTML(0, markdownBlock(reply), t("show_reply")) : "";
    return parts.join(" ") + whole;
  };

  const legsStatusHTML = (legs) => {
    if (!legs.length) return "";
    const rows = legs.slice(0, STRUCTURED_ROWS).map((leg) => [
      escapeHtml(valueText(leg.repo_id)),
      escapeHtml(branchText(leg.ref)),
      escapeHtml(wordLabel(legText(leg, "status"))),
      legResultHTML(leg),
      sessionLink(leg),
    ]);
    const headers = jobsHeaders("jobs_repository", "jobs_branch", "jobs_status", "body_result", "jobs_session");
    return tableMarkup(headers, rows) + andMoreHTML(legs.length - rows.length);
  };

  // A batch is named unless it is the one batch the row already names.
  const batchesHTML = (batches, asked) => {
    const shown = batches.slice(0, STRUCTURED_ROWS);
    const named = shown.length > 1 || shown.some((batch) => valueText(batch.batch_id) !== asked);
    const html = shown.map((batch) => {
      const name = t("jobs_batch", { id: valueText(batch.batch_id) });
      return (named ? `<p class="body-line">${escapeHtml(name)}</p>` : "") + legsStatusHTML(recordsOf(batch.legs));
    });
    return html.join("") + andMoreHTML(batches.length - shown.length);
  };

  const checkJobsBody = ({ args, result }) => {
    const data = jobsResult(result);
    if (!data) return contextSection(result);
    if (data.error != null) return "";
    const asked = valueText(args.batch_id).trim();
    return contextSection(result, () => batchesHTML(recordsOf(data.batches), asked) + noteOf(data));
  };

  const cancelledHTML = (data) => {
    const ids = (Array.isArray(data.cancelled) ? data.cancelled : []).filter((id) => typeof id === "string");
    const legs = recordsOf(data.legs);
    const lines = ids.slice(0, STRUCTURED_ROWS).map((id) => {
      const leg = legs.find((one) => valueText(one.thread_id) === id) ?? { thread_id: id };
      const status = wordLabel(legText(leg, "status")) || t("leg_stopping");
      const name = leg.repo_id == null ? [id] : [valueText(leg.repo_id), branchText(leg.ref)];
      const parts = [...name, status].map((part) => escapeHtml(part));
      const link = sessionLink(leg);
      return `<li>${(link ? [...parts, link] : parts).join(" · ")}</li>`;
    });
    return lines.length ? `<ul class="body-jobs">${lines.join("")}</ul>${andMoreHTML(ids.length - lines.length)}` : "";
  };

  const cancelJobsBody = ({ result }) => {
    const data = jobsResult(result);
    if (!data) return contextSection(result);
    if (data.error != null) return "";
    return section("body_changed", cancelledHTML(data)) + contextSection(result, () => noteOf(data));
  };

  const PROMPT_LINES = 3;

  const BODY_BY_TOOL = {
    read_file: ({ args, argsStr, result }) => contextSection(result, codeHTML(toolPath(args, argsStr))),
    write_file: ({ seg, args, result }) => {
      const content = pickKey(args, ["content", "text"]);
      const lines = succeeded(seg) && content ? String(content).replace(/\n$/, "").split("\n") : [];
      const rows = lines.map((line) => `+${line}`);
      const context = failedResult(seg.name, result) ? "" : contextSection(result, textHTML);
      return section("body_changed", diffHTML(rows)) + context;
    },
    edit_file: ({ seg, args, result }) => {
      const oldStr = pickKey(args, ["old_string"]);
      const newStr = pickKey(args, ["new_string"]);
      const complete = succeeded(seg) && oldStr != null && newStr != null && Boolean(window.Diff?.diffLines);
      const changed = complete ? diffHTML(diffRows(String(oldStr), String(newStr))) : "";
      return section("body_changed", changed) + contextSection(result, textHTML);
    },
    grep: ({ args, result }) => {
      return contextSection(result, grepHTML(pickKey(args, ["pattern", "query", "regex"])));
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
      return contextSection(result, () => noteHTML(t("skill_whole")) + link);
    },
    task: ({ args, argsStr, result }) => {
      const description = String(pickKeyOrPartial(args, ["description"], argsStr) ?? "");
      const cut = description.includes("\n") || description.length > TASK_DETAIL_CHARS;
      const prompt = String(pickKey(args, ["prompt"]) ?? (cut ? description : "")).trim();
      const input = section("body_input", prompt ? markdownHTML(prompt, PROMPT_LINES, t("show_prompt")) : "");
      return input + contextSection(result, markdownHTML) + noteHTML(t("subagent_hidden"));
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
    tool_search: toolSearchBody,
    publish_artifact: ({ seg, ctx, result }) => {
      const item = window.artifactItem(seg, ctx.latestArtifactsById);
      // The link and title follow the latest version; new or updated is what this call did.
      const own = { ...item, updated: parseArtifactResult(result)?.status === "updated" };
      const changed = item.state === "published" ? artifactHTML(own) : "";
      return section("body_changed", changed) + contextSection(result, publishedHTML);
    },
    fetch_artifact: ({ ctx, result }) => {
      const text = String(result ?? "");
      return section("body_changed", fetchedHTML(text, ctx.latestArtifactsById)) + contextSection(text);
    },
    delegate_jobs: delegateBody,
    check_delegated_jobs: checkJobsBody,
    cancel_delegated_jobs: cancelJobsBody,
  };

  const defaultBody = ({ result }) => contextSection(result);

  const bodyFor = (name, ctx) => {
    if (Object.hasOwn(BODY_BY_TOOL, name)) return BODY_BY_TOOL[name];
    const mcp = mcpTool(name, ctx);
    return mcp ? mcpBody(mcp.readOnly) : defaultBody;
  };

  window.toolBodyHTML = (seg, ctx) => {
    if (!seg) return "";
    const name = String(seg.name ?? "");
    const input = { seg, ctx: ctx ?? {}, args: parseArgs(seg.args), argsStr: seg.args, result: seg.result };
    let html;
    try {
      html = bodyFor(name, input.ctx)(input);
    } catch (err) {
      console.warn("chat-tool-renderers: body builder failed, falling back", err);
      html = contextSection(seg.result, textHTML);
    }
    const wrong = wentWrong(seg);
    return (wrong ? wrongHTML(wrong) : "") + html + rawHTML(seg);
  };
})();
