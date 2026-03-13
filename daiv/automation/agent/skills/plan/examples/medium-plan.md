# Example: Medium Plan — Add Search Filter to List View

**User request:** "Add a date range filter to the transactions list page so users can filter by start and end date."

**Complexity assessment:** Medium. Touches frontend form, backend query, and tests. Follows existing filter patterns.

**Exploration performed:** Read `src/views/TransactionListView.py`, `src/filters/transactions.py`, `src/templates/transactions/list.html`. Traced the existing `status` filter to understand the pattern. Checked `src/tests/test_transaction_views.py` for existing filter tests.

---

# Add Date Range Filter to Transactions List

## Overview

The transactions list already supports filtering by `status` via `django-filter`. Adding a date range filter follows the same pattern — extend the existing `TransactionFilter` class with `DateFromToRangeFilter` fields and add the corresponding form inputs to the template.

## Changes

1. **Add date range fields to the filter class** — extend existing filter with `created_at` range
   - `src/filters/transactions.py` (line ~12) — add `created_at` field using `DateFromToRangeFilter`

2. **Add date picker inputs to the template** — render the new filter fields in the existing filter form
   - `src/templates/transactions/list.html` (line ~28) — add two date inputs inside the existing `<form class="filters">` block, following the same markup pattern used by the status filter

3. **Update view queryset** — ensure the filterset is applied to the queryset
   - `src/views/TransactionListView.py` — no change needed, the view already applies `TransactionFilter` to the queryset via `filterset_class`

4. **Add tests** — cover the new filter
   - `src/tests/test_transaction_views.py` — add tests for: date range returns matching records, empty range returns all, invalid date returns validation error

## Testing

- Filter with both dates set → only transactions within range returned
- Filter with only start date → transactions from that date onward
- Filter with only end date → transactions up to that date
- Verify the existing `status` filter still works when combined with date range

## Edge Cases

- User enters start date after end date → `DateFromToRangeFilter` handles this by returning empty queryset; consider adding a form-level validation message
- Transactions with `created_at` exactly at midnight boundaries → use `date` lookup, not `datetime`, to match user expectations
