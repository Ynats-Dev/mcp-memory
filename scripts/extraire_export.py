#!/usr/bin/env python3
"""Extrait les fichiers de contexte d'un export claude.ai (archive « projects ») en .md, un dossier par projet.

    python3 scripts/extraire_export.py work            # dossier des <uuid>.json dézippés
    python3 scripts/extraire_export.py projects-000.zip

Résultat dans <source>/md/<Projet>/ :
  - un .md par fichier de contexte, nom d'origine conservé ;
  - _instructions.txt : instructions du projet claude.ai (pour mémoire) ;
  - _doublons/ : fichiers plus anciens qui donneraient le même document une fois le
    suffixe « _HHMM » retiré (seul le plus récent est gardé à la racine).
Les dossiers peuvent ensuite être glissés dans la zone de dépôt du projet (interface web).
"""

from __future__ import annotations

import json
import re
import sys
import unicodedata
import zipfile
from pathlib import Path


def cle_document(nom: str) -> str:
    """Même règle que app/importer.py : nom du document créé sur le serveur."""
    base = re.sub(r"[_ -]\d{4}$", "", Path(nom).stem.strip())
    base = unicodedata.normalize("NFKD", base).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", base).strip("-")


def nom_sur(nom: str) -> str:
    return re.sub(r'[/\\:*?"<>|]+', "-", nom).strip(" .") or "sans-nom"


def projets(source: Path):
    if source.is_file() and source.suffix == ".zip":
        with zipfile.ZipFile(source) as z:
            for n in sorted(z.namelist()):
                if n.endswith(".json"):
                    yield json.loads(z.read(n))
    else:
        for f in sorted(source.glob("*.json")):
            yield json.loads(f.read_text(encoding="utf-8"))


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 1
    source = Path(sys.argv[1])
    sortie = (source.parent if source.is_file() else source) / "md"
    total = 0
    for p in projets(source):
        docs = [d for d in p.get("docs", []) if d.get("filename") and d.get("content")]
        if not p.get("name") or not (docs or p.get("prompt_template")):
            continue
        dossier = sortie / nom_sur(p["name"])
        dossier.mkdir(parents=True, exist_ok=True)
        if p.get("prompt_template"):
            (dossier / "_instructions.txt").write_text(p["prompt_template"], encoding="utf-8")
        groupes: dict[str, list[dict]] = {}
        for d in docs:
            groupes.setdefault(cle_document(d["filename"]), []).append(d)
        print(f"■ {p['name']} → {dossier}")
        for cle, groupe in sorted(groupes.items()):
            groupe.sort(key=lambda d: d.get("created_at", ""), reverse=True)
            for i, d in enumerate(groupe):
                cible = dossier / ("_doublons" if i else "") / nom_sur(d["filename"])
                if cible.suffix.lower() not in (".md", ".markdown"):
                    cible = cible.with_name(cible.name + ".md")
                cible.parent.mkdir(exist_ok=True)
                cible.write_text(d["content"], encoding="utf-8")
                note = f"  (doublon plus ancien de « {groupe[0]['filename']} »)" if i else ""
                print(f"    {cible.relative_to(dossier)}{note}")
                total += not i
    print(f"{total} fichier(s) prêts dans {sortie}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
