"""Agent skills and README examples match the shipped tools and command line.

Every tool a skill names must exist in its role's MCP groups, with the
arguments it shows. Every complete ``xbloom`` or ``xbloom-mcp`` command shown
in the README or a skill must parse, and the README's MCP client
configuration must launch only the public ``xbloom-mcp`` entry point.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import re
import shlex
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from xbloom_studio import cli, mcp_server
from xbloom_studio.mcp_server import READ_ONLY, create_server

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
SKILLS = ROOT / "skills"
TOOL = re.compile(
    r"`((?:recipe|provider|history|machine|operation)_\w+)(?:\(([^`)]*)\))?`"
)
SPAN = re.compile(r"`(xbloom(?:-mcp)? [^`]+)`")
FENCE = re.compile(r"```(\w+)\n(.*?)```", re.S)
FRONT_MATTER = re.compile(r"\A---\nname: ([a-z0-9-]+)\ndescription: (.+?)\n---\n")
PLACEHOLDERS = {"FAMILY": "green", "N": "1"}


async def _noop(method: str, params: dict[str, Any]) -> Any:
    return None


def tools(groups: tuple[str, ...] = mcp_server.GROUPS) -> dict[str, set[str]]:
    """Advertised tool names and their argument names."""
    listed = asyncio.run(create_server(groups=groups, caller=_noop).list_tools())
    return {t.name: set(t.input_schema.get("properties", {})) for t in listed}


def role_tools() -> dict[str, set[str]]:
    group = {name: set(tools((name,))) for name in mcp_server.GROUPS}
    return {
        "bloom-recipes": group["recipes"],
        "bloom-tea": group["recipes"] & READ_ONLY,
        "bloom-history": group["history"],
        "bloom-operate": group["machine"] | (group["history"] & READ_ONLY),
    }


def tool_problems(
    skill: str, text: str, schemas: dict[str, set[str]], allowed: set[str]
) -> list[str]:
    problems = []
    for match in TOOL.finditer(text):
        name, arguments = match.group(1), match.group(2)
        if name not in schemas:
            problems.append(f"{skill}: unknown tool {name}")
            continue
        if name not in allowed:
            problems.append(f"{skill}: {name} is outside this skill's role")
        for argument in (arguments or "").split(","):
            argument = argument.split("=")[0].strip()
            if argument and argument != "..." and argument not in schemas[name]:
                problems.append(f"{skill}: {name} has no argument {argument}")
    return problems


def commands(text: str) -> list[list[str]]:
    """Complete commands in shell code blocks and inline code spans."""
    lines = [m.group(1) for m in SPAN.finditer(text)]
    for language, body in FENCE.findall(text):
        if language == "sh":
            lines += body.splitlines()
    found = []
    for line in lines:
        words = shlex.split(line, comments=True)
        if words and words[-1] == "&":
            words.pop()
        if words and words[0] in ("xbloom", "xbloom-mcp"):
            found.append([PLACEHOLDERS.get(word, word) for word in words])
    return found


def command_problems(text: str) -> list[str]:
    problems = []
    for words in commands(text):
        with contextlib.redirect_stderr(io.StringIO()), contextlib.suppress(SystemExit):
            if words[0] == "xbloom":
                cli.parser().parse_args(words[1:])
                continue
            with mock.patch.object(mcp_server, "create_server"):
                mcp_server.main(words[1:])
                continue
        problems.append("does not parse: " + shlex.join(words))
    return problems


class SkillTests(unittest.TestCase):
    def test_skills_are_complete_agent_skill_directories(self):
        roles = role_tools()
        found = sorted(path.name for path in SKILLS.iterdir())
        self.assertEqual(found, sorted(roles))
        for name in found:
            files = sorted(p.name for p in (SKILLS / name).iterdir())
            self.assertEqual(files, ["SKILL.md"])
            text = (SKILLS / name / "SKILL.md").read_text(encoding="utf-8")
            header = FRONT_MATTER.match(text)
            assert header is not None, name
            self.assertEqual(header.group(1), name)
            self.assertLessEqual(len(header.group(2)), 1024)

    def test_skills_name_only_their_role_tools_and_real_arguments(self):
        schemas, roles = tools(), role_tools()
        for name, allowed in roles.items():
            text = (SKILLS / name / "SKILL.md").read_text(encoding="utf-8")
            self.assertEqual(tool_problems(name, text, schemas, allowed), [])
            self.assertTrue(TOOL.search(text), name)

    def test_documented_commands_parse(self):
        documents = [README, *sorted(SKILLS.glob("*/SKILL.md"))]
        for path in documents:
            text = path.read_text(encoding="utf-8")
            self.assertTrue(commands(text), path.name)
            self.assertEqual(command_problems(text), [], path.name)

    def test_readme_client_configuration_uses_the_public_entry_point(self):
        text = README.read_text(encoding="utf-8")
        blocks = [body for language, body in FENCE.findall(text) if language == "json"]
        self.assertEqual(len(blocks), 1)
        servers = json.loads(blocks[0])["mcpServers"]
        self.assertTrue(servers)
        for server in servers.values():
            self.assertEqual(set(server), {"command", "args"})
            self.assertEqual(server["command"], "xbloom-mcp")
            line = shlex.join(["xbloom-mcp", *server["args"]])
            self.assertEqual(command_problems(f"`{line}`"), [])

    def test_checks_reject_unknown_names_roles_and_commands(self):
        schemas = {"recipe_get": {"reference"}, "machine_state": set()}
        text = "`recipe_gets` `recipe_get(ref)` `machine_state(detail)`"
        self.assertEqual(
            tool_problems("x", text, schemas, {"recipe_get"}),
            [
                "x: unknown tool recipe_gets",
                "x: recipe_get has no argument ref",
                "x: machine_state is outside this skill's role",
                "x: machine_state has no argument detail",
            ],
        )
        bad = "`xbloom recipe teaa` `xbloom-mcp --group all`\n```sh\nxbloom water\n```"
        self.assertEqual(
            command_problems(bad),
            [
                "does not parse: xbloom recipe teaa",
                "does not parse: xbloom-mcp --group all",
                "does not parse: xbloom water",
            ],
        )
