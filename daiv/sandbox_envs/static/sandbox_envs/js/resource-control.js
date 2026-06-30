/**
 * Alpine component for the Resources section of the sandbox env form.
 *
 * Owns:
 *   - `memMode` ('default'|'custom'), `memValue` (string), `memUnit` ('MiB'|'GiB')
 *   - `cpuMode` ('default'|'custom'), `cpuValue` (string)
 *
 * `resourcesOpen` is the collapsible's open state. Initialised true if any
 * resource field is overridden, has an error, or the parent template forces
 * it (GLOBAL-default form).
 *
 * Network is no longer owned here — it is managed by the top-level network
 * scope in the template.
 */
const MIB = 1024 * 1024;
const GIB = 1024 * 1024 * 1024;

document.addEventListener("alpine:init", () => {
    Alpine.data("resourceControl", (initial = {}) => ({
        memMode: initial.memValue ? "custom" : "default",
        memValue: initial.memValue || "",
        memUnit: initial.memUnit || "MiB",
        cpuMode: initial.cpuValue ? "custom" : "default",
        cpuValue: initial.cpuValue || "",
        resourcesOpen: !!initial.forceOpen
                       || !!initial.memValue
                       || !!initial.cpuValue
                       || !!initial.hasErrors,

        setMemMode(value) {
            this.memMode = value;
            if (value === "default") { this.memValue = ""; }
        },
        setCpuMode(value) {
            this.cpuMode = value;
            if (value === "default") { this.cpuValue = ""; }
        },

        get memHiddenValue() { return this.memMode === "custom" ? this.memValue : ""; },
        get memHiddenUnit() { return this.memMode === "custom" ? this.memUnit : "MiB"; },
        get cpuHiddenValue() { return this.cpuMode === "custom" ? this.cpuValue : ""; },
    }));
});
