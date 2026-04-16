# ast/

Simplified AST for workflow XML generation: three generic node types + three dispatch node types with paired renderers.

## Files

| File                    | What                                                                  | When to read                           |
| ----------------------- | --------------------------------------------------------------------- | -------------------------------------- |
| `README.md`             | Architecture, design decisions, invariants, extension guide           | Understanding why flat union + builder |
| `nodes.py`              | `TextNode`, `CodeNode`, `ElementNode`                                 | Adding/understanding base node types   |
| `builder.py`            | `W.el(...)` fluent builder API                                        | Constructing AST nodes                 |
| `renderer.py`           | `XMLRenderer`, `render()`, `render_invoke_after()`                    | Rendering AST to XML output            |
| `dispatch.py`           | `SubagentDispatchNode`, `TemplateDispatchNode`, `RosterDispatchNode`  | Sub-agent orchestration patterns       |
| `dispatch_renderer.py`  | Render functions for the three dispatch node types                    | Rendering dispatch XML                 |
| `__init__.py`           | Public API exports                                                    | Importing AST types and renderers      |

## Usage

```python
from skills.lib.workflow.ast import W, render, XMLRenderer, TextNode

# Build step header with attributes
doc = W.el("step_header", TextNode("Title"),
           script="myskill", step="1", total="5").build()
output = render(doc, XMLRenderer())

# Build current_action block from a list of action strings
action_nodes = [TextNode(a) for a in actions]
doc = W.el("current_action", *action_nodes).build()

# Build invoke_after (rendered via renderer.render_invoke_after)
# For the generic case the dedicated helper is preferred:
from skills.lib.workflow.ast import InvokeAfterNode, render_invoke_after
render_invoke_after(InvokeAfterNode(cmd=next_cmd))
```

## Node Types

Generic nodes (use via `W.el(...)` or directly):

| Type          | Purpose                                |
| ------------- | -------------------------------------- |
| `TextNode`    | Plain text content (leaf)              |
| `CodeNode`    | Code block with optional language      |
| `ElementNode` | Generic XML element (via `W.el(...)`)  |

Specialized nodes with dedicated renderers:

| Type                | Purpose                                                              |
| ------------------- | -------------------------------------------------------------------- |
| `FileContentNode`   | Embed file content with CDATA wrapping (`render_file_content`)       |
| `StepHeaderNode`    | Render `<step_header>` with script/step/category/mode/total attrs    |
| `CurrentActionNode` | Render `<current_action>` from a list of action strings              |
| `InvokeAfterNode`   | Render `<invoke_after>` with command or pass/fail branching          |

Legacy nodes removed in the simplification pass: `HeaderNode`, `ActionsNode`, `RawNode`, `CommandNode`, `GuidanceNode`, `RoutingNode`, `TextOutputNode`. Skills add new generic tags via `W.el("tag_name", ...)`; the specialized nodes above remain because they carry structured attributes the generic builder cannot express cleanly.

## Dispatch Node Types

| Type                   | Pattern | Use case                                     |
| ---------------------- | ------- | -------------------------------------------- |
| `SubagentDispatchNode` | Single  | Sequential workflows (plan -> dev -> QR)     |
| `TemplateDispatchNode` | SIMD    | Same template, N targets ($var substitution) |
| `RosterDispatchNode`   | MIMD    | Shared context, unique prompts per agent     |

```python
from skills.lib.workflow.ast import (
    TemplateDispatchNode, render_template_dispatch,
    RosterDispatchNode, render_roster_dispatch,
)

# Template dispatch: $var substituted per-target at render time
node = TemplateDispatchNode(
    agent_type="general-purpose",
    template="Explore $category in $mode mode",
    targets=({"category": "Naming", "mode": "code"}, ...),
    command='python3 -m skills.explore --category $category',
    model="haiku",
)
xml = render_template_dispatch(node)

# Roster dispatch: unique prompts, fixed command
node = RosterDispatchNode(
    agent_type="general-purpose",
    shared_context="Background...",
    agents=("Task 1...", "Task 2...", "Task 3..."),
    command='python3 -m skills.subagent --step 1',
    model="sonnet",
)
xml = render_roster_dispatch(node)
```
