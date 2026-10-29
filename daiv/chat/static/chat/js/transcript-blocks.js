(() => {
  const CALL_KINDS = {
    write_todos: null,
    ask_user_question: "question",
    publish_artifact: "publish",
    delegate_jobs: "special",
  };

  const NO_PUBLISH = Object.freeze([]);

  // Text only grows while it streams, so a segment found non-blank is not read (or tracked) again.
  const nonBlank = new WeakSet();

  const isText = (seg) => {
    if (nonBlank.has(seg)) return true;
    if (!String(seg.content ?? "").trim()) return false;
    nonBlank.add(seg);
    return true;
  };

  const kindOf = (seg) => {
    if (seg.type === "thinking") return "step";
    if (seg.type === "text") return isText(seg) ? "text" : null;
    if (seg.type !== "tool_call") return null;
    return Object.hasOwn(CALL_KINDS, seg.name) ? CALL_KINDS[seg.name] : "step";
  };

  // Built once per value of `fields` on the segment and per `ctxId`, so a re-derive reuses what did not change.
  const memoBySeg = (fields, build) => {
    const cache = new WeakMap();
    return (seg, ctx, ctxId) => {
      const hit = cache.get(seg);
      if (hit && hit.ctxId === ctxId && fields.every((f) => hit.values[f] === seg[f])) return hit.value;
      const value = build(seg, ctx);
      cache.set(seg, { ctxId, values: Object.fromEntries(fields.map((f) => [f, seg[f]])), value });
      return value;
    };
  };

  const rowFor = memoBySeg(["args", "result", "status", "startedAt", "endedAt"], (seg, servers) =>
    window.toolRow(seg, { mcpServers: servers }),
  );

  const artifactFor = memoBySeg(["args", "result", "status"], (seg, latest) => window.artifactItem(seg, latest));

  // By value: a caller may rebuild the list on every derive.
  const serversIdOf = (servers) => servers.join("\n");

  const sumKey = (key) => (`sum_${key}` in window.TranscriptLabels.defaults ? `sum_${key}` : "sum_other");

  const sentenceCase = (s) => s.charAt(0).toLocaleUpperCase() + s.slice(1);

  const tally = (rows) => {
    const counts = { failed: 0, added: 0, removed: 0 };
    for (const row of rows) {
      if (row.failed) counts.failed++;
      if (row.diff) {
        counts.added += row.diff.added;
        counts.removed += row.diff.removed;
      }
    }
    return counts;
  };

  const summarize = (steps) => {
    const T = window.TranscriptLabels;
    const line = { icon: steps[0]?.row.icon ?? "", text: "", ...tally(steps.map((s) => s.row)), running: null };
    const clauses = new Map();
    let thoughtSecs = 0;
    let timed = true;
    for (const { seg, row } of steps) {
      if (seg.status === "running") line.running = row.livePhrase;
      const { key, name } = row.category;
      if (key === "thinking") {
        if (row.thoughtSecs == null) timed = false;
        else thoughtSecs += row.thoughtSecs;
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

  // A row is cached per segment value, so the same rows mean the same line.
  const lineCache = new WeakMap();

  const lineFor = (steps) => {
    const rows = steps.map((s) => s.row);
    const hit = lineCache.get(rows[0]);
    if (hit && sameList(hit.rows, rows, identical)) return hit.line;
    const line = summarize(steps);
    lineCache.set(rows[0], { rows, line });
    return line;
  };

  // The working line's phrase: the last running entry's, or "" while thinking or between steps.
  const activityOf = (entries, servers, serversId, latest) => {
    for (let i = entries.length - 1; i >= 0; i--) {
      const { kind, seg } = entries[i];
      if (kind === "publish" ? artifactFor(seg, latest, latest).state === "running" : seg.status === "running") {
        return seg.type === "thinking" ? "" : `${rowFor(seg, servers, serversId).livePhrase}…`;
      }
    }
    return "";
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
      // A run without a prompt starts at its first turn, which carries its run_id (a failed one's is its marker).
      if (!run || turn.role === "user" || (turn.run_id && run.runId && turn.run_id !== run.runId)) open(turn, ti);
      if (turn.role === "run_status") run.statuses.push(turn);
      else run.turns.push({ turn, ti });
    });
    return runs;
  };

  const shallowSame = (a, b) => {
    if (a === b) return true;
    if (!a || !b || typeof a !== "object" || typeof b !== "object") return false;
    const keys = Object.keys(b);
    return keys.length === Object.keys(a).length && keys.every((k) => a[k] === b[k]);
  };

  const sameList = (a, b, same) => a.length === b.length && a.every((x, i) => same(x, b[i]));
  const identical = (a, b) => a === b;

  // Fields holding objects rebuilt on every derive; everything else must be the same value or object.
  const NESTED = {
    line: shallowSame,
    steps: (a, b) => sameList(a, b, shallowSame),
    items: (a, b) => sameList(a, b, shallowSame),
  };

  const sameItem = (a, b) => {
    const keys = Object.keys(b);
    if (keys.length !== Object.keys(a).length) return false;
    return keys.every((k) => a[k] === b[k] || (Object.hasOwn(NESTED, k) && NESTED[k](a[k], b[k])));
  };

  const reuseBlock = (prev, block) => {
    const prevItems = new Map(
      [...prev.items, ...prev.endingText, ...prev.cards, ...prev.endingTail].map((item) => [item.key, item]),
    );
    const keep = (list, prevList) => {
      const next = list.map((item) => {
        const old = prevItems.get(item.key);
        return old && sameItem(old, item) ? old : item;
      });
      return sameList(next, prevList, identical) ? prevList : next;
    };
    const next = {
      ...block,
      stats: shallowSame(prev.stats, block.stats) ? prev.stats : block.stats,
      items: keep(block.items, prev.items),
      endingText: keep(block.endingText, prev.endingText),
      cards: keep(block.cards, prev.cards),
      endingTail: keep(block.endingTail, prev.endingTail),
      statuses: sameList(block.statuses, prev.statuses, identical) ? prev.statuses : block.statuses,
    };
    return shallowSame(prev, next) ? prev : next;
  };

  // An unchanged block or item keeps its previous object, so x-for leaves its scope (and x-html bodies) alone.
  const reuse = (blocks, previous) => {
    if (!previous?.length) return blocks;
    const prevBlocks = new Map(previous.map((block) => [block.key, block]));
    const next = blocks.map((block) => {
      const prev = prevBlocks.get(block.key);
      return prev ? reuseBlock(prev, block) : block;
    });
    return sameList(next, previous, identical) ? previous : next;
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

    const blocks = runs.map((run, ri) => {
      const key = claim(run.anchor?.id || run.anchor?.run_id || `head-${turnKey(run.first, run.firstIndex)}`);

      const entries = [];
      const stepRows = [];
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
            stepRows.push(entry.row);
            lastStep = entries.length;
          }
          entries.push(entry);
        });
      }

      const stats = { steps: stepRows.length, ...tally(stepRows) };
      const streaming = run.turns.some(({ turn }) => turn.streaming);
      const live = streaming || (ri === runs.length - 1 && Boolean(opts.inFlight));
      const folded = !live && stats.steps > 0;
      const foldEnd = folded ? lastStep + 1 : entries.length;

      const itemOf = (e) => {
        if (e.kind === "text") return { kind: "text", key: claim(`t-${e.at}`), seg: e.seg };
        if (e.kind === "question") {
          const row = rowFor(e.seg, servers, serversId);
          return { kind: "question", key: claim(`q-${e.ref}`), seg: e.seg, turnIndex: e.ti, row };
        }
        return { kind: "special", key: claim(`x-${e.ref}`), seg: e.seg, row: rowFor(e.seg, servers, serversId) };
      };

      const items = [];
      let pending = [];
      const flush = () => {
        if (pending.length === 1) {
          items.push({ kind: "step", ...pending[0] });
        } else if (pending.length > 1) {
          items.push({ kind: "group", key: claim(`g-${pending[0].key}`), steps: pending, line: lineFor(pending) });
        }
        pending = [];
      };
      for (const e of entries.slice(0, foldEnd)) {
        if (e.kind === "step") {
          pending.push({ key: claim(`s-${e.ref}`), seg: e.seg, row: e.row });
          continue;
        }
        flush();
        if (e.kind === "publish" && !folded) {
          const artifact = artifactFor(e.seg, latest, latest);
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

      // A folded run shows its closing text, then its cards, then the rest of its ending (a question, a special call).
      const endingText = [];
      const endingTail = [];
      const cards = [];
      if (folded) {
        // A publish after the last step shows only in the run's cards.
        for (const item of entries.slice(foldEnd).filter((e) => e.kind !== "publish").map(itemOf)) {
          (item.kind === "text" ? endingText : endingTail).push(item);
        }
        const published = [];
        const seen = new Set();
        for (const e of entries) {
          if (e.kind !== "publish") continue;
          const artifact = artifactFor(e.seg, latest, latest);
          if (artifact.id && seen.has(artifact.id)) continue;
          if (artifact.id) seen.add(artifact.id);
          published.push(artifact);
        }
        if (published.length) cards.push({ kind: "artifacts", key: claim(`c-${key}`), items: published });
      }

      const lastText = entries.findLast((e) => e.kind === "text");
      return {
        key,
        anchor: run.anchor,
        runId: run.runId,
        live,
        folded,
        stats,
        startedAt: run.anchor?.started_at || null,
        endedAt: run.turns.at(-1)?.turn.received_at || null,
        items,
        endingText,
        cards,
        endingTail,
        statuses: run.statuses,
        lastTurn: run.turns.findLast(({ turn }) => turn.role === "assistant")?.turn ?? null,
        publish: run.turns.findLast(({ turn }) => turn.publish)?.turn.publish ?? NO_PUBLISH,
        // A run's answer is copyable once nothing in it streams; a later tool-only turn does not hide it.
        copyTurn: !streaming && lastText ? lastText.turn : null,
        activity: live ? activityOf(entries, servers, serversId, latest) : "",
      };
    });
    return reuse(blocks, opts.previous);
  };
})();
