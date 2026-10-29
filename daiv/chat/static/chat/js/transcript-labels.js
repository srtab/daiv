(function () {
  const DEFAULTS = {
    sum_read: ["read {count} file", "read {count} files"],
    sum_edit: ["edited {count} file", "edited {count} files"],
    sum_create: ["created {count} file", "created {count} files"],
    sum_run: ["ran {count} command", "ran {count} commands"],
    sum_search: ["searched for {count} pattern", "searched for {count} patterns"],
    sum_list: ["listed {count} folder", "listed {count} folders"],
    sum_subagent: ["ran {count} subagent", "ran {count} subagents"],
    sum_fetch: ["fetched {count} page", "fetched {count} pages"],
    sum_web: "searched the web",
    sum_skill: ["used a skill", "used {count} skills"],
    sum_tools: "loaded tools",
    sum_platform: ["called {name}", "called {name} {count} times"],
    sum_mcp: "used {name}",
    sum_artifact: ["opened {count} artifact", "opened {count} artifacts"],
    sum_jobs: ["checked delegated jobs", "checked delegated jobs {count} times"],
    sum_other: ["used {count} tool", "used {count} tools"],
    failed: "({count} failed)",
    worked_for: "Worked for {duration}",
    steps: ["{count} step", "{count} steps"],
    row_read: "Read {name}",
    live_read: "Reading {name}",
    row_edit: "Edited {name}",
    live_edit: "Editing {name}",
    row_create: "Created {name}",
    live_create: "Creating {name}",
    row_run: "Ran {command}",
    live_run: "Running {command}",
    row_grep: "Searched for \"{pattern}\"",
    live_grep: "Searching for \"{pattern}\"",
    row_glob: "Found files matching {pattern}",
    live_glob: "Finding files matching {pattern}",
    row_ls: "Listed {name}",
    live_ls: "Listing {name}",
    row_subagent: "Subagent · {name}",
    row_skill: "Used skill",
    live_skill: "Loading skill",
    row_web: "Searched the web",
    live_web: "Searching the web",
    row_fetch: "Fetched",
    live_fetch: "Fetching",
    row_tools: "Loaded tools",
    live_tools: "Loading tools",
    row_artifact_open: "Opened artifact",
    live_artifact_open: "Opening artifact",
    row_published: "Published",
    live_published: "Publishing",
    row_delegate: "Delegated jobs",
    live_delegate: "Delegating jobs",
    row_check_jobs: "Checked delegated jobs",
    live_check_jobs: "Checking delegated jobs",
    row_cancel_jobs: "Cancelled delegated jobs",
    live_cancel_jobs: "Cancelling delegated jobs",
    row_question: "Asked a question",
    live_question: "Asking a question",
    row_mcp: "{server} · {tool}",
    thinking: "Thinking…",
    thought_for: "Thought for {duration}",
    thought: "Thought",
    detail_passed: "passed",
    detail_exit: "exit {code}",

    body_wrong: "What went wrong",
    body_changed: "Changed",
    body_input: "Input",
    body_context: "Loaded into context",
    body_raw: "Raw",
    body_args: "Arguments",
    body_result: "Result",
    size_lines: ["{count} line", "{count} lines"],
    size_kb: "{size} KB",
    offloaded: "Too large to load whole: the model saw only a preview. The full result is in {path}.",
    unanswered: "No result: the run ended before this call returned.",
    show_all: "Show all {count} lines",
    and_more: "and {count} more",

    skill_whole: "The whole skill, always: skills are never cut to a file.",
    open_in_skills: "Open {name} in Skills ↗",
    show_prompt: "Show the whole prompt",
    subagent_hidden: "What the subagent did inside is not shown.",
    web_page: "Page",
    web_asked: "Asked",
    web_summary: "a summary of the page",
    redirected_to: "Redirected to",
    jump_to_error: "Jump to first error",
    log_earlier: ["{count} earlier line not shown", "{count} earlier lines not shown"],
    open_link: "Open ↗",
    saved_to_file: "Saved to {path} as asked: the model saw only a preview.",

    read_only: "read-only tool",
    can_change: "can change things",
    size_tool_schemas: ["{count} tool schema", "{count} tool schemas"],
    artifact_new: "new",
    artifact_updated: "updated",
    artifact_download: "Download",
    jobs_goal: "Goal",
    jobs_repository: "Repository",
    jobs_branch: "Branch",
    jobs_job: "Job",
    jobs_status: "Status",
    jobs_session: "Session",
    jobs_open_session: "Session →",
    jobs_default_branch: "default branch",
    jobs_after_turn: "starts after this turn",
    jobs_started: ["batch {id} · {count} started", "batch {id} · {count} started"],
    jobs_none_started: "No job started.",
    jobs_batch: "batch {id}",
    jobs_mr: "merge request",
    show_reply: "Show the whole reply",
    stopping: "stopping",
    suggested_answer: "Suggested answer",
    publish_committing: "Committing changes",
    publish_creating: "Creating merge request",
    publish_files: ["{count} file", "{count} files"],
    publish_error_conflicts: "Merge conflicts are left in the changed files",
    publish_error_mr_pending: "The merge request isn't created yet",
    publish_error_push_rejected: "The push was rejected",
    publish_error_mr_failed: "The merge request couldn't be created",
    publish_error_unknown: "Couldn't publish the changes",
  };

  const loadPayload = () => {
    try {
      const el = document.getElementById("chat-transcript-labels");
      const parsed = el ? JSON.parse(el.textContent) : null;
      return parsed && typeof parsed === "object" ? parsed : {};
    } catch (_) {
      return {};
    }
  };

  const payload = loadPayload();
  const overrides = payload.labels && typeof payload.labels === "object" ? payload.labels : {};
  const icons = payload.icons && typeof payload.icons === "object" ? payload.icons : {};

  const showRaw = payload.show_raw === true;
  const sessionPath = typeof payload.session_path === "string" ? payload.session_path : "";

  const fill = (template, values) =>
    template.replace(/\{(\w+)\}/g, (match, key) => (key in values ? String(values[key]) : match));

  const lookup = (key) => (key in overrides ? overrides[key] : DEFAULTS[key]);

  const t = (key, vars) => {
    const value = lookup(key);
    if (typeof value !== "string") return key;
    return fill(value, vars || {});
  };

  const tn = (key, count, vars) => {
    const value = lookup(key);
    if (!Array.isArray(value)) return typeof value === "string" ? fill(value, { count, ...vars }) : key;
    return fill(count === 1 ? value[0] : value[1], { count, ...vars });
  };

  const iconHTML = (name) => (Object.hasOwn(icons, name) ? icons[name] : "");

  const duration = (sec) => {
    if (sec < 60) return `${sec}s`;
    const min = Math.floor(sec / 60);
    if (min < 60) return `${min}m ${sec % 60}s`;
    return `${Math.floor(min / 60)}h ${min % 60}m`;
  };

  window.TranscriptLabels = { defaults: DEFAULTS, fill, t, tn, iconHTML, duration, showRaw, sessionPath };
})();
