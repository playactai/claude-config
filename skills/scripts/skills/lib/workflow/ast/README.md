# AST Module

Type-safe AST representation for workflow output with a fluent builder API and pluggable renderers.

## Architecture

```
Skill step handlers  (26+ call sites)
       |
       v
+------------------+
| Builder API      |
| W.el(tag, ...)   |
+------------------+
       |
       v
+-------------------------------------------+
|               AST Nodes                   |
|                                           |
|  Base:    TextNode | CodeNode | Element-  |
|                                     Node  |
|  Dispatch: SubagentDispatchNode |         |
|            TemplateDispatchNode |         |
|            RosterDispatchNode             |
+-------------------------------------------+
       |
       v
+------------------+     +--------------------+
| XMLRenderer      |     | Dispatch renderers |
| (generic XML)    |     | (one per node type)|
+------------------+     +--------------------+
       |                          |
       +------------+-------------+
                    v
                str output
```

## Data Flow

```
Skill step handler
       |
       | W.el("step_header", TextNode("Title"), script="x", step="1")
       v
ASTBuilder accumulates ElementNodes
       |
       | .build() returns Document (or .node() returns the single root ElementNode)
       v
Document(children=[ElementNode("step_header", ...), ...])
       |
       | render(doc, XMLRenderer())
       v
XMLRenderer._render_node() matches each node type,
renders attributes + children recursively
       v
"<step_header script='x' step='1'>Title</step_header>"
```

Dispatch nodes use dedicated renderers (`render_subagent_dispatch`, `render_template_dispatch`, `render_roster_dispatch`) because their output includes richer structures than generic XML: PARALLEL EXECUTION constraints, SHARED CONTEXT sections, per-agent task blocks, and IMMEDIATELY-invoke directives.

## Why This Structure

### Module Organization

- **nodes.py**: Node definitions isolated from construction logic. Importing a node type does not drag in the builder.
- **builder.py**: `W.el(tag, *children, **attrs)` is the single fluent method. Earlier drafts had `W.header()`, `W.text_output()`, etc., which collapsed into `W.el()` once every caller converged on it.
- **renderer.py**: Generic XML rendering decoupled from the AST. A future renderer (plain text, JSON) can live alongside without touching node definitions.
- **dispatch.py + dispatch_renderer.py**: Dispatch nodes and their renderers are separated from the generic renderer because their output is fundamentally richer (multi-section parallel-agent blocks) and does not fit the generic tag-with-attributes-and-children shape.

### Design Choices

**Three base node types only**: `TextNode`, `CodeNode`, `ElementNode`. Earlier iterations had per-concept classes (HeaderNode, ActionsNode, CommandNode, RoutingNode, GuidanceNode, RawNode, TextOutputNode). Those collapsed into `ElementNode` once all skills used `W.el("tag_name", ...)` with free-form tags and attributes. Fewer classes means fewer places to update when conventions evolve; the tag is a string, not a type.

**Frozen dataclasses**: Immutability aligns with FP style, prevents accidental mutation, and enables safe sharing of nodes between renders and caching.

**Flat union (`children: list[Node]`)**: Workflow output is sequential composition (header + body + invoke), not nested prose. A flat list matches the actual patterns better than a layered inline/block distinction.

**Separate dataclass per node type**: Type-safe field access with IDE autocomplete. More explicit than a shared attrs dict. Standard Python pattern for discriminated unions.

**Builder API**: Direct `ElementNode(...)` construction requires knowing field names and types. `W.el(...)` provides fluent syntax with autocomplete, reducing cognitive load for skill authors.

**Immutable builder pattern**: Each `W.el(...)` call returns a NEW `ASTBuilder` instance with the accumulated node. No shared mutable state between calls.

**External `render()` function**: Separation of concerns — `Document` does not need to know about renderers. Easier to add new renderers without modifying the Document class. Multiple dispatch without coupling nodes to a renderer interface.

## Invariants

1. **Node types are frozen dataclasses**: Immutable after construction. No field mutation allowed.
2. **`ElementNode.children` is always `list[Node]`**, never `None`. Leaf elements carry an empty list.
3. **`XMLRenderer` must handle every node type** reachable from the public API. `_render_node()` uses `match` for exhaustiveness.
4. **Builder methods return NEW builder instances**. Final `.build()` returns a `Document`; `.node()` returns the single accumulated node when there is exactly one.
5. **Invoke commands in dispatch output are self-contained**: `cd {working_dir} && {cmd}`. Sub-agent Bash tools sometimes ignore a separate `working-dir` attribute, so the cd must be in the command itself.

## Dispatch Node Semantics

| Type                   | Pattern | Use case                                     |
| ---------------------- | ------- | -------------------------------------------- |
| `SubagentDispatchNode` | Single  | Sequential workflows (plan -> dev -> QR)     |
| `TemplateDispatchNode` | SIMD    | Same template, N targets ($var substitution) |
| `RosterDispatchNode`   | MIMD    | Shared context, unique prompts per agent     |

`TemplateDispatchNode` expands `$var` placeholders per target at render time, so the sub-agent sees only final prompts — no runtime substitution. `RosterDispatchNode` takes a list of unique task strings and a single shared context block.

## Tradeoffs

### Flat union vs typed children

**Chose**: Flat `children: list[Node]` over typed `list[InlineNode]`.
**Why**: Loses compile-time nesting enforcement but simplifies the API. Workflow output is sequential composition, not nested prose. Runtime validation can catch invalid nesting if it ever becomes a problem.

### Builder vs direct construction

**Chose**: Builder adds one layer of indirection but improves ergonomics.
**Why**: Skill authors write `W.el("step_header", TextNode(...), step="1")`, not `ElementNode(tag="step_header", attrs={"step": "1"}, children=[...])`. Autocomplete + fluent chaining reduces cognitive load.

### One `W.el()` vs many `W.header()` / `W.text_output()` / ...

**Chose**: Single `W.el(tag, ...)` method.
**Why**: The specialized builder methods existed transitionally. Once every caller converged on `W.el()`, keeping the specialized methods was pure maintenance cost — a tag change required updating both the builder and every caller. Free-form tags also mean the AST does not need to know about skill-specific vocabulary.

## Extending the AST

To add a new element type:

1. Prefer `W.el("new_tag", ...)` — `ElementNode` supports arbitrary tags and attributes.
2. If the rendering shape is fundamentally different (richer than generic tag-children-attrs), add:
   - A frozen dataclass to `nodes.py` (for base nodes) or `dispatch.py` (for dispatch nodes).
   - A builder helper or argument shape if needed.
   - A `render_*` function in the matching renderer module.
   - A case in `XMLRenderer._render_node()` if the type flows through the generic renderer.

The match statement catches missing cases at runtime if a case is not handled.
