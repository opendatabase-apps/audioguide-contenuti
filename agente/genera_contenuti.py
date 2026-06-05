#!/usr/bin/env python3
"""
Agente generatore di audioguide — versione GitHub Actions.

Legge i file *_master.json, genera le destinazioni "da_fare" non ancora
presenti in parchi.json, aggiorna parchi.json con versione incrementata
e aggiorna lo status nei master.

Uso locale:
    python agente/genera_contenuti.py
    python agente/genera_contenuti.py --master cina_master.json
    python agente/genera_contenuti.py --master cina_master.json --priorita 1
    python agente/genera_contenuti.py --dry-run   # mostra cosa farebbe senza generare

In GitHub Actions viene chiamato dal workflow con i parametri passati come input.

Richiede: ANTHROPIC_API_KEY nella env / GitHub secrets.
"""

import anthropic, json, sys, re, os, time
from datetime import datetime
from pathlib import Path

MODEL     = "claude-haiku-4-5-20251001"
BASE_DIR  = Path(__file__).parent.parent   # root del repo contenuti
PARCHI_JSON = BASE_DIR / "parchi.json"

# Tutti i master file presenti nel repo
MASTER_FILES = [
    BASE_DIR / "parchi_master.json",
    BASE_DIR / "cina_master.json",
    # aggiungere qui: europa_master.json, giappone_master.json, ecc.
]

TIPI_POI = ["viewpoint","trail","oasis","water","culture","geology",
            "photo","info","temple","palace","market","museum"]

SCHEMA = """{
  "id":"snake_case_id","nome":"Nome","sottotitolo":"Tipo - Paese/Città",
  "paese":"Italia","area":"lombardia","icona":"emoji",
  "colore":"#C8102E","coloreSfondo":"#FFF5F5","gratuito":false,"prezzo":4.99,
  "stripeLink":"https://buy.stripe.com/placeholder",
  "pois":[{
    "id":"poi_snake","order":1,"tours":["short","full","extended"],
    "name":"Nome POI","subtitle":"Tappa 1 - descrizione","icon":"emoji",
    "color":"#FFF0E0","lat":45.4,"lng":9.1,"type":"viewpoint",
    "difficulty":null,"duration":null,"distance":null,"altitude":null,
    "text":"Par1 narrativo italiano min 80 parole.\\n\\nPar2 storico min 80 parole.\\n\\nPar3 consigli min 80 parole.",
    "tips":["emoji consiglio 1","emoji consiglio 2","emoji consiglio 3"]
  }]
}"""


# ─── Lettura / scrittura parchi.json ──────────────────────────────────────────

def leggi_parchi_json():
    if not PARCHI_JSON.exists():
        print("[WARN] parchi.json non trovato — parto da zero")
        return {"versione": "1.0.0", "parchi": []}
    with open(PARCHI_JSON, encoding="utf-8") as f:
        return json.load(f)


def salva_parchi_json(dati):
    with open(PARCHI_JSON, "w", encoding="utf-8") as f:
        json.dump(dati, f, ensure_ascii=False, indent=2)
    print(f"[OK] parchi.json salvato — versione {dati['versione']} — {len(dati['parchi'])} destinazioni")


def bump_versione(versione_str):
    """1.0.5 -> 1.0.6"""
    parti = versione_str.split(".")
    try:
        parti[-1] = str(int(parti[-1]) + 1)
    except (ValueError, IndexError):
        parti = ["1", "0", "1"]
    return ".".join(parti)


def ids_presenti(dati):
    return {p["id"] for p in dati.get("parchi", [])}


# ─── Agent Claude ──────────────────────────────────────────────────────────────

def cerca_informazioni(client, nome):
    print(f"  [SEARCH] {nome}")
    prompt = (
        f'Informazioni turistiche dettagliate su "{nome}": posizione, storia, '
        "8-10 punti di interesse con coordinate GPS lat/lng, orari, prezzi, consigli per turisti italiani."
    )
    r = client.messages.create(
        model=MODEL, max_tokens=1500,
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        messages=[{"role": "user", "content": prompt}]
    )
    testo = "".join(b.text for b in r.content if b.type == "text")[:4000]
    print(f"  [SEARCH OK] {len(testo)} caratteri")
    return testo


