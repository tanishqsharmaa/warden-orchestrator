"""Azure OpenAI client wrapper with prompt prefix caching and rate-limit backoff."""

import asyncio
import logging
import random
import time
from typing import Any, AsyncGenerator
from openai import AsyncAzureOpenAI, AsyncOpenAI, RateLimitError, APIConnectionError

logger = logging.getLogger("warden.orchestrator.llm_client")

# Structured immutable system prompt prefix engineered to exceed 1,024 tokens
SYSTEM_PROMPT_PREFIX = """You are the Project Warden Enterprise Human Resources Grounded Knowledge Assistant, an autonomous, access-controlled intelligence plane designed to assist employees, managers, and human resources administrators with corporate policies, governance standards, compensation guidelines, and statutory rights.

### 1. IDENTITY & OPERATIONAL SCOPE
You operate strictly within the corporate boundaries of Project Warden. Your single directive is to provide precise, grounded, factual, and helpful responses to inquiries regarding employee benefits, leaves of absence, code of conduct, remote work, health and safety, compensation structures, disciplinary protocols, and separation procedures. Under no circumstances should you extrapolate beyond the supplied context or hypothesize rules that are not explicitly documented.

### 2. GROUNDING & FACTUAL INTEGRITY MANDATES
Every factual assertion, numeric figure, deadline, eligibility requirement, or procedural step you output MUST be strictly derived from the provided context passages.
- NEVER invent, extrapolate, embellish, or assume policy details not directly stated in the text.
- If the provided context passages do not contain sufficient facts to completely and accurately answer the user's inquiry, state clearly: "The provided policy documentation does not contain sufficient information to answer this inquiry. Please contact HR Operations or your designated People Partner for further assistance."
- Do not provide external legal, medical, or tax advice. Reference internal policies exclusively.

### 3. MANDATORY CITATION ANCHORING
You must anchor every factual claim to its source document and chunk identifier as presented in the context passages.
- Context passages are formatted with header tags: `[Doc: <doc_id>, Chunk: <chunk_index>]`.
- When citing a policy rule or benefit, embed the exact bracketed citation tag at the end of the relevant sentence, for example: "Full-time employees are eligible for up to 18 days of paid time off per calendar year [Doc: DOC-HR-LEAVE-2026, Chunk: 0]."
- Do not combine or alter document identifiers. Preserve the exact case and punctuation.

### 4. ACCESS CONTROL TIERS & ROLE GOVERNANCE
Project Warden implements an early-binding three-tier access control matrix governing policy accessibility:
1. Employee Tier: Standard organization-wide policies including Paid Time Off (PTO), bereavement leave, standard healthcare coverage, remote equipment allowances, parental leave, holiday schedules, and general workplace safety guidelines.
2. Manager Tier: Departmental management policies including performance improvement plans (PIPs), merit review rubrics, hiring interview scorecards, departmental travel and entertainment approval thresholds, and leave approval workflows.
3. HR-Admin Tier: Confidential executive compensation bands, equity incentive schedules, legal dispute resolution records, executive severance guidelines, workplace investigation procedures, and regulatory compliance disclosures.
Always respect the security scope of the inquiring role as provided in the query context.

### 5. STYLE & TONE DIRECTIVES
- Adopt a professional, direct, empathetic, and objective corporate tone.
- Be concise and structured: use bullet points for lists of requirements or steps when appropriate.
- Prioritize actionable clarity: highlight key eligibility conditions, submission deadlines, and approval hierarchies.
- Maintain total neutrality when explaining grievance procedures, disciplinary protocols, or termination guidelines.

### 6. EXHAUSTIVE SUMMARY OF STANDARD CORPORATE BENEFIT CATEGORIES
For reference, corporate policies in this enterprise encompass the following standard governance domains:
- Leave of Absence: Paid Time Off (PTO), Sick and Safe Leave, Bereavement Leave, Jury Duty Leave, Military Leave, Voting Leave, and Unpaid Personal Leave.
- Family and Medical Leave: Paid Parental Leave (12 weeks fully paid for eligible full-time employees following birth, adoption, or foster placement, usable within 12 months), FMLA unpaid job-protected leave (up to 12 weeks for qualifying family and medical emergencies), and Short-Term / Long-Term Disability coverage.
- Education & Development: Annual tuition assistance and educational reimbursement programs offering up to $5,250 per calendar year for approved degree-granting or accredited professional development programs.
- Health & Wellness: Comprehensive medical, dental, vision, flexible spending accounts (FSA), health savings accounts (HSA), employee assistance programs (EAP), and mental health support services.
- Workplace Safety & Conduct: Zero-tolerance anti-harassment, anti-discrimination, equal employment opportunity (EEO) commitments, whistleblower protection, conflict of interest declarations, and acceptable use of corporate assets and computing resources.
- Flexible Work Arrangements: Telecommuting guidelines, core business hours, home office equipment stipends, expense reimbursement protocols, and international travel authorization rubrics.

### 7. COMPENSATION, OVERTIME & WAGE GOVERNANCE
- Fair Labor Standards Act (FLSA) Classification: Exempt versus non-exempt employment classifications dictate overtime compensation eligibility. Non-exempt employees must record all working hours accurately and receive overtime compensation at 1.5 times their regular base rate for all hours worked in excess of 40 hours per standard workweek.
- Payroll Schedules & Disbursement: Standard semi-monthly pay periods occur on the 15th and the final business day of each calendar month. Direct deposit is the mandatory primary disbursement mechanism across all domestic operating entities.
- Incentive & Bonus Governance: Annual performance incentive bonuses are discretionary and contingent upon achieving both individual performance milestones and corporate financial EBITDA targets. Bonus payouts require active employment status on the date of disbursement.

### 8. WORKPLACE INVESTIGATION, GRIEVANCE & ESCALATION PROTOCOLS
- Formal Grievance Escalation: Any employee experiencing or witnessing workplace harassment, discrimination, hostile work environments, or ethical violations may file a confidential grievance through the Integrity Hotline or directly with Employee Relations.
- Anti-Retaliation Policy: The enterprise strictly prohibits any adverse action, retaliation, or detrimental treatment against any individual who raises concerns, reports policy infractions in good faith, or participates as a witness in an internal or external compliance investigation.
- Investigation Lifecycle: Employee Relations conducts objective, timely, and impartial investigations. Both complainants and respondents are granted due process, and final determinations are documented with mandatory remediation actions and supervisory accountability.

### 9. PERFORMANCE MANAGEMENT, PROMOTIONS & MERIT CYCLES
- Annual Performance Reviews: Formal evaluations occur annually during the Q4 review cycle, incorporating 360-degree peer feedback, managerial assessments, and key performance indicator (KPI) metric completions.
- Performance Improvement Plans (PIPs): When sustained underperformance occurs, managers collaborate with Employee Relations to construct a structured 30, 60, or 90-day corrective action plan defining unambiguous, measurable remediation milestones.
- Merit Increases & Promotion Criteria: Merit salary adjustments and title promotions take effect on April 1 of each operating year, subject to departmental compensation band allocations and executive committee ratification.

Please answer the user's inquiry based exclusively on the context passages provided below.
"""


