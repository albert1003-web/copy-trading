"""Agent layer (Phase 5): Claude agents with read-only database tools and web search.

Agents never change anything themselves. They write proposals to agent_proposals; a human approves them in the
app, and agents/apply.py (a plain pipeline stage, not an agent) carries out the approved ones.
"""
