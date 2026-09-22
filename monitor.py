"""
Boligovervågning: tjekker boligsider og sender en besked til din telefon
(via ntfy), når der dukker nye boliglinks op.
Du behøver ikke ændre noget i denne fil. Sider indstilles i sites.json.
"""
import json
import os
import urllib.request
from pathlib import Path
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

from playwright.sync_api import sync_playwright

CONFIG = Path("sites.json")
SEEN = Path("seen.json")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def clean_url(url):
    """Fjerner sporings-parametre, så samme bolig altid har samme link."""
    p = urlparse(url)
    query = [
        (k, v) for k, v in parse_qsl(p.query)
        if not k.lower().startswith(("utm_", "fbclid", "gclid", "_ga"))
    ]
    path = p.path.rstrip("/") or "/"
    return urlunparse((p.scheme, p.netloc.lower(), path, "", urlencode(query), ""))


def notify(title, message, click=None):
    if not NTFY_TOPIC:
        print("ADVARSEL: NTFY_TOPIC mangler, så der blev ikke sendt besked.")
        return
    data = {"topic": NTFY_TOPIC, "title": title, "message": message, "tags": ["house"]}
    if click:
        data["click"] = click
        data["actions"] = [{"action": "view", "label": "Åbn boligen", "url": click}]
    req = urllib.request.Request(
        "https://ntfy.sh",
        data=json.dumps(data).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(req, timeout=20)
    except Exception as e:
        print(f"Kunne ikke sende besked: {e}")


def get_links(page, site):
    page.goto(site["url"], wait_until="domcontentloaded", timeout=60000)
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass  # nogle sider bliver aldrig helt "stille"; det er ok
    # Scroll ned, så boliger der først indlæses ved scroll også kommer med
    for _ in range(6):
        page.mouse.wheel(0, 4000)
        page.wait_for_timeout(800)

    anchors = page.eval_on_selector_all(
        "a[href]", "els => els.map(e => [e.href, (e.innerText || '').trim()])"
    )
    domain = urlparse(site["url"]).netloc.lower().removeprefix("www.")
    must_contain = [s.lower() for s in site.get("link_skal_indeholde", []) if s]
    start_url = clean_url(site["url"])

    links = {}
    for href, text in anchors:
        if not href.startswith("http"):
            continue
        url = clean_url(href)
        host = urlparse(url).netloc.removeprefix("www.")
        if not host.endswith(domain) or url == start_url:
            continue
        if must_contain and not any(m in url.lower() for m in must_contain):
            continue
        label = " ".join(text.split())[:150]
        if url not in links or (label and not links[url]):
            links[url] = label
    return links


def check_waitlist(page, site, seen):
    """Holder øje med, om en 'lukket'-tekst forsvinder fra en side."""
    name = site.get("navn", "Venteliste")
    url = site["url"]
    closed_text = site.get("lukket_tekst", "Lukket for opskrivning").lower()
    must_have = site.get("side_skal_indeholde", "venteliste").lower()

    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    page.wait_for_timeout(2000)
    text = " ".join(page.inner_text("body").split()).lower()

    # Sikkerhed: hvis siden ikke er indlæst ordentligt, gør vi ingenting
    if must_have and must_have not in text:
        print(f"[{name}] Siden så ikke ud som forventet. Springes over denne gang.")
        return False

    status = "lukket" if closed_text in text else "åben"
    key = "venteliste:" + url
    old = seen.get(key)
    print(f"[{name}] Status: {status} (før: {old})")

    if old is None:
        seen[key] = status
        notify(f"Overvågning startet: {name}",
               f"Ventelisten er lige nu {status}. Du får besked, hvis det ændrer sig.")
        return True
    if status != old:
        seen[key] = status
        if status == "åben":
            notify(f"VENTELISTEN ER ÅBEN: {name}",
                   "Skynd dig at skrive dig op! Tryk for at åbne siden.", click=url)
        else:
            notify(f"Ventelisten er lukket igen: {name}",
                   "Den er lukket for opskrivning nu.", click=url)
        return True
    return False


def main():
    sites = load_json(CONFIG, [])
    seen = load_json(SEEN, {})
    changed = False

    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(
            locale="da-DK",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
            ),
        )
        for site in sites:
            name = site.get("navn", "Ukendt side")
            url = site.get("url", "")
            if not url.startswith("http"):
                print(f"[{name}] Springes over: der er ikke indsat et link endnu.")
                continue

            page = context.new_page()
            if site.get("type") == "venteliste":
                try:
                    if check_waitlist(page, site, seen):
                        changed = True
                except Exception as e:
                    print(f"[{name}] FEJL ved indlæsning: {e}")
                page.close()
                continue
            try:
                links = get_links(page, site)
            except Exception as e:
                print(f"[{name}] FEJL ved indlæsning: {e}")
                page.close()
                continue
            page.close()

            print(f"[{name}] Fandt {len(links)} links.")
            if not links:
                continue

            if url not in seen:
                # Første gang: gem alt som "set", og send en bekræftelse
                seen[url] = sorted(links)
                changed = True
                notify(
                    f"Overvågning startet: {name}",
                    f"Jeg holder nu øje med siden ({len(links)} links fundet). "
                    "Du får besked, når der kommer nye boliger.",
                )
                continue

            old = set(seen[url])
            new = [u for u in links if u not in old]
            for u in new[:10]:
                print(f"[{name}] NYT: {u}")
                notify(f"Ny bolig hos {name}", links[u] or u, click=u)
            if len(new) > 10:
                notify(f"{name}: mange nye opslag",
                       f"{len(new)} nye links. Tjek siden.", click=url)
            if new:
                seen[url] = sorted(old | set(links))
                changed = True

        browser.close()

    if changed:
        SEEN.write_text(json.dumps(seen, indent=2, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
