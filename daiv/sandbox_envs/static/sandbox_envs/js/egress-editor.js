/**
 * Alpine component: egress policy editor for the sandbox environment form.
 *
 * Rules-first with inline credentials. Each host row may attach a credential
 * (header + masked value); the named secret the backend stores is synthesised
 * here as a stable `secret_name` (minted once when the credential is enabled)
 * and round-tripped so unchanged secrets are preserved on save.
 *
 * Constructor arg: {default, intercept, hosts: [...]} — the masked initial
 * state produced by SandboxEnvironmentForm._initial_egress_json().
 */
function mintSecretName() {
    const rand = (window.crypto && crypto.randomUUID)
        ? crypto.randomUUID()
        : Math.random().toString(36).slice(2);
    return "s_" + rand.replace(/-/g, "");
}

function egressEditorState(initial = {}, labels = {}) {
    const hosts = Array.isArray(initial.hosts) ? initial.hosts : [];
    return {
        default: initial.default === "allow" ? "allow" : "deny",
        intercept: initial.intercept === "credentialed" ? "credentialed" : "all",
        labels: {
            duplicateHost: labels.duplicateHost || "Duplicate host",
        },
        hosts: hosts.map((h) => {
            const methods = Array.isArray(h.methods) && h.methods.length ? h.methods : ["*"];
            const isAny = methods.length === 1 && methods[0] === "*";
            return {
                host: h.host || "",
                methodsMode: isAny ? "any" : "custom",
                methodsText: isAny ? "" : methods.join(", "),
                showCredential: !!(h.header || h.has_existing_value),
                header: h.header || "",
                value: h.value || "",
                secret_name: h.secret_name || "",
                has_existing_value: !!h.has_existing_value,
            };
        }),

        addHost() {
            this.hosts.push({
                host: "", methodsMode: "any", methodsText: "",
                showCredential: false, header: "", value: "",
                secret_name: "", has_existing_value: false,
            });
        },

        removeHost(i) { this.hosts.splice(i, 1); },

        toggleCredential(i) {
            const h = this.hosts[i];
            h.showCredential = !h.showCredential;
            if (h.showCredential) {
                if (!h.secret_name) h.secret_name = mintSecretName();
            } else {
                h.header = "";
                h.value = "";
                h.secret_name = "";
                h.has_existing_value = false;
            }
        },

        hostError(i) {
            const h = this.hosts[i];
            if (!h) return "";
            const host = (h.host || "").trim();
            if (!host) return "";  // blank rows are dropped, not flagged
            for (let j = 0; j < i; j++) {
                if ((this.hosts[j].host || "").trim() === host) return this.labels.duplicateHost;
            }
            return "";
        },

        _methodsFor(h) {
            if (h.methodsMode !== "custom") return ["*"];
            const parts = (h.methodsText || "").split(",").map((s) => s.trim()).filter(Boolean);
            return parts.length ? parts : ["*"];
        },

        get serialised() {
            return {
                default: this.default,
                intercept: this.intercept,
                hosts: this.hosts
                    .filter((h) => (h.host || "").trim())
                    .map((h) => {
                        const hasCred = h.showCredential && (h.header || "").trim();
                        return {
                            host: (h.host || "").trim(),
                            methods: this._methodsFor(h),
                            header: hasCred ? (h.header || "").trim() : "",
                            value: hasCred ? (h.value || "") : "",
                            secret_name: hasCred ? h.secret_name : "",
                            has_existing_value: !!h.has_existing_value,
                        };
                    }),
            };
        },
    };
}

document.addEventListener("alpine:init", () => {
    Alpine.data("egressEditor", egressEditorState);
});

window.egressEditorState = egressEditorState;
