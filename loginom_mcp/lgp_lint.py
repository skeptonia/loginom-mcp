"""Structural lint for a Loginom .lgp: XML and BGB (Unit.bin) must agree.

Loginom reads the binary ``Unit_0/Unit.bin``; the XML is the human-readable
mirror. A package patched by hand (or rebuilt by a script) can end up with a
valid ZIP and valid XML but GUIDs that no longer match the binary — Loginom then
fails with «Атрибут не существует» / «не удалось загрузить элемент».

The checks here are cheap and need no Loginom installation.
"""
from __future__ import annotations

import uuid
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

XSI = "{http://www.w3.org/2001/XMLSchema-instance}type"
MAGIC = b"BGB\xc3"

PORT_SECTIONS = (
    "InputPorts",
    "OutputPorts",
    "ServiceInputPorts",
    "ServiceOutputPorts",
)

REQUIRED_ENTRIES = (
    "PackageIndex.bin",
    "PackageIndex.xml",
    "PackageInfo.bin",
    "PackageInfo.xml",
    "Unit_0/Unit.bin",
    "Unit_0/Unit.xml",
    "Unit_0/Info.bin",
    "Unit_0/Info.xml",
)


def _guid_bytes(value: str) -> bytes:
    return uuid.UUID(value).bytes_le


def _entry(names: set[str], name: str) -> str | None:
    if name in names:
        return name
    alt = name.replace("/", "\\")
    return alt if alt in names else None


def _collect_nodes(container: ET.Element | None, errors: list[str]):
    """Index nodes and their ports.

    Port GUIDs are per-port-type constants: the same "DataSource" port GUID is
    reused by every node of that component. So the identity of a port is the
    (node Guid, port Guid) pair, not the GUID alone.
    """
    nodes: dict[str, str] = {}
    ports: dict[tuple[str, str], str] = {}
    if container is None:
        return nodes, ports
    for item in container:
        if item.tag != "Item":
            continue
        guid = item.get("Guid")
        display = item.get("DisplayName") or ""
        if not guid:
            errors.append(f"node {display!r} has no Guid")
            continue
        if guid in nodes:
            errors.append(f"duplicate node Guid {guid} ({display!r})")
        nodes[guid] = display
        seen_here: set[str] = set()
        for section in PORT_SECTIONS:
            sec = item.find(section)
            if sec is None:
                continue
            for port in sec:
                pguid = port.get("Guid")
                if not pguid:
                    continue
                if pguid in seen_here:
                    errors.append(
                        f"duplicate port Guid {pguid} inside node {display!r}"
                    )
                seen_here.add(pguid)
                ports[(guid, pguid)] = port.get("Name") or ""
    return nodes, ports


def _check_links(
    links_el: ET.Element | None,
    nodes: dict[str, str],
    ports: dict[tuple[str, str], str],
    errors: list[str],
    label: str = "link",
) -> int:
    """Validate SourcePort/TargetPort pairs against the node/port index."""
    if links_el is None:
        return 0
    count = 0
    for link in links_el:
        if link.tag != "Item":
            continue
        count += 1
        for side in ("SourcePort", "TargetPort"):
            el = link.find(side)
            if el is None:
                errors.append(f"{label} #{count}: no {side}")
                continue
            ng = el.get("NodeGuid")
            pg = el.get("PortGuid")
            if ng and ng not in nodes:
                errors.append(f"{label} #{count} {side}: unknown node {ng}")
            if pg:
                if (ng, pg) not in ports:
                    owners = [nodes.get(n, n) for (n, p) in ports if p == pg]
                    errors.append(
                        f"{label} #{count} {side}: port {pg} not on node "
                        f"{nodes.get(ng, ng)!r} (port exists on {owners})"
                    )
    return count


