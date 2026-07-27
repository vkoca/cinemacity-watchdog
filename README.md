# cinemacity-watchdog

Hlídá rozpis [Cinema City](https://www.cinemacity.cz) a když přibude nový termín
**Odyssei v IMAXu**, založí v tomhle repu issue a **přiřadí ho vlastníkovi repa**.
GitHub z něj pošle e-mail i push do mobilní appky.

Na přiřazení záleží: e-mail chodí ve výchozím nastavení jen u „Participating"
notifikací (přiřazení, zmínky, odpovědi). Pouhé sledování repa („Watching")
dává jen web/mobile notifikaci — e-mail je pro něj v Settings → Notifications
vypnutý, dokud si ho člověk nezapne.

Běží v GitHub Actions, takže funguje i když je Mac vypnutý.

## Jak to funguje

- Workflow [`.github/workflows/watch.yml`](.github/workflows/watch.yml) běží
  **každou půlhodinu** (v :13 a :43 — mimo špičky, kdy GitHub cron nejvíc
  zahazuje běhy). Repo je veřejné, takže minuty Actions jsou zdarma bez limitu.
- [`watch.py`](watch.py) stáhne rozpis z veřejného JSON API cinemacity.cz
  (`/cz/data-api-service/v1/quickbook/10101/…`) — bez klíče, bez přihlášení.
- Seznam už viděných představení drží v [`state/seen.json`](state/seen.json),
  který si workflow po každém běhu commitne zpátky. Hlásí se tedy jen přírůstky.
- Nová představení → issue s časem, sálem, příznaky (70mm / titulky / vyprodáno)
  a přímým odkazem na nákup vstupenky. Hlásí se i termíny, které z rozpisu
  **zmizely** (zrušené projekce).
- `soldOut` z quickbook API počítá i vozíčkářská/doprovodná místa nekonzistentně
  — u téměř vyprodaného představení tak umí ukazovat "volno", i když fakticky
  zbývá jen pár míst pro vozíčkáře. Watchdog proto k nově nahlášeným
  představením dopočítává odhad počtu volných míst (`availabilityRatio` ×
  kapacita sálu z veřejného plánu sálu) a pokud jich zbývá jen hrstka, označí
  je jako **vyprodáno (odhadem)** místo skutečně volných — viz sekce
  [Odhad dostupnosti](#odhad-dostupnosti-místo-přesných-sedadel) níže.
- Issue se **hned po založení zavírá**. Slouží jen jako doručovací kanál pro
  e-mail, který GitHub pošle už při jeho vzniku — seznam otevřených issues tak
  zůstává prázdný a nic není potřeba uklízet ručně. Obsah zůstává čitelný mezi
  zavřenými.
- Časy se počítají v zóně kina (`Europe/Prague`), ne v UTC runneru. Bez toho
  by projekce, která právě doběhla, vypadala jako budoucí a při zmizení
  z rozpisu by se falešně nahlásila jako zrušená.

Jeden běh je ~45 HTTP dotazů a trvá ~20 sekund.

## Co přesně se hlídá

Představení, kde **název filmu** obsahuje `odyss` **a** **název sálu** obsahuje
`imax`. Aktuálně tomu odpovídá jediné kino v ČR — **Praha Flora**, sál
`IMAX VOLVO`, kde Odyssea běží v 70mm s titulky.

Aby se netahal celý rozpis všech třinácti kin, hledá se dvoufázově: nejdřív se
zjistí, která kina vůbec mají IMAX sál (jedna sonda na nejbližší hrací den plus
nápověda z API přes atribut `70-mm`), a do hloubky se projdou jen ta. Kdyby
IMAX přibyl v jiném kině, chytí se to samo.

Chování jde změnit proměnnými prostředí ve workflow:

| Proměnná | Výchozí | Význam |
| --- | --- | --- |
| `FILM_PATTERN` | `odyss` | podřetězec názvu filmu (case-insensitive) |
| `AUDITORIUM_PATTERN` | `imax` | podřetězec názvu sálu |
| `HORIZON_DAYS` | `180` | jak daleko dopředu se ptát |
| `HINT_ATTR` | `70-mm` | atribut pro levné dohledání kandidátských kin |
| `REQUEST_DELAY` | `0.25` | pauza mezi dotazy na API (s) |
| `AVAILABILITY_CHECK` | `1` | `0` vypne odhad volných míst, hlásí se jen syrové `soldOut` |
| `MIN_FREE_SEATS` | `6` | odhadovaný počet volných míst, pod kterým se termín označí jako vyprodaný (vozíčkářská místa) |
| `MIN_REPORT_FREE_SEATS` | `10` | nový termín se nahlásí (e-mail) jen když odhad volných míst dosáhne aspoň tohohle čísla |

Hlídat cokoli jiného (třeba `FILM_PATTERN=dune`, `AUDITORIUM_PATTERN=4dx`) tedy
znamená přepsat dvě proměnné a smazat `state/seen.json`.

## Odhad dostupnosti místo přesných sedadel

Skutečnou obsazenost po jednotlivých sedadlech (a tedy i to, jestli jsou dvě
volná místa vedle sebe) nabízí `tickets.cinemacity.cz/api/seats/seats-statusV2`.
Ten je ale za Cloudflare ochranou, která **cíleně blokuje skriptované volání**
na tenhle konkrétní endpoint — ověřeno při vývoji:

- prostý `curl` bez session dostane `403` s prázdným tělem,
- i `fetch`/`XMLHttpRequest` spuštěný v konzoli **uvnitř reálné, přihlášené
  browser session** (stejné cookies, těsně po úspěšném načtení stránky) dostane
  `403`,
- projde jen samotná navigace prohlížeče na `/order/{presentationCode}` —
  appka si endpoint zavolá sama a dostane `200`.

Jiné endpointy stejného API (`presentations/{id}`, `seats/seatplanV2` — plán
sálu) touhle ochranou omezené nejsou a fungují i skriptovaně bez session.
Cílená ochrana právě na živou obsazenost sedadel vypadá jako záměrné
opatření proti automatizovanému sledování volných míst — obcházet ji
(headless prohlížeč apod.) by znamenalo stavět nástroj přímo proti tomuhle
opatření, což tenhle projekt záměrně nedělá.

Místo přesných sedadel se proto počítá jen odhad:

1. `availabilityRatio` u představení z quickbook API (podíl volných míst,
   0–1) × kapacita sálu spočítaná z veřejného `seatplanV2` (počet sedadel v
   plánu) = odhadovaný počet volných míst.
2. Pokud jich zbývá `MIN_FREE_SEATS` nebo méně a `soldOut` přitom hlásí
   "volno", termín se v hlášení označí jako **vyprodáno (odhadem)** —
   předpoklad je, že zbývající místa jsou vozíčkářská/doprovodná.

Je to nepřesné (odhad, ne přesný seznam sedadel) a nefunguje z něj detekce
páru sedadel vedle sebe ani vynechání konkrétních řad — na to by bylo potřeba
přesně to zablokované API.

### Hlášení jen od určitého počtu volných míst

Spousta nově vypsaných termínů má už od prvního zveřejnění obsazenou naprostou
většinu míst (typicky předprodej pro predplatitele) — takové jsou k ničemu,
vstupenku na ně stejně nekoupíš. `MIN_REPORT_FREE_SEATS` (výchozí `10`) proto
filtruje **nové termíny** v běžném běhu: e-mailem se pošlou jen ty, kde odhad
volných míst dosahuje aspoň tohohle prahu. Odfiltrované termíny se přesto
zapíšou do stavu jako už viděné (aby se sondovaly jen jednou, ne pořád
dokola) — pokud později uvolní víc míst, watchdog už to nezachytí, protože z
pohledu ID termínu jde o "už známé" představení. (Chceš-li i tohle — hlásit
zpětně, když se u známého termínu zlepší dostupnost přes práh — je to
rozšíření navíc, dnes to takhle nedělá.)

`--force-report` tenhle filtr obchází (ukáže úplně vše, i s malým počtem
volných míst) — je to ruční "co se teď hraje", ne pravidelné hlášení.

## Chci to hlídat taky (fork)

Watchdog nepotřebuje žádné tokeny ani secrets — API Cinema City je veřejné
a na zakládání issues stačí vestavěný `GITHUB_TOKEN`. Rozjedeš ho takhle:

1. **Forkni** si tohle repo.
2. **Settings → General → Features → zaškrtni `Issues`.** Forky mají issues
   vypnuté a bez nich by watchdog neměl kudy hlásit.
3. **Actions → „I understand my workflows, go ahead and enable them".**
   GitHub v forcích naplánované workflows nespouští, dokud je nepovolíš.
4. Hotovo. Issues se zakládají a přiřazují tobě, protože workflow používá
   `${{ github.repository_owner }}` — nic přepisovat nemusíš.

Stav v `state/seen.json` se forkne s sebou, takže tě to nezasype aktuálním
rozpisem a ozve se až s prvním novým termínem. Chceš-li hned vidět, co se
hraje teď, spusť workflow ručně s `force_report`.

Hlídat jiný film než Odysseu: přepiš `FILM_PATTERN` (a případně
`AUDITORIUM_PATTERN`) ve workflow a smaž obsah `state/seen.json`.

## Ruční spuštění

**Actions → Cinema City watchdog → Run workflow**. Zaškrtnutí *force_report*
nahlásí všechny aktuální termíny, i ty už známé — hodí se na ověření, že to žije,
nebo jako „ukaž mi, co teď hrajou“.

```bash
gh workflow run watch.yml --repo vkoca/cinemacity-watchdog -f force_report=true
```

## Lokální spuštění

Čisté Python 3, žádné závislosti:

```bash
python3 watch.py --state state/seen.json
```

Užitečné přepínače: `--seed` (jen zapíše stav, nic nehlásí — dobré po změně
filtru), `--force-report` (vypíše vše bez ohledu na stav).

Testy (potřebují `pytest`, nic dalšího):

```bash
pip install pytest
pytest tests/
```

## Údržba

- **Kvóta Actions:** repo je záměrně veřejné — u veřejných rep jsou minuty
  Actions zdarma bez limitu. Kdyby se překlopilo na privátní, běhy by se začaly
  počítat do free limitu 2 000 minut měsíčně a půlhodinová kadence by ho
  přečerpala; pak je potřeba zároveň zpomalit cron (např. `23 */2 * * *`).
- **60denní pauza:** GitHub automaticky vypne cron, pokud v repu 60 dní nic
  nepřibude. Tady to nehrozí — workflow si sám commituje stav.
- **Až Odyssea dohraje,** watchdog jen přestane cokoli hlásit. Buď ho vypni
  (Actions → *Disable workflow*), nebo přepiš `FILM_PATTERN` na další film.
- Kdyby Cinema City API změnilo, workflow spadne s chybou a GitHub o tom
  pošle e-mail.
