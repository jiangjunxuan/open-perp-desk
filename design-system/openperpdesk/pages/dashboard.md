# OpenPerpDesk Dashboard Override

> This page-level override refines the global OpenPerpDesk design system for the
> operational perpetual-contract trading workspace. It does not change the
> safety model or the global product direction.

## Design Read

Chinese-language trading and operations dashboard for repeat, high-attention
use. Keep the interface dense and calm, with the visual hierarchy ordered as:
market evidence, decision layer, risk gate, then account and audit history.

## Dials

- Variance: 3/10
- Motion: 2/10
- Density: 9/10

## Tokens

- Background: `#101113`
- Surface: `#1d1f22`
- Raised surface: `#292c30`
- Workspace: `#16181b`
- Border: `#3b3e44`
- Primary text: `#f1f2f4`
- Muted text: `#a9aeb7`
- Positive accent: `#51d6a3`
- Primary action: `#e8ebee` with `#202329` text
- Warning: `#e9bd67`
- Destructive: `#ff797f`
- Informational focus: `#a6c8ff`
- Radius scale: 5px controls, 8px dialogs, unframed page sections

## Layout

- Preserve existing section anchors as hash routes and keep control IDs.
- Nine separate work views with browser history and deep links; never stack
  all administration modules into one long page.
- Keep the top context bar visible while scrolling.
- Use the chart and safety gate as the first main grid.
- Treat metrics as a connected summary strip, not four unrelated feature cards.
- Keep wide tables inside horizontal overflow wrappers on narrow screens.
- Collapse the content columns below 1024px and use a native navigation dialog
  below 768px. Generate its routes from the existing sidebar navigation.
- Use the existing plain HTML, CSS and JavaScript stack. No framework migration.
- Use local Lucide assets and their upstream license, not hand-drawn glyphs.

## Interaction

- Maintain 44px minimum targets for buttons, inputs, selects, and navigation.
- Keep focus rings visible and reserve scroll space below the sticky header.
- Use text plus semantic color for states; never use color as the only signal.
- Show loading, empty, error, locked, and read-only states inline.
- Respect `prefers-reduced-motion`, including the live status indicator.
- Keep demo mode and execution locks visible in the first viewport.
- Use a native dialog for administrator access with explicit input labels,
  Escape handling, input clearing and focus restoration.
- Reserve fixed 44px tool-button dimensions and preserve icon markup while busy.
- Use request sequence checks so obsolete market responses do not repaint a
  newer selected contract.

## Skill Application

Taste Skill supplies the audit-first and anti-template checks. Its marketing
layout defaults do not apply to this terminal. UI UX Pro Max's explicit
`product` search selected Financial Dashboard / Data-Dense Dashboard.
The generic design-system search also returned an Enterprise Gateway marketing
pattern; that pattern was rejected as off-topic, not persisted as a design
decision. This page override takes precedence over the older generated master.

## Chart

Retain the project's native Canvas plot. Include OHLC, volume from the actual
OKX candle rows, a line-view option, and 40/80/100 candle ranges. Show a vertical
cursor with mouse and keyboard support. Keep current quote, market summary,
update timestamp, OHLC readout and accessible text outside the canvas.
Label the browser's 15-second snapshot refresh honestly. Display only the
observed best bid/ask, not fabricated depth rows.

## Terminal Layout Refinement

- Compact title and controls; chart plus tabbed ledger occupy the main column.
- Strategy, execution controls and preflight review occupy one right rail.
- Ledger tabs support arrow keys, Home/End, roving focus and tabpanel labels.
- Preserve all nine routes; tabs do not replace independent account views.
- Include contextual unlock entry points and return dialog focus to the opener.
- Invalidate preflight on any draft change. Distinguish simulation from
  exchange-backed checks and do not imply a preview is an order fill.
- Keep absent account and market values unknown rather than formatting as zero.

## September 13 Refactor

- Merge contract controls and the watchlist into one responsive market toolbar.
- Keep the chart above repeated system summaries; use a compact workspace title.
- Separate signal research from order verification using accessible terminal
  tabs. Keep their inputs in the same DOM and preserve state across tab changes.
- Show both panes in the dedicated strategy view, alongside strategy settings.
- Filter only loaded records, explicitly display loaded and matching counts,
  and do not change account metrics or ledger totals when filtering.