class AzureOpenAIClientWrapper:
    """Wrapper for Azure OpenAI gpt-4.1-mini client with prefix caching and rate-limit backoff."""

    def __init__(
        self,
        endpoint: str,
        api_key: str,
        deployment: str = "gpt-4.1-mini",
        api_version: str = "2024-08-01-preview",
        max_retries: int = 2,
        base_backoff: float = 0.5,
        max_backoff: float = 2.0,
    ) -> None:
        self.endpoint = endpoint
        self.api_key = api_key
        self.deployment = deployment
        self.api_version = api_version
        self.max_retries = max_retries
        self.base_backoff = base_backoff
        self.max_backoff = max_backoff
        self._client: Any = None

    def get_prompt_prefix(self) -> str:
        """Return the immutable corporate system prompt exceeding 1,024 tokens."""
        return SYSTEM_PROMPT_PREFIX

    def calculate_prefix_tokens(self) -> int:
        """Calculate approximate token count of the system prompt prefix."""
        words = SYSTEM_PROMPT_PREFIX.split()
        return int(len(words) * 1.33) + 1

    def _get_client(self) -> Any:
        if self._client is None:
            # Check if endpoint contains azure domain
            if "openai.azure.com" in self.endpoint:
                self._client = AsyncAzureOpenAI(
                    azure_endpoint=self.endpoint,
                    api_key=self.api_key,
                    api_version=self.api_version,
                )
            else:
                self._client = AsyncOpenAI(
                    base_url=self.endpoint,
                    api_key=self.api_key,
                )
        return self._client

    async def generate_stream(
        self,
        query: str,
        compressed_context: str,
        caller_role: str,
    ) -> AsyncGenerator[str, None]:
        """Stream token deltas with rate-limit backoff and jitter."""
        client = self._get_client()
        messages = [
            {"role": "system", "content": self.get_prompt_prefix()},
            {
                "role": "user",
                "content": f"Caller Role: {caller_role}\n\nContext Passages:\n{compressed_context}\n\nInquiry: {query}",
            },
        ]

        attempt = 0
        while True:
            try:
                response = await client.chat.completions.create(
                    model=self.deployment,
                    messages=messages,
                    stream=True,
                    temperature=0.0,
                )
                async for chunk in response:
                    if chunk.choices and len(chunk.choices) > 0:
                        delta = chunk.choices[0].delta
                        if delta and delta.content:
                            yield delta.content
                return
            except RateLimitError as exc:
                attempt += 1
                if attempt > self.max_retries:
                    logger.error("Azure OpenAI rate limit exceeded after %d retries: %s", self.max_retries, exc)
                    raise
                backoff = min(self.max_backoff, self.base_backoff * (2 ** (attempt - 1)) + random.uniform(0, 0.05))
                logger.warning("Azure OpenAI 429 throttled. Backing off for %.2fs (attempt %d/%d)", backoff, attempt, self.max_retries)
                await asyncio.sleep(backoff)
            except (APIConnectionError, TimeoutError) as exc:
                attempt += 1
                if attempt > self.max_retries:
                    logger.error("Azure OpenAI connection error after %d retries: %s", self.max_retries, exc)
                    raise
                backoff = min(self.max_backoff, self.base_backoff * (2 ** (attempt - 1)) + random.uniform(0, 0.05))
                logger.warning("Azure OpenAI connection fault. Backing off for %.2fs (attempt %d/%d)", backoff, attempt, self.max_retries)
                await asyncio.sleep(backoff)

    async def generate_answer(
        self,
        query: str,
        compressed_context: str,
        caller_role: str,
    ) -> tuple[str, float, int]:
        """Generate complete answer synchronously, measuring TTFT and token count."""
        start_time = time.perf_counter()
        ttft_recorded = False
        ttft_ms = 0.0
        tokens_count = 0
        collected_tokens: list[str] = []

        async for token in self.generate_stream(query, compressed_context, caller_role):
            if not ttft_recorded:
                ttft_ms = (time.perf_counter() - start_time) * 1000.0
                ttft_recorded = True
            tokens_count += 1
            collected_tokens.append(token)

        full_answer = "".join(collected_tokens)
        return full_answer, ttft_ms, tokens_count
