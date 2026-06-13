"""Regression tests for XML/shell escaping in renderer and prompt assemblers.

Guards merged_bug_005 from the 2026-04-17 ultrareview:
- InvokeAfterNode rendering must XML-escape cmd/if_pass/if_fail values
- working_dir must be shlex-quoted so shell metacharacters can't inject
- format_step / sub_agent_invoke must embed the shlex-quoted SKILLS_DIR in
  shell strings they tell the LLM to copy verbatim
- incoherence.py self-chaining uses --thoughts "<ACCUMULATED_CONTEXT>" which
  must survive XML attribute embedding without malforming the element
"""

from __future__ import annotations

import shlex
import xml.etree.ElementTree as ET

import pytest
from hypothesis import given
from hypothesis import strategies as st

from skills.lib.workflow.ast.dispatch import (
    RosterDispatchNode,
    SubagentDispatchNode,
    TemplateDispatchNode,
)
from skills.lib.workflow.ast.dispatch_renderer import (
    render_roster_dispatch,
    render_subagent_dispatch,
    render_template_dispatch,
)
from skills.lib.workflow.ast.nodes import ElementNode, InvokeAfterNode, StepHeaderNode, TextNode
from skills.lib.workflow.ast.renderer import XMLRenderer, render_invoke_after, render_step_header
from skills.lib.workflow.prompts.step import SKILLS_DIR, format_step
from skills.lib.workflow.prompts.subagent import sub_agent_invoke
from skills.refactor.refactor import (
    _invoke_tag,
    build_explore_dispatch,
    format_step_1_output,
)


def _cmd_attr(rendered: str) -> str:
    """Parse rendered <invoke_after> fragment and return the <invoke> cmd attribute.

    Uses ElementTree to verify the output is well-formed XML, then returns the
    resolved attribute value (with entity references already decoded).
    """
    root = ET.fromstring(rendered)
    invoke = root.find("invoke")
    if invoke is None:
        invoke = root.find(".//invoke")
    assert invoke is not None, f"no <invoke> element in:\n{rendered}"
    return invoke.get("cmd", "")


class TestInvokeAfterEscaping:
    """render_invoke_after must produce well-formed XML for hostile cmd values."""

    def test_escapes_double_quotes_in_cmd(self):
        node = InvokeAfterNode(cmd='x --thoughts "ctx"')
        rendered = render_invoke_after(node)
        # Raw unescaped " inside cmd="..." would close the attribute early.
        # ElementTree would raise ParseError if that happened.
        cmd = _cmd_attr(rendered)
        assert 'x --thoughts "ctx"' in cmd

    def test_escapes_angle_brackets_in_cmd(self):
        """The live incoherence.py:198 trigger: --thoughts "<ACCUMULATED_CONTEXT>"."""
        node = InvokeAfterNode(
            cmd="uv run python -m skills.incoherence.incoherence --step-number 2 "
            '--thoughts "<ACCUMULATED_CONTEXT>"'
        )
        rendered = render_invoke_after(node)
        cmd = _cmd_attr(rendered)
        assert '"<ACCUMULATED_CONTEXT>"' in cmd

    def test_escapes_ampersand_in_cmd(self):
        """&& must survive attribute embedding (produces &amp;amp; in raw XML)."""
        node = InvokeAfterNode(cmd="a && b")
        rendered = render_invoke_after(node)
        cmd = _cmd_attr(rendered)
        assert "a && b" in cmd

    def test_shell_quotes_working_dir_with_space(self):
        """working_dir with a space must be single-quoted so cd sees one argument."""
        node = InvokeAfterNode(cmd="true", working_dir="/tmp/a b")
        rendered = render_invoke_after(node)
        cmd = _cmd_attr(rendered)
        assert cmd.startswith("cd '/tmp/a b' && ")

    def test_shell_quotes_working_dir_with_metachar(self):
        node = InvokeAfterNode(cmd="true", working_dir="/tmp/$(evil)")
        rendered = render_invoke_after(node)
        cmd = _cmd_attr(rendered)
        # shlex.quote wraps the whole string; the $( must not leak as bare shell.
        assert cmd.startswith("cd '/tmp/$(evil)' && "), cmd

    def test_branching_form_escapes_both_branches(self):
        node = InvokeAfterNode(
            if_pass='x --msg "pass"',
            if_fail='y --msg "fail"',
        )
        rendered = render_invoke_after(node)
        root = ET.fromstring(rendered)
        if_pass_elem = root.find("if_pass/invoke")
        if_fail_elem = root.find("if_fail/invoke")
        assert if_pass_elem is not None
        assert if_fail_elem is not None
        if_pass = if_pass_elem.get("cmd", "")
        if_fail = if_fail_elem.get("cmd", "")
        assert 'x --msg "pass"' in if_pass
        assert 'y --msg "fail"' in if_fail

    # Printable ASCII + common shell metacharacters. XML 1.0 disallows most
    # control characters (NUL, DEL, etc.) regardless of escaping, so restricting
    # the alphabet here keeps the property focused on what the renderer owns:
    # attribute-safety for realistic shell command strings.
    _SHELL_ALPHABET = st.characters(
        whitelist_categories=("L", "N", "P", "S", "Zs"),
        blacklist_characters="\x00",
    )

    @given(st.text(alphabet=_SHELL_ALPHABET, min_size=0, max_size=50))
    def test_arbitrary_cmd_produces_well_formed_xml(self, cmd: str):
        """Property: any realistic shell cmd must round-trip through XML parsing."""
        node = InvokeAfterNode(cmd=cmd)
        rendered = render_invoke_after(node)
        # ElementTree raises on malformed XML; decoded attribute must equal the input.
        decoded = _cmd_attr(rendered)
        assert decoded.endswith(cmd), (decoded, cmd)


