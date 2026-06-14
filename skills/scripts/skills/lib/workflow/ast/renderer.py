"""Renderer for converting AST to string output.

Simplified renderer handling only the core node types: TextNode, CodeNode, ElementNode.
"""

import re
from typing import Protocol
from xml.sax.saxutils import escape, quoteattr

from skills.lib.workflow.ast.nodes import (
    CodeNode,
    CurrentActionNode,
    Document,
    ElementNode,
    FileContentNode,
    InvokeAfterNode,
    Node,
    StepHeaderNode,
    TextNode,
)
from skills.lib.workflow.prompts.step import pin_cwd


class Renderer(Protocol):
    """Abstract renderer interface."""

    def render_text(self, node: TextNode) -> str: ...
    def render_code(self, node: CodeNode) -> str: ...
    def render_element(self, node: ElementNode) -> str: ...
    def render_file_content(self, node: FileContentNode) -> str: ...
    def render_step_header(self, node: StepHeaderNode) -> str: ...
    def render_current_action(self, node: CurrentActionNode) -> str: ...
    def render_invoke_after(self, node: InvokeAfterNode) -> str: ...


# XML 1.0 attribute-name character class (NameStartChar + NameChar).
# Keys that fail this validation would produce malformed XML; rejecting them
# here is a fail-fast guard rather than silently emitting broken markup.
_XML_NAME_RE = re.compile(r'^[A-Za-z_:][\w.:-]*$')


class XMLRenderer:
    """Renders AST nodes to XML format."""

    def render_text(self, node: TextNode) -> str:
        """Render text node as plain string."""
        return node.content

    def render_code(self, node: CodeNode) -> str:
        """Render code node as markdown code block."""
        if node.language:
            return f"```{node.language}\n{node.content}\n```"
        return f"```\n{node.content}\n```"

    def render_element(self, node: ElementNode) -> str:
        """Render generic element with attributes and children.

        Attribute values go through quoteattr so a value containing a quote,
        ``&``, or ``<`` cannot break out of the attribute (matches
        render_invoke_after).
        """
        attrs_str = ""
        if node.attrs:
            for k in node.attrs:
                if not _XML_NAME_RE.match(k):
                    raise ValueError(f"Invalid XML attribute key: {k!r}")
            attrs_str = " " + " ".join(f"{k}={quoteattr(str(v))}" for k, v in node.attrs.items())

        if not node.children:
            return f"<{node.tag}{attrs_str} />"

        children_str = "\n".join(self._render_node(child) for child in node.children)
        return f"<{node.tag}{attrs_str}>\n{children_str}\n</{node.tag}>"

    def render_file_content(self, node: FileContentNode) -> str:
        """Render file content node with CDATA wrapping.

        CDATA protects against content containing literal </file> strings
        (e.g., code examples in markdown showing XML parsing).

        Content containing "]]>" would break CDATA, so we escape by splitting
        into multiple CDATA sections: "foo]]>bar" -> "foo]]]]><![CDATA[>bar"
        """
        # Escape "]]>" sequences to prevent premature CDATA termination
        escaped = node.content.replace("]]>", "]]]]><![CDATA[>")
        return f"<file path={quoteattr(node.path)}><![CDATA[\n{escaped}\n]]></file>"

    def render_step_header(self, node: StepHeaderNode) -> str:
        """Render step header with title as content, metadata as attributes.

        Attributes go through quoteattr and the title through escape so a
        quote/ampersand/angle bracket (or a literal ``</step_header>``) in any
        value cannot malform the element.
        """
        attrs = {"script": node.script, "step": str(node.step)}
        if node.category:
            attrs["category"] = node.category
        if node.mode:
            attrs["mode"] = node.mode
        if node.total is not None:
            attrs["total"] = str(node.total)

        attrs_str = " " + " ".join(f"{k}={quoteattr(str(v))}" for k, v in attrs.items())
        return f"<step_header{attrs_str}>{escape(node.title)}</step_header>"

    def render_current_action(self, node: CurrentActionNode) -> str:
        """Render current_action with actions as text children."""
        children_str = "\n".join(action for action in node.actions)
        return f"<current_action>\n{children_str}\n</current_action>"

    def render_invoke_after(self, node: InvokeAfterNode) -> str:
        """Render invoke_after with command or branching structure.

        WHY no validation here: __post_init__ validates at construction time.
        Renderer assumes valid node, focuses solely on XML generation.

        WHY pin_cwd: routes through the absolute-cd helper (same as
        dispatch_renderer.py) so the emitted invoke is cwd-independent; the
        agent can run it from any directory without a "No module named 'skills'"
        error. quoteattr escapes the final shell string for safe XML attribute
        embedding so quotes/angle brackets inside node.cmd do not produce
        malformed XML. Composition of caller-supplied node.cmd parts remains
        the caller's responsibility.
        """
        if node.cmd is not None:
            invoke = f"<invoke cmd={quoteattr(pin_cwd(node.cmd))} />"
            return f"<invoke_after>\n{invoke}\n</invoke_after>"
        else:
            # __post_init__ guarantees both branches are set when cmd is None.
            assert node.if_pass is not None and node.if_fail is not None
            if_pass_invoke = f"<invoke cmd={quoteattr(pin_cwd(node.if_pass))} />"
            if_fail_invoke = f"<invoke cmd={quoteattr(pin_cwd(node.if_fail))} />"
            return f"<invoke_after>\n  <if_pass>\n    {if_pass_invoke}\n  </if_pass>\n  <if_fail>\n    {if_fail_invoke}\n  </if_fail>\n</invoke_after>"

    def _render_node(self, node: Node) -> str:
        """Dispatch node to appropriate render method."""
        match node:
            case TextNode():
                return self.render_text(node)
            case CodeNode():
                return self.render_code(node)
            case ElementNode():
                return self.render_element(node)
            case FileContentNode():
                return self.render_file_content(node)
            case StepHeaderNode():
                return self.render_step_header(node)
            case CurrentActionNode():
                return self.render_current_action(node)
            case InvokeAfterNode():
                return self.render_invoke_after(node)


def render(doc: Document, renderer: Renderer) -> str:
    """Render document using provided renderer.

    Args:
        doc: Document to render
        renderer: Renderer implementation (XMLRenderer, etc)

    Returns:
        Rendered string output
    """
    if isinstance(renderer, XMLRenderer):
        parts = [renderer._render_node(child) for child in doc.children]
        return "\n".join(parts)

    raise NotImplementedError(f"Renderer {type(renderer).__name__} not implemented")


def render_step_header(node: StepHeaderNode) -> str:
    """Render StepHeaderNode directly without Document wrapper.

    WHY convenience: Most callers need standalone XML fragments, not full documents.
    """
    return XMLRenderer().render_step_header(node)


def render_current_action(node: CurrentActionNode) -> str:
    """Render CurrentActionNode directly without Document wrapper.

    WHY convenience: Most callers need standalone XML fragments, not full documents.
    """
    return XMLRenderer().render_current_action(node)


def render_invoke_after(node: InvokeAfterNode) -> str:
    """Render InvokeAfterNode directly without Document wrapper.

    WHY convenience: Most callers need standalone XML fragments, not full documents.
    """
    return XMLRenderer().render_invoke_after(node)
