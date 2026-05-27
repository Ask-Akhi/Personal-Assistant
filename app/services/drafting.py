"""LLM-powered draft generation - Gemini primary, Groq fallback, Anthropic last resort.

Provider chain (configured via LLM_PROVIDER env var, default "gemini"):
  gemini    -> google-genai SDK  (free: 1500 req/day on Flash)
  groq      -> groq SDK          (free: 14400 req/day on llama-3.3-70b)
  anthropic -> anthropic SDK     (paid ~$0.002/call on claude-haiku-4-5)

On any provider error the chain falls through to the next.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from app.config import get_settings
from app.logging_setup import log
from app.models import Contact, InboundMessage
from app.services import cost_guard

_SYSTEM = (
    "You are a personal communication assistant. The user is busy; you help draft "
    "WhatsApp replies on their behalf. Always:\n"
    "- Match the language/script the sender used (English / Hindi / Hinglish).\n"
    "- Match the formality level of the sender.\n"
    "- Stay truthful - never invent facts, dates, or commitments.\n"
    "- Keep each draft SHORT (1-4 sentences typical).\n"
    "- Never add greetings/sign-offs unless the original had them.\n"
    "Output ONLY valid JSON - no markdown fences, no explanation."
)

_USER_TEMPLATE = """\
## Contact
name: {name}
tier: {tier}
tone_profile: {tone_profile}

## Inbound message (from contact)
{inbound_text}

## Task
Generate 3 reply drafts in the SAME language/script as the inbound message.
Return JSON exactly matching this schema:
{{
  "drafts": [
    {{"tone": "concise", "text": "..."}},
    {{"tone": "warm",    "text": "..."}},
    {{"tone": "formal",  "text": "..."}}
  ]
}}
"""


@dataclass
class DraftSuggestion:
    tone: str
    text: str


async def _call_gemini(prompt: str, settings) -> str:
    if not settings.gemini_api_key:
        raise RuntimeError("GEMINI_API_KEY not set")
    from google import genai  # type: ignore
    client = genai.Client(api_key=settings.gemini_api_key)
    response = client.models.generate_content(
        model="gemini-2.0-flash",
        contents=f"{_SYSTEM}\n\n{prompt}",
    )
    return response.text


async def _call_groq(prompt: str, settings) -> str:
    if not settings.groq_api_key:
        raise RuntimeError("GROQ_API_KEY not set")
    from groq import AsyncGroq  # type: ignore
    client = AsyncGroq(api_key=settings.groq_api_key)
    resp = await client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": prompt},
        ],
        max_tokens=512,
        temperature=0.4,
    )
    return resp.choices[0].message.content


async def _call_anthropic(prompt: str, settings, *, session) -> str:
    if not settings.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    import anthropic  # type: ignore
    await cost_guard.check(session, "anthropic", projected_usd=0.005)
    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    response = await client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=512,
        system=_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    usage = response.usage
    usd = (usage.input_tokens * 0.80 + usage.output_tokens * 4.00) / 1_000_000
    await cost_guard.record(
        session, provider="anthropic",
        units=usage.input_tokens + usage.output_tokens,
        usd=usd,
        meta={"model": "claude-haiku-4-5", "input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens},
    )
    return response.content[0].text


def _parse(raw: str) -> list[DraftSuggestion]:
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(l for l in lines if not l.strip().startswith("```")).strip()
    parsed = json.loads(text)
    return [DraftSuggestion(**d) for d in parsed["drafts"]]


async def generate(
    contact: Contact,
    inbound: InboundMessage,
    *,
    session,
) -> list[DraftSuggestion]:
    """Return 3 draft suggestions. Chain: LLM_PROVIDER -> groq -> anthropic."""
    settings = get_settings()
    prompt = _USER_TEMPLATE.format(
        name=contact.display_name or contact.external_id,
        tier=contact.tier,
        tone_profile=json.dumps(contact.tone_profile or {}),
        inbound_text=(inbound.text or f"[{inbound.message_type} message - no text body]"),
    )

    primary = settings.llm_provider.lower()
    all_providers = ["gemini", "groq", "anthropic"]
    ordered = [primary] + [p for p in all_providers if p != primary]

    last_error: Exception | None = None
    for provider in ordered:
        try:
            log.info("drafting.attempt", provider=provider)
            if provider == "gemini":
                raw = await _call_gemini(prompt, settings)
                await cost_guard.record(session, provider="gemini", units=0, usd=0.0, meta={"model": "gemini-2.0-flash"})
            elif provider == "groq":
                raw = await _call_groq(prompt, settings)
                await cost_guard.record(session, provider="groq", units=0, usd=0.0, meta={"model": "llama-3.3-70b-versatile"})
            elif provider == "anthropic":
                raw = await _call_anthropic(prompt, settings, session=session)
            else:
                log.warning("drafting.unknown_provider", provider=provider)
                continue
            suggestions = _parse(raw)
            log.info("drafting.success", provider=provider, count=len(suggestions))
            return suggestions
        except Exception as exc:
            log.warning("drafting.provider_failed", provider=provider, error=str(exc)[:300])
            last_error = exc
            continue

    raise RuntimeError(f"All LLM providers failed. Last error: {last_error}")
