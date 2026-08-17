#!/usr/bin/env python3
"""
Agente generatore di audioguide — versione GitHub Actions.

Legge i file *_master.json, genera le destinazioni "da_fare" e "da_rifare"
non ancora presenti o da rigenerare in parchi.json.

Modelli per tier:
  opus   → claude-opus-5      (destinazioni flagship)
  sonnet → claude-sonnet-5    (destinazioni molto importanti)
  haiku  → claude-haiku-4-5-20251001  (tutte le altre)

Status:
  da_fare    → non ancora in parchi.json, da generare
  da_rifare  → già in parchi.json ma generata con modello inferiore, da sostituire
  completato → pronta, non toccare

Uso locale:
    python agente/genera_contenuti.py
    python agente/genera_contenuti.py --master parchi_master.json
    python agente/genera_contenuti.py --master parchi_master.json --priorita 1
    python agente/genera_contenuti.py --modello opus   # solo destinazioni opus
    python agente/genera_contenuti.py --dry-run

Richiede: ANTHROPIC_API_KEY nella env / GitHub secrets.
"""

import anthropic, json, sys, re, os, time
from datetime import datetime
from pathlib import Path

MODEL_MAP = {
    "opus":   "claude-opus-5",
    "sonnet": "claude-sonnet-5",
    "haiku":  "claude-haiku-4-5-20251001",
}
DEFAULT_MODEL = "haiku"

BASE_DIR    = Path(__file__).parent.parent
PARCHI_JSON = BASE_DIR / "parchi.json"

MASTER_FILES = [
    BASE_DIR / "parchi_master.json",
    # cina_master.json escluso — verrà ripreso in futuro
]

TIPI_POI = ["viewpoint","trail","oasis","water","culture","geology",
            "photo","info","temple","palace","market","museum","restaurant","food"]

