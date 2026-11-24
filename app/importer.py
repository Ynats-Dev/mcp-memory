"""Import de fichiers Markdown (ex. contexte d'un projet claude.ai) dans le contexte.

    python -m app.importer --categorie home /import            # import
    python -m app.importer --categorie home /import --essai    # aperçu, rien n'est écrit

Nom du document : nom de fichier sans extension, sans suffixe « _HHMM » ajouté par
claude.ai, en minuscules avec tirets. « serveur nas_0145.md » → « maison/serveur-nas ».
Un document existant est mis à jour (nouvelle version, l'ancienne reste dans l'historique).
"""

from __future__ import annotations

import argparse
import re
import sys
import unicodedata
from pathlib import Path

from .fichier_projet import categorie_valide
from .store import ErreurContexte, Store


def nom_document(fichier: Path) -> str:
    nom = re.sub(r"[_ -]\d{4}$", "", fichier.stem.strip())
    nom = unicodedata.normalize("NFKD", nom).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", nom).strip("-")[:64]


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("sources", nargs="+", type=Path, help="fichiers .md ou dossiers")
    p.add_argument("--categorie", required=True)
    p.add_argument("--auteur", default="import")
    p.add_argument("--essai", action="store_true", help="afficher sans rien écrire")
    a = p.parse_args()
    categorie = categorie_valide(a.categorie)

    fichiers = []
    for s in a.sources:
        fichiers += sorted(s.glob("*.md")) if s.is_dir() else [s]
    if not fichiers:
        print("Aucun fichier .md trouvé.")
        return 1

    cibles: dict[str, list[Path]] = {}
    for f in fichiers:
        cibles.setdefault(f"{categorie}/{nom_document(f)}", []).append(f)
    doublons = {c: fs for c, fs in cibles.items() if len(fs) > 1}
    if doublons:
        print("Plusieurs fichiers donnent le même document ; n'en garder qu'un :")
        for c, fs in doublons.items():
            print(f"  {c} ← " + " | ".join(f.name for f in fs))
        return 1

    store = Store()
    for chemin, (f,) in sorted(cibles.items()):
        contenu = f.read_text(encoding="utf-8").replace("\r\n", "\n")
        etat = "mise à jour" if store.existe(chemin) else "création"
        print(f"{f.name:45} → {chemin:35} ({etat}, {len(contenu) // 1024} ko)")
        if not a.essai:
            store.ecrire(chemin, contenu, tags=[categorie], auteur=a.auteur,
                         message=f"Import de « {f.name} »")
    print("Aperçu seulement : relancer sans --essai pour importer." if a.essai else f"{len(cibles)} document(s) importé(s).")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ErreurContexte, ValueError) as e:
        print(f"Erreur : {e}")
        sys.exit(1)
