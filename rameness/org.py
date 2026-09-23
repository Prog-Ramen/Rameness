"""Private customization layer: who the user/organization is and what their words mean.

``org.json`` (``~/.rameness/org.json`` then ``./.rameness/org.json``)::

    {
      "name": "Acme",
      "facts": ["Customer data lives in the analytics Postgres (read replica)."],
      "defaults": {"data_source": "postgres", "report_type": "monthly_customer_acquisition"},
      "glossary": {"customer report": "monthly customer acquisition report"},
      "private_terms": ["acme-internal", "analytics.acme.corp"]
    }

``defaults`` resolve SOP-tree requirements (see ``sops.unresolved``) so a
generic request can traverse straight to the organization's procedures.
``private_terms`` feed the publish scrubber so they never leak into public SOPs.
Nothing here is ever published.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Org:
    name: str = ""
    facts: list[str] = field(default_factory=list)
    defaults: dict = field(default_factory=dict)
    glossary: dict = field(default_factory=dict)
    private_terms: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, cwd: Path, home: Path) -> "Org":
        org = cls()
        for p in (home / "org.json", cwd / ".rameness" / "org.json"):
            if p.exists():
                d = json.loads(p.read_text())
                org.name = d.get("name", org.name)
                org.facts += d.get("facts", [])
                org.defaults.update(d.get("defaults", {}))
                org.glossary.update(d.get("glossary", {}))
                org.private_terms += d.get("private_terms", [])
        return org

    def expand(self, task: str) -> str:
        """Append glossary meanings for terms the task uses."""
        low = task.lower()
        notes = [f'"{k}" means {v}' for k, v in self.glossary.items() if k.lower() in low]
        return task + ("\n(" + "; ".join(notes) + ")" if notes else "")

    def render(self) -> str:
        if not (self.name or self.facts or self.defaults):
            return ""
        parts = [f"Organization: {self.name}" if self.name else ""]
        parts += [f"- {f}" for f in self.facts]
        if self.defaults:
            parts.append("Defaults: " + ", ".join(f"{k}={v}" for k, v in self.defaults.items()))
        return "\n".join(p for p in parts if p)
