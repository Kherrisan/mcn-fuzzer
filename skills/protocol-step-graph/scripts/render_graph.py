#!/usr/bin/env python3
"""Render protocol steps and dependencies as channel-grouped DOT, PNG, and SVG."""

import argparse
import json
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path


COLORS = ("#dbeafe", "#dcfce7", "#fef3c7", "#fce7f3", "#ede9fe", "#cffafe")
ACTION_COLORS = {"listen": "#ede9fe", "connect": "#fef3c7", "close": "#fee2e2"}


def quote(value):
    return json.dumps(str(value), ensure_ascii=False)


def wrapped(value):
    return "\n".join(
        line for paragraph in str(value).splitlines()
        for line in (textwrap.wrap(paragraph, width=44) or [""])
    )


def records(model, key):
    rows = model.get(key)
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError(f"{key} must be an array of objects")
    return rows


def named(rows, kind):
    index = {}
    for row in rows:
        name = row.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"every {kind} needs a nonempty name")
        if name in index:
            raise ValueError(f"duplicate {kind} name: {name}")
        index[name] = row
    return index


def build_dot(model, rankdir="LR", edge_labels=False):
    if not isinstance(model, dict):
        raise ValueError("protocol.json must contain a model object")
    steps = records(model, "steps")
    dependencies = records(model, "step_dependencies")
    channels = named(records(model, "channels"), "channel")
    messages = named(records(model, "messages"), "message")
    by_name = named(steps, "step")
    node_ids = {name: f"n{index}" for index, name in enumerate(by_name)}
    groups = {name: [] for name in channels}
    seen_ids = set()
    for step in steps:
        identity = step.get("id")
        if identity is not None:
            if not isinstance(identity, str) or not identity:
                raise ValueError(f"invalid step id: {identity!r}")
            if identity in seen_ids:
                raise ValueError(f"duplicate step id: {identity}")
            seen_ids.add(identity)
        channel = step.get("channel_name")
        if channel not in channels:
            raise ValueError(f"step {step['name']} references unknown channel {channel!r}")
        action = step.get("action", "send")
        if action not in ("send", "listen", "connect", "close"):
            raise ValueError(f"step {step['name']} has unknown action {action!r}")
        if action == "send" and step.get("message_name") not in messages:
            raise ValueError(f"step {step['name']} references an unknown message")
        roles = ("sender_name", "receiver_name") if action == "send" else ("party_name",)
        if any(not isinstance(step.get(key), str) or not step[key].strip() for key in roles):
            raise ValueError(f"step {step['name']} needs {', '.join(roles)}")
        groups[channel].append(step)

    senders = sorted({step["sender_name"] for step in steps
                      if step.get("action", "send") == "send"})
    sender_colors = {name: COLORS[index % len(COLORS)] for index, name in enumerate(senders)}

    for edge in dependencies:
        for key in ("prior_step", "dependent_step"):
            if edge.get(key) not in by_name:
                raise ValueError(f"dependency {key} references unknown step {edge.get(key)!r}")

    meta = model.get("meta", {})
    title = meta.get("protocol", "Protocol steps")
    if meta.get("complete") is False:
        title += " (incomplete model)"
    lines = [
        "digraph protocol {",
        f"  graph [rankdir={rankdir}, compound=true, bgcolor=white, pad=0.3,",
        f"         label={quote(title + chr(10) + 'send: color by sender; hexagon: network action' + chr(10) + 'solid: within channel; dashed: cross-channel dependency')},",
        '         labelloc=t, fontname="DejaVu Sans", fontsize=16];',
        '  node [shape=box, style="rounded,filled", fontname="DejaVu Sans", fontsize=10, margin=0.12];',
        '  edge [fontname="DejaVu Sans", fontsize=9, color="#64748b", arrowsize=0.7];',
    ]
    for index, (channel, members) in enumerate(groups.items()):
        lines.extend([
            f"  subgraph cluster_{index} {{",
            f"    label={quote(channel)}; color={quote('#94a3b8')}; style=rounded;",
        ])
        for step in members:
            action = step.get("action", "send")
            identity = step.get("id", "—")
            short_id = identity.removeprefix("step-")[:8]
            message = step.get("message_name") if action == "send" else "—"
            role = (f"{step['sender_name']} → {step['receiver_name']}" if action == "send"
                    else step["party_name"])
            label = "\n".join((wrapped(step["name"]), f"id: {short_id} · {action}",
                                wrapped(f"msg: {message}"), wrapped(role)))
            tooltip = json.dumps(step, ensure_ascii=False, indent=2)
            shape = "box" if action == "send" else "hexagon"
            color = sender_colors[step["sender_name"]] if action == "send" else ACTION_COLORS[action]
            lines.append(f"    {node_ids[step['name']]} [label={quote(label)}, shape={shape}, "
                         f"fillcolor={quote(color)}, tooltip={quote(tooltip)}];")
        lines.append("  }")
    cross_channel = 0
    for index, edge in enumerate(dependencies):
        prior, dependent = edge["prior_step"], edge["dependent_step"]
        cross = by_name[prior]["channel_name"] != by_name[dependent]["channel_name"]
        cross_channel += int(cross)
        attributes = [f"id={quote(f'dependency-{index}')}",
                      f"tooltip={quote(json.dumps(edge, ensure_ascii=False, indent=2))}"]
        if cross:
            attributes.extend(['style=dashed', 'color="#7c3aed"'])
        if edge_labels and edge.get("condition"):
            attributes.append(f"label={quote(wrapped(edge['condition']))}")
        lines.append(f"  {node_ids[prior]} -> {node_ids[dependent]} [{', '.join(attributes)}];")
    lines.append("}")
    return "\n".join(lines) + "\n", {
        "nodes": len(steps), "edges": len(dependencies),
        "channels": len(channels), "cross_channel_edges": cross_channel,
        "complete": meta.get("complete"),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("protocol", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--rankdir", choices=("LR", "TB"), default="LR")
    parser.add_argument("--edge-labels", action="store_true")
    args = parser.parse_args()
    try:
        model = json.loads(args.protocol.read_text(encoding="utf-8"))
        dot, summary = build_dot(model, args.rankdir, args.edge_labels)
        executable = shutil.which("dot")
        if not executable:
            raise ValueError("Graphviz 'dot' is required; install Graphviz and retry")
        rendered = subprocess.run([executable, "-Tsvg"], input=dot, text=True,
                                  encoding="utf-8", capture_output=True, check=True)
        png = subprocess.run([executable, "-Tpng", "-Gdpi=120"], input=dot.encode("utf-8"),
                             capture_output=True, check=True)
        args.output_dir.mkdir(parents=True, exist_ok=True)
        dot_path = args.output_dir / "steps.dot"
        svg_path = args.output_dir / "steps.svg"
        png_path = args.output_dir / "steps.png"
        dot_path.write_text(dot, encoding="utf-8")
        svg_path.write_text(rendered.stdout, encoding="utf-8")
        png_path.write_bytes(png.stdout)
        print(json.dumps({**summary, "dot": str(dot_path.resolve()),
                          "png": str(png_path.resolve()),
                          "svg": str(svg_path.resolve())}, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        detail = error.stderr if isinstance(error, subprocess.CalledProcessError) else str(error)
        if isinstance(detail, bytes):
            detail = detail.decode("utf-8", errors="replace")
        print(f"protocol-step-graph: {detail}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
