/**
 * Alpine component: dynamic Django formset row addition/removal.
 *
 * Usage:
 *   <div x-data="djangoFormset({
 *            totalFormsId: '{{ formset.management_form.TOTAL_FORMS.id_for_label }}',
 *            initialTotal: {{ formset.total_form_count }} })">
 *     <div x-ref="rows">{# rendered rows #}</div>
 *     <template x-ref="rowTemplate">{# empty_form template (uses __prefix__) #}</template>
 *     <button @click="addRow()">Add</button>
 *   </div>
 *
 * Each row's remove button calls `removeRow($el.closest('[data-row]'))`.
 */
document.addEventListener("alpine:init", () => {
    Alpine.data("djangoFormset", ({ totalFormsId, initialTotal, rowsRef }) => ({
        total: initialTotal,
        addRow() {
            const totalInput = document.getElementById(totalFormsId);
            const target = rowsRef ? this.$refs[rowsRef] : this.$refs.rows;
            const html = this.$refs.rowTemplate.innerHTML.replaceAll("__prefix__", String(this.total));
            target.insertAdjacentHTML("beforeend", html);
            this.total += 1;
            totalInput.value = String(this.total);
        },
        removeRow(rowEl) {
            const idInput = rowEl.querySelector('input[name$="-id"]');
            const deleteInput = rowEl.querySelector('input[name$="-DELETE"]');
            // A row is "server-rendered" — and must be kept in the POST so its
            // index slot survives — when it maps to a persisted object (ModelFormSet:
            // has a non-empty ``-id``) or is explicitly marked ``data-initial``
            // (plain formset: dropping its DOM node would leave a gap that an
            // initial-index form validates as an empty, invalid row). Such rows are
            // marked for deletion and hidden; only brand-new client rows are removed.
            const serverRendered = (idInput && idInput.value) || rowEl.hasAttribute("data-initial");
            if (serverRendered) {
                if (deleteInput) deleteInput.checked = true;
                rowEl.classList.add("hidden");
            } else {
                rowEl.remove();
            }
        },
    }));
});
