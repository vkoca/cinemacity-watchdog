#!/usr/bin/env python3
"""Hlídá rozpis Cinema City a hlásí nově vypsaná představení.

Ve výchozím nastavení: film "Odyssea" v sále, jehož název obsahuje "IMAX".
Data bere z veřejného JSON API cinemacity.cz (bez klíče, bez přihlášení).

Stav (už viděná představení) drží v JSON souboru, takže při každém běhu
hlásí jen to, co přibylo od minule.
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

SITE_ID = "10101"  # cinemacity.cz
BASE = f"https://www.cinemacity.cz/cz/data-api-service/v1/quickbook/{SITE_ID}"
TICKETS_BASE = "https://tickets.cinemacity.cz/api"
LANG = "cs_CZ"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

FILM_PATTERN = os.environ.get("FILM_PATTERN", "odyss").lower()
AUDITORIUM_PATTERN = os.environ.get("AUDITORIUM_PATTERN", "imax").lower()
HORIZON_DAYS = int(os.environ.get("HORIZON_DAYS", "180"))
# Atribut, podle kterého API umí filtrovat kina — levná nápověda, kde hledat
# IMAX sály. Doplňuje (nenahrazuje) sondu podle názvu sálu.
HINT_ATTR = os.environ.get("HINT_ATTR", "70-mm")
DELAY = float(os.environ.get("REQUEST_DELAY", "0.25"))

# soldOut z quickbook API počítá i vozíčkářská/doprovodná místa jako
# "obsazeno" i "volno" nekonzistentně — u téměř vyprodaných představení proto
# soldOut umí být False, i když fakticky zbývá jen pár míst pro vozíčkáře.
# Přesná obsazenost po sedadlech (tickets.cinemacity.cz/api/seats/seats-statusV2)
# je za Cloudflare ochranou, která blokuje skriptované volání (ověřeno: curl
# i fetch/XHR z reálné browser session dostanou 403, projde jen navigace
# živým prohlížečem) — nedá se tedy spolehlivě strojově číst. Místo přesných
# sedadel proto jen odhad: kolik míst reálně zbývá (availabilityRatio × počet
# sedadel v sále, obojí veřejné a nechráněné) a heuristika, že hrstka
# zbývajících míst je nejspíš ta vozíčkářská.
AVAILABILITY_CHECK = os.environ.get("AVAILABILITY_CHECK", "1") != "0"
MIN_FREE_SEATS = int(os.environ.get("MIN_FREE_SEATS", "6"))
# Většina nově vypsaných termínů má od prvního zveřejnění obsazenou naprostou
# většinu míst (předprodej pro predplatitele apod.) — na tyhle si stejně
# vstupenky nekoupíš, takže nemá smysl kvůli nim chodit e-mail. Hlásí se proto
# jen nové termíny, kde odhad volných míst dosahuje aspoň tohohle prahu.
MIN_REPORT_FREE_SEATS = int(os.environ.get("MIN_REPORT_FREE_SEATS", "10"))

_capacity_cache = {}

CZ_DAYS = ["po", "út", "st", "čt", "pá", "so", "ne"]

# API vrací eventDateTime bez zóny, v místním čase kina. Runner v GitHub
# Actions jede v UTC, takže by se čas představení porovnával s časem o dvě
# hodiny pozadu — projekce, která právě doběhla, by vypadala jako budoucí
# a při zmizení z rozpisu by se falešně nahlásila jako zrušená.
CINEMA_TZ = ZoneInfo("Europe/Prague")


def now():
    """Aktuální čas v zóně kina, bez tzinfo — porovnatelný s daty z API."""
    return datetime.now(CINEMA_TZ).replace(tzinfo=None)


def api(path):
    """GET na data-api-service; vrací obsah klíče "body"."""
    url = f"{BASE}{path}"
    last = None
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))["body"]
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last = exc
            time.sleep(2 ** attempt)
    raise SystemExit(f"API selhalo po 4 pokusech: {url}\n{last}")


def tickets_api(path, method="GET"):
    """GET/POST na tickets.cinemacity.cz; vrací rovnou celý JSON.

    Jen pro veřejné, nechráněné endpointy (presentations, seatplanV2) —
    seats-statusV2 (skutečná obsazenost sedadel) tudy záměrně nejde, viz
    komentář u AVAILABILITY_CHECK.
    """
    url = f"{TICKETS_BASE}{path}"
    last = None
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA}, method=method)
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last = exc
            time.sleep(2 ** attempt)
    raise RuntimeError(f"tickets API selhalo po 4 pokusech: {url}\n{last}")


def seatplan_capacity(venue_id, seatplan_id):
    """Celkový počet sedadel v sále, spočítaný z veřejného plánu sálu."""
    key = (venue_id, seatplan_id)
    if key in _capacity_cache:
        return _capacity_cache[key]
    body = tickets_api(f"/seats/seatplanV2?venueId={venue_id}&seatplanId={seatplan_id}", method="POST")
    total = sum(
        len(row.get("S", {}))
        for section in body.get("S", {}).values()
        for group in section.get("G", {}).values()
        for row in group.get("R", {}).values()
    )
    _capacity_cache[key] = total
    return total


def filter_reportable(events):
    """Nechá jen termíny s odhadem volných míst >= MIN_REPORT_FREE_SEATS.

    Termín bez odhadu (enrich_availability selhalo, nebo soldOut nemělo
    availabilityRatio) se bere jako nedostatečný — radši nenahlásit nejistou
    dostupnost než zaspamovat termínem, na který stejně nejde koupit lístek.
    """
    return [e for e in events if e.get("freeSeats") is not None and e["freeSeats"] >= MIN_REPORT_FREE_SEATS]


def enrich_availability(event):
    """Doplní event o odhad volných míst a heuristiku „jen vozíčkářská místa“.

    Fail-soft: cokoliv se tu nepovede (síť, neočekávaný tvar odpovědi),
    event zůstane jen s tím, co už měl (raw soldOut z quickbook) — hlídání
    nesmí kvůli téhle doplňkové sondě spadnout.
    """
    ratio = event.get("availabilityRatio")
    if ratio is None:
        return
    try:
        time.sleep(DELAY)
        presentation = tickets_api(f"/presentations/{event['presentationCode']}?referralMiniSiteId=0")[
            "presentation"
        ]
        time.sleep(DELAY)
        capacity = seatplan_capacity(presentation["venueId"], presentation["seatplanId"])
        if not capacity:
            return
        free = round(ratio * capacity)
        event["freeSeats"] = free
        event["likelyWheelchairOnly"] = (not event["soldOut"]) and free <= MIN_FREE_SEATS
    except Exception:
        pass


def horizon():
    return (date.today() + timedelta(days=HORIZON_DAYS)).isoformat()


def fetch_cinemas():
    body = api(f"/cinemas/with-event/until/{horizon()}?attr=&lang={LANG}")
    return {c["id"]: c for c in body["cinemas"]}


def fetch_dates(cinema_id):
    time.sleep(DELAY)
    return api(f"/dates/in-cinema/{cinema_id}/until/{horizon()}?attr=&lang={LANG}")["dates"]


def fetch_day(cinema_id, day):
    time.sleep(DELAY)
    body = api(f"/film-events/in-cinema/{cinema_id}/at-date/{day}?attr=&lang={LANG}")
    films = {f["id"]: f for f in body.get("films", [])}
    return films, body.get("events", [])


def hint_cinema_ids():
    """Kina, která podle API mají představení s atributem HINT_ATTR."""
    if not HINT_ATTR:
        return set()
    body = api(f"/cinemas/with-event/until/{horizon()}?attr={HINT_ATTR}&lang={LANG}")
    return {c["id"] for c in body["cinemas"]}


def is_target_hall(event):
    return AUDITORIUM_PATTERN in (event.get("auditorium") or "").lower()


def collect():
    """Projde relevantní kina a vrátí {event_id: záznam} pro hlídaná představení.

    Aby se netahal celý rozpis všech kin, běží to dvoufázově: nejdřív se
    zjistí, která kina vůbec mají hlídaný sál (jedna sonda na kino + nápověda
    z API), a teprve ta se projdou do hloubky.
    """
    cinemas = fetch_cinemas()
    dates_by_cinema = {cid: fetch_dates(cid) for cid in cinemas}

    candidates = hint_cinema_ids() & set(cinemas)
    day_cache = {}
    for cid, days in dates_by_cinema.items():
        if not days:
            continue
        probe = days[0]
        day_cache[(cid, probe)] = fetch_day(cid, probe)
        if any(is_target_hall(e) for e in day_cache[(cid, probe)][1]):
            candidates.add(cid)

    found = {}
    for cid in sorted(candidates):
        for day in dates_by_cinema.get(cid, []):
            films, events = day_cache.get((cid, day)) or fetch_day(cid, day)
            for e in events:
                film = films.get(e["filmId"], {})
                if FILM_PATTERN not in film.get("name", "").lower():
                    continue
                if not is_target_hall(e):
                    continue
                found[e["id"]] = {
                    "id": e["id"],
                    "film": film.get("name", e["filmId"]),
                    "filmLink": film.get("link"),
                    "cinema": cinemas[cid]["displayName"],
                    "cinemaId": cid,
                    "datetime": e["eventDateTime"],
                    "auditorium": e.get("auditorium"),
                    "attrs": e.get("attributeIds", []),
                    # Žádné z polí, která API nabízí, není použitelné jako
                    # odkaz: bookingLink vrací na GET 404, obsoleteBookingUrl
                    # je i podle názvu mrtvý a bookingRouterLaunchLink vede na
                    # stránku se samoodesílacím POST formulářem, jehož cíl
                    # (tickets.rel.…) na přímý GET odpoví 403. Ten POST ale
                    # skončí na prosté adrese /order/{id}, která funguje i na
                    # GET a otevře rovnou výběr sedadel. Pozor, parametr lang
                    # tady dělá 404 — musí se vynechat.
                    "presentationCode": e.get("presentationCode") or e["id"],
                    "booking": f"https://tickets.cinemacity.cz/order/{e.get('presentationCode') or e['id']}",
                    "soldOut": bool(e.get("soldOut")),
                    "availabilityRatio": e.get("availabilityRatio"),
                }
    return found


def load_state(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return {"updated": None, "events": {}}


def save_state(path, events):
    """Zapíše stav, ale jen když se změnila množina představení.

    Kdyby se soubor přepisoval při každém běhu, měnilo by se v něm razítko
    "updated" a workflow by si po sobě commitoval prázdnou změnu 48× denně.
    Rozhoduje proto seznam ID — to je přesně to, na čem stojí hlášení.
    Volatilní pole (soldOut) se tím pádem neaktualizují; drží se hodnota
    z chvíle, kdy se představení objevilo poprvé, což je i to, co se hlásí.
    """
    if set(events) == set(load_state(path).get("events", {})):
        return False
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    payload = {
        "updated": now().replace(microsecond=0).isoformat(),
        "events": dict(sorted(events.items(), key=lambda kv: kv[1]["datetime"])),
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1, sort_keys=False)
        fh.write("\n")
    return True


def prune_past(events):
    """Zahodí ze stavu představení, která už proběhla — ať soubor neroste."""
    cutoff = (now() - timedelta(days=1)).isoformat()
    return {k: v for k, v in events.items() if v["datetime"] >= cutoff}


def fmt_dt(iso):
    dt = datetime.fromisoformat(iso)
    return f"{CZ_DAYS[dt.weekday()]} {dt.day}. {dt.month}. {dt.year} v {dt:%H:%M}"


def fmt_short(iso):
    dt = datetime.fromisoformat(iso)
    return f"{dt.day}. {dt.month}."


def render(new_events, gone_events):
    """Markdown tělo hlášení."""
    lines = []
    if new_events:
        lines.append(f"### Nově vypsáno ({len(new_events)})\n")
        for cinema, group in group_by_cinema(new_events):
            lines.append(f"**{cinema}**\n")
            for e in group:
                flags = []
                if "70-mm" in e["attrs"]:
                    flags.append("70mm")
                if "subbed" in e["attrs"]:
                    flags.append("titulky")
                if "dubbed" in e["attrs"]:
                    flags.append("dabing")
                if e["soldOut"]:
                    flags.append("**vyprodáno**")
                elif e.get("likelyWheelchairOnly"):
                    flags.append(f"**vyprodáno** (odhadem zbývá jen {e['freeSeats']} míst — nejspíš vozíčkářská)")
                elif "freeSeats" in e:
                    flags.append(f"~{e['freeSeats']} volných")
                suffix = f" — {', '.join(flags)}" if flags else ""
                link = f" — [koupit]({e['booking']})" if e["booking"] else ""
                lines.append(f"- {fmt_dt(e['datetime'])} · {e['auditorium']}{suffix}{link}")
            lines.append("")
    if gone_events:
        lines.append(f"### Zmizelo z rozpisu ({len(gone_events)})\n")
        for cinema, group in group_by_cinema(gone_events):
            lines.append(f"**{cinema}**\n")
            for e in group:
                lines.append(f"- {fmt_dt(e['datetime'])} · {e['auditorium']}")
            lines.append("")
    film_link = next(
        (e["filmLink"] for e in list(new_events) + list(gone_events) if e.get("filmLink")),
        None,
    )
    if film_link:
        lines.append(f"[Stránka filmu na Cinema City]({film_link})")
    lines.append("")
    lines.append(
        f"<sub>Zkontrolováno {now():%d. %m. %Y %H:%M} · "
        f"film ~ `{FILM_PATTERN}` · sál ~ `{AUDITORIUM_PATTERN}`</sub>"
    )
    return "\n".join(lines)


def group_by_cinema(events):
    order = {}
    for e in sorted(events, key=lambda x: (x["cinema"], x["datetime"])):
        order.setdefault(e["cinema"], []).append(e)
    return order.items()


def title_for(new_events):
    film = new_events[0]["film"]
    days = sorted({e["datetime"][:10] for e in new_events})
    span = fmt_short(days[0])
    if len(days) > 1:
        span += f"–{fmt_short(days[-1])}"
    n = len(new_events)
    word = "nový termín" if n == 1 else ("nové termíny" if n < 5 else "nových termínů")
    return f"🎬 {film} v IMAXu: {n} {word} ({span})"


def gh_output(**kwargs):
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as fh:
        for key, value in kwargs.items():
            fh.write(f"{key}={value}\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--state", default="state/seen.json", help="soubor se stavem")
    ap.add_argument("--seed", action="store_true", help="jen ulož stav, nic nehlas")
    ap.add_argument("--force-report", action="store_true", help="nahlas vše, i známé")
    ap.add_argument("--report", default="report.md", help="kam zapsat markdown hlášení")
    ap.add_argument("--title", default="title.txt", help="kam zapsat titulek issue")
    args = ap.parse_args()

    current = collect()
    state = load_state(args.state)
    known = state.get("events", {})

    print(f"Nalezeno {len(current)} hlídaných představení, ve stavu {len(known)}.")

    if args.seed:
        save_state(args.state, prune_past(current))
        print(f"Stav zapsán do {args.state} (seed, nic se nehlásí).")
        gh_output(has_news="false")
        return

    if args.force_report:
        new_events = sorted(current.values(), key=lambda e: e["datetime"])
        gone = []
    else:
        new_events = sorted(
            (v for k, v in current.items() if k not in known),
            key=lambda e: e["datetime"],
        )
        future = now().isoformat()
        gone = sorted(
            (v for k, v in known.items() if k not in current and v["datetime"] > future),
            key=lambda e: e["datetime"],
        )

    save_state(args.state, prune_past(current))

    if AVAILABILITY_CHECK:
        for e in new_events:
            enrich_availability(e)
        if not args.force_report:
            reportable = filter_reportable(new_events)
            skipped = len(new_events) - len(reportable)
            if skipped:
                print(f"Přeskočeno {skipped} nových termínů s odhadem < {MIN_REPORT_FREE_SEATS} volných míst.")
            new_events = reportable

    if not new_events and not gone:
        print("Nic nového.")
        gh_output(has_news="false")
        return

    body = render(new_events, gone)
    title = title_for(new_events) if new_events else "🎬 Odyssea v IMAXu: zrušené termíny"
    with open(args.report, "w", encoding="utf-8") as fh:
        fh.write(body + "\n")
    with open(args.title, "w", encoding="utf-8") as fh:
        fh.write(title + "\n")

    print(f"\n{title}\n")
    print(body)
    gh_output(has_news="true")


if __name__ == "__main__":
    sys.exit(main())
