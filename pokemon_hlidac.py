#!/usr/bin/env python3
"""
Hlídač naskladnění Pokémon TCG: 30th Celebration
------------------------------------------------
Pravidelně prochází e-shopy ze souboru obchody.txt, najde všechny zabalené
produkty ze sady 30th Celebration (jednotlivé karty ignoruje) a ozve se,
když je některý SKLADEM nebo jde PŘEDOBJEDNAT.

Instalace (jednou):
    pip install playwright
    python -m playwright install chromium

Spuštění:
    python pokemon_hlidac.py                       # kontrola každých 5 minut
    python pokemon_hlidac.py --jednou --ladeni     # jedna kontrola s podrobným výpisem
    python pokemon_hlidac.py --ntfy moje-tema      # push na mobil přes aplikaci ntfy
    python pokemon_hlidac.py --bez-predobjednavek  # hlásit jen zboží skladem
"""

import argparse
import json
import os
import platform
import random
import re
import subprocess
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sys.exit("Chybí Playwright. Spusť 1_instalace.bat (nebo: pip install playwright && python -m playwright install chromium)")

SLOZKA = Path(__file__).resolve().parent
OBCHODY_SOUBOR = SLOZKA / "obchody.txt"
PROFIL_PROHLIZECE = SLOZKA / ".prohlizec_profil"
STAV_SOUBOR = SLOZKA / ".hlidac_stav.json"
STOP_SOUBOR = SLOZKA / "ZASTAVIT"
LOG_SOUBOR = SLOZKA / "hlidac_log.txt"
ZAMEK_SOUBOR = SLOZKA / ".hlidac_zamek"

# Co musí být v názvu nebo adrese produktu (porovnává se bez diakritiky, malými písmeny)
KLICOVA_SLOVA = ["30th celebration", "30 vyroci", "30 vyrocie", "30th anniversary"]
# Co vyřadit: jednotlivé karty ("Lapras 131/128", "(30C 146)", "[Holo]") a použité zboží
VYLOUCIT = r"\b\d{1,3}\s?/\s?\d{2,3}\b|\(\s*[A-Z0-9]{2,4}\s+\d{1,3}\s*\)|\[\s*(holo|reverse)|použit"

MAX_STRANEK = 5
TICHY = False          # na GitHubu: žádné zvuky ani okna
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")

# --------------------------------------------------------------------------- dostupnost

NEDOSTUPNE = ["vyprodano", "vypredane", "neni skladem", "nie je skladom", "nie je na sklade",
              "neni dostupn", "nedostupne", "not in stock", "out of stock", "sold out",
              "prodej skoncil", "pouze na prodejn", "dostupne jen", "len na predajn", "na dotaz"]
SKLADEM = ["skladem", "skladom", "na sklade", "ihned", "in stock", "posledni kus"]
PREDOBJEDNAVKA = ["predobjednav", "predobjednat", "pre-order", "preorder", "pre order", "presale"]
PRIPRAVUJE = ["pripravujeme", "ocekavame", "vychadza", "vychazi", "coming soon", "na ceste",
              "neznama dostupnost"]

# dostupnost na kamenné prodejně nás nezajímá (např. "Skladem na prodejně > 4 ks")
PRODEJNA = re.compile(r"(neni\s+|nie je\s+)?(skladem|skladom|dostupne)\s+(na|v|pre)\s+(prodejn|predajn)\w*"
                      r"(\s*[>]?\s*\d+\s*(ks|kusu|kusy))?|skladem\s+prodejn\w*")


def bez_diakritiky(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in text if not unicodedata.combining(c)).lower()


def urci_stav(text: str) -> str:
    t = PRODEJNA.sub(" ", bez_diakritiky(text))
    if re.search(r"(^|\s)[01]\s*kc\b", t):          # cena 0/1 Kč = jen zástupná položka
        return "pripravuje"
    if any(k in t for k in NEDOSTUPNE):
        return "nedostupne"
    if any(k in t for k in SKLADEM):
        return "skladem"
    if any(k in t for k in PREDOBJEDNAVKA):
        return "predobjednavka"
    if any(k in t for k in PRIPRAVUJE):
        return "pripravuje"
    return "neznamo"


def vyhodnot(produkt: dict) -> str:
    """Nejdřív text dostupnosti produktu, když chybí nebo nic neříká, celý text karty."""
    stav = urci_stav(produkt["dostupnost"]) if produkt["dostupnost"] else "neznamo"
    if stav == "neznamo":
        stav = urci_stav(produkt["text_karty"])
    elif stav == "pripravuje" and urci_stav(produkt["text_karty"]) == "predobjednavka":
        stav = "predobjednavka"      # např. "Vychádza november" + štítek "Predobjednávka"
    return stav


