"""
Core agent logic for the Job Search Copilot.

Runs on Google's Gemini API (via the official `google-genai` SDK) because
Gemini currently offers a genuinely free API tier (no credit card) -- see
README.md for how to get a key. Swapping to a different provider (Claude,
OpenAI, etc.) only requires changing this file; tools.py and app.py don't
know or care which model is behind run_agent()/tailor_application().

Two entry points:

  run_agent(messages, on_tool_call=None) -> str
      A standard tool-use ("agentic") loop: the model can call
      search_jobs / web_search / track_application / list_applications
      as many times as it needs, and the loop feeds tool results back
      in until the model produces a final text answer. Use this for the
      conversational "Chat" tab.

  tailor_application(resume_text, job_description) -> dict
      A single-shot generation (no tools) that turns a resume + a job
      description into a tailored bullet list and cover letter draft.

Both require GEMINI_API_KEY to be set in the environment (see
.env.example). Get a free key at https://aistudio.google.com/apikey.
"""

from __future__ import annotations

import os
from typing import Any, Callable

from google import genai
from google.genai import types

from tools import TOOL_SPECS, run_tool

MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash")
MAX_TOOL_ITERATIONS = 8

SYSTEM_PROMPT = """\
You are Job Search Copilot, an assistant that helps a job seeker move faster \
through their search. You can search live job postings, research companies, \
and keep a tracker of what they've applied to.

Guidelines:
- When the user describes what kind of role they want, use search_jobs to find \
real, current postings rather than guessing.
- When the user mentions applying to, saving, hearing back from, or interviewing \
at a company, call track_application to log it.
- When asked "what have I applied to" or similar, call list_applications.
- Be concise and concrete: prefer short lists of real job titles/companies/links \
over generic advice.
- You are not able to submit applications on external sites -- always leave the \
final "submit" step to the user, and say so if asked to auto-apply.
"""


def _client() -> genai.Client:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "GEMINI_API_KEY is not set. Copy .env.example to .env and add your key "
            "(free, no credit card required, from https://aistudio.google.com/apikey), "
            "or set it as an environment variable / Streamlit secret."
        )
    return genai.Client(api_key=api_key)


def _build_tool() -> types.Tool:
    """Convert our provider-agnostic TOOL_SPECS (tools.py) into a Gemini Tool."""
    return types.Tool(
        function_declarations=[
            types.FunctionDeclaration(
                name=spec["name"],
                description=spec["description"],
                parameters_json_schema=spec["input_schema"],
            )
            for spec in TOOL_SPECS
        ]
    )


def _to_gemini_role(role: str) -> str:
    return "model" if role == "assistant" else "user"


def _history_to_contents(messages: list[dict[str, Any]]) -> list[types.Content]:
    return [
        types.Content(
            role=_to_gemini_role(m["role"]),
            parts=[types.Part.from_text(text=m["content"])],
        )
        for m in messages
    ]


def run_agent(
    messages: list[dict[str, Any]],
    on_tool_call: Callable[[str, dict[str, Any], Any], None] | None = None,
) -> str:
    """
    Run the tool-use agent loop to completion and return the final text reply.

    `messages` is the running conversation as plain-text turns:
    [{"role": "user"/"assistant", "content": "..."}, ...]. This function
    converts that into Gemini's Content format internally; the caller
    only ever needs to persist plain-text turns across calls (see app.py).

    `on_tool_call`, if given, is called as (tool_name, tool_input, result)
    each time a tool executes -- handy for showing "thinking" steps in a UI.
    """
    client = _client()
    tool = _build_tool()
    contents = _history_to_contents(messages)
    config = types.GenerateContentConfig(system_instruction=SYSTEM_PROMPT, tools=[tool])

    for _ in range(MAX_TOOL_ITERATIONS):
        response = client.models.generate_content(model=MODEL, contents=contents, config=config)

        if not response.function_calls:
            return response.text or ""

        # Keep the model's own turn (containing the function_call parts) in context.
        contents.append(response.candidates[0].content)

        response_parts = []
        for fc in response.function_calls:
            args = dict(fc.args) if fc.args else {}
            result = run_tool(fc.name, args)
            if on_tool_call:
                on_tool_call(fc.name, args, result)
            response_parts.append(
                types.Part.from_function_response(
                    name=fc.name,
                    response=result if isinstance(result, dict) else {"result": result},
                )
            )
        contents.append(types.Content(role="tool", parts=response_parts))

    return (
        "I made several tool calls but couldn't reach a final answer in the allotted "
        "steps -- try narrowing your question."
    )


TAILOR_PROMPT_TEMPLATE = """\
You are an expert resume writer and ATS optimization specialist. Given a \
candidate's resume and a target job description, produce:

1. A short "match summary" (2-3 sentences) on how well the candidate fits.
2. 5-8 tailored resume bullet points, rewritten from the candidate's actual \
experience below (never invent experience they don't have) to mirror the \
language and priorities of the job description, front-loaded with strong \
verbs and quantified impact where the original supports it.
3. A concise, specific cover letter (3-4 short paragraphs) for this exact role.

Resume:
---
{resume_text}
---

Job description:
---
{job_description}
---

Respond in clean Markdown with headings: "## Match Summary", "## Tailored Resume Bullets", "## Cover Letter".
"""


def tailor_application(resume_text: str, job_description: str) -> str:
    """Single-shot generation: tailored resume bullets + cover letter for one job."""
    client = _client()
    prompt = TAILOR_PROMPT_TEMPLATE.format(
        resume_text=resume_text.strip(), job_description=job_description.strip()
    )
    response = client.models.generate_content(model=MODEL, contents=prompt)
    return response.text or ""
