/**
 * Alpine component: agent model + thinking-effort pill + popover.
 *
 * The model catalog is no longer rendered server-side — we fetch it on first
 * popover open via ``catalogUrl`` and hold the result for the page lifetime.
 * The search input doubles as a free-text submit field (Enter or click the
 * "Use exact name" affordance) so users can always type a model even when
 * the catalog is empty or the endpoint failed.
 *
 * Selection changes fire ``daiv:agent-changed`` on ``window`` so listeners
 * (e.g. the chat composer's locked pill) can mirror the picker's current state
 * without re-deriving labels or effort dots from the raw spec. Payload:
 * ``{model, thinking_level, label, effort_dots}`` — ``label`` matches
 * ``pillLabel.name`` and ``effort_dots`` matches ``pillLabel.effortDots`` so
 * the picker remains the single source of truth client-side.
 *
 * Constructor args (passed via x-data):
 *   providers:            Array<{slug, label}> — enabled providers.
 *   catalogUrl:           string — fetched lazily on first popover open.
 *   initialAgentModel:    string — stored ``provider:model`` spec; ``""`` when no value
 *                                  was stored.
 *   initialThinkingLevel: string — ``"minimal" | "low" | "medium" | "high"``; ``""`` = unset.
 *   defaultAgentModel:    string — system default ``provider:model`` spec. In
 *                                  ``seedDefault=true`` mode it acts as a seed value for
 *                                  the picker (so submission is always concrete). In
 *                                  ``seedDefault=false`` mode (settings) it is only used
 *                                  to render the placeholder pill label ("Default (…)")
 *                                  when nothing is selected — the input value stays empty.
 *   defaultThinkingLevel: string — effort analogue of ``defaultAgentModel``.
 *   placeholderLabel:     string — pill label when the picker is unselected. ``""`` falls
 *                                  back to "Default (…)" if a default is configured, else
 *                                  to "Pick a model".
 *   seedDefault:          bool   — see ``defaultAgentModel``. Defaults to ``true`` so
 *                                  existing run-time callers behave identically; settings
 *                                  pass ``false``.
 *   required:             bool   — when ``true`` Alpine renders an sr-only required
 *                                  input that gates form submission on an empty
 *                                  selection. Defaults to ``true``; settings pass ``false``
 *                                  because an empty save is meaningful ("use default").
 *   changeEvent:          string — window event name fired on selection change.
 *                                  Defaults to ``"daiv:agent-changed"``; pass ``""`` to
 *                                  disable (settings form has no consumer).
 */
const EFFORT_LEVELS = ["minimal", "low", "medium", "high", "xhigh"];

function shortenModel(spec) {
    // Strip the ``provider:`` prefix and any ``org/`` path so the placeholder pill
    // stays compact ("Default (claude-haiku-4.5)" rather than
    // "Default (openrouter:anthropic/claude-haiku-4.5)"). Mirrors the run-time
    // pillLabel rendering for consistency.
    const afterProvider = spec.includes(":") ? spec.slice(spec.indexOf(":") + 1) : spec;
    return afterProvider.split("/").pop() || afterProvider;
}

