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

from skills.lib.workflow.ast.nodes import InvokeAfterNode
from skills.lib.workflow.ast.renderer import render_invoke_after
from skills.lib.workflow.prompts.step import SKILLS_DIR, format_step
from skills.lib.workflow.prompts.subagent import sub_agent_invoke


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
            cmd="python3 -m skills.incoherence.incoherence --step-number 2 "
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
        if_pass = root.find("if_pass/invoke").get("cmd")
        if_fail = root.find("if_fail/invoke").get("cmd")
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
        out = sub_agent_invoke("python3 -m foo --step 1")
        assert f"cd {self._EXPECTED} && python3 -m foo --step 1" in out


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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