class TestSkillsDirShellQuoting:
    """format_step and sub_agent_invoke must shlex-quote SKILLS_DIR in shell lines."""

    _EXPECTED = shlex.quote(str(SKILLS_DIR))

    def test_format_step_branching_quotes_skills_dir(self):
        out = format_step("body", if_pass="cmd-a", if_fail="cmd-b")
        # Both branches reference the quoted path.
        assert f"cd {self._EXPECTED} && cmd-a" in out
        assert f"cd {self._EXPECTED} && cmd-b" in out

    def test_format_step_next_cmd_quotes_skills_dir(self):
        out = format_step("body", next_cmd="run --step 2")
        assert f"cd {self._EXPECTED} && run --step 2" in out

    def test_sub_agent_invoke_quotes_skills_dir(self):
        out = sub_agent_invoke("uv run python -m foo --step 1")
        assert f"cd {self._EXPECTED} && uv run python -m foo --step 1" in out


class TestIncoherenceRoundTrip:
    """incoherence.format_incoherence_output must emit parseable XML even with
    the <ACCUMULATED_CONTEXT> placeholder baked into the next_cmd."""

    def test_step_1_invoke_after_parses(self):
        from skills.incoherence import incoherence

        guidance = incoherence.STEPS[1]
        out = incoherence.format_incoherence_output(
            step=1,
            phase="detection",
            agent_type="parent",
            guidance=guidance,
        )
        # Step 1 mentions "<invoke_after>" inside the xml_format_mandate prose,
        # so match the last real element by scanning from the last opening tag.
        start = out.rfind("<invoke_after>")
        assert start != -1, f"no invoke_after block in output:\n{out}"
        end = out.find("</invoke_after>", start) + len("</invoke_after>")
        fragment = out[start:end]
        cmd = _cmd_attr(fragment)
        # After XML decoding the attribute, the placeholder survives intact.
        assert '--thoughts "<ACCUMULATED_CONTEXT>"' in cmd
        assert "--step-number 2" in cmd


