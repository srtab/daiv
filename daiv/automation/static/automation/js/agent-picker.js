/**
 * Alpine component: agent model + thinking-effort pill + popover.
 *
 * Mirrors the structure of ``sandbox_envs/js/env-picker.js`` — same registration
 * pattern (``Alpine.data`` inside ``alpine:init``), same popover idioms — but the
 * picker carries TWO pieces of state (provider/model and effort) instead of one.
 *
 * Constructor args (passed via x-data):
 *   providers:            Array<{slug, label}> — enabled providers from ``Provider.objects``.
 *   models:               Object<{providerSlug: Array<modelName>}> — server-curated suggestions
 *                         per provider. Free-text model names are also accepted by the backend
 *                         (``parse_model_spec``); the suggestions are a convenience only.
 *   initialAgentModel:    string — e.g. ``"openrouter:anthropic/claude-haiku-4.5"``; ``""`` = Auto.
 *   initialThinkingLevel: string — ``"minimal" | "low" | "medium" | "high"``; ``""`` = unset.
 *
 * Emits no window events — the hidden inputs ``agent_model`` / ``agent_thinking_level``
 * are mirrored from the component's state and submitted with the form.
 */
const EFFORT_LEVELS = ["minimal", "low", "medium", "high"];

document.addEventListener("alpine:init", () => {
    Alpine.data("agentPicker", ({
        providers = [],
        models = {},
        initialAgentModel = "",
        initialThinkingLevel = "",
    } = {}) => ({
        providers: [...providers],
        models: {...models},
        selectedProvider: "",
        modelName: "",
        thinkingLevel: "",
        query: "",
        open: false,
        LEVELS: EFFORT_LEVELS,

        init() {
            // Pinned model is encoded as ``provider_slug:model_name``; split on the
            // FIRST colon so model names containing ``:`` (rare but valid) survive.
            if (initialAgentModel && initialAgentModel.includes(":")) {
                const idx = initialAgentModel.indexOf(":");
                this.selectedProvider = initialAgentModel.slice(0, idx);
                this.modelName = initialAgentModel.slice(idx + 1);
            }
            this.thinkingLevel = initialThinkingLevel || "";
        },

        toggle() {
            this.open = !this.open;
            if (this.open) {
                this.query = "";
                this.$nextTick(() => this.$refs.search?.focus());
            }
        },

        close() {
            this.open = false;
        },

        selectAuto() {
            this.selectedProvider = "";
            this.modelName = "";
            this.thinkingLevel = "";
            this.query = "";
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
        },

        selectEffort(level) {
            this.thinkingLevel = level;
        },

        get filteredModels() {
            const list = this.models[this.selectedProvider] || [];
            const q = this.query.trim().toLowerCase();
            if (!q) return list;
            return list.filter(m => m.toLowerCase().includes(q));
        },

        get agentModelValue() {
            // Hidden-input value: empty string means "Auto" (server reads this as
            // "no override" and falls back to the default model).
            return this.selectedProvider && this.modelName
                ? `${this.selectedProvider}:${this.modelName}`
                : "";
        },

        get pillLabel() {
            // Auto state: dot meter is hidden (effortDots === 0). When a model is
            // pinned, ``effortDots`` indexes into [minimal, low, medium, high] +1
            // so the meter is always at least one dot when an effort is set.
            if (!this.selectedProvider || !this.modelName) {
                return {name: "Auto", effortDots: 0};
            }
            const effortIdx = EFFORT_LEVELS.indexOf(this.thinkingLevel);
            // Strip the provider prefix (e.g. ``anthropic/claude-haiku-4.5`` →
            // ``claude-haiku-4.5``) so the pill stays compact next to the env pill.
            const display = this.modelName.split("/").pop() || this.modelName;
            return {
                name: display,
                effortDots: effortIdx >= 0 ? effortIdx + 1 : 0,
            };
        },
    }));
});
