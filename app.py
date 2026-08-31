"""
Streamlit UI for the Job Search Copilot agent.

Three tabs:
  1. Chat     -- talk to the agent; it can search jobs, research companies,
                 and log applications using Gemini tool-use (function calling).
  2. Tailor   -- paste your resume + a job description, get tailored bullets
                 and a cover letter draft (single-shot generation).
  3. Tracker  -- dashboard view of everything logged in applications.json.

Run locally:    streamlit run app.py
Deploy free on: https://share.streamlit.io (see README.md)
"""

from __future__ import annotations

import os

import streamlit as st
from dotenv import load_dotenv

from agent import run_agent, tailor_application
from tools import list_applications

load_dotenv()

st.set_page_config(page_title="Job Search Copilot", page_icon="\U0001F4BC", layout="wide")

# Streamlit Cloud: read the key from st.secrets if it's not already in the env
if "GEMINI_API_KEY" not in os.environ:
    try:
        os.environ["GEMINI_API_KEY"] = st.secrets["GEMINI_API_KEY"]
    except (KeyError, FileNotFoundError):
        pass

st.title("\U0001F4BC Job Search Copilot")
st.caption(
    "An AI agent that searches live job postings, tailors your resume per role, "
    "and tracks every application -- so applying takes minutes, not hours."
)

if not os.environ.get("GEMINI_API_KEY"):
    st.warning(
        "No GEMINI_API_KEY found. Get a free key (no credit card) at "
        "https://aistudio.google.com/apikey, then add it to a local `.env` file "
        "(see `.env.example`) or, if deployed on Streamlit Cloud, under "
        "App settings → Secrets.",
        icon="⚠️",
    )

tab_chat, tab_tailor, tab_tracker = st.tabs(["\U0001F4AC Chat", "✒️ Tailor", "\U0001F4CB Tracker"])

# ---------------------------------------------------------------------------
# Tab 1: Chat
# ---------------------------------------------------------------------------
with tab_chat:
    st.write(
        "Ask things like *\"find me remote backend roles using Python\"*, "
        "*\"what's it like working at Stripe\"*, or *\"I applied to the Acme "
        "Corp data analyst role\"*."
    )

    if "chat_history" not in st.session_state:
        st.session_state.chat_history = []  # list of {"role", "content"} for display
    if "agent_messages" not in st.session_state:
        st.session_state.agent_messages = []  # plain-text turns sent to the agent

    for turn in st.session_state.chat_history:
        with st.chat_message(turn["role"]):
            st.markdown(turn["content"])

    user_input = st.chat_input("Ask your job search copilot...")
    if user_input:
        st.session_state.chat_history.append({"role": "user", "content": user_input})
        st.session_state.agent_messages.append({"role": "user", "content": user_input})
        with st.chat_message("user"):
            st.markdown(user_input)

        with st.chat_message("assistant"):
            status_box = st.status("Thinking...", expanded=False)
            tool_log: list[str] = []

            def _log_tool_call(name, tool_input, result):
                tool_log.append(f"**{name}**({tool_input}) -> `{str(result)[:200]}`")
                status_box.update(label=f"Calling tool: {name}")

            try:
                reply = run_agent(st.session_state.agent_messages, on_tool_call=_log_tool_call)
            except RuntimeError as exc:
                reply = f"⚠️ {exc}"

            status_box.update(label="Done", state="complete")
            if tool_log:
                with status_box:
                    for line in tool_log:
                        st.markdown(line)

            st.markdown(reply)

        st.session_state.chat_history.append({"role": "assistant", "content": reply})
        st.session_state.agent_messages.append({"role": "assistant", "content": reply})

    if st.button("Clear conversation"):
        st.session_state.chat_history = []
        st.session_state.agent_messages = []
        st.rerun()

# ---------------------------------------------------------------------------
# Tab 2: Tailor
# ---------------------------------------------------------------------------
with tab_tailor:
    st.write("Paste your resume and a job description to get tailored bullets + a cover letter.")

    col1, col2 = st.columns(2)
    with col1:
        resume_text = st.text_area("Your resume (plain text)", height=350, key="resume_text")
    with col2:
        job_description = st.text_area("Job description", height=350, key="job_description")

    if st.button("Generate tailored materials", type="primary"):
        if not resume_text.strip() or not job_description.strip():
            st.error("Paste both your resume and the job description first.")
        else:
            with st.spinner("Tailoring..."):
                try:
                    result = tailor_application(resume_text, job_description)
                    st.markdown(result)
                except RuntimeError as exc:
                    st.error(str(exc))

# ---------------------------------------------------------------------------
# Tab 3: Tracker
# ---------------------------------------------------------------------------
with tab_tracker:
    st.write("Everything the agent has logged for you (via the Chat tab).")
    data = list_applications()["applications"]

    if not data:
        st.info("No applications tracked yet. Try telling the agent in the Chat tab "
                 "that you applied somewhere.")
    else:
        status_order = {"interview": 0, "offer": 1, "applied": 2, "saved": 3, "rejected": 4}
        data = sorted(data, key=lambda a: status_order.get(a.get("status", ""), 5))
        st.dataframe(
            data,
            column_order=["company", "role", "status", "url", "notes", "updated_at"],
            use_container_width=True,
            hide_index=True,
        )
