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
 * Pass an optional ``rowsRef`` to target a different x-ref (e.g. ``'customRows'``)
 * when multiple row containers share the same formset.
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
            if (idInput && idInput.value) {
                if (deleteInput) deleteInput.checked = true;
                rowEl.classList.add("hidden");
            } else {
                rowEl.remove();
            }
        },
    }));
});