document.addEventListener("alpine:init", () => {
    Alpine.data("agentPicker", ({
        providers = [],
        catalogUrl = "",
        initialAgentModel = "",
        initialThinkingLevel = "",
        defaultAgentModel = "",
        defaultThinkingLevel = "",
        placeholderLabel = "",
        seedDefault = true,
        required = true,
        changeEvent = "daiv:agent-changed",
    } = {}) => ({
        providers: [...providers],
        catalogUrl,
        catalog: {byProvider: {}, status: "idle", error: null},
        selectedProvider: "",
        modelName: "",
        thinkingLevel: "",
        query: "",
        open: false,
        required,
        defaultAgentModel,
        defaultThinkingLevel,
        placeholderLabel,
        changeEvent,
        LEVELS: EFFORT_LEVELS,

        init() {
            // Stored spec always wins. The system default is only seeded into the
            // picker state when ``seedDefault`` is true (run-time pickers) — in
            // settings we leave the input empty so an unchanged form persists
            // NULL ("use the configured default"). Split on the FIRST colon so
            // model names containing ``:`` (rare but valid) survive.
            const seed = initialAgentModel || (seedDefault ? defaultAgentModel : "");
            if (seed && seed.includes(":")) {
                const idx = seed.indexOf(":");
                this.selectedProvider = seed.slice(0, idx);
                this.modelName = seed.slice(idx + 1);
            }
            this.thinkingLevel = initialThinkingLevel || (seedDefault ? defaultThinkingLevel : "");

            // ``setCustomValidity`` is sticky — once set, it persists until cleared even
            // after ``agentModelValue`` flips to a valid spec. Watch the computed and
            // clear the message on transition to non-empty so the form submits cleanly
            // once the user picks something. Only relevant when the required input
            // template is rendered (i.e. ``required`` is true).
            this.$watch("agentModelValue", (val) => {
                if (val) this.$refs.modelInput?.setCustomValidity("");
                this._dispatchChange();
            });
            this.$watch("thinkingLevel", () => this._dispatchChange());

            // Propagate the seeded default to listeners. Alpine ``$watch`` does not fire
            // on init, so a user who submits without touching the picker would otherwise
            // leave consumers (chat composer's locked pill) stuck on the empty-thread
            // fallback ("Pick a model") while submit() forwards the seeded default spec
            // from the hidden inputs. ``$nextTick`` defers until window listeners on
            // ``daiv:agent-changed`` are wired.
            this.$nextTick(() => this._dispatchChange());
        },

        _dispatchChange() {
            // Surface the picker's current pill state so listeners stay in sync without
            // re-deriving labels from the raw spec. Fires on every selection change
            // and once on init (via ``$nextTick``) so the seeded default reaches
            // consumers — Alpine ``$watch`` does not fire on initial assignments.
            // Empty ``changeEvent`` opts out (settings form has no consumer).
            // The try/catch isolates a misbehaving listener: ``dispatchEvent`` is
            // synchronous, so a listener that throws would otherwise abort the
            // ``$watch`` callback mid-tick and could leave the hidden inputs updated
            // without the locked pill catching up.
            if (!this.changeEvent) return;
            try {
                const pill = this.pillLabel;
                window.dispatchEvent(new CustomEvent(this.changeEvent, {detail: {
                    model: this.agentModelValue,
                    thinking_level: this.thinkingLevel,
                    label: pill.name,
                    effort_dots: pill.effortDots,
                }}));
            } catch (err) {
                console.error("agent picker: listener for", this.changeEvent, "threw", err);
            }
        },

        toggle() {
            this.open = !this.open;
            if (this.open) {
                this.query = "";
                this.$nextTick(() => this.$refs.search?.focus());
                this.ensureCatalogLoaded();
            }
        },

        close() {
            this.open = false;
        },

        async ensureCatalogLoaded() {
            // Re-fetch on subsequent opens after an error so transient failures
            // (brief network glitch, server restart) self-heal next time the user
            // opens the popover. "loading" and "loaded" still short-circuit.
            if (this.catalog.status === "loading" || this.catalog.status === "loaded") return;
            if (!this.catalogUrl) {
                this.catalog.status = "error";
                this.catalog.error = "No catalog URL configured";
                return;
            }
            this.catalog.status = "loading";
            this.catalog.error = null;
            try {
                const res = await fetch(this.catalogUrl, {credentials: "same-origin"});
                if (!res.ok) throw new Error(`HTTP ${res.status}`);
                const data = await res.json();
                if (!data || typeof data.catalog !== "object" || data.catalog === null) {
                    throw new Error("Unexpected catalog response shape");
                }
                this.catalog.byProvider = data.catalog;
                this.catalog.status = "loaded";
            } catch (err) {
                this.catalog.status = "error";
                this.catalog.error = (err && err.message) || "Network error";
                // Surface in devtools — the in-UI hint is dismissable and easy to miss.
                console.error("agent picker: catalog fetch failed", err);
            }
        },

        selectProvider(slug) {
            // Keep the previously selected ``modelName`` so the pill doesn't fall
            // back to the "Pick a model" prompt mid-edit; the new provider's list
            // simply won't highlight a row until the user picks one explicitly.
            this.selectedProvider = slug;
            this.query = "";
        },

        selectModel(name) {
            this.modelName = name;
            this.query = "";
        },

        selectEffort(level) {
            this.thinkingLevel = level;
        },

        clearSelection() {
            // Revert to the "no explicit pick" state. The pill then renders
            // ``placeholderLabel`` (typically "Default (…)" in settings) and the
            // hidden input goes empty so save persists NULL — i.e. "use the
            // configured default" rather than "no model". Close the popover too:
            // the "Use default" button hides itself once the picker is empty,
            // leaving the open popover otherwise actionless.
            this.selectedProvider = "";
            this.modelName = "";
            this.thinkingLevel = "";
            this.query = "";
            this.open = false;
        },

        handleInvalidModel(event) {
            // Browser refused to submit because the sr-only model input is required+empty.
            // Set a friendly validity message and open the popover so the user can act —
            // the browser's default "Please fill out this field" is meaningless when the
            // field itself is invisible. The ``$watch`` set up in ``init()`` clears the
            // validity message as soon as ``agentModelValue`` flips to a non-empty spec.
            event.target.setCustomValidity(
                "Pick a model — no system default is configured, so a model must be selected explicitly."
            );
            this.open = true;
            this.$nextTick(() => this.$refs.search?.focus());
        },

        submitFreeText() {
            const raw = this.query.trim();
            if (!raw) return;
            // ``slug:model`` overrides the selected provider; otherwise we treat
            // the value as a bare model name under the currently selected provider.
            if (raw.includes(":")) {
                const idx = raw.indexOf(":");
                this.selectedProvider = raw.slice(0, idx);
                this.modelName = raw.slice(idx + 1);
            } else if (this.selectedProvider) {
                this.modelName = raw;
            } else {
                // Defensive: the search input and "Use exact name" button both
                // live inside ``x-show="selectedProvider"`` in the template, so
                // this branch is unreachable from the current UI. Kept as a
                // no-op guard against future template restructuring that would
                // expose those controls before a provider is picked.
                return;
            }
            this.query = "";
        },

        get filteredModels() {
            const entry = this.catalog.byProvider[this.selectedProvider];
            const list = (entry && entry.models) || [];
            const q = this.query.trim().toLowerCase();
            if (!q) return list;
            return list.filter(m => m.toLowerCase().includes(q));
        },

        get providerHint() {
            // Free-text-only state messages — surfaced above the search input when
            // the catalog can't deliver suggestions for the current provider.
            if (this.catalog.status === "error") {
                return `Couldn't load model list — ${this.catalog.error}. Type a model name and press Enter.`;
            }
            if (this.catalog.status !== "loaded") return "";
            const entry = this.catalog.byProvider[this.selectedProvider];
            if (entry && entry.error) {
                return `Couldn't load ${this.selectedProvider} models — ${entry.error}. Type a model name and press Enter.`;
            }
            if (entry && entry.models.length === 0) {
                return `No chat-capable models reported by ${this.selectedProvider}.`;
            }
            return "";
        },

        get agentModelValue() {
            // Submitted input value. In ``seedDefault=true`` mode (run-time pickers)
            // the picker is seeded with the system default at init when one is
            // configured, so this is usually a concrete ``provider:model`` spec.
            // In ``seedDefault=false`` mode (settings), an empty value is a
            // meaningful "use the configured default" save — the picker is
            // deliberately not seeded with the default. Empty also occurs at
            // run time when no usable default reached the client AND the user
            // hasn't picked one; the form's ``required`` validation refuses
            // submission in that state (server-side ``ensure_agent_model_available``
            // is the backstop).
            return this.selectedProvider && this.modelName
                ? `${this.selectedProvider}:${this.modelName}`
                : "";
        },

        get pillLabel() {
            // Unselected fallback. The label and dot meter both depend on
            // whether a configured default exists:
            //   - explicit ``placeholderLabel`` (settings pass "Default (env-value)")
            //   - else "Default (short-name)" derived from ``defaultAgentModel``
            //   - else the run-time "Pick a model" prompt
            // In the "Default (…)" cases we still light the dots for the default
            // effort so the empty-state pill shows what would be sent.
            if (!this.selectedProvider || !this.modelName) {
                if (this.placeholderLabel) {
                    return {
                        name: this.placeholderLabel,
                        effortDots: this.dotsFor(this.defaultThinkingLevel),
                    };
                }
                if (this.defaultAgentModel) {
                    return {
                        name: `Default (${shortenModel(this.defaultAgentModel)})`,
                        effortDots: this.dotsFor(this.defaultThinkingLevel),
                    };
                }
                return {name: "Pick a model", effortDots: 0};
            }
            // Strip any ``org/`` path segment from the model name (e.g.
            // ``anthropic/claude-haiku-4.5`` → ``claude-haiku-4.5``) so the pill
            // stays compact next to the env pill. The ``provider:`` prefix was
            // already split off into ``selectedProvider`` at init time.
            const display = this.modelName.split("/").pop() || this.modelName;
            return {
                name: display,
                effortDots: this.dotsFor(this.thinkingLevel),
            };
        },

        dotsFor(level) {
            const idx = EFFORT_LEVELS.indexOf(level);
            return idx >= 0 ? idx + 1 : 0;
        },
    }));
});
