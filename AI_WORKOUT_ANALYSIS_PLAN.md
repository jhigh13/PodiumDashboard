# AI Workout Compliance Analysis Plan

## Objectives
- Augment rule-based workout compliance with AI-driven evaluations that can interpret structured plan data and detailed execution files (e.g., power traces).
- Keep the feature optional so coaches can enable it per environment via configuration without code changes.
- Provide clear audit trails by persisting AI-produced insights alongside existing compliance summaries.
- Preserve privacy/security by controlling which data leave the platform and logging AI requests.

## Toggle & Settings
- Introduce a `ai_analysis_enabled` boolean in `app.utils.settings.Settings` bound to an `AI_ANALYSIS_ENABLED` env var (default `False`).
- Add optional settings for provider selection and model identifiers, e.g.:
  - `ai_analysis_provider` ("azure_openai", "openai", etc.).
  - `ai_analysis_model` (e.g., `gpt-4o-mini`).
  - `ai_analysis_max_tokens`, `ai_analysis_temperature` for tuning.
- Display the toggle and current provider/model on the dashboard (read-only) so staff can confirm status.

## Data Flow Overview
1. **Data Packaging**
   - Reuse existing plan parsing helpers to build a structured prompt containing: planned segments, targets, constraints.
   - Summarize actual execution data (power trace stats, intervals, anomalies) into a compact JSON payload.
   - Include compliance service outputs (distance %, duration %, pace deltas) for context.
2. **AI Request Layer**
   - Create `app/services/ai_analysis.py` to orchestrate prompt assembly, provider calls, telemetry, and retries.
   - Support dry-run mode (log only) when the feature is enabled but no provider credentials exist.
3. **Result Persistence**
   - Extend `WorkoutCompliance` (or add a companion table) with fields for `ai_summary`, `ai_recommendations`, `ai_model`, and `ai_run_at`.
   - Store raw response metadata (token usage, latency) for monitoring.
4. **UI Rendering**
   - When AI data is present, show an expandable "Coach Notes (AI)" panel in the compliance table with bullet insights and recommended follow-ups.
   - Provide indicators when the AI analysis is pending or failed.

## Provider Integration Steps
- Abstract provider specifics behind an interface so switching from (Azure) OpenAI to another vendor is configuration-only.
- Start with a text completion or responses API that supports JSON tool outputs for structured recommendations.
- Build defensive prompt templates: include explicit instructions on tone, formatting, max bullets, and caution around definitive medical claims.
- Implement rate limiting/backoff to respect provider quotas.

## Error Handling & Observability
- Log every AI call (prompt hashes, model, duration, status) to a dedicated table or structured log for later audits.
- Surface failures in the UI (e.g., "AI analysis unavailable; using rule-based scores only") without blocking the ingest pipeline.
- Add health checks to verify credentials at startup when the feature is enabled.

## Testing Strategy
- Unit tests for prompt builders to ensure planned/actual data formatting.
- Integration tests mocking the AI client to simulate success, timeout, and error cases.
- Feature-flag tests confirming that when `ai_analysis_enabled=False`, no AI code paths execute and the UI hides AI panels.
- Manual end-to-end test: ingest a workout with known anomalies, trigger AI analysis, confirm persisted summary and UI rendering.

## Rollout Plan
1. **Phase 1** – Scaffold (settings, toggles, plumbing, placeholder responses).
2. **Phase 2** – Provider integration with mocked responses and limited pilot usage.
3. **Phase 3** – Expand UI display, add coach feedback loop, consider storing delta between AI judgement and rule-based score.
4. **Phase 4** – Performance tuning, caching of analyses, option to re-run AI on demand.

## Risks & Mitigations
- **Cost Overruns**: Implement caching and incremental updates; allow per-day limits via settings.
- **Data Privacy**: Redact personal identifiers; document what fields leave the system.
- **Model Drift**: Schedule periodic evaluations comparing AI outputs vs. coach feedback.
- **Feature Adoption**: Provide clear UX controls and documentation so coaches understand when AI insights are active.

This document captures the high-level approach; revisit before coding to align on provider selection and any additional compliance requirements.