class TestSpecializedNodeEscaping:
    """render_step_header / render_element must XML-escape hostile attrs/title.

    Guards 2026-06-11 audit bug #9: these emitted attrs/text via raw f-strings,
    so a quote / & / < (or a literal </step_header>) in a title or attribute
    could malform the element. render_invoke_after already did this correctly.
    """

    def test_step_header_title_with_markup(self):
        node = StepHeaderNode(title='x </step_header> & "q" <b>', script="s", step=1)
        root = ET.fromstring(render_step_header(node))
        assert root.text == 'x </step_header> & "q" <b>'

    def test_step_header_attr_with_quote_and_markup(self):
        node = StepHeaderNode(title="t", script='a"b&c', step=1, category="<x>")
        root = ET.fromstring(render_step_header(node))
        assert root.get("script") == 'a"b&c'
        assert root.get("category") == "<x>"

    def test_element_attrs_and_children_well_formed(self):
        node = ElementNode(tag="group", attrs={"id": 'a"&<b', "n": "1"}, children=[TextNode("in")])
        root = ET.fromstring(XMLRenderer().render_element(node))
        assert root.get("id") == 'a"&<b'
        assert root.get("n") == "1"

    def test_self_closing_element_attr_escaped(self):
        node = ElementNode(tag="x", attrs={"v": 'has"quote'}, children=[])
        root = ET.fromstring(XMLRenderer().render_element(node))
        assert root.get("v") == 'has"quote'


class TestDispatchRendererEscaping:
    """dispatch_renderer must XML-escape attribute values (agent_type, invoke cmd).

    Guards 2026-06-11 audit bug #9 whole-class sibling: dispatch_renderer emitted
    agent=/cmd= attrs via raw f-strings, so a quote/&/< in agent_type or a shell
    command (reachable via `refactor --scope`) malformed the dispatch. The invoke
    cmd is quoteattr-escaped exactly like render_invoke_after (so `&&` -> `&amp;&amp;`
    and round-trips back to `&&` on decode). Prose bodies (prompt/task) are NOT
    escaped -- they intentionally carry literal <invoke> markup the sub-agent runs.
    """

    _HOSTILE_AGENT = 'gp"<x>&'
    _HOSTILE_CMD = 'uv run x --flag "q" && y'

    def test_subagent_dispatch_attrs_well_formed(self):
        node = SubagentDispatchNode(
            agent_type=self._HOSTILE_AGENT, command=self._HOSTILE_CMD, prompt="plain prompt"
        )
        root = ET.fromstring(render_subagent_dispatch(node))  # raises if malformed
        assert root.get("agent") == self._HOSTILE_AGENT
        invoke = root.find(".//invoke")
        assert invoke is not None
        cmd = invoke.get("cmd", "")
        assert cmd.startswith("cd ")  # pin_cwd applied
        assert cmd.endswith(self._HOSTILE_CMD)  # quotes + && survive XML decoding

    def test_template_dispatch_attrs_well_formed(self):
        # Hostile chars live in the agent_type/command attrs; the prose template
        # stays plain so the whole fragment is parseable.
        node = TemplateDispatchNode(
            agent_type=self._HOSTILE_AGENT,
            template="Explore $name",
            targets=({"name": "Naming"},),
            command=self._HOSTILE_CMD,
            model="haiku",
        )
        root = ET.fromstring(render_template_dispatch(node))
        assert root.get("agent") == self._HOSTILE_AGENT
        invoke = root.find(".//invoke")
        assert invoke is not None
        assert invoke.get("cmd", "").endswith(self._HOSTILE_CMD)

    def test_roster_dispatch_attrs_well_formed(self):
        node = RosterDispatchNode(
            agent_type=self._HOSTILE_AGENT,
            agents=("plain task",),
            command=self._HOSTILE_CMD,
            shared_context="ctx",
        )
        root = ET.fromstring(render_roster_dispatch(node))
        assert root.get("agent") == self._HOSTILE_AGENT
        invoke = root.find(".//invoke")
        assert invoke is not None
        assert invoke.get("cmd", "").endswith(self._HOSTILE_CMD)

    def test_prose_markup_is_preserved_not_escaped(self):
        # The prompt body intentionally carries literal <invoke> markup the
        # sub-agent reads and runs (refactor's "Start: <invoke .../>"). Escaping
        # it would corrupt the instruction, so prose must stay raw.
        node = SubagentDispatchNode(
            agent_type="general-purpose",
            command="uv run x --step 1",
            prompt='Start: <invoke cmd="do" /> use a & b',
        )
        out = render_subagent_dispatch(node)
        prose = out.split("<directive")[0]  # everything before the structured invoke
        assert 'Start: <invoke cmd="do" />' in prose  # literal markup preserved
        assert "use a & b" in prose  # prose '&' left literal, not &amp;


