## Findings

### High

**1. `process_payment` silently swallows exceptions and returns `None`** — [payments/service.py:87](https://gitlab.example.com/org/project/-/blob/feature-branch/payments/service.py#L87)

<details>
<summary>Details</summary>

The bare `except Exception: pass` on line 87 catches all errors including `IntegrityError` and `TimeoutError`, discarding them. The caller checks `if result:` but `None` is also returned on success when the payment amount is zero, so the error path is indistinguishable from a legitimate zero-amount payment.

Replace with explicit exception handling that re-raises unexpected errors:

```python
except (PaymentGatewayError, ValidationError) as e:
    logger.error("Payment failed for order %s: %s", order.id, e)
    return None
```

</details>

**2. Missing `is_provider` argument in recursive `check_routing` call** — [commands/calculate_retainer.py:64](https://gitlab.example.com/org/project/-/blob/feature-branch/commands/calculate_retainer.py#L64)

<details>
<summary>Details</summary>

The recursive call `self.check_routing(routing)` drops the `is_provider` parameter. Provider renovations for chained transfers are silently skipped because the recursive step always takes the insurer path.

Fix:

```python
self.check_routing(routing, is_provider)
```

</details>

### Medium

**3. `count` uses `=` instead of `+=` inside the loop** — [commands/create_facts.py:72](https://gitlab.example.com/org/project/-/blob/feature-branch/commands/create_facts.py#L72)

<details>
<summary>Details</summary>

`count = len(...)` resets the counter each iteration. If the outer loop runs more than once, only the last iteration's count is kept. Currently the loop runs once, but this is fragile.

```python
count += len(ProcessFacts.objects.bulk_create(...))
```

</details>

## Suggestions

- `getDistinctCountries()` runs on every page render with no caching — consider wrapping in a short-lived cache.
- The two `TODO` comments in `calculate_retainer.py` are self-described production blockers — track them.

## Tests

- `process_payment` with a gateway timeout — should propagate the error, not return `None`.
- `check_routing` with chained transfers and `is_provider=True` — catches finding #2.
- `create_facts` loop with multiple date iterations — verifies count accumulation.