POPIS_STAVU = {"skladem": "SKLADEM", "predobjednavka": "PŘEDOBJEDNÁVKA", "nedostupne": "vyprodáno",
               "pripravuje": "připravuje se", "neznamo": "neznámý stav"}

# --------------------------------------------------------------------------- JavaScript pro stránku

JS_PRODUKTY = r"""
(args) => {
  const KLICOVA = args.klicova;
  const VYLOUCIT = new RegExp(args.vyloucit, 'i');
  const norm = s => (s || '').normalize('NFKD').replace(/[̀-ͯ]/g, '').toLowerCase()
                     .replace(/[-_.:/]+/g, ' ').replace(/\s+/g, ' ').trim();
  const abs = h => { try { const u = new URL(h, location.href); u.hash = ''; return u.href; } catch (e) { return null; } };
  const textOdkazu = a => [a.innerText, a.getAttribute('title'), a.getAttribute('aria-label'),
                           ...Array.from(a.querySelectorAll('img')).map(i => i.alt)].filter(Boolean).join(' ');
  const PRODUKT = /\d\s*(kc|€|eur|czk|,-)|sklad|vyprod|vypredan|nedostup|predobjed|pripravuj|in stock|sold out|ihned/i;
  const NEPRODUKT = /\/(blog|clanky?|novinky|tag|magazin|aktuality|poradna|recenze)\//i;

  const skupiny = new Map();
  for (const a of document.querySelectorAll('a[href]')) {
    const h = a.getAttribute('href') || '';
    if (h.startsWith('#') || h.startsWith('javascript') || h.startsWith('mailto')) continue;
    const url = abs(h);
    if (!url) continue;
    const u = new URL(url);
    if (u.host !== location.host || u.pathname === location.pathname || NEPRODUKT.test(u.pathname)) continue;
    if (!skupiny.has(url)) skupiny.set(url, []);
    skupiny.get(url).push(a);
  }

  const vysledky = [];
  for (const [url, odkazy] of skupiny) {
    const texty = odkazy.map(a => (a.innerText || '').replace(/\s+/g, ' ').trim()).filter(Boolean);
    const vse = odkazy.map(a => textOdkazu(a).replace(/\s+/g, ' ').trim()).filter(Boolean);
    const nazev = texty.sort((x, y) => y.length - x.length)[0] || vse.sort((x, y) => y.length - x.length)[0] || '';
    const hledano = norm(vse.join(' ')) + ' ' + norm(decodeURIComponent(new URL(url).pathname));
    if (!KLICOVA.some(k => hledano.includes(k))) continue;
    if (VYLOUCIT.test(nazev)) continue;

    // karta produktu = největší předek, který neobsahuje odkaz na jiný produkt
    const a = odkazy.find(x => typeof x.className === 'string' && x.className.trim()) || odkazy[0];
    const tr = typeof a.className === 'string' && a.className.trim() ? a.className.trim().split(/\s+/)[0] : null;
    const sel = tr ? 'a.' + CSS.escape(tr) : 'a[href]';
    const jinych = el => new Set(Array.from(el.querySelectorAll(sel)).map(x => abs(x.getAttribute('href')))
                                   .filter(u => u && u !== url)).size;
    let box = a;
    while (box.parentElement && box.parentElement !== document.body && jinych(box.parentElement) === 0) box = box.parentElement;
    const text = (box.innerText || '').replace(/\s+/g, ' ').trim();
    if (!PRODUKT.test(norm(text)) || text.length > 2500) continue;
    // karta nesmí být záhlaví/menu stránky (filtr "Skladem" v záhlaví by dával falešné nálezy)
    if (box.matches('header, nav') || box.querySelector('header, nav, [role=search], [role=navigation], input[type=search], input[name=q], input[name=exps], input[name=string], input[name=term], input[name=query], input[name*=search i]')
        || box.querySelectorAll('a[href]').length > 30) continue;
    const avl = box.querySelector('[class*="avail" i], [class*="avl" i], [class*="stock" i], [class*="dostupn" i], [class*="sklad" i]');
    vysledky.push({ url, nazev: nazev.slice(0, 150),
                    dostupnost: avl ? avl.innerText.replace(/\s+/g, ' ').trim().slice(0, 120) : '',
                    text_karty: text.slice(0, 600) });
  }
  return vysledky;
}
"""