def lint(path: str | Path) -> dict:
    lgp = Path(path)
    errors: list[str] = []
    warnings: list[str] = []
    stats: dict = {}

    if not lgp.exists():
        return {"ok": False, "path": str(lgp), "errors": ["file not found"]}

    try:
        zf = zipfile.ZipFile(lgp)
    except zipfile.BadZipFile as exc:
        return {"ok": False, "path": str(lgp), "errors": [f"bad zip: {exc}"]}

    with zf:
        names = set(zf.namelist())
        missing = [e for e in REQUIRED_ENTRIES if _entry(names, e) is None]
        if missing:
            errors.append(f"missing entries: {missing}")

        def read(name: str) -> bytes:
            real = _entry(names, name)
            return zf.read(real) if real else b""

        for bin_name in (
            "PackageInfo.bin",
            "PackageIndex.bin",
            "Unit_0/Unit.bin",
            "Unit_0/Info.bin",
        ):
            data = read(bin_name)
            if data and not data.startswith(MAGIC):
                errors.append(f"{bin_name}: bad BGB magic")

        raw_xml = read("Unit_0/Unit.xml")
        if not raw_xml:
            errors.append("Unit.xml is empty")
            return {"ok": False, "path": str(lgp), "errors": errors, "warnings": warnings}
        try:
            root = ET.fromstring(raw_xml)
        except ET.ParseError as exc:
            errors.append(f"Unit.xml parse: {exc}")
            return {"ok": False, "path": str(lgp), "errors": errors, "warnings": warnings}

        unit_bin = read("Unit_0/Unit.bin")

        wf = root.find("WorkFlow")
        if wf is None:
            errors.append("no WorkFlow element")
            return {"ok": False, "path": str(lgp), "errors": errors, "warnings": warnings}

        nodes, ports = _collect_nodes(wf.find("Nodes"), errors)
        links = _check_links(wf.find("Links"), nodes, ports, errors)

        # Visualizer nodes (views) live in ModelViews/Nodes, links between them
        # in ModelViews/Links. A visualizer link goes from a workflow node to a
        # view (or view to view), so both indexes apply.
        view_nodes, view_ports = _collect_nodes(root.find("ModelViews/Nodes"), errors)
        view_links = _check_links(
            root.find("ModelViews/Links"),
            {**nodes, **view_nodes},
            {**ports, **view_ports},
            errors,
            label="view link",
        )

        # --- XML vs Unit.bin ---
        def check_guid(kind: str, guid: str, label: str) -> None:
            try:
                blob = _guid_bytes(guid)
            except ValueError:
                errors.append(f"{kind} {label!r}: malformed guid {guid}")
                return
            if unit_bin and blob not in unit_bin:
                errors.append(f"{kind} {label!r}: guid {guid} missing in Unit.bin")

        for kind, index in (("node", nodes), ("view", view_nodes)):
            for guid, display in index.items():
                check_guid(kind, guid, display)
                if unit_bin and display and display.encode("utf-8") not in unit_bin:
                    warnings.append(f"{kind} {display!r}: DisplayName missing in Unit.bin")

        for (node_guid, pguid), pname in ports.items():
            check_guid("port", pguid, f"{nodes.get(node_guid, node_guid)}.{pname}")
        for (node_guid, pguid), pname in view_ports.items():
            check_guid("view port", pguid, f"{view_nodes.get(node_guid, node_guid)}.{pname}")

        for links_el, label in (
            (wf.find("Links"), "link"),
            (root.find("ModelViews/Links"), "view link"),
        ):
            if links_el is None:
                continue
            for link in links_el:
                if link.tag != "Item":
                    continue
                for side in ("SourcePort", "TargetPort"):
                    el = link.find(side)
                    if el is None:
                        continue
                    for attr in ("NodeGuid", "PortGuid"):
                        val = el.get(attr)
                        if val:
                            check_guid(f"{label} {side}", val, attr)

        stats = {
            "nodes": len(nodes),
            "ports": len(ports),
            "links": links,
            "views": len(view_nodes),
            "view_links": view_links,
            "unit_bin_size": len(unit_bin),
        }

    return {
        "ok": not errors,
        "path": str(lgp),
        "stats": stats,
        "errors": errors,
        "warnings": warnings,
    }
