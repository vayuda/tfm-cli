"""Regenerate tfm/data/*.json from a built checkout of the terraforming-mars app.

Usage:
    # in the app checkout, after `npm ci && npm run build`:
    cp tools/tfm_cli_export.ts tools/tfm_cli_cards_export.ts <app>/src/tools/
    (cd <app> && npx tsx src/tools/tfm_cli_export.ts /tmp/tfm_export.json)
    (cd <app> && npx tsx src/tools/tfm_cli_cards_export.ts /tmp/tfm_cards_export.json)
    python3 tools/build_data.py <app> /tmp/tfm_export.json /tmp/tfm_cards_export.json

The third file (card effects and planetary tracks, used by `tfm value`) is optional; without it
tfm/data/behaviors.json is left as it is.
"""
import json
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "tfm" / "data"

ICON_SYMBOLS = {"->": "→", ":": ":", "/": "/", "*": "*", "nbsp": " ", " ": " ", "+": "+", "-": "-", "OR": " or "}


def render_icons(node):
    """Rough text rendering of a card's icon layout, used when it has no written text."""
    if isinstance(node, str):
        return ""
    if isinstance(node, list):
        return " ".join(filter(None, (render_icons(n) for n in node)))
    kind = node.get("is")
    if kind in ("root", "effect"):
        return " ".join(filter(None, (render_icons(r) for r in node.get("rows", []))))
    if kind == "production-box":
        return "prod[" + " ".join(filter(None, (render_icons(r) for r in node.get("rows", [])))) + "]"
    if kind == "symbol":
        return ICON_SYMBOLS.get(node.get("type"), node.get("type", ""))
    if kind == "tile":
        return "tile"
    if kind == "item":
        if node.get("type") == "text":
            return ""
        name = node.get("resource") or node.get("type", "")
        amount = node.get("amount")
        s = f"{amount} {name}" if amount not in (None, 1, -1) else name
        if node.get("anyPlayer"):
            s = "any " + s
        return s
    return ""


def collect_texts(node, out):
    if isinstance(node, str):
        if node.strip():
            out.append(node.strip())
    elif isinstance(node, list):
        for n in node:
            collect_texts(n, out)
    elif isinstance(node, dict):
        if node.get("is") == "item" and node.get("type") == "text" and node.get("text"):
            out.append(node["text"].strip())
        for r in node.get("rows", []):
            collect_texts(r, out)


def requirement_text(r):
    if "tag" in r:
        s = f"{r.get('count', 1)} {r['tag']} tag"
    elif "party" in r:
        s = f"{r['party']} ruling"
    elif "production" in r:
        s = f"{r.get('count', 1)} {r['production']} production"
    elif "plantsRemoved" in r:
        s = "plants removed this generation"
    else:
        key = next((k for k in r if k not in ("count", "max", "all", "nextTo", "text")), "?")
        s = f"{key} {r.get('count', '')}".strip()
    if r.get("max"):
        s = "max " + s
    if r.get("all"):
        s += " (any player)"
    if r.get("text"):
        s += f" {r['text'].lower()}"
    return s


def vp_text(vp):
    if vp is None:
        return None
    if isinstance(vp, (int, str)):
        return str(vp)
    if "resourcesHere" in vp:
        per = vp.get("per", 1)
        return "1 per resource here" if per == 1 else f"1 per {per} resources here"
    if "tag" in vp:
        per = vp.get("per", 1)
        return f"1 per {per} {vp['tag']} tags" if per != 1 else f"1 per {vp['tag']} tag"
    return "special"


def build_cards(app):
    cards = json.loads((app / "src/genfiles/cards.json").read_text())
    out = {}
    for c in cards:
        md = c.get("metadata", {})
        desc = md.get("description")
        if isinstance(desc, dict):
            desc = desc.get("text")
        texts = []
        collect_texts(md.get("renderData", {}), texts)
        parts = [t for t in texts if t] + ([desc] if desc else [])
        seen, text = set(), []
        for p in parts:
            if p not in seen:
                seen.add(p)
                text.append(p)
        entry = {
            "type": c["type"],
            "cost": c.get("cost"),
            "tags": c.get("tags", []),
            "text": " ".join(text) or render_icons(md.get("renderData", {})),
            "module": c.get("module"),
        }
        if c.get("requirements"):
            entry["req"] = [requirement_text(r) for r in c["requirements"]]
        vp = vp_text(c.get("victoryPoints"))
        if vp:
            entry["vp"] = vp
        if c.get("resourceType"):
            entry["resource"] = c["resourceType"]
        if c.get("startingMegaCredits") is not None:
            entry["startingMC"] = c["startingMegaCredits"]
        out[c["name"]] = entry
    return out


def main():
    app = Path(sys.argv[1])
    export = json.loads(Path(sys.argv[2]).read_text())
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "cards.json").write_text(json.dumps(build_cards(app), separators=(",", ":"), ensure_ascii=False))
    ma = {
        "milestones": {m["name"]: m["description"] for m in json.loads((app / "src/genfiles/milestones.json").read_text())},
        "awards": {a["name"]: a["description"] for a in json.loads((app / "src/genfiles/awards.json").read_text())},
    }
    (OUT / "ma.json").write_text(json.dumps(ma, separators=(",", ":"), ensure_ascii=False))
    colonies = {}
    for c in json.loads((app / "src/genfiles/colonies.json").read_text()):
        colonies[c["name"]] = {
            "build": c["build"]["description"],
            "trade": c["trade"]["description"],
            "tradeQty": c["trade"].get("quantity"),
            "colonyBonus": c["colony"]["description"],
        }
        if isinstance(c["trade"].get("resource"), list):  # the reward type changes along the track
            colonies[c["name"]]["tradeResource"] = c["trade"]["resource"]
    (OUT / "colonies.json").write_text(json.dumps(colonies, separators=(",", ":"), ensure_ascii=False))
    (OUT / "board.json").write_text(json.dumps(export, separators=(",", ":")))
    if len(sys.argv) > 3:
        behaviors = json.loads(Path(sys.argv[3]).read_text())
        (OUT / "behaviors.json").write_text(json.dumps(behaviors, separators=(",", ":"), ensure_ascii=False))
        print(f"wrote effects for {len(behaviors['cards'])} cards to {OUT / 'behaviors.json'}")
    print(f"wrote {len(ma['milestones'])} milestones, {len(colonies)} colonies to {OUT}")


if __name__ == "__main__":
    main()