def pulisci_json(testo):
    testo = re.sub(r"^```json\s*|^```\s*|\s*```$", "", testo).strip()
    if not testo.endswith("}"):
        ultimo = testo.rfind('"}')
        if ultimo > 0:
            testo = testo[:ultimo + 2]
        testo += "]" * max(0, testo.count("[") - testo.count("]"))
        testo += "}" * max(0, testo.count("{") - testo.count("}"))
    return testo


def genera_json(client, nome, info, paese=""):
    print(f"  [GEN] Generazione JSON...")
    ctx = f" La destinazione e' in {paese}." if paese else ""
    prompt = (
        f'Crea JSON audioguida italiana per "{nome}".{ctx}\n'
        f"Schema:\n{SCHEMA}\n\n"
        "Regole: testo TUTTO italiano, SOLO 6 POI max, coordinate GPS reali, "
        "3 paragrafi per 'text' (sep \\n\\n, min 80 parole ciascuno), "
        "type in: viewpoint/trail/culture/geology/photo/oasis/water/info/temple/palace/market/museum, "
        "tours: short/full/extended (attrazioni) oppure half/full/extended (parchi naturali), "
        "colori coerenti col luogo. Rispondi SOLO JSON valido, zero markdown.\n\n"
        f"Dati:\n{info}"
    )
    r = client.messages.create(
        model=MODEL, max_tokens=6000,
        messages=[{"role": "user", "content": prompt}]
    )
    testo = pulisci_json(r.content[0].text.strip())
    print(f"  [GEN] stop={r.stop_reason} len={len(testo)}")
    try:
        dati = json.loads(testo)
        print(f"  [GEN OK] {len(dati.get('pois', []))} POI")
        return dati
    except json.JSONDecodeError as e:
        print(f"  [GEN ERR] JSON non valido: {e}")
        raise


def valida(att, paese_default=""):
    out = {
        "id":           att.get("id", "nuova_attrazione"),
        "nome":         att.get("nome", "Nuova Attrazione"),
        "sottotitolo":  att.get("sottotitolo", "Attrazione mondiale"),
        "paese":        att.get("paese", paese_default),
        "area":         att.get("area", ""),
        "icona":        att.get("icona", "🗺️"),
        "colore":       att.get("colore", "#3B6D11"),
        "coloreSfondo": att.get("coloreSfondo", "#EAF3DE"),
        "gratuito":     att.get("gratuito", False),
        "prezzo":       att.get("prezzo", 4.99),
        "stripeLink":   att.get("stripeLink", "https://buy.stripe.com/placeholder"),
        "pois": []
    }
    for i, p in enumerate(att.get("pois", [])):
        t = p.get("type", "viewpoint")
        if t not in TIPI_POI:
            t = "viewpoint"
        poi = {
            "id":         p.get("id", f"poi_{i+1}"),
            "order":      p.get("order", i + 1),
            "tours":      p.get("tours", ["full"]),
            "name":       p.get("name", f"Tappa {i+1}"),
            "subtitle":   p.get("subtitle", ""),
            "icon":       p.get("icon", "📍"),
            "color":      p.get("color", "#F5F5F5"),
            "lat":        float(p.get("lat", 0)),
            "lng":        float(p.get("lng", 0)),
            "type":       t,
            "difficulty": p.get("difficulty"),
            "duration":   p.get("duration"),
            "distance":   p.get("distance"),
            "altitude":   p.get("altitude"),
            "text":       p.get("text", ""),
            "tips":       p.get("tips", [])
        }
        if poi["lat"] == 0:
            print(f"  [WARN] Coordinate mancanti: {poi['name']}")
        out["pois"].append(poi)
    return out


def pausa(secondi=65):
    print(f"  [WAIT] {secondi}s rate limit...")
    for i in range(secondi, 0, -10):
        print(f"  [WAIT] {i}s...", flush=True)
        time.sleep(10)
    print("  [WAIT] OK")