JS_NACIST_DALSI = r"""
() => {
  const norm = s => (s || '').normalize('NFKD').replace(/[̀-ͯ]/g, '').toLowerCase().trim();
  const re = /^(zobrazit|nacist|nacitat|ukazat|show|load)\s+(dalsi|vice|viac|dalsie|more)/;
  const b = Array.from(document.querySelectorAll('button, a, [role=button]'))
    .find(e => re.test(norm(e.innerText)) && e.offsetParent !== null && !e.disabled);
  if (!b) return false;
  b.scrollIntoView({block: 'center'}); b.click(); return true;
}
"""

JS_DALSI_STRANKA = r"""
() => {
  const norm = s => (s || '').normalize('NFKD').replace(/[̀-ͯ]/g, '').toLowerCase().trim();
  const ok = h => { try { const u = new URL(h, location.href); return u.host === location.host && u.href !== location.href ? u.href : null; } catch (e) { return null; } };
  const rel = document.querySelector('link[rel=next], a[rel=next]');
  if (rel && ok(rel.getAttribute('href'))) return ok(rel.getAttribute('href'));
  const odkazy = Array.from(document.querySelectorAll('a[href]'));
  const t = odkazy.find(a => /^(dalsi|next|nasledujici|dalsia|›|»)(\s|>|›|»|$)/.test(norm(a.innerText)) && ok(a.getAttribute('href')));
  if (t) return ok(t.getAttribute('href'));
  const cur = new URL(location.href);
  const curPage = parseInt(cur.searchParams.get('page') || cur.searchParams.get('p') || '1', 10);
  for (const a of odkazy) {
    const h = ok(a.getAttribute('href')); if (!h) continue;
    const u = new URL(h);
    if (u.pathname !== cur.pathname) continue;
    const p = parseInt(u.searchParams.get('page') || u.searchParams.get('p') || '0', 10);
    if (p === curPage + 1) return h;
  }
  return null;
}
"""


def ted() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def nacti_obchody() -> list[tuple[str, str]]:
    obchody = []
    for radek in OBCHODY_SOUBOR.read_text(encoding="utf-8").splitlines():
        radek = radek.strip()
        if not radek or radek.startswith("#") or ";" not in radek:
            continue
        nazev, url = (x.strip() for x in radek.split(";", 1))
        obchody.append((nazev, url))
    return obchody


def je_blokovano(page) -> bool:
    try:
        t = bez_diakritiky(page.title() + " " + page.inner_text("body")[:3000])
    except Exception:
        return False
    return any(k in t for k in ["just a moment", "verify you are human", "overte, ze jste clovek",
                                "checking your browser", "access denied", "captcha", "pristup odepren",
                                "pristup byl zablokovan", "request blocked", "forbidden", "too many requests",
                                "unusual traffic", "enable javascript and cookies", "neobvykly provoz"])


def popis_stranky(page, odpoved) -> str:
    """Krátký popis načtené stránky pro diagnostiku (stav HTTP, nadpis, začátek textu)."""
    try:
        text = re.sub(r"\s+", " ", page.inner_text("body"))[:200]
    except Exception:
        text = "?"
    stav = odpoved.status if odpoved else "?"
    return f"HTTP {stav}, adresa {page.url[:90]}, nadpis „{page.title()[:60]}“, text: {text}"


def prohledej_stranku(page, url: str) -> list[dict]:
    produkty, videno = [], set()
    adresa = url
    for _ in range(MAX_STRANEK):
        odpoved = page.goto(adresa, wait_until="domcontentloaded", timeout=60_000)
        if odpoved and odpoved.status in (401, 403, 429, 503):
            raise PermissionError(f"obchod odmítl přístup (HTTP {odpoved.status})")
        if odpoved and odpoved.status in (404, 410):
            raise RuntimeError(f"stránka neexistuje (HTTP {odpoved.status}) – obchod ji asi přesunul, je potřeba upravit obchody.txt")
        try:
            page.wait_for_load_state("networkidle", timeout=8_000)
        except Exception:
            pass
        page.wait_for_timeout(1500)
        if je_blokovano(page):
            raise PermissionError("stránka zobrazila ochranu proti robotům")
        for _ in range(3):  # dočtení obsahu, který se načítá při posouvání
            page.mouse.wheel(0, 20000)
            page.wait_for_timeout(600)
        for _ in range(6):  # tlačítka "Zobrazit další produkty"
            if not page.evaluate(JS_NACIST_DALSI):
                break
            page.wait_for_timeout(2500)
        for p in page.evaluate(JS_PRODUKTY, {"klicova": KLICOVA_SLOVA, "vyloucit": VYLOUCIT}):
            if p["url"] not in videno:
                videno.add(p["url"])
                produkty.append(p)
        if not produkty and (TICHY or os.getenv("HLIDAC_DIAGNOSTIKA")):
            print(f"     [diagnostika] {popis_stranky(page, odpoved)}", flush=True)
        dalsi = page.evaluate(JS_DALSI_STRANKA)
        if not dalsi or dalsi == adresa:
            break
        adresa = dalsi
    return produkty