SCHEMA = """{
  "id":"snake_case_id","nome":"Nome","sottotitolo":"Tipo - Paese/Citta",
  "paese":"USA","area":"southwest","icona":"emoji",
  "colore":"#C8102E","coloreSfondo":"#FFF5F5","gratuito":false,"prezzo":4.99,
  "stripeLink":"https://buy.stripe.com/placeholder",
  "pois":[
    {
      "id":"poi_principale","order":1,"tours":["short","full","extended"],
      "name":"Nome Punto di Interesse","subtitle":"Tappa 1 - descrizione breve","icon":"emoji",
      "color":"#FFF0E0","lat":39.9,"lng":116.4,"type":"viewpoint",
      "difficulty":null,"duration":null,"distance":null,"altitude":null,
      "text":"Par1 narrativo italiano min 80 parole.\\n\\nPar2 storico/culturale min 80 parole.\\n\\nPar3 consigli pratici min 80 parole.",
      "tips":["emoji consiglio 1","emoji consiglio 2","emoji consiglio 3"]
    },
    {
      "id":"dove_mangiare","order":6,"tours":["full","extended"],
      "name":"Dove mangiare","subtitle":"Cucina locale e ristoranti consigliati","icon":"🍜",
      "color":"#FFF8E8","lat":39.9,"lng":116.4,"type":"restaurant",
      "difficulty":null,"duration":"1-2h","distance":null,"altitude":null,
      "text":"Par1: descrizione della cucina locale e piatti tipici, min 80 parole.\\n\\nPar2: 2-3 ristoranti specifici con nome reale, specialita e fascia di prezzo, min 80 parole.\\n\\nPar3: consigli pratici su orari, etichetta, zone dove mangiare, min 80 parole.",
      "tips":["🍜 Piatto tipico da non perdere","💰 Budget medio per pasto","⏰ Orario migliore","📍 Zona migliore","🗣️ Parola utile in lingua locale"]
    }
  ]
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
    parti = versione_str.split(".")
    try:
        parti[-1] = str(int(parti[-1]) + 1)
    except (ValueError, IndexError):
        parti = ["1", "0", "1"]
    return ".".join(parti)


def ids_presenti(dati):
    return {p["id"] for p in dati.get("parchi", [])}


# ─── Agent Claude ──────────────────────────────────────────────────────────────

def cerca_informazioni(client, nome, model_id):
    print(f"  [SEARCH] {nome} ({model_id})")
    prompt = (
        f'Informazioni turistiche complete su "{nome}" per turisti italiani:\n'
        "1. Posizione geografica, come arrivare, trasporti\n"
        "2. Storia, cultura, significato del luogo\n"
        "3. 8-10 punti di interesse principali con coordinate GPS lat/lng precise\n"
        "4. Orari di apertura, prezzi, prenotazioni necessarie\n"
        "5. RISTORANTI E CIBO LOCALE: 3-5 ristoranti consigliati con nome, specialità, "
        "fascia di prezzo, indirizzo o zona. Specialità gastronomiche locali da non perdere.\n"
        "6. ALLOGGI: tipologie di alloggio consigliate nella zona\n"
        "7. Consigli pratici: periodo migliore, abbigliamento, sicurezza, fuso orario, valuta\n"
        "8. Itinerari: tour breve (2-3h), completo (mezza giornata), esteso (giornata intera)"
    )
    r = client.messages.create(
        model=model_id, max_tokens=2000,
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        messages=[{"role": "user", "content": prompt}]
    )
    testo = "".join(b.text for b in r.content if b.type == "text")[:5000]
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


def genera_json(client, nome, info, paese, model_id):
    print(f"  [GEN] Generazione JSON con {model_id}...")
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
        model=model_id, max_tokens=6000,
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

def carica_da_processare(master_path, filtro_priorita=None, filtro_modello=None):
    """Restituisce destinazioni da_fare e da_rifare dal master."""
    if not master_path.exists():
        return [], ""
    with open(master_path, encoding="utf-8") as f:
        master = json.load(f)
    chiave = "parchi" if "parchi" in master else "destinazioni"
    dest = [d for d in master.get(chiave, []) if d.get("status") in ("da_fare", "da_rifare")]
    if filtro_priorita is not None:
        dest = [d for d in dest if d.get("priorita") == filtro_priorita]
    if filtro_modello is not None:
        dest = [d for d in dest if d.get("modello", DEFAULT_MODEL) == filtro_modello]
    return dest, master.get("paese", "")


def run(master_filter=None, filtro_priorita=None, filtro_modello=None, dry_run=False):
    dati_json = leggi_parchi_json()
    presenti   = ids_presenti(dati_json)
    client     = anthropic.Anthropic() if not dry_run else None

    masters = MASTER_FILES
    if master_filter:
        masters = [BASE_DIR / master_filter]

    totale_generati = 0
    totale_sostituiti = 0
    totale_saltati  = 0

    for master_path in masters:
        if not master_path.exists():
            print(f"[SKIP] {master_path.name} non trovato")
            continue

        da_fare, paese_master = carica_da_processare(master_path, filtro_priorita, filtro_modello)
        print(f"\n[MASTER] {master_path.name} — {len(da_fare)} da processare" +
              (f" (priorita={filtro_priorita})" if filtro_priorita else "") +
              (f" (modello={filtro_modello})" if filtro_modello else ""))

        for i, dest in enumerate(da_fare, 1):
            att_id    = dest.get("id")
            nome      = dest.get("nome_completo", dest.get("nome", "?"))
            paese     = dest.get("paese", paese_master)
            status    = dest.get("status", "da_fare")
            tier      = dest.get("modello", DEFAULT_MODEL)
            model_id  = MODEL_MAP.get(tier, MODEL_MAP[DEFAULT_MODEL])
            is_rifare = status == "da_rifare"

            print(f"\n[{i}/{len(da_fare)}] {nome} (id={att_id}, modello={tier}, status={status})")

            if dry_run:
                azione = "Sostituirebbe" if is_rifare else "Genererebbe"
                print(f"  [DRY-RUN] {azione} con {model_id}")
                continue

            try:
                info = cerca_informazioni(client, nome, model_id)
                pausa()
                att  = valida(genera_json(client, nome, info, paese, model_id), paese)
                att["id"] = att_id

                if is_rifare and att_id in presenti:
                    # Sostituisce la versione esistente
                    dati_json["parchi"] = [p if p["id"] != att_id else att
                                           for p in dati_json["parchi"]]
                    totale_sostituiti += 1
                    print(f"  [REPLACE] {att_id} sostituito")
                else:
                    dati_json["parchi"].append(att)
                    presenti.add(att_id)
                    totale_generati += 1

                aggiorna_status_master(master_path, att_id, "completato")
                dati_json["versione"] = bump_versione(dati_json["versione"])
                salva_parchi_json(dati_json)

            except Exception as e:
                print(f"  [ERR] {e}")
                continue

            if i < len(da_fare):
                pausa(70)

    print(f"\n{'='*50}")
    print(f"[DONE] Nuovi: {totale_generati} | Sostituiti: {totale_sostituiti} | Saltati: {totale_saltati}")
    print(f"[DONE] parchi.json versione: {dati_json['versione']} | totale: {len(dati_json['parchi'])}")
    print(f"{'='*50}")


# ─── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    args = sys.argv[1:]
    master_filter   = None
    filtro_priorita = None
    filtro_modello  = None
    dry_run         = "--dry-run" in args

    for i, a in enumerate(args):
        if a == "--master" and i + 1 < len(args):
            master_filter = args[i + 1]
        if a == "--priorita" and i + 1 < len(args):
            try:
                filtro_priorita = int(args[i + 1])
            except ValueError:
                pass
        if a == "--modello" and i + 1 < len(args):
            filtro_modello = args[i + 1]

    run(master_filter, filtro_priorita, filtro_modello, dry_run)
