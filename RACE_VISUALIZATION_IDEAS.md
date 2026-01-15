# Race + Recovery Visualization Ideas (PodiumDashboard)

Context: We now have (a) cached WTO race results (event date, name, status, finish placement) and (b) TrainingPeaks daily metrics when available (sleep hours, HRV, RHR, etc.). The coach’s primary question is typically: **what differs in the lead-up to good vs bad races**.

This document captures brainstormed visualization patterns. Initial implementation focus: **Sleep Hours + Finish Placement**.

## 1) Timeline overlay (season view)

**Goal:** Quickly spot relationships between a recovery signal and race outcomes across a season.

- X-axis: calendar date (last 2 years / selected range)
- Line (left axis): sleep hours (daily) + optional rolling average (7d)
- Race markers (right axis): finish placement at each race date
  - Consider reversing the placement axis so “1” is at the top (better)
  - Non-finishes (DNF/DNS/DSQ/LAP) can be shown as special markers/annotations

Why it works:
- Coaches can immediately see if sleep dips/spikes cluster before poorer outcomes.

## 2) Lead-up profile (race-aligned view)

**Goal:** Compare best vs worst races aligned to race day.

- X-axis: days relative to race (e.g. -28 … 0)
- Y-axis: sleep hours (or 7d average)
- Overlay lines:
  - best finished race
  - worst finished race
  - worst including non-finishes (optional)

Why it works:
- Removes season noise and highlights taper / travel disruption patterns.

## 3) Small multiples (stacked charts)

**Goal:** Keep charts readable by splitting signals into separate bands.

- Chart A: sleep hours (7d)
- Chart B (later): HRV (7d) if available
- Race markers repeated on each chart for alignment

Why it works:
- Cleaner than combining many lines on one plot.

## 4) Compact race list (improve readability)

**Goal:** Make races scannable and avoid huge blocks of text.

- Emphasize numeric outcome (placement) and date
- Truncate long event names in the table, but keep full details in hover/expanders
- Use smaller typography for descriptors (event/program), larger for the key number (placement)

Why it works:
- Coaches scan placements and dates first; details are secondary.

## 5) Scatter plot (relationship view)

**Goal:** Show correlation between lead-up sleep and finish placement.

- Each point = one race
- X-axis: average sleep in the N days pre-race (e.g. 28d)
- Y-axis: finish placement (or normalized performance score)

Why it works:
- Highlights patterns even when the timeline is messy.

## Notes / constraints

- Some athletes are non-premium in TrainingPeaks; daily metrics (sleep/HRV/RHR) may be missing.
- For those athletes, race visualizations should still work, and the UI should gracefully communicate “sleep unavailable”.
- Coach preference: avoid TrainingPeaks TSS/CTL as a primary load metric; focus on sleep first.