def aggiorna_status_master(master_path, att_id, nuovo_status="completato"):
    with open(master_path, encoding="utf-8") as f:
        master = json.load(f)
    chiave = "parchi" if "parchi" in master else "destinazioni"
    for d in master.get(chiave, []):
        if d.get("id") == att_id:
            d["status"] = nuovo_status
            break
    with open(master_path, "w", encoding="utf-8") as f:
        json.dump(master, f, ensure_ascii=False, indent=2)
    print(f"  [MASTER] {att_id} -> {nuovo_status}")


# ─── Pipeline principale ───────────────────────────────────────────────────────

def carica_da_fare(master_path, filtro_priorita=None):
    """Restituisce lista di destinazioni da_fare dal master."""
    if not master_path.exists():
        return []
    with open(master_path, encoding="utf-8") as f:
        master = json.load(f)
    chiave = "parchi" if "parchi" in master else "destinazioni"
    dest = [d for d in master.get(chiave, []) if d.get("status") == "da_fare"]
    if filtro_priorita is not None:
        dest = [d for d in dest if d.get("priorita") == filtro_priorita]
    return dest, master.get("paese", "")


def run(master_filter=None, filtro_priorita=None, dry_run=False):
    dati_json = leggi_parchi_json()
    presenti   = ids_presenti(dati_json)
    client     = anthropic.Anthropic() if not dry_run else None

    # Seleziona master da processare
    masters = MASTER_FILES
    if master_filter:
        masters = [BASE_DIR / master_filter]

    totale_generati = 0
    totale_saltati  = 0

    for master_path in masters:
        if not master_path.exists():
            print(f"[SKIP] {master_path.name} non trovato")
            continue

        da_fare, paese_master = carica_da_fare(master_path, filtro_priorita)
        print(f"\n[MASTER] {master_path.name} — {len(da_fare)} da fare" +
              (f" (priorita={filtro_priorita})" if filtro_priorita else ""))

        for i, dest in enumerate(da_fare, 1):
            att_id   = dest.get("id")
            nome     = dest.get("nome_completo", dest.get("nome", "?"))
            paese    = dest.get("paese", paese_master)

            print(f"\n[{i}/{len(da_fare)}] {nome} (id={att_id})")

            # Gia' presente -> aggiorna solo status
            if att_id in presenti:
                print(f"  [SKIP] Gia' in parchi.json")
                aggiorna_status_master(master_path, att_id, "completato")
                totale_saltati += 1
                continue

            if dry_run:
                print(f"  [DRY-RUN] Verrebbe generato")
                continue

            try:
                info  = cerca_informazioni(client, nome)
                pausa()
                att   = valida(genera_json(client, nome, info, paese), paese)

                # Assicura che l'id corrisponda a quello del master
                att["id"] = att_id

                dati_json["parchi"].append(att)
                presenti.add(att_id)
                aggiorna_status_master(master_path, att_id, "completato")
                totale_generati += 1

                # Salva dopo ogni generazione (sicurezza se il workflow si interrompe)
                dati_json["versione"] = bump_versione(dati_json["versione"])
                salva_parchi_json(dati_json)

            except Exception as e:
                print(f"  [ERR] {e}")
                continue

            # Pausa extra tra destinazioni successive
            if i < len(da_fare):
                pausa(70)

    print(f"\n{'='*50}")
    print(f"[DONE] Generati: {totale_generati} | Saltati: {totale_saltati}")
    print(f"[DONE] parchi.json versione: {dati_json['versione']} | totale: {len(dati_json['parchi'])}")
    print(f"{'='*50}")


# ─── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    args = sys.argv[1:]
    master_filter   = None
    filtro_priorita = None
    dry_run         = "--dry-run" in args

    for i, a in enumerate(args):
        if a == "--master" and i + 1 < len(args):
            master_filter = args[i + 1]
        if a == "--priorita" and i + 1 < len(args):
            try:
                filtro_priorita = int(args[i + 1])
            except ValueError:
                pass

    run(master_filter, filtro_priorita, dry_run)