class TestRefactorScopeEscaping:
    """refactor --scope must not malform dispatch/invoke XML (audit #9 whole-class).

    scope is an unvalidated CLI path; shlex.quote protects the shell layer but not
    XML, so a scope containing &/</" once broke the hand-built <invoke> attributes
    (and build_explore_dispatch double-wrapped a pre-built <invoke> as the dispatch
    command). _invoke_tag and the bare-command dispatch now route through quoteattr.
    """

    _HOSTILE = 'src/"weird"&<x>'

    def test_invoke_tag_escapes_hostile_cmd(self):
        root = ET.fromstring(_invoke_tag("uv run x --scope 'a&b\"c<d'"))
        assert root.tag == "invoke"
        # _invoke_tag now cwd-pins via pin_cwd (audit #12); the hostile &/"/< still
        # survive the quoteattr round-trip intact, and the relative working-dir is gone.
        assert root.get("cmd") == f"cd {SKILLS_DIR} && uv run x --scope 'a&b\"c<d'"
        assert root.get("working-dir") is None

    def test_explore_dispatch_well_formed_with_hostile_scope(self):
        out = build_explore_dispatch(n=2, mode_filter="both", scope=self._HOSTILE)
        invoke = ET.fromstring(out).find(".//invoke")  # raises if malformed
        assert invoke is not None
        cmd = invoke.get("cmd", "")
        assert self._HOSTILE in cmd  # scope survives XML decoding
        assert "<invoke" not in cmd  # no double-wrap (bare command, single render)

    def test_step_1_invoke_after_well_formed_with_hostile_scope(self):
        out = format_step_1_output(2, {"title": "Mode Selection"}, "both", self._HOSTILE)
        start = out.rfind("<invoke_after>")
        frag = out[start : out.find("</invoke_after>", start) + len("</invoke_after>")]
        ET.fromstring(frag)  # raises if malformed


class TestCliOutputEscaping:
    """CLI result/error frames must stay well-formed when agent-influenced values
    carry XML metacharacters (audit #9 whole-class tail: plan.py/qr.py/output.py
    emitted <message>/<actual>/entity JSON via raw f-strings)."""

    @staticmethod
    def _text(root: ET.Element, tag: str) -> str | None:
        el = root.find(tag)
        assert el is not None, f"<{tag}> missing"
        return el.text

    def test_print_entity_result_escapes_id(self, capsys):
        from skills.planner.cli.output import EntityResult, print_entity_result

        print_entity_result(EntityResult(id="M&<1>", version=2, operation="created"))
        root = ET.fromstring(capsys.readouterr().out.strip())
        assert self._text(root, "id") == "M&<1>"

    def test_version_mismatch_cdata_preserves_json(self, capsys):
        from skills.planner.cli.output import VersionMismatchError, exit_with_version_error

        blob = '{"diff": "a < b && c", "name": "<x>"}'
        with pytest.raises(SystemExit):
            exit_with_version_error(VersionMismatchError("M&1", 1, 2, blob))
        root = ET.fromstring(capsys.readouterr().out.strip())  # raises if malformed
        assert self._text(root, "current_entity") == blob  # CDATA: byte-for-byte JSON

    def test_plan_validation_error_escapes_values(self, capsys):
        from skills.planner.cli.plan import validation_error

        with pytest.raises(SystemExit):
            validation_error("loc", "exp", 'got "<bad>&"', "do x")
        root = ET.fromstring(capsys.readouterr().out.strip())
        assert self._text(root, "actual") == 'got "<bad>&"'

    def test_qr_error_exit_escapes_message(self, capsys):
        from skills.planner.cli.qr import error_exit

        with pytest.raises(SystemExit):
            error_exit("bad <thing> & co")
        root = ET.fromstring(capsys.readouterr().out.strip())
        assert self._text(root, "message") == "bad <thing> & co"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
