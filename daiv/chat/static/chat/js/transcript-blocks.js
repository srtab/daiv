(() => {
  const CALL_KINDS = {
    write_todos: null,
    ask_user_question: "question",
    publish_artifact: "publish",
    delegate_jobs: "special",
  };

  const kindOf = (seg) => {
    if (seg.type === "thinking") return "step";
    if (seg.type === "text") return String(seg.content ?? "").trim() ? "text" : null;
    if (seg.type === "publish_phase") return "special";
    if (seg.type !== "tool_call") return null;
    return Object.hasOwn(CALL_KINDS, seg.name) ? CALL_KINDS[seg.name] : "step";
  };

  const rowCache = new WeakMap();

  const rowFor = (seg, servers, serversId) => {
    const hit = rowCache.get(seg);
    if (
      hit &&
      hit.args === seg.args &&
      hit.result === seg.result &&
      hit.status === seg.status &&
      hit.startedAt === seg.startedAt &&
      hit.endedAt === seg.endedAt &&
      hit.serversId === serversId
    ) {
      return hit.row;
    }
    const row = window.toolRow(seg, { mcpServers: servers });
    const { args, result, status, startedAt, endedAt } = seg;
    rowCache.set(seg, { args, result, status, startedAt, endedAt, serversId, row });
    return row;
  };

  const serversIdOf = (servers) => servers.join("\n");

  const sumKey = (key) => (`sum_${key}` in window.TranscriptLabels.defaults ? `sum_${key}` : "sum_other");

  const sentenceCase = (s) => s.charAt(0).toLocaleUpperCase() + s.slice(1);

  const summarize = (steps) => {
    const T = window.TranscriptLabels;
    const line = { icon: steps[0]?.row.icon ?? "", text: "", failed: 0, added: 0, removed: 0, running: null };
    const clauses = new Map();
    let thoughtSecs = 0;
    let timed = true;
    for (const { seg, row } of steps) {
      if (row.failed) line.failed++;
      if (row.diff) {
        line.added += row.diff.added;
        line.removed += row.diff.removed;
      }
      if (seg.status === "running") line.running = row.livePhrase;
      const { key, name } = row.category;
      if (key === "thinking") {
        if (seg.startedAt != null && seg.endedAt != null) {
          thoughtSecs += Math.max(1, Math.round((seg.endedAt - seg.startedAt) / 1000));
        } else {
          timed = false;
        }
        continue;
      }
      const id = `${key}\n${name ?? ""}`;
      const clause = clauses.get(id) ?? { key, name, count: 0 };
      clause.count++;
      clauses.set(id, clause);
    }
    if (clauses.size) {
      line.text = [...clauses.values()]
        .map((c, i) => {
          const phrase = T.tn(sumKey(c.key), c.count, { name: c.name ?? "" });
          return i === 0 ? sentenceCase(phrase) : phrase;
        })
        .join(", ");
    } else {
      line.text = timed ? T.t("thought_for", { duration: T.duration(thoughtSecs) }) : T.t("thought");
    }
    return line;
  };

  window.groupLine = (steps, ctx) => {
    const servers = ctx?.mcpServers ?? [];
    const serversId = serversIdOf(servers);
    return summarize(steps.map((step) => (step.row ? step : { ...step, row: rowFor(step.seg, servers, serversId) })));
  };

  const turnKey = (turn, ti) => turn.id || `@${ti}`;

  const splitRuns = (turns) => {
    const runs = [];
    let run = null;
    const open = (turn, ti) => {
      run = {
        first: turn,
        firstIndex: ti,
        anchor: turn.role === "user" ? turn : null,
        runId: turn.run_id || null,
        turns: [],
        statuses: [],
      };
      runs.push(run);
    };
    turns.forEach((turn, ti) => {
      if (turn.role === "run_status") {
        if (!run) open(turn, ti);
        run.statuses.push(turn);
        return;
      }
      // A run without a prompt starts at its first turn, which carries its run_id.
      if (!run || turn.role === "user" || (turn.run_id && turn.run_id !== run.runId)) open(turn, ti);
      run.turns.push({ turn, ti });
    });
    return runs;
  };

  window.buildRunBlocks = (turns, opts = {}) => {
    const servers = opts.mcpServers ?? [];
    const serversId = serversIdOf(servers);
    const latest = opts.latestArtifactsById;
    const used = new Set();
    const claim = (base) => {
      let key = base;
      for (let n = 2; used.has(key); n++) key = `${base}~${n}`;
      used.add(key);
      return key;
    };

    const runs = splitRuns(turns);
    // A prompt queued behind the running run is never the live block.
    const liveTail = opts.inFlight ? runs.findLastIndex((run) => !run.anchor?.pending) : -1;

    return runs.map((run, ri) => {
      const key = claim(run.anchor?.run_id || run.anchor?.id || `head-${turnKey(run.first, run.firstIndex)}`);

      const entries = [];
      const stats = { steps: 0, failed: 0, added: 0, removed: 0 };
      let lastStep = -1;
      for (const { turn, ti } of run.turns) {
        if (turn.role === "user") continue;
        const tk = turnKey(turn, ti);
        (turn.segments || []).forEach((seg, si) => {
          const kind = kindOf(seg);
          if (!kind) return;
          const entry = { kind, seg, turn, ti, at: `${tk}-${si}`, ref: seg.id || `${tk}-${si}`, row: null };
          if (kind === "step") {
            entry.row = rowFor(seg, servers, serversId);
            stats.steps++;
            if (entry.row.failed) stats.failed++;
            if (entry.row.diff) {
              stats.added += entry.row.diff.added;
              stats.removed += entry.row.diff.removed;
            }
            lastStep = entries.length;
          }
          entries.push(entry);
        });
      }

      const live = run.turns.some(({ turn }) => turn.streaming) || ri === liveTail;
      const folded = !live && stats.steps > 0;
      const foldEnd = folded ? lastStep + 1 : entries.length;

      const itemOf = (e) => {
        if (e.kind === "text") return { kind: "text", key: claim(`t-${e.at}`), seg: e.seg, turn: e.turn };
        if (e.kind === "question") {
          return { kind: "question", key: claim(`q-${e.ref}`), seg: e.seg, turn: e.turn, turnIndex: e.ti };
        }
        const row = e.seg.type === "tool_call" ? rowFor(e.seg, servers, serversId) : null;
        return { kind: "special", key: claim(`x-${e.ref}`), seg: e.seg, row };
      };

      const items = [];
      let pending = [];
      const flush = () => {
        if (pending.length === 1) {
          items.push({ kind: "step", key: pending[0].key, step: pending[0] });
        } else if (pending.length > 1) {
          items.push({ kind: "group", key: claim(`g-${pending[0].key}`), steps: pending, line: summarize(pending) });
        }
        pending = [];
      };
      for (const e of entries.slice(0, foldEnd)) {
        if (e.kind === "step") {
          pending.push({ key: claim(`s-${e.ref}`), seg: e.seg, turn: e.turn, row: e.row });
          continue;
        }
        flush();
        if (e.kind === "publish" && !folded) {
          const artifact = window.artifactItem(e.seg, latest);
          const last = items[items.length - 1];
          if (last?.kind !== "artifacts") {
            items.push({ kind: "artifacts", key: claim(`a-${e.ref}`), items: [artifact] });
          } else if (!artifact.id || !last.items.some((it) => it.id === artifact.id)) {
            last.items.push(artifact);
          }
        } else {
          items.push(itemOf(e));
        }
      }
      flush();

      const ending = [];
      const cards = [];
      if (folded) {
        // A publish after the last step shows in the run's cards, right below the ending.
        const rest = entries.slice(foldEnd).filter((e) => e.kind !== "publish").map(itemOf);
        ending.push(...rest.filter((i) => i.kind === "text"), ...rest.filter((i) => i.kind !== "text"));
        const published = [];
        const seen = new Set();
        for (const e of entries) {
          if (e.kind !== "publish") continue;
          const artifact = window.artifactItem(e.seg, latest);
          if (artifact.id && seen.has(artifact.id)) continue;
          if (artifact.id) seen.add(artifact.id);
          published.push(artifact);
        }
        if (published.length) cards.push({ kind: "artifacts", key: claim(`c-${key}`), items: published });
      }

      const own = run.turns.map(({ turn }) => turn);
      return {
        key,
        anchor: run.anchor,
        runId: run.runId,
        live,
        folded,
        stats,
        startedAt: run.anchor?.started_at || null,
        endedAt: own[own.length - 1]?.received_at || null,
        items,
        ending,
        cards,
        statuses: run.statuses,
        lastTurn: own.findLast((turn) => turn.role === "assistant") ?? null,
      };
    });
  };
})();
