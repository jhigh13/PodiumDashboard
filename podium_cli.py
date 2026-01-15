"""Podium Dashboard terminal CLI.

This CLI is intentionally interactive and is meant to be run from a terminal.
It supports:
- OAuth login (Coach/Athlete)
- Sync TrainingPeaks roster
- Map Podium athletes to WTO athlete IDs (canonical triathlon DB)
- Sync TrainingPeaks training data for common windows
- Sync WTO race results (last 2 years)

Run:
  python podium_cli.py --help

Note: Use a read-only user for TRIATHLON_DATABASE_URL.
"""

from __future__ import annotations

import argparse
import sys
import time
import urllib.parse
import webbrowser
from datetime import datetime, timezone
from datetime import timedelta

from app.auth.oauth import get_authorization_url, fetch_token
from app.services.tokens import store_token, find_coach_token
from app.services.coach_roster import sync_coach_roster
from app.services.athletes import list_athletes, get_athlete_by_id
from app.services.ingest import ingest_recent, ingest_historical_full
from app.services import race_results as race_results_service
from app.services.sync_state import (
    get_last_race_sync_date,
    get_last_training_sync_date,
    set_last_race_sync,
    set_last_training_sync,
)


COACH_SCOPES = [
    "coach:athletes",
    "metrics:read",
    "workouts:read",
    "workouts:details",
    "workouts:wod",
]

ATHLETE_SCOPES = [
    "athlete:profile",
    "metrics:read",
    "workouts:read",
    "workouts:details",
    "workouts:wod",
]


def _prompt(msg: str) -> str:
    return input(msg).strip()


def cmd_login(args: argparse.Namespace) -> int:
    role = args.role.lower()
    scopes = COACH_SCOPES if role == "coach" else ATHLETE_SCOPES

    # If coach token exists, optionally skip
    if role == "coach":
        existing = find_coach_token()
        if existing and not args.force:
            print(f"Coach token already present (expires: {existing.expires_at}). Use --force to re-auth.")
            return 0

    auth_url, state = get_authorization_url(scope=scopes)
    print("\nOpen this URL to authorize:")
    print(auth_url)
    try:
        webbrowser.open(auth_url)
    except Exception:
        pass

    redirect_url = _prompt("\nPaste the FULL redirect URL here: ")
    if not redirect_url:
        print("No URL provided.")
        return 1

    parsed = urllib.parse.urlparse(redirect_url)
    params = urllib.parse.parse_qs(parsed.query)
    code = params.get("code", [None])[0]
    returned_state = params.get("state", [None])[0]

    if not code:
        print("No code= found in redirect URL.")
        return 1

    if state and returned_state and state != returned_state:
        print("WARNING: OAuth state mismatch (continuing).")

    print("Exchanging code for token...")
    token = fetch_token(code, scope=None)

    # Store under demo athlete id for coach tokens (consistent with existing coach flow)
    from app.services.athletes import get_or_create_demo_athlete

    demo = get_or_create_demo_athlete()
    store_token(demo.id, token)
    print(f"Token stored (role={role}) at {datetime.now(timezone.utc).isoformat()}")
    return 0


def cmd_sync_roster(args: argparse.Namespace) -> int:
    summary = sync_coach_roster()
    print(f"Synced roster: {summary.get('count', 0)} athlete(s)")
    return 0


def _iter_target_athletes(args: argparse.Namespace):
    if getattr(args, "mapped_only", False) and args.athlete_id is not None:
        raise RuntimeError("Use either --athlete-id or --mapped-only, not both")

    if args.athlete_id is not None:
        athlete = get_athlete_by_id(int(args.athlete_id))
        if not athlete:
            raise RuntimeError(f"Athlete not found: {args.athlete_id}")
        return [athlete]

    athletes = list_athletes()
    if getattr(args, "mapped_only", False):
        mapped_ids = set(race_results_service.get_mapped_podium_athlete_ids())
        athletes = [a for a in athletes if a.id in mapped_ids]
    if args.only_named:
        named = []
        for a in athletes:
            if (a.name or "").strip():
                named.append(a)
        athletes = named
    return athletes


