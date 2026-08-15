/**
 * One floating surface open at a time — the `.picker-popover` / `.composer-sheet` roster
 * in `input.css`. Below 1100px they are all bottom sheets pinned to the same edge, so two
 * open at once render stacked.
 *
 * They cannot dismiss each other the usual way: a pill trigger carries `@click.stop`, or
 * the popover's own document-level `@click.outside` would fire on the click that opened it,
 * and the neighbours never hear that click either. So each surface announces its own open.
 *
 * Only the last opener can still be showing, so one slot replaces a broadcast, and a stale
 * entry left by a torn-down component costs nothing — hence no unsubscribe.
 *
 *     init() { this._announceOpen = surfaceGroup.join(() => this.close()); },
 *     toggle() { this.open = !this.open; if (this.open) this._announceOpen(); },
 */
let current = null;

window.surfaceGroup = {
    /** Join the group; call the returned function whenever this surface opens. */
    join(close) {
        return () => {
            if (current && current !== close) current();
            current = close;
        };
    },
};