- Preserve filter input on data refresh, and offer an empty-result reset.
- Use text and semantic status dots for order states, including unknown outcomes.
- Render the latest candle close as a horizontal reference line and axis label.
- Use two explicit mobile chart-toolbar rows with fixed 44px tools.
- Recheck all seven viewport sizes, containment, keyboard focus, tab state,
  filtering, draft invalidation and locked execution controls.

## Operational Polish

- Tighten header and market layout rhythm while preserving 44px pointer targets.
- Give the terminal action rail a continuous surface, without rounded section cards.
- Collapse secondary market statistics on phones with an explicitly labelled,
  keyboard-operable disclosure; keep all six statistics visible on desktop.
- Group strategy fields semantically as run mode, technical indicators and
  position constraints. Preserve IDs, DOM-held drafts and existing commands.
- Expose account synchronization from the terminal ledger and share busy state
  with the existing account-sync entry point.
- Empty or locked tables use viewport-width states without an empty wide header;
  populated tables keep their complete horizontally scrollable columns.
- Render risk thresholds from server status in a read-only column. Missing values
  remain unknown and numeric zero is valid.
- Present execution locks consistently across the header, risk summaries and
  submit controls. Never replace or weaken server-side gates.

## Terminal Refactor And Focus Mode

- Align the terminal action rail with the contract toolbar, with chart and
  tabbed records sharing the main column. On wide screens, place the instrument
  price alongside the six market statistics.
- Keep a continuous watchlist tape, clear numeric hierarchy and a smaller
  overview chart. Do not add marketing sections, fictional balances or depth.
- Focus mode is an in-memory CSS layout state. Preserve chart data, strategy
  signals, ledger tabs, input drafts and the user's collapsed-sidebar state.
  Always retain global execution status and emergency controls.
- Exit focus mode with the same button, Escape or a route change. Restore the
  chart button's keyboard focus after explicit exit.
- Mobile navigation uses the existing native dialog pattern, current-route
  focus, explicit close focus restoration and automatic closure at desktop
  widths. Do not change routes, labels or create a second route source.
- Mirror execution status in the mobile header, using the same server status
  and gate calculation as desktop.
- Preserve keyboard focus when watchlist buttons are replaced by fresh data.
- Extend browser checks for focus-mode canvas pixels and layout restoration,
  safe controls, drafts, navigation and Escape. Inspect the actual screenshots.

## Research Workspace

- Keep the research reader on the left and existing decision/execution controls
  on the right; collapse into one column below the existing laptop breakpoint.
- Use a native disclosure for history and a labelled select for report sections.
  Keep history rows keyboard-operable with visible selection and focus.
- Fetch only paginated history metadata, then one full report on demand.
- Report identity and market evidence are always explicit. A report from another
  contract or interval must never appear to belong to the current selection.
- Viewing history does not mutate the active signal or preflight draft. New AI
  research clears the old executable signal and never creates one.
- Treat model text as untrusted: locally vendored parser and sanitizer, strict
  tags, no embedded media, no raw HTML, no relative application-action links.
- Label runtime readiness separately from actual model-service connectivity.
- Verify stale requests, permission loss, errors, malicious markup, table
  containment and full report navigation at all seven existing viewport sizes.

## Appearance And Reading Refactor

- Preserve dark as the default; provide an explicitly selected light theme.
  All page surfaces, controls, charts and dialogs follow the same theme.
- Light tokens use a white workspace, `#f4f5f7` shell, `#20242b` primary text,
  `#5c6470` muted text, `#08764f` positive and `#bd2c3c` negative states.
- Separate neutral primary actions from positive market/status colors.
  Use semantic hover, pressed, destructive and chart-label tokens in both themes.
- Bootstrap the validated appearance preference before CSS. Only store the
  light/dark preference, with a non-blocking storage fallback.
- Preserve form nodes, active reports, trading state and chart focus on theme
  changes. Redraw both canvases without issuing orders or fetching private data.
- Keep the desktop toggle in the header and the mobile toggle in the navigation
  dialog, with synchronized pressed states and local Lucide sun/moon assets.
- Expand desktop navigation to 208px and use a two-level workspace heading.
  Continue using continuous surfaces and dividers rather than section cards.
- Use three research columns at 1440px and above: history, report, actions.
  Keep the report measure bounded and retain existing smaller-screen fallbacks.
- Validate both themes across nine routes and seven widths, including 1920px.
  Verify text contrast, canvas labels, hover colors, reload persistence, blocked
  storage, multi-tab appearance changes and unchanged execution gates.
