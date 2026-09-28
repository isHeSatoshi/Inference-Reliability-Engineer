"""Register the inference-ops MCP connector and create/update the Firefighter agent in TrueForge.

  TRUEFORGE_BASE_URL=http://localhost:8791 AGENT_MODEL=openai/<model-id> \
  MCP_PUBLIC_URL=http://<control-private-ip>:8765/mcp MCP_AUTH_TOKEN=... \
  python -m agent.create_agent [--use-skill] [--skip-connector]

By default the runbook (skills/incident-runbook/SKILL.md) is inlined into the instructions.
--use-skill   attach the git-backed runbook skill instead; first import it under Settings -> Skills ->
              Import from GitHub (TrueForge rejects unknown skills).
--book-skill  register + attach the "inference-engineering" skill (distilled from the Inference
              Engineering book) straight from this public repo (SKILLS_REPO, SKILLS_REF).
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from trueforge_sdk import TrueForge
from trueforge_sdk.core.api_error import ApiError

ROOT = Path(__file__).resolve().parent.parent
AGENT_NAME = "inference-firefighter"
MCP_NAME = "inference-ops"
GATED = ["apply_production_config", "rollback_production"]


SKILLS_REPO = os.environ.get("SKILLS_REPO", "https://github.com/isHeSatoshi/Inference-Reliability-Engineer")
SKILLS_REF = os.environ.get("SKILLS_REF", "main")


def register_git_skill(client: TrueForge, name: str, description: str) -> None:
    client.settings.skills.create_or_update(manifest={
        "type": "git", "name": name, "description": description,
        "url": SKILLS_REPO, "ref": SKILLS_REF, "path": f"skills/{name}"})
    print(f"skill '{name}' <- {SKILLS_REPO}@{SKILLS_REF}:skills/{name}")


def build_spec(model: str, inline_runbook: bool, book_skill: bool = False) -> dict:
    instructions = (ROOT / "agent" / "instructions.md").read_text()
    if inline_runbook:
        runbook = (ROOT / "skills" / "incident-runbook" / "SKILL.md").read_text().split("---", 2)[-1]
        instructions += "\n\n# incident-runbook (inlined)\n" + runbook
    spec: dict = {
        "model": {"name": model},
        "instructions": instructions,
        "mcp_servers": [{
            "name": MCP_NAME,
            "enable_tools": ["@all"],
            # Named explicitly AND annotated destructive on the server: belt and braces.
            "require_approval_for_tools": GATED + ["@destructive"],
            "preload": True,
        }],
        "config": {
            "sandbox": {"enabled": True},
            "generative_ui": {"enabled": True},
            "ask_user_questions": {"enabled": True},
            "dynamic_sub_agents": {"enabled": True},
            "iteration_limit": int(os.environ.get("AGENT_ITERATION_LIMIT", "200")),
        },
    }
    effort = os.environ.get("AGENT_REASONING_EFFORT")  # only for models that support it, e.g. "medium"
    if effort:
        spec["model"]["params"] = {"reasoning_effort": effort}
    skills = [] if inline_runbook else [{"name": "incident-runbook"}]
    if book_skill:
        skills.append({"name": "inference-engineering"})
    if skills:
        spec["skills"] = skills
    return spec


def main() -> None:
    try:
        _main()
    except ApiError as e:
        sys.exit(f"TrueForge rejected the request (HTTP {e.status_code}): {e.body}")


def _main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--use-skill", action="store_true")
    ap.add_argument("--book-skill", action="store_true")
    ap.add_argument("--skip-connector", action="store_true")
    args = ap.parse_args()

    model = os.environ.get("AGENT_MODEL")
    if not model:
        sys.exit("set AGENT_MODEL to the TrueForge model FQN, e.g. openai/<model-id> (see Settings -> Models)")
    client = TrueForge(base_url=os.environ.get("TRUEFORGE_BASE_URL", "http://localhost:8791"), timeout=120)

    if not args.skip_connector:
        url, token = os.environ.get("MCP_PUBLIC_URL"), os.environ.get("MCP_AUTH_TOKEN")
        if not url or not token:
            sys.exit("set MCP_PUBLIC_URL (as reachable FROM the TrueForge server) and MCP_AUTH_TOKEN")
        client.settings.mcp_servers.create_or_update(manifest={
            "type": "remote", "name": MCP_NAME, "url": url,
            "description": "vLLM serving fleet: telemetry, shadow experiments, gated production changes",
            "auth": {"type": "header", "headers": {"Authorization": f"Bearer {token}"}},
        })
        print(f"connector '{MCP_NAME}' -> {url}")

    if args.book_skill:
        register_git_skill(client, "inference-engineering",
                           "Inference-engineering mental models, decision rules and anti-patterns for LLM serving "
                           "(distilled from the Inference Engineering book by Philip Kiely, Baseten).")
    spec = build_spec(model, inline_runbook=not args.use_skill, book_skill=args.book_skill)
    existing = [a for a in client.agents.list(agent_name=AGENT_NAME) if a.name == AGENT_NAME]
    description = "Diagnoses degraded vLLM inference, proves a fix on a shadow GPU, asks before touching prod."
    if existing:
        client.agents.update(agent_id=existing[0].id, manifest=spec, description=description)
        print(f"updated agent {AGENT_NAME} ({existing[0].id})")
    else:
        created = client.agents.create(name=AGENT_NAME, description=description, manifest=spec)
        print(f"created agent {AGENT_NAME} ({created.data.id})")
    print(f"model={model} gated={GATED} skill={'incident-runbook' if args.use_skill else 'inline'}")


if __name__ == "__main__":
    main()