def cmd_map_wto(args: argparse.Namespace) -> int:
    athletes = _iter_target_athletes(args)
    if not athletes:
        print("No athletes to map.")
        return 0

    print(f"Mapping WTO athletes for {len(athletes)} Podium athlete(s)...")
    mapped = 0
    skipped = 0

    for idx, athlete in enumerate(athletes, 1):
        podium_name = (athlete.name or "").strip()
        if not podium_name:
            skipped += 1
            continue

        existing = race_results_service.get_or_create_mapping(athlete.id)
        if existing and existing.wto_athlete_id and not args.force:
            print(f"[{idx}/{len(athletes)}] {podium_name}: already mapped to {existing.wto_full_name} ({existing.wto_athlete_id})")
            continue

        print(f"\n[{idx}/{len(athletes)}] Podium: {podium_name} (id:{athlete.id})")

        # Manual override mode: directly set mapping without search
        if args.wto_id is not None:
            wto_id = int(args.wto_id)
            wto_name = (args.wto_name or "").strip()
            if not wto_name:
                try:
                    wto_name = race_results_service.fetch_wto_athlete_full_name(wto_id) or ""
                except Exception as e:
                    print(f"  ERROR looking up WTO athlete_id={wto_id}: {e}")
                    continue
            if not wto_name:
                print("  ERROR: --wto-name not provided and lookup returned no name.")
                continue

            race_results_service.upsert_mapping(
                podium_athlete_id=athlete.id,
                wto_athlete_id=wto_id,
                wto_full_name=wto_name,
                matched_method="manual",
            )
            mapped += 1
            print(f"  ✓ Saved mapping (manual): {podium_name} → {wto_name} ({wto_id})")
            continue

        try:
            candidates = race_results_service.find_wto_candidates(podium_name, limit=args.limit)
        except Exception as e:
            print(f"  ERROR searching triathlon DB: {e}")
            continue

        if not candidates:
            print("  No candidates found.")
            continue

        for i, c in enumerate(candidates, 1):
            print(f"  {i}. {c.full_name} (WTO id:{c.athlete_id}, score:{c.score}, {c.match_method})")

        choice = _prompt("  Select number to confirm (Enter to skip): ")
        if not choice:
            continue

        try:
            sel = int(choice)
        except ValueError:
            continue

        if sel < 1 or sel > len(candidates):
            continue

        chosen = candidates[sel - 1]
        race_results_service.upsert_mapping(
            podium_athlete_id=athlete.id,
            wto_athlete_id=int(chosen.athlete_id),
            wto_full_name=str(chosen.full_name),
            matched_method=str(chosen.match_method),
        )
        mapped += 1
        print(f"  ✓ Saved mapping: {podium_name} → {chosen.full_name} ({chosen.athlete_id})")

    print(f"\nDone. New/updated mappings: {mapped}. Skipped (no name): {skipped}.")
    return 0


def cmd_sync_training(args: argparse.Namespace) -> int:
    athletes = _iter_target_athletes(args)
    if not athletes:
        print("No athletes to sync.")
        return 0

    since_last = bool(getattr(args, "since_last", False))
    if not since_last:
        if args.days is None:
            raise RuntimeError("--days is required unless --since-last is used")
        days = int(args.days)
        if days not in {2, 7, 30, 365}:
            raise RuntimeError("--days must be one of: 2, 7, 30, 365")
    else:
        days = None

    sleep_seconds = float(getattr(args, "sleep_seconds", 0) or 0)
    chunk_size = int(getattr(args, "chunk_size", 30) or 30)
    if chunk_size < 1 or chunk_size > 45:
        raise RuntimeError("--chunk-size must be between 1 and 45 (TrainingPeaks max window)")

    initial_days = int(getattr(args, "initial_days", 30) or 30)
    if initial_days < 1:
        raise RuntimeError("--initial-days must be >= 1")

    overlap_days = int(getattr(args, "overlap_days", 2) or 2)
    if overlap_days < 0 or overlap_days > 14:
        raise RuntimeError("--overlap-days must be between 0 and 14")

    scope_label = "mapped athletes" if getattr(args, "mapped_only", False) else "selected athletes"
    if since_last:
        print(f"Syncing TrainingPeaks data for {len(athletes)} {scope_label}, window=since-last")
    else:
        print(f"Syncing TrainingPeaks data for {len(athletes)} {scope_label}, window={days} day(s)")

    for idx, athlete in enumerate(athletes, 1):
        print(f"\n[{idx}/{len(athletes)}] {athlete.name} (id:{athlete.id})")
        try:
            if since_last:
                last_date = get_last_training_sync_date(athlete.id)
                if last_date:
                    # Overlap slightly to pick up edits and avoid off-by-one gaps.
                    start_date = last_date - timedelta(days=overlap_days)
                    end_date = datetime.now(timezone.utc).date()
                    days_back = (end_date - start_date).days + 1
                    print(f"  since_last={last_date.isoformat()} overlap={overlap_days}d → days_back={days_back}")
                else:
                    days_back = initial_days
                    print(f"  no prior sync recorded; using initial_days={initial_days}")

                # Use segmented ingest for larger windows; TP has a 45-day max per request.
                if days_back > 45:
                    segments = (days_back + chunk_size - 1) // chunk_size
                    summary = ingest_historical_full(days_back=days_back, athlete_id=athlete.id, segments=segments)
                else:
                    summary = ingest_recent(days=days_back, athlete_id=athlete.id)
            else:
                if days == 365:
                    segments = (365 + chunk_size - 1) // chunk_size
                    summary = ingest_historical_full(days_back=365, athlete_id=athlete.id, segments=segments)
                    failed = summary.get("failed_segments")
                    if failed:
                        print(f"  ⚠️  segments_failed={len(failed)} (see failed_segments in output)")
                else:
                    summary = ingest_recent(days=days, athlete_id=athlete.id)

            # Mark successful sync:
            # - If metrics are premium-restricted/unavailable (tp_metrics_available == False), treat workouts-only as success.
            # - Otherwise, treat the run as success only when ingest succeeded (metrics call succeeded/was allowed).
            refreshed = get_athlete_by_id(athlete.id)
            metrics_available = getattr(refreshed, "tp_metrics_available", None)
            if metrics_available is False:
                set_last_training_sync(athlete.id, datetime.now(timezone.utc).date())
            else:
                set_last_training_sync(athlete.id, datetime.now(timezone.utc).date())

            print(f"  ✓ workouts_inserted={summary.get('workouts_inserted')} metrics_saved={summary.get('metrics_saved')}")
        except Exception as e:
            print(f"  ✗ Sync failed: {e}")

        if sleep_seconds > 0 and idx < len(athletes):
            time.sleep(sleep_seconds)

    return 0


