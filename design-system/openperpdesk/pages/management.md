# Management Workspace Override

## Design Read

Preserve the Chinese trading terminal and its nine routes. Management pages
use a dense, quiet financial-dashboard language with continuous surfaces,
semantic status colors, and low motion. Variance 3, motion 2, density 9.

## Audit And Skills

Read the installed `taste-skill` redesign protocol and `ui-ux-pro-max` entrypoint.
Use Taste's audit, preservation, anti-template and contrast rules, not its
marketing layouts. UI/UX Pro Max's explicit `financial trading dashboard`
product query matched Financial Dashboard. Its table and error-feedback
queries support contained scrolling, visible labels and announced failures.
Keep the existing native HTML/CSS/JS stack, local Lucide assets and theme tokens.
The generated global master remains unchanged.

The audit found that backtest results existed only in a temporary message;
their input equity was taken from the hidden order form. Audit events lacked
filters and hierarchy. Existing market/research interactions remain intact.

## Performance And Backtest

- Keep fill-based performance separate from hypothetical backtest results.
- Show the ledger's starting equity and basis; do not label it account NAV.
- Use the main column for ledger metrics and its curve. Put independent
  backtest parameters and retained results in a continuous right-hand surface.
- Stack on narrower screens. Keep the form's visual order and tab order aligned.
- Capture the contract, interval, requested sample count, equity and fee
  parameter with each result. Changed drafts do not relabel previous results.
- Preserve inputs and previous results when a new request fails.
- Scope the UI to the existing close-price replay engine. State its missing
  funding, slippage and intrabar execution models; do not imply exchange fills.
- Losing private access clears retained results and invalidates late responses.

## Audit And Account Tables

- Summary counts describe loaded records only, never the whole server history.
- Filter loaded events by severity and event/message text, with a reset action.
- Keep severity text alongside color. Render event identifiers and messages
  as escaped text, with no raw payload expansion.
- Keep filters on refresh and previous rows on failure; clear rows on access loss.
- Use explicit keyboard-focusable scroll regions for account tables.
- Bound long tables and keep headers visible while their body scrolls.
- Hide wide headers only for a single empty row, not for a truncation footer
  following real data.

## Risk And Shared Shell

Keep the global execution lock and emergency control visible. Compact the
secondary account metrics, and separate risk connections into account/execution
and external-service columns on wide screens. Retain a single-column fallback,
server-authoritative thresholds, and the existing protected live-unlock controls.

## Historical Bills

- Use a disclosure below daily bills, with visible UTC date labels and distinct
  query/import commands. Keep the eight-column table inside a labelled,
  keyboard-focusable scroll region.
- Keep pagination in one row on the wide ledger. Research sidebar wrapping
  must be scoped to that sidebar and cannot affect full-width ledger controls.
- Show import acceptance, progress, completion, failure and interruption as
  distinct states. Coverage completeness is not a profitability claim.
- Show original-currency amounts without JavaScript numeric conversion.
  Separate recognized perpetual PnL from trading-account transfers.
- Preserve the previous result identity on errors or edited date drafts;
  invalidate cursors until a new query succeeds. Clear private results and
  busy states when permission is lost.
- Poll only while the history disclosure and performance view are active.

## Verification

`infra/management-ui-checks.mjs` is part of `infra/ui-smoke.mjs`. It tests both
themes at eight viewport sizes, parameter isolation, retained result identity,
request failures, duplicate clicks, late responses after permission loss,
escaped audit content, filter reset/focus, table headers, containment and
nonblank curve pixels. Content-rich screenshots use explicit browser fixtures,
not real account performance or OKX execution.
`infra/bill-history-ui-checks.mjs` adds both-theme history controls, exact
amount display, range/pagination, coverage, error preservation, permission
loss, delayed responses, duplicate imports and keyboard scroll-region checks.
