/**
 * Alpine component: agent model + thinking-effort pill + popover.
 *
 * The model catalog is no longer rendered server-side — we fetch it on first
 * popover open via ``catalogUrl`` and hold the result for the page lifetime.
 * The search input doubles as a free-text submit field (Enter or click the
 * "Use exact name" affordance) so users can always type a model even when
 * the catalog is empty or the endpoint failed.
 *
 * Constructor args (passed via x-data):
 *   providers:            Array<{slug, label}> — enabled providers.
 *   catalogUrl:           string — fetched lazily on first popover open.
 *   initialAgentModel:    string — stored ``provider:model`` spec; ``""`` when no value
 *                                  was stored (legacy threads, or new threads where the
 *                                  admin default was unset/invalid). Falls back to
 *                                  ``defaultAgentModel``.
 *   initialThinkingLevel: string — ``"minimal" | "low" | "medium" | "high"``; ``""`` = unset.
 *   defaultAgentModel:    string — system default ``provider:model`` spec used to seed
 *                                  the picker when ``initialAgentModel`` is empty. May
 *                                  itself be ``""`` if the server has no usable default
 *                                  (unset, or gated out as unparseable / provider
 *                                  disabled) — in that case the picker renders the
 *                                  unselected "Auto" pill.
 */
const EFFORT_LEVELS = ["minimal", "low", "medium", "high"];

document.addEventListener("alpine:init", () => {
    Alpine.data("agentPicker", ({
        providers = [],
        catalogUrl = "",
        initialAgentModel = "",
        initialThinkingLevel = "",
        defaultAgentModel = "",
    } = {}) => ({
        providers: [...providers],
        catalogUrl,
        catalog: {byProvider: {}, status: "idle", error: null},
        selectedProvider: "",
        modelName: "",
        thinkingLevel: "",
        query: "",
        open: false,
        LEVELS: EFFORT_LEVELS,

        init() {
            // Stored spec wins; otherwise pre-select the system default so the form
            // always submits a concrete ``provider:model`` value. Split on the FIRST
            // colon so model names containing ``:`` (rare but valid) survive.
            const seed = initialAgentModel || defaultAgentModel;
            if (seed && seed.includes(":")) {
                const idx = seed.indexOf(":");
                this.selectedProvider = seed.slice(0, idx);
                this.modelName = seed.slice(idx + 1);
            }
            this.thinkingLevel = initialThinkingLevel || "";
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
            if (this.catalog.status !== "idle") return;
            if (!this.catalogUrl) {
                this.catalog.status = "error";
                this.catalog.error = "No catalog URL configured";
                return;
            }
            this.catalog.status = "loading";
            try {
                const res = await fetch(this.catalogUrl, {credentials: "same-origin"});
                if (!res.ok) throw new Error(`HTTP ${res.status}`);
                const data = await res.json();
                this.catalog.byProvider = data.catalog || {};
                this.catalog.status = "loaded";
            } catch (err) {
                this.catalog.status = "error";
                this.catalog.error = (err && err.message) || "Network error";
            }
        },

        selectProvider(slug) {
            // Switching provider invalidates the model — clear it so the popover
            // forces a re-pick from the new provider's list.
            if (this.selectedProvider !== slug) {
                this.modelName = "";
            }
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
                // No provider yet and the typed value has no colon — refuse silently;
                // user must pick a provider tab first.
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
            // Hidden-input value. The picker is seeded with the system default at init
            // when one is configured, so this is usually a concrete ``provider:model``
            // spec. Empty when no usable default reached the client AND the user hasn't
            // picked one — server falls through to ``model_config.model`` in that case.
            return this.selectedProvider && this.modelName
                ? `${this.selectedProvider}:${this.modelName}`
                : "";
        },

        get pillLabel() {
            // Unselected fallback (no usable default reached the client): show "Auto"
            // so the pill never renders blank. The dot meter is hidden in this state.
            if (!this.selectedProvider || !this.modelName) {
                return {name: "Auto", effortDots: 0};
            }
            const effortIdx = EFFORT_LEVELS.indexOf(this.thinkingLevel);
            // Strip any ``org/`` path segment from the model name (e.g.
            // ``anthropic/claude-haiku-4.5`` → ``claude-haiku-4.5``) so the pill
            // stays compact next to the env pill. The ``provider:`` prefix was
            // already split off into ``selectedProvider`` at init time.
            const display = this.modelName.split("/").pop() || this.modelName;
            return {
                name: display,
                effortDots: effortIdx >= 0 ? effortIdx + 1 : 0,
            };
        },
    }));
});
