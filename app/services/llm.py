from __future__ import annotations

from typing import Any, Dict, Optional
from app.utils.settings import settings

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None  # type: ignore[assignment]


class LLMClient:
    def __init__(self) -> None:
        self.api_key = settings.openai_api_key
        self.model = settings.openai_model
        self.client = None
        
        if self.api_key and OpenAI:
            self.client = OpenAI(api_key=self.api_key)

    def generate_daily_summary(
        self, 
        athlete_name: str, 
        date_str: str, 
        recovery_data: Dict[str, Any], 
        compliance_data: Optional[Dict[str, Any]]
    ) -> str:
        """
        Generate a daily summary email using OpenAI.
        
        Args:
            athlete_name: Name of the athlete
            date_str: Date of the summary
            recovery_data: Dictionary containing recovery metrics status
            compliance_data: Dictionary containing workout compliance data
            
        Returns:
            Generated email body text
        """
        if not self.client:
            return "Error: OpenAI client not initialized. Check API key and installation."

        # Construct the prompt with system instructions embedded
        prompt = self._build_prompt(athlete_name, date_str, recovery_data, compliance_data)
        full_prompt = f"You are an expert triathlon coach assistant. Your goal is to write a concise, professional, and actionable daily summary email for the head coach about an athlete's performance.\n\n{prompt}"
        
        try:
            response = self.client.responses.create(
                model=self.model,
                input=full_prompt
            )
            return response.output_text or "Error: Empty response from LLM."
        except Exception as e:
            return f"Error generating summary: {str(e)}"

    def _build_prompt(
        self, 
        athlete_name: str, 
        date_str: str, 
        recovery_data: Dict[str, Any], 
        compliance_data: Optional[Dict[str, Any]]
    ) -> str:
        
        # Format recovery section
        recovery_text = "Recovery Metrics:\n"
        if recovery_data.get("triggered"):
            recovery_text += f"⚠️ ALERT TRIGGERED: {recovery_data.get('reason')}\n"
        
        metrics = recovery_data.get("metrics", {})
        for name, data in metrics.items():
            status = "🔴 BREACHED" if data.get("breached") else "🟢 OK"
            recovery_text += f"- {name}: {data.get('current')} (Baseline: {data.get('baseline')}) - {status}\n"

        # Format compliance section
        compliance_text = "Workout Compliance:\n"
        if compliance_data:
            compliance_text += f"Overall Score: {compliance_data.get('overall_score', 'N/A')}\n"
            compliance_text += f"Notes: {compliance_data.get('notes', 'None')}\n"
            
            # Add details for each workout if available (assuming compliance_data structure)
            # This depends on how we pass the data. If it's a list of workouts or a single summary.
            # For now, let's assume it might be a list or a single dict.
            if "records" in compliance_data: # From get_compliance_for_day
                for record in compliance_data["records"]:
                    sport = record.get("sport", "Unknown")
                    score = record.get("overall_score", "N/A")
                    compliance_text += f"- {sport}: Score {score}\n"
                    if record.get("metrics"):
                        for m in record["metrics"]:
                            compliance_text += f"  * {m.get('metric')}: Planned {m.get('planned')} vs Actual {m.get('actual')} ({m.get('rating')})\n"
            elif "metrics" in compliance_data: # Direct compliance summary
                 for m in compliance_data["metrics"]:
                    compliance_text += f"  * {m.get('metric')}: Planned {m.get('planned')} vs Actual {m.get('actual')} ({m.get('rating')})\n"
        else:
            compliance_text += "No workout data available for today.\n"

        return f"""
        Write a daily summary email for the head coach about athlete {athlete_name} for {date_str}.
        This email is TO THE COACH, not the athlete.
        
        Here is the data:
        
        {recovery_text}
        
        {compliance_text}
        
        Instructions:
        1. Address the coach professionally (e.g., "Coach," or similar).
        2. Summarize the recovery status first. If there is an alert, highlight it immediately.
        3. Summarize the workout performance/compliance with specific details.
        4. Provide actionable insights or recommendations if relevant.
        5. Keep it concise and professional.
        """

    def generate_batch_coach_summary(
        self,
        date_str: str,
        athlete_summaries: list[Dict[str, Any]],
        errors: list[str]
    ) -> str:
        """
        Generate a batch summary email for the coach covering multiple athletes.
        
        Args:
            date_str: Date of the summary
            athlete_summaries: List of athlete summary dictionaries
            errors: List of error messages encountered during processing
            
        Returns:
            Generated email body text
        """
        if not self.client:
            return "Error: OpenAI client not initialized. Check API key and installation."

        # Build the batch prompt with system instructions embedded
        prompt = self._build_batch_prompt(date_str, athlete_summaries, errors)
        full_prompt = f"You are an expert triathlon coach assistant. Your goal is to write a comprehensive daily summary email for the head coach covering all Project Podium athletes. Highlight key issues, recovery alerts, and compliance problems at the top, then provide individual athlete summaries.\n\n{prompt}"
        
        try:
            response = self.client.responses.create(
                model=self.model,
                input=full_prompt
            )
            return response.output_text or "Error: Empty response from LLM."
        except Exception as e:
            return f"Error generating batch summary: {str(e)}"

    def _build_batch_prompt(
        self,
        date_str: str,
        athlete_summaries: list[Dict[str, Any]],
        errors: list[str]
    ) -> str:
        """
        Build the prompt for batch coach summary.
        """
        # Build top-level issues section
        issues = []
        for summary in athlete_summaries:
            # Check for sleep breach specifically
            sleep_breached = False
            if summary['recovery_metrics']:
                sleep_data = summary['recovery_metrics'].get('sleep_hours', {})
                if sleep_data.get('breached', False):
                    sleep_breached = True
                    issues.append(f"🚨 {summary['name']}: Sleep breach - {sleep_data.get('current')} hours (baseline: {sleep_data.get('baseline')})")
            
            # Check for other recovery alerts (if not already flagged for sleep)
            if summary['recovery_triggered'] and not sleep_breached:
                issues.append(f"🚨 {summary['name']}: Recovery alert - {summary['recovery_reason']}")
            
            # Check for low compliance
            if summary['avg_compliance'] is not None and summary['avg_compliance'] < 80:
                issues.append(f"⚠️ {summary['name']}: Low compliance score ({summary['avg_compliance']:.0f}/100)")
        
        issues_text = "\n".join(issues) if issues else "No critical issues detected."
        
        # Build individual athlete summaries
        athlete_details = []
        for summary in athlete_summaries:
            detail = f"\n**{summary['name']}**\n"
            if summary['avg_compliance'] is not None:
                detail += f"- Avg Compliance: {summary['avg_compliance']:.0f}/100\n"
            else:
                detail += f"- Avg Compliance: N/A\n"
            
            # Add recovery metrics
            if summary['recovery_triggered']:
                detail += f"- Recovery Alert: {summary['recovery_reason']}\n"
            if summary['recovery_metrics']:
                detail += "- Recovery Metrics:\n"
                for metric_name, metric_data in summary['recovery_metrics'].items():
                    current = metric_data.get('current', 'N/A')
                    baseline = metric_data.get('baseline', 'N/A')
                    breached = metric_data.get('breached', False)
                    status = "🔴 BREACHED" if breached else "🟢 OK"
                    detail += f"  * {metric_name}: {current} (baseline: {baseline}) {status}\n"
            
            # Add workout details with planned vs actual
            if summary['compliance_records']:
                for workout in summary['compliance_records']:
                    score = workout.get('overall_score', 'N/A')
                    sport = workout.get('sport', 'Unknown').lower()
                    
                    # Get planned and actual values
                    planned_summary = workout.get('planned', {})
                    actual_summary = workout.get('actual', {})
                    
                    # Format workout detail based on sport
                    detail_str = ""
                    if sport == 'swim':
                        planned_dist = planned_summary.get('distance_value')
                        actual_dist = actual_summary.get('distance_value')
                        unit = planned_summary.get('distance_unit', 'yd')
                        if planned_dist is not None and actual_dist is not None:
                            detail_str = f"({planned_dist:.0f} {unit} vs {actual_dist:.0f} {unit})"
                        else:
                            detail_str = "(N/A)"
                    elif sport == 'run':
                        planned_dist = planned_summary.get('distance_value')
                        actual_dist = actual_summary.get('distance_value')
                        unit = planned_summary.get('distance_unit', 'mi')
                        if planned_dist is not None and actual_dist is not None:
                            detail_str = f"({planned_dist:.2f} {unit} vs {actual_dist:.2f} {unit})"
                        else:
                            detail_str = "(N/A)"
                    elif sport == 'bike':
                        planned_dur = planned_summary.get('duration_seconds')
                        actual_dur = actual_summary.get('duration_seconds')
                        if planned_dur is not None and actual_dur is not None:
                            planned_min = int(planned_dur / 60)
                            actual_min = int(actual_dur / 60)
                            detail_str = f"({planned_min} min vs {actual_min} min)"
                        else:
                            detail_str = "(N/A)"
                    else:
                        detail_str = ""
                    
                    detail += f"  * {sport.capitalize()}: Score {score} {detail_str}\n"
            
            athlete_details.append(detail)
        
        athlete_text = "".join(athlete_details)
        
        errors_text = "\n".join(errors) if errors else "None"
        
        return f"""
        Write a comprehensive daily summary email for the head coach covering all Project Podium athletes for {date_str}.
        
        KEY ISSUES (highlight at top):
        {issues_text}
        
        INDIVIDUAL ATHLETE SUMMARIES:
        {athlete_text}
        
        ERRORS ENCOUNTERED:
        {errors_text}
        
        Instructions:
        1. Start with a professional greeting to the coach.
        2. FIRST: Summarize the key issues/alerts section prominently at the top.
        3. Then provide a brief summary of each athlete with their compliance score out of 100 AND recovery metrics (if available).
        4. Keep individual athlete summaries concise but informative.
        5. End with a brief observation section (2-3 sentences maximum) about overall trends.
        6. DO NOT include lengthy recommendations, action items, or follow-up suggestions at the bottom.
        7. Professional tone throughout.
        """

llm_client = LLMClient()