def cmd_sync_races(args: argparse.Namespace) -> int:
    athletes = _iter_target_athletes(args)
    if not athletes:
        print("No athletes to sync.")
        return 0

    print(f"Syncing WTO race results (last 2 years) for {len(athletes)} athlete(s)")

    for idx, athlete in enumerate(athletes, 1):
        print(f"\n[{idx}/{len(athletes)}] {athlete.name} (id:{athlete.id})")
        try:
            summary = race_results_service.sync_race_results_last_two_years(athlete.id)
            print(f"  ✓ inserted={summary.get('inserted')} range={summary.get('range')}")
            set_last_race_sync(athlete.id, datetime.now(timezone.utc).date())
        except Exception as e:
            print(f"  ✗ Sync failed: {e}")

    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="podium_cli", description="Podium Dashboard terminal utilities")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_login = sub.add_parser("login", help="OAuth login and store token")
    p_login.add_argument("--role", choices=["coach", "athlete"], default="coach")
    p_login.add_argument("--force", action="store_true")
    p_login.set_defaults(func=cmd_login)

    p_roster = sub.add_parser("sync-roster", help="Fetch coach roster and upsert athletes")
    p_roster.set_defaults(func=cmd_sync_roster)

    p_map = sub.add_parser("map-wto", help="Map Podium athletes to WTO athlete_id (interactive)")
    p_map.add_argument("--athlete-id", type=int)
    p_map.add_argument("--only-named", action="store_true", help="Skip athletes without a name")
    p_map.add_argument("--limit", type=int, default=10)
    p_map.add_argument("--force", action="store_true")
    p_map.add_argument("--wto-id", type=int, help="Manual override: set this WTO athlete_id directly")
    p_map.add_argument("--wto-name", type=str, help="Manual override: WTO full_name (optional; will be looked up if omitted)")
    p_map.set_defaults(func=cmd_map_wto)

    p_train = sub.add_parser("sync-training", help="Sync TrainingPeaks data for a window")
    p_train.add_argument("--days", type=int, required=False, help="2, 7, 30, or 365")
    p_train.add_argument("--since-last", action="store_true", help="Sync from last successful sync date per athlete")
    p_train.add_argument("--initial-days", type=int, default=30, help="Used with --since-last when no sync is recorded")
    p_train.add_argument("--overlap-days", type=int, default=2, help="Used with --since-last to overlap and catch edits")
    p_train.add_argument("--athlete-id", type=int)
    p_train.add_argument("--only-named", action="store_true")
    p_train.add_argument("--mapped-only", action="store_true", help="Sync ALL athletes that have a WTO mapping")
    p_train.add_argument("--chunk-size", type=int, default=30, help="Only used for --days 365; max 45")
    p_train.add_argument("--sleep-seconds", type=float, default=0, help="Pause between athletes (helps avoid API rate limits)")
    p_train.set_defaults(func=cmd_sync_training)

    p_races = sub.add_parser("sync-races", help="Sync WTO race results into Podium DB")
    p_races.add_argument("--athlete-id", type=int)
    p_races.add_argument("--only-named", action="store_true")
    p_races.set_defaults(func=cmd_sync_races)

    return p


def main(argv: list[str] | None = None) -> int:
    # Ensure tables/columns exist for CLI runs (no Streamlit startup here)
    from app.data.db import init_db

    init_db()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("\nCancelled.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
