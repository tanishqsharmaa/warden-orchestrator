"""Deterministic rule-governed HyDE query expander for warden-orchestrator."""

import re

HR_TOPIC_ENRICHMENTS: dict[str, str] = {
    "bereavement": "bereavement leave policy immediate family member definition consecutive paid days off allowance",
    "parental": "paid parental leave duration 12 weeks eligibility service months birth adoption bonding",
    "maternity": "paid maternity leave duration disability coverage eligibility healthcare benefits",
    "paternity": "paid paternity leave bonding leave duration eligibility coverage",
    "pto": "paid time off accrual 18 days annual leave carryover policy vacation schedule",
    "vacation": "vacation leave policy annual accrual days carryover supervisor approval",
    "tuition": "annual tuition reimbursement maximum $5250 calendar year approved degree programs course eligibility",
    "education": "education assistance program tuition reimbursement degree certification policy",
    "health": "company health insurance benefits coverage medical dental vision employee contribution",
    "medical": "medical leave of absence health benefits coverage disability claims",
    "insurance": "group health insurance life insurance coverage plan enrollment open enrollment",
    "severance": "executive severance package termination protocol equity vesting continuation of benefits",
    "equity": "stock option grants equity allocation bands vesting schedule compensation guidelines",
    "harassment": "workplace harassment reporting protocol investigation procedure non-retaliation code of conduct",
    "conduct": "employee code of conduct workplace behavior compliance standards disciplinary actions",
    "remote": "remote work equipment allowance home office subsidy telecommuting guidelines",
    "holiday": "company official paid holidays calendar floating holiday observance",
}


class HyDEExpander:
    """Enriches HR policy queries using deterministic rule templates within a 15ms latency budget."""

    def expand_query(self, query: str, caller_role: str = "Employee") -> str:
        """Expand user query with relevant contextual keywords and policy framing."""
        cleaned = query.strip()
        if not cleaned:
            return ""

        lower_query = cleaned.lower()
        matched_enrichments: list[str] = []

        for keyword, enrichment in HR_TOPIC_ENRICHMENTS.items():
            if re.search(r"\b" + re.escape(keyword) + r"\b", lower_query):
                matched_enrichments.append(enrichment)

        if matched_enrichments:
            # Combine up to 2 distinct topic enrichments
            enrichment_text = " ".join(matched_enrichments[:2])
            expanded = f"{cleaned} — Policy Guide: {enrichment_text}"
        else:
            expanded = f"{cleaned} — Company HR Policy Guide and Employee Handbook"

        # Strictly enforce concise word budget (<= 60 words)
        words = expanded.split()
        if len(words) > 60:
            words = words[:60]
            expanded = " ".join(words)

        return expanded
