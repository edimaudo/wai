# Accessibility implementation and verification

## Target

The application is engineered toward WCAG 2.2 Level AA. Accessibility is treated as a release requirement, not a visual theme. This document distinguishes implemented code-level measures from the manual testing still required for a formal conformance claim.

## Implemented measures

| Area | Implemented measure | Verification in this build |
|---|---|---|
| Language | Document language switches between `en-CA` and `fr-CA`; selected language persists through sign-in, and calendar dates use French month names when French is selected. | Rendered-page check across public and authenticated pages. |
| Structure | Shared `header`, `nav`, `aside`, `main` and `footer` landmarks; one main-content target; page titles. | DOM inspection on all key routes. |
| Keyboard navigation | Skip-to-content link, visible `:focus-visible` outline, native buttons/links/forms, keyboard-focusable scrollable table regions and no pointer-only core workflow. | DOM inspection; keyboard walk-through still required on deployed browsers. |
| Names and instructions | Form inputs/selects/textareas have associated labels or accessible names; icon-only controls have ARIA labels. Flash messages use status semantics, with assertive alerts for errors. | Automated label audit on all key pages. |
| Contrast | Core text, link, focus, status and notice colours are paired with their light/dark backgrounds; theme-specific focus indicators are used. | Automated test checks the core foreground/background token pairs against the 4.5:1 AA normal-text threshold. All component states still need a full contrast audit. |
| Resize and reflow | Small/medium/large text scaling, responsive layouts, wrapping and horizontally scrollable data tables. | CSS inspection; 200% zoom and 320 CSS-pixel reflow test still required. |
| Touch targets | Theme and language controls are at least 44px; compact buttons at least 40px; focusable summary controls are enlarged. | CSS inspection. |
| Motion and display modes | Reduced-motion media query, forced-colors support and light/dark themes. | CSS inspection; OS/browser mode checks still required. |
| Charts | Plotly visualizations have associated, visible-on-demand HTML data tables with captions and row/column headers. | DOM inspection of chart/table relationships. |
| Constituent source data | The profile view uses a labelled search field, table caption, scoped headers, row headers and a keyboard-focusable horizontal table region. | Route and form-label tests include `/app/constituents`. |
| Authentication | Native input fields; password manager/autofill and paste are not blocked by custom scripts. | Code inspection; accessible authentication flow must be validated with assistive technology. |

## Manual release checklist

Complete and record these checks with the deployed application and real data before describing it as fully WCAG 2.2 AA conformant:

- Keyboard-only operation of every workflow, dialog, menu, filter, details disclosure, chart alternative, form and download; verify logical focus order and no keyboard traps.
- Screen-reader review with NVDA + Firefox or Chrome on Windows and VoiceOver + Safari on macOS/iOS; verify landmarks, headings, labels, validation messages, status updates and chart table alternatives.
- Browser zoom at 200% and 400%, plus a 320 CSS-pixel viewport; verify no loss of content or functionality and no two-dimensional scrolling except where data tables require it.
- Contrast testing of all text, controls, focus indicators, errors, selected states and hover states in both light and dark themes, including focus and disabled states. The automated check covers core token pairs, not every possible rendered combination.
- Verify target sizes and spacing for all interactive controls, including dense tables and responsive navigation.
- Run an automated WCAG audit (for example, axe-core) against every route and resolve all applicable serious/critical findings; automated scans do not replace manual review.
- Validate French screen-reader pronunciation, accents, translated validation/errors, and language consistency in both sign-in states.

## Current status

Code-level checks and route/DOM tests are included in the automated suite. A full cross-browser assistive-technology audit and third-party WCAG conformance certification have not been completed in this build. Do not claim formal conformance until the manual release checklist passes.