# --------------------------------------------------------------------------- upozornění

def pipni():
    if TICHY:
        return
    try:
        if platform.system() == "Windows":
            import winsound
            for _ in range(3):
                winsound.Beep(1200, 300)
        elif platform.system() == "Darwin":
            subprocess.run(["afplay", "/System/Library/Sounds/Glass.aiff"], check=False)
        else:
            print("\a", end="", flush=True)
    except Exception:
        pass


def systemove_upozorneni(titulek: str, text: str):
    if TICHY:
        return
    try:
        system = platform.system()
        if system == "Darwin":
            subprocess.run(["osascript", "-e", f"display notification {json.dumps(text)} with title {json.dumps(titulek)}"], check=False)
        elif system == "Linux":
            subprocess.run(["notify-send", titulek, text], check=False)
        elif system == "Windows":
            ps = ("[void][System.Reflection.Assembly]::LoadWithPartialName('System.Windows.Forms');"
                  "$n=New-Object System.Windows.Forms.NotifyIcon;"
                  "$n.Icon=[System.Drawing.SystemIcons]::Information;$n.Visible=$true;"
                  f"$n.ShowBalloonTip(10000,{json.dumps(titulek)},{json.dumps(text)},'Info');"
                  "Start-Sleep -s 10;$n.Dispose()")
            subprocess.Popen(["powershell", "-NoProfile", "-Command", ps],
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:
        pass


def posli_ntfy(tema: str, titulek: str, text: str, url: str | None):
    """Pošle upozornění přes ntfy (formát JSON – spolehlivě přenese i diakritiku)."""
    server = os.getenv("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    zprava = {"topic": tema, "title": titulek, "message": text, "priority": 4, "tags": ["rotating_light"]}
    if url:
        zprava["click"] = url
    req = urllib.request.Request(server, data=json.dumps(zprava).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=15)


def ohlas(nalezy: list[dict], args, souhrn: bool = False):
    """nalezy: seznam produktů, které se právě objevily skladem / v předobjednávce."""
    if not nalezy:
        return
    radky = [f"[{p['obchod']}] {p['nazev']} – {POPIS_STAVU[p['stav']]}" for p in nalezy]
    if souhrn:
        titulek = f"Hlídač: právě dostupných {len(nalezy)} produktů"
    elif len(nalezy) == 1:
        p = nalezy[0]
        titulek = f"{POPIS_STAVU[p['stav']]}: {p['obchod']}"
    else:
        titulek = f"Nově dostupných {len(nalezy)} produktů"

    print("\n" + "!" * 70)
    print(f"[{ted()}] {titulek}")
    for p, r in zip(nalezy, radky):
        print(f"  {r}\n    {p['url']}")
    print("!" * 70 + "\n", flush=True)

    pipni()
    systemove_upozorneni(titulek, "\n".join(radky[:4]) + ("\n…" if len(radky) > 4 else ""))
    if not args.neotvirat and not souhrn:
        for p in nalezy[:3]:
            webbrowser.open(p["url"])
    if args.ntfy:
        try:
            if len(nalezy) <= 3 and not souhrn:
                for p, r in zip(nalezy, radky):
                    posli_ntfy(args.ntfy, f"{POPIS_STAVU[p['stav']]}: {p['obchod']}", r, p["url"])
            else:
                posli_ntfy(args.ntfy, titulek, "\n".join(radky[:15]), None)
        except Exception as e:
            print(f"  ntfy se nepodařilo odeslat: {e}")


# --------------------------------------------------------------------------- prohlížeč, běh

def spust_prohlizec(pw, rezim: str):
    """rezim: 'skryty' (bez okna), 'mimo_obrazovku' (okno mimo viditelnou plochu), 'viditelny'."""
    argumenty = ["--disable-blink-features=AutomationControlled"]
    if rezim == "mimo_obrazovku":
        argumenty += ["--window-position=-32000,-32000", "--window-size=1366,900"]
    volby = dict(headless=(rezim == "skryty"), locale="cs-CZ", viewport={"width": 1366, "height": 900},
                 user_agent=USER_AGENT, args=argumenty)
    try:
        ctx = pw.chromium.launch_persistent_context(str(PROFIL_PROHLIZECE), channel="chromium", **volby)
    except Exception:
        ctx = pw.chromium.launch_persistent_context(str(PROFIL_PROHLIZECE), **volby)
    # obrázky, videa a písma nestahujeme – kontrola je rychlejší a šetří data
    ctx.route("**/*", lambda route: route.abort()
              if route.request.resource_type in ("image", "media", "font") else route.continue_())
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    return ctx, page


def zamkni_jedinou_instanci():
    f = open(ZAMEK_SOUBOR, "a+")
    try:
        if platform.system() == "Windows":
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        systemove_upozorneni("Hlídač Pokémonů", "Hlídač už běží na pozadí – není potřeba ho spouštět znovu.")
        sys.exit("Hlídač už běží.")
    return f


def spi(sekundy: float) -> bool:
    konec = time.time() + sekundy
    while time.time() < konec:
        if STOP_SOUBOR.exists():
            return True
        time.sleep(min(5, max(0, konec - time.time())))
    return STOP_SOUBOR.exists()


def nacti_stav() -> dict:
    try:
        return json.loads(STAV_SOUBOR.read_text(encoding="utf-8"))
    except Exception:
        return {}


def uloz_stav(stav: dict):
    STAV_SOUBOR.write_text(json.dumps(stav, ensure_ascii=False, indent=1), encoding="utf-8")


def zapis_souhrn_github(radky: list[str]):
    """Tabulka výsledků, která se na GitHubu zobrazí u každého běhu."""
    cil = os.getenv("GITHUB_STEP_SUMMARY")
    if not cil:
        return
    with open(cil, "a", encoding="utf-8") as f:
        f.write(f"## Kontrola {ted()}\n\n| Obchod | Výsledek |\n|---|---|\n")
        f.write("\n".join(radky) + "\n")


def main():
    global STAV_SOUBOR, TICHY
    ap = argparse.ArgumentParser(description="Hlídá naskladnění Pokémon TCG: 30th Celebration")
    ap.add_argument("--interval", type=float, default=5, help="minuty mezi koly kontrol (výchozí 5)")
    ap.add_argument("--bez-predobjednavek", action="store_true", help="nehlásit předobjednávky")
    ap.add_argument("--zobrazit", action="store_true", help="prohlížeč poběží ve viditelném okně")
    ap.add_argument("--jednou", action="store_true", help="jedno kolo kontrol a konec")
    ap.add_argument("--neotvirat", action="store_true", help="neotevírat produkty v prohlížeči")
    ap.add_argument("--ntfy", default=os.getenv("NTFY_TEMA"), help="téma na ntfy.sh pro push na mobil")
    ap.add_argument("--ladeni", action="store_true", help="vypsat všechny nalezené produkty")
    ap.add_argument("--log", action="store_true", help="zapisovat výpis do hlidac_log.txt")
    ap.add_argument("--github", action="store_true", help="režim pro GitHub Actions (jedno kolo, stav v stav.json)")
    args = ap.parse_args()

    if args.github:
        args.jednou = args.neotvirat = True
        TICHY = True
        STAV_SOUBOR = SLOZKA / "stav.json"
        if not args.ntfy:
            print("POZOR: není nastaven Secret NTFY_TEMA – upozornění na mobil nepůjdou odeslat!", flush=True)
    if args.log:
        f = open(LOG_SOUBOR, "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = f

    zamek = zamkni_jedinou_instanci()
    STOP_SOUBOR.unlink(missing_ok=True)
    hlasit = {"skladem"} if args.bez_predobjednavek else {"skladem", "predobjednavka"}
    stav = nacti_stav()
    chyby = stav.setdefault("_chyby", {})          # kolikrát po sobě se stránku nepodařilo načíst
    prvni_beh = not any(not k.startswith("_") for k in stav)
    obchody = nacti_obchody()
    print(f"[{ted()}] Hlídám {len({o for o, _ in obchody})} obchodů ({len(obchody)} stránek)"
          + ("" if args.jednou else f", kolo každých {args.interval:g} min")
          + f". Hlásím: {', '.join(POPIS_STAVU[s] for s in sorted(hlasit))}.", flush=True)
    if args.log:
        systemove_upozorneni("Hlídač Pokémonů", f"Hlídání běží na pozadí ({len({o for o, _ in obchody})} obchodů).")

    with sync_playwright() as pw:
        rezim = "viditelny" if args.zobrazit else "skryty"
        ctx, page = spust_prohlizec(pw, rezim)
        try:
            while True:
                nove, ok_stranek, souhrn_radky = [], 0, []
                for obchod, url in obchody:
                    if STOP_SOUBOR.exists():
                        break
                    try:
                        try:
                            produkty = prohledej_stranku(page, url)
                        except PermissionError:
                            if rezim != "skryty":
                                raise
                            print(f"[{ted()}] {obchod}: blokuje skrytý prohlížeč, přepínám na okno mimo obrazovku.", flush=True)
                            ctx.close()
                            rezim = "mimo_obrazovku"
                            ctx, page = spust_prohlizec(pw, rezim)
                            produkty = prohledej_stranku(page, url)
                        ok_stranek += 1
                    except Exception as e:
                        duvod = str(e).splitlines()[0][:150] if str(e) else type(e).__name__
                        print(f"[{ted()}] {obchod}: chyba – {duvod}", flush=True)
                        souhrn_radky.append(f"| {obchod} | ❌ chyba: {duvod} |")
                        predtim_chyb = chyby.get(url, 0)
                        chyby[url] = min(predtim_chyb + 1, 6)   # strop 6, ať se stav neukládá zbytečně
                        if predtim_chyb == 5:      # ohlásit jednou, když to selže 6× po sobě
                            zprava = f"Obchod {obchod} se už 6× po sobě nepodařilo zkontrolovat ({duvod})."
                            systemove_upozorneni("Hlídač Pokémonů", zprava)
                            if args.ntfy:
                                try:
                                    posli_ntfy(args.ntfy, "Hlídač: problém s obchodem", zprava, url)
                                except Exception:
                                    pass
                        continue
                    if chyby.get(url):
                        chyby[url] = 0

                    pocty = {}
                    for p in produkty:
                        p["obchod"] = obchod
                        p["stav"] = vyhodnot(p)
                        pocty[p["stav"]] = pocty.get(p["stav"], 0) + 1
                        predtim = stav.get(p["url"], {}).get("stav")
                        if p["stav"] in hlasit and predtim != p["stav"]:
                            nove.append(p)
                        stav[p["url"]] = {"stav": p["stav"], "nazev": p["nazev"], "obchod": obchod}
                        if args.ladeni:
                            print(f"     {POPIS_STAVU[p['stav']]:15} | {p['nazev'][:70]}"
                                  f"  [{(p['dostupnost'] or '-')[:40]}]", flush=True)
                    souhrn = ", ".join(f"{POPIS_STAVU[k]} {v}" for k, v in sorted(pocty.items())) or "nic nenalezeno"
                    print(f"[{ted()}] {obchod}: {len(produkty)} produktů ({souhrn})", flush=True)
                    souhrn_radky.append(f"| {obchod} | {len(produkty)} produktů ({souhrn}) |")

                stav["_den"] = datetime.now().strftime("%Y-%m-%d")   # na GitHubu = 1 uložení denně, drží rozvrh aktivní
                uloz_stav(stav)
                # při úplně prvním spuštění pošli jeden souhrn místo desítek upozornění
                ohlas(nove, args, souhrn=prvni_beh)
                prvni_beh = False
                if nove:
                    souhrn_radky.append(f"| **Nově dostupné** | {len(nove)} – upozornění odesláno |")
                zapis_souhrn_github(souhrn_radky)
                if ok_stranek == 0:
                    systemove_upozorneni("Hlídač Pokémonů", "Žádný obchod se nepodařilo zkontrolovat – je připojení k internetu?")

                if args.jednou or STOP_SOUBOR.exists():
                    break
                if spi(args.interval * 60 * random.uniform(0.85, 1.15)):
                    break
        except KeyboardInterrupt:
            print("\nUkončeno.")
        finally:
            if STOP_SOUBOR.exists():
                print(f"[{ted()}] Hlídač zastaven.", flush=True)
                systemove_upozorneni("Hlídač Pokémonů", "Hlídání bylo zastaveno.")
            try:
                ctx.close()
            except Exception:
                pass
            STOP_SOUBOR.unlink(missing_ok=True)
            zamek.close()


if __name__ == "__main__":
    main()
