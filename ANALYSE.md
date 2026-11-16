# Automatisierter Zugriff auf Suchmaschinen: Analyse und Empfehlung

Stand: 05.11.2026 · Ziel: kostenlose Dienste als Basis (günstige Bezahl-APIs optional als Reserve), voll automatisiert, parallel, möglichst ohne Captchas
Ausgelegt für: KI-Agent-Recherche und Monitoring/Alerts mit ca. 100–2.000 Anfragen pro Tag, Betrieb mit Docker oder auf einem eigenen Server

---

## 1. Kurzfazit

1. **Kostenlose offizielle Websuch-APIs der großen Anbieter gibt es praktisch nicht mehr.** Die Bing Search API wurde am 11.08.2025 abgeschaltet. Die Google Custom Search JSON API nimmt keine Neukunden mehr an und endet am 01.01.2027. Brave hat den Free-Plan durch 5 $ Monatsguthaben ersetzt, das eine Kreditkarte voraussetzt.
2. **Das tragfähige kostenlose Fundament ist eine selbst gehostete SearXNG-Instanz.** Sie fragt Google, Bing, Brave, DuckDuckGo, Qwant, Wikipedia usw. ab, liefert JSON und **pausiert gesperrte Engines von selbst**.
3. **Seit 2026 schützen sich weitere Suchmaschinen aktiv.** Startpage und Mojeek setzen ein Proof-of-Work-Captcha ein, SearXNG führt beide deshalb offiziell als `inactive`. Google, Bing, Qwant und Yahoo liefert SearXNG standardmäßig *deaktiviert* aus. Man muss also bewusst auswählen, welche Engines man nutzt.
4. **Dazu kommen offizielle, kostenlose Feeds und APIs, die nie Captchas zeigen:** Bing-RSS, Google-News-RSS, GDELT, Hacker News (Algolia), Wikipedia und Marginalia. Für das Monitoring sind sie sogar die bessere Quelle.
5. **Captchas werden vermieden, nicht gelöst:**
   - hostweite Drossel pro Quelle, die auch über Prozesse hinweg gilt
   - jede Engine nur einmal pro Anfrage
   - Cache
   - automatische Pause nach Sperren, die sich nicht hochschaukelt
   - Erkennung „stiller“ Sperren
   - **Betrieb von einer sauberen IP** (Büro- oder Heimanschluss statt Rechenzentrum)
   - Plan B ohne saubere IP: günstige Proxys **nur für die Engines, die sperren** (z.B. Google), mit festem Budget; ca. 5 $ reichen für rund 50.000 Ergebnisseiten im Monat (Abschnitt 3.1)

   Captcha-Solver sind für diesen Zweck weder nötig noch empfehlenswert (Abschnitt 6).
6. **Günstige Bezahl-APIs als Reserve:** Exa, Linkup, Tavily, Brave, Perplexity und Serper sind optional eingebaut (jeweils im günstigsten Tarifmodus, max. 10 Treffer pro Abfrage) und springen **nur als letzte Reserve** ein, wenn die freien Quellen wegen Sperren versagen. Zusätzlich gibt es 2026 nennenswerte **Gratiskontingente**, im Prototyp nutzbar zusammen ca. 8.500 Anfragen pro Monat (Exa ca. 2.500, Linkup ca. 4.000, Tavily 1.000, Brave ca. 1.000; mit Firecrawl und SerpApi ca. 9.250). Die günstigste sofort nutzbare Option ohne Paketkauf ist **Perplexity Fast Search mit 1 $/1k**; Serper kostet ebenfalls 1 $/1k (im großen Paket bis 0,30 $), verlangt aber den Kauf eines 50-$-Pakets, DataForSEO (0,60 $/1k) liefert nur asynchron (Abschnitt 7). Harte Kostengrenzen (`WEBSEARCH_PAID_DAILY_LIMIT`, `WEBSEARCH_PAID_MONTHLY_LIMIT`) verhindern Überraschungen.
7. Der **Prototyp** in diesem Repo setzt das um: ein paralleler Such-Client, Monitoring mit Neu-Treffer-Erkennung und ein **MCP-Server** für KI-Agents.

---

## 2. Marktlage: welche Zugänge gibt es (Nov. 2026)?

### 2.1 Suchmaschinen im Überblick

Viele „Suchmaschinen“ haben gar keinen eigenen Index, sondern zeigen Ergebnisse von Bing oder Google. Für die Automatisierung zählt deshalb, **welcher Index** dahintersteht und **über welchen Weg** man ihn erreicht. Stand: 05.11.2026. SearXNG-Status aus `searx/settings.yml` (Commit 3e95f6c), `ddgs`-Backends aus `ddgs.engines.ENGINES` (v9.16.1). „websearch“ meint die mitgelieferte `searxng/settings.yml` bzw. das Tool.

| Suchmaschine | Index | Offizielle API (Nov. 2026) | SearXNG-Engine (upstream) | `ddgs` | Bewertung |
|---|---|---|---|---|---|
| **Google** | eigener | Custom Search JSON API: keine Neukunden, Ende 01.01.2027. Indirekt: Serper (2.500 gratis, ab 1 $/1k), SerpApi (250/Monat gratis) | `google`: disabled, in websearch **aktiv** | deaktiviert | über SearXNG; empfindlichste Engine, Reserve: Serper |
| **Bing** | eigener | Web Search API abgeschaltet 11.08.2025 | `bing`: disabled, in websearch **aktiv** | Text deaktiviert (nur News/Bilder) | über SearXNG + **Bing-RSS** (`bing_rss`) |
| **Brave** | eigener | Brave Search API: 5 $/1k, 5 $ Gratisguthaben/Monat, Karte nötig | `brave`, `brave.news`: aktiv; `braveapi` (mit Key) vorhanden | ✅ | über SearXNG; Reserve `brave_api` |
| **DuckDuckGo** | Bing + eigene Quellen | keine Websuch-API (nur Instant Answers) | `duckduckgo`, `duckduckgo news`: aktiv | ✅ | über SearXNG; direkt oft 202-Captcha |
| **Yahoo** | Bing | keine | `yahoo`: disabled | ✅ | nur Fallback `ddgs:yahoo` (schwankend) |
| **Ecosia** | Bing (+ Google) | keine | keine Engine | – | kein eigener Index → über Bing abgedeckt |
| **Startpage** | Google | keine | `startpage`: **inactive** (Proof-of-Work-Captcha) | ✅ (aber PoW) | nicht nutzbar; Google direkt bzw. Serper |
| **Qwant** | eigener (EU) + Bing | keine öffentliche | `qwant`, `qwant news`: disabled, in websearch **aktiv** | – | über SearXNG; gut für DE/EU |
| **Mojeek** | eigener (UK) | ab 2 £/1k (Gratisguthaben, Karte nötig) | `mojeek`: **inactive** (PoW) | ✅ (aber PoW) | Scraping tot; API optional |
| **Yandex** | eigener (RU) | Yandex Cloud Search API: ca. 480 ₽ (≈ 4 $)/1k, Tageslimits | `yandex`: disabled; `yandex api`: inactive | – | nur für russischsprachige Recherche |
| **Baidu** | eigener (CN) | keine offene internationale Web-API | `baidu`: disabled | – | nur für chinesische Recherche |
| **Seznam** | eigener (CZ) | keine | `seznam`: disabled | – | nur für tschechische Recherche |
| **Naver** | eigener (KR) | Naver Search API (Kontingent, laut Dritt-Quellen kostenlos; 2026 Umzug auf NAVER API HUB) | `naver`, `naver news`: disabled | – | nur für koreanische Recherche |
| **Kagi** | eigener + Mix | Search API 12 $/1k | `kagi` (mit Key), auskommentiert | – | zu teuer als Reserve bei 100–2.000 Anfragen pro Tag |
| **You.com** | eigener | Web Search API 5 $/1k; MCP-Server mit Gratis-Profil | keine | – | Alternative zu Brave als Reserve |
| **Perplexity** | eigener | Search API 5 $/1k, „Fast Search“ **1 $/1k**, bis 5 Anfragen pro Request | keine | – | **günstigste Bezahl-Reserve** (Abschnitt 7) |
| **Exa** | eigener (semantisch) | ab 4 $/1k, **10 $ Gratisguthaben jeden Monat** ohne Karte | `exaapi` (mit Key): inactive | – | gut für Agent-Recherche, Gratiskontingent |
| **Jina** (`s.jina.ai`) | Drittanbieter-Suche + Reader | Token-basiert, ≥ 10.000 Tokens pro Suche | `jina` (mit Key): inactive | – | eher als Seiten-Reader (`r.jina.ai`) interessant |
| **Firecrawl** | Drittanbieter-Suche + Scraper | Free: 1.000 Credits/Monat (≈ 500 Suchen) | keine | – | eher zum Seitenabruf |
| **Tavily** | Drittanbieter, für LLMs | 1.000 Credits/Monat gratis, dann 0,008 $/Credit | keine | – | Reserve `tavily` |
| **Marginalia** | eigener (kleines Web) | gratis (Key per Mail, nicht-kommerziell) | `marginalia` (mit Key): disabled/inactive | – | Zusatzquelle `marginalia` |
| **Wikipedia** | eigener | MediaWiki-API gratis | `wikipedia`: aktiv | ✅ | `wikipedia` + SearXNG |

**Regionale Engines zuschalten** (Yandex, Baidu, Seznam, Naver): In `searxng/settings.yml` den Namen unter `use_default_settings.engines.keep_only` eintragen und unter `engines:` aktivieren, z.B.

```yaml
use_default_settings:
  engines:
    keep_only: [..., yandex, seznam]
engines:
  - name: yandex
    disabled: false
```

Jede zusätzliche Engine bekommt **jede** Anfrage der Kategorie. Regionale Engines deshalb nur aktivieren, wenn man sie wirklich braucht, oder sie gezielt per SearXNG-Bang (`!yd`, `!szn`) ansprechen.

### 2.2 Offizielle APIs

| Zugang | Status / Kosten | Captcha-Risiko | Eignung |
|---|---|---|---|
| Google Custom Search JSON API | geschlossen für Neukunden, **Ende 01.01.2027**; Nachfolger Vertex AI Search ist kostenpflichtig und keine echte Websuche | – | ❌ nicht mehr planbar |
| Bing Web Search API | **abgeschaltet 11.08.2025**; Nachfolger „Grounding with Bing“ nur über Azure AI Agents (bezahlt, keine Rohdaten) | – | ❌ |
| Brave Search API | Free-Plan entfallen; 5 $ Monatsguthaben ≈ 1.000 Anfragen, **Kreditkarte nötig** | keins | ⚠️ optional als Reserve (`BRAVE_API_KEY`) |
| Marginalia Search API | Key `public` gratis, aber geteilt und oft ausgeschöpft; **eigener Key kostenlos per Mail** (nur nicht-kommerziell, CC-BY-NC-SA) | keins | ✅ als Zusatzquelle (eigener, kleiner Index) |
| Mojeek API | kostenpflichtig (ab 2 £ pro 1.000 Anfragen) | – | ⚠️ siehe Abschnitt 7 |
| Wikipedia / Wikimedia API | gratis, ohne Key; User-Agent mit Kontakt angeben | keins | ✅ Fakten / Nachschlagen |
| **GDELT DOC 2.0** | gratis, ohne Key; **max. 1 Anfrage pro 5 s pro IP** | keins (429 bei Überlast, IP-Sperre länger als der Verstoß) | ✅ weltweites News-Monitoring |
| **Google News RSS** (`news.google.com/rss/search`) | gratis, offizieller Feed | sehr gering | ✅ News-Monitoring |
| **Bing RSS** (`bing.com/search?format=rss`) | gratis; laut Feed nur für **privaten, nicht-kommerziellen** Gebrauch | gering | ✅ (Lizenzhinweis beachten; abschaltbar mit `WEBSEARCH_DISABLE=bing_rss`) |
| Hacker News (Algolia API) | gratis, ohne Key | keins | ✅ Tech-Monitoring |
| Reddit | ohne OAuth meist 403 | ~100/min mit OAuth | ✅ eingebaut (`reddit`), braucht eine kostenlose „script“-App (README, „Quellen“) |
| Common Crawl Index | gratis, riesig, aber nicht tagesaktuell | keins | ⚠️ für Massenrecherche, nicht für Live-Suche |

### 2.3 Inoffizielle Zugänge (Scraping der Suchseiten)

| Werkzeug | Was es tut | Bewertung |
|---|---|---|
| **SearXNG** (selbst gehostet) | Metasuche über mehrere hundert Engine-Definitionen, JSON-API, Suspendierung gesperrter Engines pro Engine, Tor/Proxy/IPv6-Rotation konfigurierbar | ✅ **Rückgrat**, Details unten |
| **`ddgs`** (Python, v9.16) | fragt einzelne Engines direkt ab, mit Browser-TLS-Fingerprint (primp) | ✅ zweites Standbein (nur Fallback), Details unten |
| Headless-Browser (Playwright, Playwright-MCP) | echte Browser-Sitzung | ⚠️ nur für Einzelseiten, die JavaScript brauchen; zum Suchen zu langsam und zu auffällig |

**SearXNG: Engine-Stand upstream (Nov. 2026) und die Konfiguration von websearch**

| Engine | Upstream | `searxng/settings.yml` von websearch |
|---|---|---|
| Google, Bing | `disabled: true` | bewusst aktiviert |
| Qwant, Qwant News | `disabled: true` | bewusst aktiviert |
| DuckDuckGo, DuckDuckGo News, Brave, Wikipedia, Wikidata, GitHub, StackOverflow, arXiv | aktiv | aktiv |
| **Startpage, Mojeek, Mojeek News** | **`inactive: true`** (Proof-of-Work-Captcha, upstream PR #6669) | entfernt |
| Google News, Bing News | aktiv | **entfernt**: die holt der Client direkt als offiziellen RSS-Feed, damit jede Engine pro Anfrage nur einmal belastet wird |
| Yahoo | `disabled: true` | nicht genutzt (Bing-Index, über `ddgs:yahoo` als Fallback) |

Die Sperrzeiten (`suspended_times`) entsprechen wieder den **längeren Standardwerten von SearXNG**:

| Fehler | Sperrzeit |
|---|---|
| Access denied | 1 Tag |
| Captcha | 1 Tag |
| Too many requests | 1 h |
| Cloudflare-Captcha | 15 Tage |
| Cloudflare access denied | 1 Tag |
| reCAPTCHA | 7 Tage |

Wer zu früh wieder anfragt, verlängert Sperren meist nur.

**`ddgs` 9.16: Stand und Einschränkungen**

- `google`, `bing` und `yandex` sind für Text deaktiviert. Ein solches Backend fällt **still** auf `auto` zurück und fragt dann *alle* Engines ab. Der Prototyp prüft das und meldet einen Fehler.
- `ddgs` verschluckt Sperren: Bei HTTP ≠ 200 meldet die Bibliothek nur „No results found“. Der Prototyp schneidet deshalb den HTTP-Status mit und erkennt die Sperre trotzdem.
- `ddgs:startpage` und `ddgs:mojeek` sind wegen des Proof-of-Work-Captchas aus den Fallbacks entfernt. Mit `-s ddgs:startpage` sind sie weiterhin explizit nutzbar.

### 2.4 MCP-Server (fertig nutzbar)

| MCP | Nutzen |
|---|---|
| **`websearch` (dieses Repo)** | parallele Suche über alle obigen Quellen, Dedup, Ranking, Sperrverwaltung; Tools `web_search`, `news_search`, `search_many`, `fetch_url` (Seite als Markdown, SSRF-Schutz, robots.txt), `source_status`; Ein-/Ausgabe-Schemas |
| `mcp-searxng` (ihor-sokoliuk, `npx -y mcp-searxng`) | schlanker Zugriff nur auf SearXNG; Tools `searxng_web_search` und `web_url_read` (URL als Text/Markdown lesen). Gute Alternative, wenn nur SearXNG gebraucht wird. |
| Fetch-MCP (offizieller Referenz-Server, Python: `uvx mcp-server-fetch`) | Seiteninhalt als Markdown abrufen, also der Schritt nach der Suche |
| Playwright-MCP | Seiten mit JavaScript bzw. interaktive Seiten |
| Brave-Search-MCP (offiziell, `@brave/brave-search-mcp-server`) | nur mit API-Key, d.h. Guthaben und Kreditkarte |
| Exa-MCP (gehostet) | laut Exa-Changelog ohne Key nutzbar (3 QPS, 150 Aufrufe/Tag), mit Key volles Kontingent; zusätzliche Recherchequelle für Agents |
| You.com-MCP (`https://api.you.com/mcp`, Gratis-Profil `?profile=free`) | Websuche, Seitenabruf und Recherche; laut You.com-Doku ist die Live-Websuche im Gratis-Profil kostenlos |

Für Claude Code liegt eine `.mcp.json` im Repo. Der relative Pfad `.venv/bin/python` funktioniert, weil Claude Code den Server im Projektverzeichnis startet (geprüft). Projekt-Server muss man beim ersten Start von `claude` einmal freigeben.

---

## 3. Warum Captchas entstehen und wie man sie verhindert

Suchmaschinen bewerten jede Anfrage nach diesen Signalen (wichtigste zuerst):

1. **IP-Reputation.** Rechenzentrums- und Cloud-IPs (AWS, Hetzner, Cloud-Entwicklungsumgebungen) gelten von vornherein als verdächtig. **Das ist der größte Einzelfaktor** (siehe Messung unten).
2. **Rate pro IP und pro Engine:** Spitzen, gleichmäßige Maschinentakte, viele Anfragen hintereinander.
3. **Client-Fingerprint:** TLS/JA3, HTTP/2-Einstellungen, Header-Reihenfolge, fehlende Cookies und fehlendes JavaScript.
4. **Aktive Abwehr:** Proof-of-Work- und JavaScript-Challenges (Startpage, Mojeek, Cloudflare). Dagegen hilft weder eine saubere IP noch ein gutes Tempo, nur der Verzicht auf diese Engines.
5. **Muster:** identische Anfragen, ungewöhnliche Operatoren in Massen, keine „menschlichen“ Pausen.

**Maßnahmen, im Prototyp umgesetzt:**

| Maßnahme | Wo |
|---|---|
| **Hostweite Drossel pro Quelle:** Die Zeit-Slots werden über SQLite vergeben. Monitor, MCP-Server und CLI auf demselben Host teilen sich deshalb z.B. GDELTs 1 Anfrage pro 5 s. Dazu kommt eine maximale Parallelität pro Quelle. | `core.RateLimiter`, `StateStore.claim_slot`, `Provider.min_interval` |
| **Circuit-Breaker:** Nach Captcha, 429, 403 oder 402 (Guthaben leer) wird die Quelle pausiert, mit Verdopplung bis max. 6 h (Startwert je Quelle: meist 5 min, ddgs 10 min, GDELT 15 min, Marginalia 2 min). Der Zustand liegt in SQLite und gilt für **alle Prozesse**. Das Lesen, Ändern und Schreiben passiert atomar (`BEGIN IMMEDIATE`). | `core.CircuitBreaker`, `StateStore.update_breaker` |
| **Keine Eskalation:** Melden bereits laufende oder wartende Anfragen dieselbe Sperre, verlängert das die Pause nicht. Wer in der Warteschlange stand, prüft nach dem Warten erneut und fragt eine inzwischen gesperrte Quelle gar nicht erst an. | `CircuitBreaker.trip`, `engine._query_source` |
| **Gleichmäßige Last im Monitoring:** höchstens `WEBSEARCH_MONITOR_CONCURRENCY` Watches gleichzeitig (Standard 2), gestaffelter Start im Dauerbetrieb und ±10 % Zufall auf jedes Intervall – kein Maschinentakt, keine Lastspitze beim Start. | `monitor.run_monitor` |
| **Nur echte Ausfälle zählen:** bewusst übersprungene (`skipped`, z.B. GDELT bei Kurzwörtern), gedrosselte (`rate_limited`) oder nicht konfigurierte Quellen lösen weder Bezahlabfragen noch „blind“-Warnungen aus. | `core.NON_FAILURE_STATES` |
| **Stille Sperren erkennen:** Nur bei *gescrapten* Quellen (`ddgs:*`, `bing_rss`). Nach 3 leeren Antworten, während andere Quellen Treffer hatten, wird die Quelle pausiert. Offizielle APIs (HN, GDELT, Wikipedia) dürfen legitim leer sein. | `Provider.scraped`, `CircuitBreaker.note_empty` |
| **Versteckte `ddgs`-Sperren:** Der HTTP-Status der Engine wird mitgeschnitten. 403, 429, DDG-202 oder eine Captcha-Seite gelten als Sperre, auch wenn `ddgs` nur „No results“ meldet. | `providers.DDGS` |
| **`ddgs`-Threads:** Ein Thread lässt sich nicht abbrechen. Deshalb bleibt der Platz in der Warteschlange belegt, bis der Thread wirklich fertig ist. Nach einer Deadline laufen so nie zwei Anfragen parallel gegen dieselbe Engine. | `providers.DDGS._inflight` |
| **Keine Fehlalarme:** Sperrseiten-Marker (z.B. „captcha“) werden nur in HTML-Antworten gesucht. Treffer in JSON oder RSS, die das Wort enthalten, lösen keine Pause aus. | `providers._check_http` |
| **Jede Engine nur einmal pro Anfrage:** Google News und Bing News werden nicht doppelt gefragt (über SearXNG und RSS). Fallback-Quellen überspringen Engines, die SearXNG gerade als gesperrt meldet. | `searxng/settings.yml`, `Provider.same_engine`, `engine.search` |
| **Cache pro Suchart:** News 20 min, Web 6 h, Nachschlagen 24 h; beim Monitoring 90 % des Intervalls. **Leere Antworten werden nicht gecacht**, weil sie eine stille Sperre sein können (Ausnahme: Bezahlquellen – sonst würde eine seltene Suche bei jedem Lauf erneut bezahlt). Alte Einträge (über 7 Tage) werden automatisch gelöscht. | `engine.DEFAULT_MAX_AGE`, `core.StateStore` |
| **Gestuftes Vorgehen:** kostenlos primär → kostenlose Fallbacks → höchstens *eine* erfolgreiche Bezahl-API mit harter Tages-/Monatsgrenze (siehe Abschnitt 5) | `engine.DEFAULT_POLICY` (`TierPolicy`) |
| Merge per Reciprocal Rank Fusion; jede Quelle zählt pro Treffer nur einmal. Bei News werden gleiche Schlagzeilen zusammengeführt, dabei wird die direkte Artikel-URL statt der Google-News-Weiterleitung bevorzugt. | `core.merge_rrf` |
| Gesamt-Deadline 20 s pro Suche: langsame Quellen blockieren die Antwort nicht, spätere Stufen bekommen nur die Restzeit | `engine.search(deadline=20)` |
| Offizielle Feeds bevorzugen, wo es sie gibt (News) | `GoogleNewsRSS`, `BingRSS`, `GDELT`, `HackerNews` |
| GDELT: Wörter unter 3 Zeichen (z.B. „EU“, „AI“) werden vor der Anfrage entfernt, sonst lehnt GDELT die ganze Suche ab | `GDELT.build_query` |
| SearXNG: lange Sperrzeiten, Limiter aus (interner Dienst), Timeouts, bewusste Engine-Auswahl | `searxng/settings.yml` |

**Maßnahmen bei der Infrastruktur (empfohlen):**

- **SearXNG auf einem Rechner mit Büro- oder Heim-IP betreiben** (z.B. Mini-PC oder Server im Büro), nicht in der Cloud. Das kostet nichts extra und bringt am meisten.
- **IPv6:** Hat der Anschluss ein /64-Netz, kann SearXNG pro Anfrage eine zufällige Quelladresse wählen (`outgoing.source_ips`, in der `settings.yml` vorbereitet). Engines, die IPv6 nach ganzen Präfixen bewerten, lassen sich dadurch nur begrenzt täuschen.
- **Tor** ist als letzter Ausweg konfigurierbar (`outgoing.proxies`), ist aber langsam, und Google, Bing und DuckDuckGo blocken Tor-Ausgänge häufig. Nicht als Hauptweg einplanen.
- **Kostenlose öffentliche Proxy-Listen: nicht verwenden.** Sie sind unzuverlässig und meist längst gesperrt, und es ist unklar, wer den Verkehr mitliest. Seriöse, günstige Proxys als Plan B: siehe Abschnitt 3.1.
- Öffentliche SearXNG-Instanzen (searx.space) **nicht automatisiert anzapfen**. Die haben selbst Limiter, und das ist unfair gegenüber den Betreibern.

### 3.1 Günstige Proxys als Plan B

Wenn keine Büro- oder Heim-IP verfügbar ist (z.B. Betrieb nur in der Cloud) oder einzelne Engines wie Google trotz Drossel dauerhaft sperren, sind kommerzielle Proxys der übliche Plan B. Sie **verringern** Captchas, sie beseitigen sie nicht: Die Engines bewerten weiterhin Tempo, Fingerprint und Muster (siehe oben), und gute Proxy-IPs werden von anderen Kunden mitbenutzt und „verbrannt“.

Preise Stand 05.11.2026 (✓ = auf der Anbieterseite geprüft, (D) = nur Dritt-Quelle):

| Typ | Beispiele und Preis | Wirkung gegen Captchas (Google / DDG / Brave) | Risiken |
|---|---|---|---|
| Datacenter, gratis | Webshare Free: 10 geteilte Proxys, 1 GB/Monat, ohne Karte ✓ | gering: Rechenzentrums-IPs, oft schon geblockt; für Bing/Qwant manchmal ausreichend, für Google kaum | geteilte IPs mit unbekannter Vorgeschichte; Anbieter sieht den Verkehr |
| Datacenter, bezahlt (pro IP) | Webshare 100 geteilte Proxys 2,99 $/Monat ✓; IPRoyal ab 1,39 $/IP ✓; Decodo ab 0,02 $/IP ✓; DataImpulse 0,50 $/GB ✓ | gering bis mittel: mehr IPs = weniger Anfragen pro IP, aber dieselbe schlechte IP-Klasse | wie oben |
| ISP / „statisch residential“ (pro IP) | Webshare ab 6 $/Monat für 20 Proxys ✓; IPRoyal ab 1,80 $/IP ✓; Decodo ab 0,27 $/IP ✓ | mittel bis gut: IPs von Internetprovidern, aber in Rechenzentren gehostet; feste IPs, also weiter sauber drosseln | vergleichsweise sauber beschafft (keine Endgeräte); Anbieter sieht den Verkehr |
| Residential, rotierend (pro GB) | DataImpulse 1 $/GB (Pay-as-you-go, verfällt nicht) ✓; Webshare ab 3,50 $/GB ✓; Decodo 4 $/GB PAYG bzw. ab 2 $/GB im Abo ✓; IPRoyal 7 $/GB (1 GB) bis 1,75 $/GB ab 10 TB ✓ | gut: Anfragen kommen von Privatanschlüssen | **ethisch und rechtlich heikel:** die IPs gehören fremden Menschen, deren Geräte über SDKs in Gratis-Apps, Smart-TVs usw. zu Ausgangsknoten werden; die Einwilligung ist oft fragwürdig oder fehlt. 2026 hat Google das IPIDEA-Netz (über 600 Android-Apps) juristisch zerschlagen, und Recherchen zu SDKs in Smart-TV-Apps haben gezeigt, dass Nutzer kaum verstehen, worin sie eingewilligt haben (D). Unter 1 $/GB sind laut Branchenvergleichen oft umetikettierte Datacenter-IPs (D). |

**Grundsätzlich für alle Proxys:**
- **Nutzungsbedingungen:** Proxys ändern nichts daran, dass z.B. Google automatisierte Anfragen untersagt (Abschnitt 8). Wer Proxys gezielt zum Umgehen von Sperren einsetzt, handelt den Bedingungen noch deutlicher zuwider.
- **Datenschutz:** Der Proxy-Betreiber sieht Ziel, Zeitpunkt und bei HTTP auch Inhalte; Suchbegriffe können vertraulich sein. HTTPS schützt den Inhalt, nicht die Ziel-Domain. Anbieter mit Sitz und Verarbeitung in der EU bzw. AV-Vertrag bevorzugen.
- **Herkunft prüfen:** Bei residential Proxys nur Anbieter, die die Beschaffung offenlegen (eigene Apps mit klarer Einwilligung, Vergütung der Teilnehmer) – Marketingangaben wie „100 % ethisch“ allein reichen nicht.

**Was kosten ca. 5 $ im Monat?** Eine Ergebnisseite kostet über den Proxy grob 50–150 KB Verkehr (komprimierte HTML-Seite plus TLS; gemessen in der Testumgebung: Bing-Ergebnisseite 128 KB unkomprimiert, DuckDuckGo-HTML 14 KB). Mit ca. 100 KB pro Anfrage:

| Budget / Angebot | Volumen | Anfragen pro Monat |
|---|---|---|
| Webshare Free (1 GB) | 1 GB | ca. 10.000 |
| DataImpulse residential, 5 $ | 5 GB | ca. 50.000 |
| Webshare residential, 5 $ (≈ 1,4 GB) | 1,4 GB | ca. 14.000 |

Wichtig: Durch den Proxy laufen nur die **gescrapten** Anfragen, und auch nur für die Engines, die man darauf leitet. Bei 100–2.000 Suchen pro Tag (3.000–60.000 pro Monat) und einem Proxy **nur für Google** (eine Google-Anfrage pro Suche, abzüglich Cache-Treffern) reichen 5 $ bei DataImpulse für ca. 50.000 Google-Anfragen im Monat – also für das obere Ende knapp, für das untere Ende reichlich. Offizielle Feeds (Google-News-RSS, GDELT, HN, Wikipedia) brauchen keinen Proxy.

**Einbinden:**
- **SearXNG** (`searxng/settings.yml`): global über `outgoing.proxies` oder – empfohlen – **nur für die blockierende Engine**, z.B. Google:
  ```yaml
  engines:
    - name: google
      disabled: false
      proxies:
        all://:
          - http://BENUTZER:PASSWORT@proxy.example:8080
  ```
- **`ddgs`-Quellen:** Umgebungsvariable `DDGS_PROXY` (wertet `ddgs` selbst aus).
- **Eigene httpx-Quellen und `fetch_url`:** `WEBSEARCH_PROXY` (gilt für alle direkt vom Tool angefragten Dienste; offizielle APIs brauchen ihn normalerweise nicht).

**Empfehlung:** Zuerst eine Büro- oder Heim-IP nutzen (kostet nichts, wirkt am meisten). Erst wenn dort eine Engine dauerhaft sperrt, einen Proxy **nur für diese Engine** einsetzen, bevorzugt ISP-Proxys oder einen residential Anbieter mit nachvollziehbarer Herkunft, mit festem Budget (Pay-as-you-go ohne Abo, z.B. 5 $ Guthaben) – und die Sperrstatistik in `http://localhost:8888/stats` beobachten. Für gelegentliche Lücken ist eine Bezahl-API (Abschnitt 7) meist die sauberere Alternative zum Proxy.

---

## 4. Messung von einer Rechenzentrums-IP (05.11.2026)

Bewusst ein „Worst Case“: Die Testumgebung lief hinter einer Rechenzentrums-IP. Ergebnisse direkter Anfragen:

| Quelle | Ergebnis |
|---|---|
| Bing (HTML, RSS, über SearXNG) | ✅ funktioniert zuverlässig |
| Brave (über SearXNG) | ✅ anfangs; nach Testlast „too many requests“ → von SearXNG automatisch pausiert |
| Google (über SearXNG) | ❌ „access denied“ |
| DuckDuckGo | ⚠️ schwankend: direkt (HTML/`ddgs`) HTTP 202 mit Anomalie-Captcha, über SearXNG teils erfolgreich |
| Mojeek direkt | ❌ JS-/Proof-of-Work-Challenge. **Das liegt nicht an der IP, sondern gilt überall** (upstream `inactive`). |
| Startpage über ddgs | ❌ 0 Treffer. **Proof-of-Work-Captcha, gilt ebenfalls überall** (upstream `inactive`). |
| Yahoo über ddgs | ⚠️ schwankend (zeitweise HTTP 500) |
| Google News RSS, Bing News RSS | ✅ |
| Hacker News API, Wikipedia API | ✅ |
| GDELT | ❌ 429. Die geteilte Rechenzentrums-IP war bereits über dem Limit (1 Anfrage / 5 s pro IP) |
| Marginalia (Key `public`) | ❌ „QPM Limit Exceeded“ bzw. 429. Der geteilte Key ist ausgeschöpft, daher eigenen Key beantragen |
| Reddit JSON | ❌ 403 ohne OAuth (deshalb nutzt das Tool die offizielle OAuth-API) |

**Ergebnis der Pipeline trotz dieser Sperren:** siehe Abschnitt 10, Teststand.

Von einer Büro- oder Heim-IP sind Google (über SearXNG) und DuckDuckGo normalerweise erreichbar. **Das ist hier nicht gemessen** und sollte im eigenen Betrieb als Erstes geprüft werden (`websearch status` bzw. `http://localhost:8888/stats`). Startpage und Mojeek bleiben auch dort hinter dem Proof-of-Work-Captcha.

---

## 5. Empfohlene Architektur

```
                   ┌───────────────── Server mit Büro-/Heim-IP (Docker, Podman oder bubblewrap) ────────────────┐
KI-Agents ──MCP──▶ │ websearch-mcp ─┐   (Token, /healthz, /metrics)                                             │
(Claude Code …)    │                ├─▶ SearchEngine (parallel, hostweite Drossel + Sperren in SQLite, RRF)     │
watches.json ─────▶│ websearch-     │   Stufe 1 (frei) ─▶ SearXNG ──▶ Google · Bing · Brave · DDG · Qwant ·      │
                   │ monitor ───────┘                │              Wikipedia · GitHub · arXiv · News-, Bild-,  │
                   │   └─▶ Webhook/Slack/Discord/    │              Video-Engines (YouTube …)                   │
                   │       Teams/Telegram/E-Mail     └▶ Feeds/APIs ─▶ Google-News-RSS · Bing-RSS · GDELT · HN ·  │
                   │                                                 Reddit (OAuth, optional)                  │
                   │                    Stufe 2 (frei) ─▶ Bing-RSS · ddgs (Yahoo · DDG · Brave) · Marginalia     │
                   │                    Stufe 3 (opt.) ─▶ Exa · Linkup · Tavily · Brave · Perplexity · Serper     │
                   │                                      (bis EINE antwortet; nie für Bilder/Videos)          │
                   └────────────────────────────────────────────────────────────────────────────────────────────┘
```

`websearch-monitor` ist kein Cron-Job, sondern eine **Dauerschleife** (`websearch monitor watches.json --loop`): Sie prüft alle 30 s, welche Watches laut `every_min` fällig sind.

**Stufen pro Suchart:**

| Suchart | Stufe 1: frei, immer | Stufe 2: frei, nur bei < min(n, max(3, n/2)) Treffern | Stufe 3: optional, bezahlt |
|---|---|---|---|
| `web` | `searxng`, `reddit`¹ | `bing_rss`, `ddgs:yahoo`, `ddgs:duckduckgo`, `ddgs:brave`, `marginalia` | der Reihe nach `exa`, `linkup`, `tavily`, `brave_api`, `perplexity`, `serper`, bis eine normal antwortet |
| `news` | `searxng` (News-Engines), `google_news`, `bing_rss`, `hackernews`, `gdelt`, `reddit`¹ | `ddgs:yahoo`, `ddgs:duckduckgo` | wie oben |
| `ref` | `wikipedia`, `searxng` | `bing_rss`, `ddgs:yahoo` | wie oben |
| `images`, `videos` | `searxng` (Bild-/Video-Engines) | `ddgs:duckduckgo` | – (nie bezahlt) |

¹ nur mit eigenen Reddit-Zugangsdaten (`REDDIT_CLIENT_ID`/`_SECRET`), sonst übersprungen.

**Zeitfilter (`days`):** Quellen mit eigenem Filter bekommen ihn mitgeschickt (SearXNG `time_range`, Google News `when:`,
GDELT `timespan`, Hacker News, ddgs `timelimit`, Reddit `t`, Bezahl-APIs). Danach prüft die Engine jedes Datum nach;
undatierte Treffer bleiben nur, wenn die Quelle genau auf diese Spanne gefiltert hat. Der Cache-Schlüssel richtet sich nach
dem tatsächlich gesendeten Fenster (z.B. teilen sich `days=3` und `days=7` bei SearXNG die Anfrage „week“), damit
verschiedene `days`-Werte keine zusätzlichen Anfragen an die Suchmaschinen auslösen. Mit `days` springt die Bezahlstufe nur
mit Quellen an, die selbst filtern.

- **Stufe 2** überspringt Quellen derselben Suchmaschine, die SearXNG gerade als gesperrt meldet. Meldet SearXNG z.B. `duckduckgo: CAPTCHA`, wird `ddgs:duckduckgo` nicht gefragt.
- **Stufe 3** läuft nur, wenn insgesamt weniger als min(n, 3) Treffer kamen **und** dabei echte Fehler oder Sperren auftraten (nicht bloß Tempo-Wartezeiten oder bewusst übersprungene Quellen). Meldet SearXNG einzelne gesperrte Engines (auf Rechenzentrums-IPs fast immer), während andere Engines normal leer antworten, gilt die Suche als selten und nicht als Ausfall – sonst würde jede seltene Suche bezahlt. Die Bezahlquellen werden der Reihe nach probiert (zuerst die mit Gratiskontingent), bis **eine** normal antwortet – auch mit 0 Treffern, denn eine zweite Bezahl-API findet einen seltenen Begriff auch nicht; leere Bezahl-Antworten werden gecacht. Eine Quelle mit falschem Key oder leerem Guthaben wird pausiert (1 h, verdoppelt bis 6 h) und übersprungen. Eine legitim seltene Suche wird durch Bezahlen nicht besser. Abschalten lässt sich die Stufe mit `--no-paid`, `paid=False` oder für MCP-Agents mit `WEBSEARCH_MCP_PAID=0`.
- **Harte Kostengrenze:** Bezahlabfragen werden hostweit in SQLite gezählt; ab `WEBSEARCH_PAID_DAILY_LIMIT` (Standard 100) bzw. `WEBSEARCH_PAID_MONTHLY_LIMIT` (Standard 1.000) wird die Stufe übersprungen. Den Zählerstand zeigt `websearch status`. Wichtig für den schlechten Tag: Sperrt SearXNG mehrere Engines für 24 h, würde sonst ein großer Teil der Suchen bezahlt.
- **Gesamtbudget 20 s** pro Suche. Spätere Stufen bekommen nur die verbleibende Zeit.

**Kapazität bei 100–2.000 Anfragen pro Tag:** 2.000 pro Tag sind im Schnitt ca. 1,4 Anfragen pro Minute.

- **Wichtig:** SearXNG verteilt eine Anfrage *nicht* auf einzelne Engines, sondern schickt sie an **alle** aktiven Engines der Kategorie. Ohne Cache sieht also jede Engine (Google, Bing, Brave, DDG, Qwant …) bis zu 1,4 Anfragen pro Minute.
- **Entlastung bringen:**
  - der Cache (Monitoring fragt dieselben Begriffe wiederholt; News 20 min, Web 6 h)
  - dass jede Engine pro Anfrage nur einmal gefragt wird
  - dass die Fallbacks nur selten einspringen
- Von einer sauberen IP ist dieses Tempo für Bing, Brave und Qwant erfahrungsgemäß unkritisch. **Google ist die empfindlichste Engine.** Fällt sie aus, liefern die übrigen weiter.
- Bei Lastspitzen von Agents (viele Teilfragen auf einmal) begrenzt `search_many` die gleichzeitigen Suchen (Standard 4), ohne die Takte pro Quelle zu überschreiten.

---

## 6. Captcha-Solver: Bewertung

| Option | Bewertung |
|---|---|
| Kostenlose lokale Solver (z.B. Audio-reCAPTCHA per Spracherkennung) | ❌ unzuverlässig, bricht bei jeder Änderung; die IP bleibt trotzdem markiert, und Google zeigt danach einfach mehr Captchas |
| Proof-of-Work-Captchas (Startpage, Mojeek) „durchrechnen“ | ❌ genau dafür gebaut, Automatisierung teuer zu machen (CPU-Last pro Anfrage); stattdessen auf diese Engines verzichten |
| FlareSolverr (Open Source, für Cloudflare-JS-Challenges) | ⚠️ höchstens für einzelne Zielseiten beim *Abrufen*, nicht für Suchmaschinen |
| Bezahl-Solver (2Captcha usw.) | ❌ ausgeschlossen (kostenpflichtig, und Captchas zu umgehen widerspricht den Nutzungsbedingungen) |

**Empfehlung:** Keinen Solver in die Pipeline einbauen. Ein Captcha ist ein Signal: „diese IP ist verbrannt, Pause machen“. Genau so behandelt es der Prototyp. Mit dem Quellen-Mix gehen dadurch keine Ergebnisse verloren, nur einzelne Engines fallen zeitweise weg. Captchas zu umgehen widerspricht außerdem ausdrücklich den Nutzungsbedingungen der Anbieter (siehe 8). Wer eine verlässliche Reserve braucht, nimmt eine günstige offizielle API (Abschnitt 7).

---

## 7. Günstige Bezahl-Optionen (optional)

Stand 05.11.2026. **✓** = auf der Anbieterseite bzw. in der Anbieter-Doku geprüft (Abruf am 05.11.2026), **(D)** = nur aus Dritt-Quellen.

| Anbieter | Gratis | Preis pro 1.000 Anfragen | Index / Art | Hinweis | Geprüft |
|---|---|---|---|---|---|
| **Perplexity Search API** | – (Prepaid, auch über AWS Marketplace) | **5 $**, mit `search_type: "fast"` **1 $** | eigener Index, Rohtreffer als JSON | bis zu 5 Suchanfragen pro Request = 1 Abrechnungseinheit; fehlerhafte, rate-limitierte und fehlgeschlagene Requests werden nicht berechnet; leere Antworten schon | ✓ |
| **Exa** | **10 $ Guthaben jeden Monat** (≈ 2.500 Lite-Suchen), ohne Zahlungsmittel; einmalig +10 $ Bonus | Lite **4 $**, Auto 7 $, Deep 12 $; je Treffer über 10: +1 $/1k | eigener, semantischer Index | Exa-MCP zusätzlich ohne Key nutzbar (3 QPS, 150 Aufrufe/Tag) | ✓ |
| **Linkup** | monatliches Auffüllen auf **20 $** für berechtigte Konten (Geschäfts-Mail) ≈ 4.000 Suchen | **5 $** (fast/standard), deep 50 $ | eigener Index, für Agents | seit Mai 2026 in USD | ✓ |
| **Brave Search API** | **5 $ Guthaben jeden Monat** (≈ 1.000 Anfragen) | 5 $ | eigener Index | kein Abo, aber **Kreditkarte nötig**: nach dem Freiguthaben wird nutzungsabhängig weiter abgerechnet (Ausgabelimit im Dashboard setzen) | ✓ |
| **You.com Web Search API** | MCP-Server mit Gratis-Profil (`?profile=free`); Startguthaben laut (D) 100 $ | 5 $ (bis 100 Treffer pro Aufruf); Seiteninhalt +1 $/1k | eigener Index | | ✓ Preis / (D) Startguthaben |
| **Serper** | 2.500 Anfragen einmalig, ohne Karte | 1 $ (kleinstes Paket 50 $ = 50.000), größere Pakete bis 0,30 $ | **Google**-Ergebnisse | Pakete verfallen laut (D) nach 6 Monaten; mehr als 10 Treffer pro Anfrage kosten 2 Credits | ✓ Preis / (D) Verfall |
| **DataForSEO** | 1 $ Startguthaben | **0,60 $** in der Warteschlange (asynchron, Minuten), live 2 $ | Google, Bing, Yahoo u.a. | 50 $ Mindesteinzahlung; Guthaben verfällt laut (D) nicht | ✓ |
| **Mojeek API** | Gratisguthaben im Startup-Plan, Karte nötig | 2 £ (Startup), 3 £ (Business) | eigener, unabhängiger Index (UK) | Caching im Startup-Plan nur 1 h erlaubt | ✓ |
| **Tavily** | **1.000 Credits pro Monat**, ohne Karte | 8 $ (0,008 $ pro Credit; „advanced“ = 2 Credits) | für LLMs aufbereitet, inkl. Seiteninhalt | teuer jenseits des Gratiskontingents | ✓ |
| **Firecrawl** | 1.000 Credits pro Monat ≈ **500 Suchen**, ohne Karte | Suche = 2 Credits pro 10 Treffer; Hobby-Plan ab ca. 16 $/Monat (D) | Suche + Scraping | eher zum Abrufen von Seiten | ✓ Free-Plan / (D) Preise |
| **Jina** (`s.jina.ai`) | Gratis-Key mit Startguthaben, laut (D) 10 Mio. Tokens ≈ 1.000 Suchen, nicht-kommerziell | Token-basiert, **≥ 10.000 Tokens pro Suche** | Suche + Reader | `r.jina.ai` (Seite → Markdown) ist der eigentliche Mehrwert | ✓ Tokens/Suche / (D) Gratismenge |
| **SerpApi** | **250 Suchen pro Monat** | 25 $/Monat für 1.000 Suchen (= 25 $/1k), 75 $ für 5.000 | Google u.v.m. | Abo | ✓ |
| **Kagi Search API** | – | **12 $** | eigener Index + Mix | Abrechnung alle 30 Tage oder ab 100 $ | ✓ |
| Yandex Search API | 1.000 Nacht-Anfragen pro Monat (nur API v1) | ca. 480 ₽ (≈ 4 $) synchron, 25 ₽ verzögert | eigener Index (RU) | nur für russischsprachige Recherche | (D) (Yandex-Cloud-Doku) |

**Einordnung:** Bei 100–2.000 Anfragen pro Tag (3.000–60.000 pro Monat) reichen ca. 5 $ im Monat **nicht als Hauptquelle**. 60.000 Anfragen kosten selbst mit Perplexity Fast Search ca. 60 $, bei Brave ca. 300 $. **Als Notreserve reicht es aber gut.** Die kostenlose Pipeline bleibt das Rückgrat. Eine Bezahl-API springt nur ein, wenn die freien Quellen wegen Sperren fast nichts liefern (Stufe 3, Abschnitt 5). Zusammen mit dem Cache sind das erfahrungsgemäß wenige Prozent der Anfragen.

**Was es ganz ohne Geld zusätzlich gibt** (Gratiskontingente pro Monat, Stand Nov. 2026):
- Exa: ca. 2.500 Lite-Suchen, ohne Zahlungsmittel
- Linkup: ca. 4.000 Suchen, für berechtigte Konten
- Tavily: 1.000 Credits
- Brave: ca. 1.000 Anfragen, mit Kreditkarte
- Firecrawl: ca. 500 Suchen
- SerpApi: 250 Google-Suchen

Zusammen sind das ca. **9.250 Reserve-Anfragen im Monat für 0 $** (davon ca. 8.500 über die im Prototyp integrierten Quellen Exa, Linkup, Tavily und Brave). Für eine reine Notreserve reicht das in der Regel.

**Empfehlung (neu gewichtet):**
1. **Erst die Gratiskontingente:** **Exa** (10 $/Monat, ohne Karte) und **Linkup** (20 $/Monat für berechtigte Konten) decken zusammen ca. 6.500 Reserve-Suchen pro Monat ab, ohne Kosten.
2. **Brave** dazu, wenn eine Kreditkarte hinterlegt werden darf: ca. 1.000 Anfragen pro Monat gratis, eigener Index.
3. **Wenn wirklich ca. 5 $ im Monat ausgegeben werden sollen:** **Perplexity Fast Search** (1 $/1k) bringt dafür ca. 5.000 Anfragen. Das ist das beste Preis-Leistungs-Verhältnis unter den sofort nutzbaren APIs.
4. **Speziell Google-Ergebnisse:** **Serper**, also 2.500 gratis und danach 1 $/1k. Das kleinste Paket kostet allerdings 50 $, was bei 6 Monaten Laufzeit ca. 8 $ pro Monat entspricht.
5. Nicht empfohlen für dieses Volumen: Kagi (12 $/1k) und SerpApi (25 $/1k), weil zu teuer. DataForSEO ist zwar am billigsten, liefert aber asynchron und ist damit für Agents ungeeignet.

**Im Prototyp integriert:** `exa`, `linkup`, `tavily`, `brave_api`, `perplexity` und `serper`, jeweils nur aktiv mit `EXA_API_KEY`, `LINKUP_API_KEY`, `TAVILY_API_KEY`, `BRAVE_API_KEY`, `PERPLEXITY_API_KEY` bzw. `SERPER_API_KEY`.
- **Kostenbewusst voreingestellt:** Exa im Modus `fast` (Lite-Tarif), Perplexity im Modus `fast` (1 $/1k), umschaltbar über `EXA_SEARCH_TYPE` / `PERPLEXITY_SEARCH_TYPE`. Bezahlquellen werden immer mit höchstens 10 Treffern gefragt, auch wenn der Monitor intern einen größeren Pool sucht (zusätzliche Treffer kosten bei Exa extra).
- Die Bezahlstufe ist in Abschnitt 5 beschrieben. Bezahlquellen springen nur als letzte Reserve ein, in dieser Reihenfolge: erst die mit monatlichem Gratiskontingent (Exa → Linkup → Tavily → Brave), dann die günstigsten (Perplexity → Serper). Probiert wird, bis **eine** liefert.
- **Brave steht wegen des monatlichen 5-$-Freiguthabens vor Perplexity.** Ist das Guthaben verbraucht, belastet Brave die hinterlegte Karte mit 5 $/1k weiter, ohne mit HTTP 402 abzubrechen – dann ist Perplexity Fast (1 $/1k) fünfmal günstiger. Wer kein Brave-Freiguthaben nutzt, sollte `perplexity` vor `brave_api` setzen (eigene `TierPolicy` bzw. `DEFAULT_POLICY` in `websearch/engine.py`) oder `BRAVE_API_KEY` weglassen.
- **Kosten deckeln:** Zusätzlich zu den Grenzen im Tool (`WEBSEARCH_PAID_DAILY_LIMIT`, Standard 100, und `WEBSEARCH_PAID_MONTHLY_LIMIT`, Standard 1.000 Bezahlabfragen) in den Dashboards der Anbieter ein Ausgabelimit setzen – vor allem bei Brave und Linkup, die nach dem Gratiskontingent automatisch weiter abrechnen.
- Ein aufgebrauchtes Guthaben wird erkannt: HTTP 402, bei Serper HTTP 400 mit „credits“, bei Tavily 432/433 (Codes laut Tavily-Doku, mangels Key nicht live geprüft). Die Quelle wird dann pausiert.
- Nicht integriert: You.com, DataForSEO und die Mojeek-API. DataForSEO liefert in der günstigen Variante asynchron, was für Agents ungeeignet ist.

---

## 8. Recht und Nutzungsbedingungen (keine Rechtsberatung)

- **Google** untersagt in den AGB automatisierte Anfragen. Das Scraping über SearXNG ist verbreitet, aber vertraglich nicht gedeckt. Risiko: Sperren, kein Rechtsanspruch.
- **Bing-RSS:** laut Feed nur für privaten, nicht-kommerziellen Gebrauch. Bei kommerzieller Nutzung `WEBSEARCH_DISABLE=bing_rss` setzen.
- **Marginalia:** Gratis-Key nur nicht-kommerziell (CC-BY-NC-SA).
- **GDELT, Wikimedia:** frei, aber mit Tempo- und User-Agent-Regeln. Im Prototyp eingehalten; Kontakt in `WEBSEARCH_UA` eintragen.
- **Reddit:** offizielle Data API mit eigener (kostenloser) OAuth-App; es gelten die Reddit-Data-API-Bedingungen (u.a. keine Weitergabe/Speicherung über den Zweck hinaus, gelöschte Inhalte nicht weiter vorhalten). Kommerzielle Nutzung braucht eine Vereinbarung mit Reddit.
- **Google-News-Links auflösen** (`fetch_url`): Googles robots.txt sperrt `/rss/articles` und `/_/` für Crawler. Das Tool löst nur einzelne, vom Nutzer bzw. Agenten angefragte Links auf (wie ein Klick im Browser), nie während der Suche, gedrosselt und gecacht. Wer das nicht möchte: `WEBSEARCH_GNEWS_DECODE=0`.
- **Bezahl-APIs** (Exa, Linkup, Tavily, Brave, Perplexity, Serper): Es gelten die jeweiligen API-Bedingungen, z.B. zu Speicherung und Weiterverwendung der Ergebnisse. Vor produktiver Nutzung lesen.
- **Gespeicherte Treffer** (Monitoring-DB) können personenbezogene Daten enthalten. Daran denken: DSGVO, Löschfristen.

---

## 9. Prototyp in diesem Repo

```
docker-compose.yml        SearXNG + MCP-Server (HTTP) + Monitoring-Dienst, alle mit Healthcheck und Log-Rotation
Dockerfile                Image für websearch (läuft ohne Root, OCI-Labels)
.github/workflows/        CI (pytest 3.10-3.13, mypy, Paket-Build) und Release (PyPI + Multi-Arch-Image auf ghcr.io)
searxng/settings.yml      SearXNG-Konfiguration (JSON an, Engine-Auswahl inkl. Bild-/Video-Engines, lange Sperrzeiten)
websearch/core.py         Datenmodell (Result inkl. Bild/Video-Feldern, SourceStatus), RateLimiter + CircuitBreaker
                          (hostweit via SQLite), StateStore inkl. Metrik-Zählern, Normalisierung, RRF-Merge
websearch/providers.py    18 Quellen: SearXNG, ddgs (yahoo/duckduckgo/brave/startpage/mojeek), Bing-RSS, Wikipedia,
                          Marginalia, Google-News-RSS, Hacker News, GDELT; optional Exa, Linkup, Tavily, Brave, Perplexity,
                          Serper - jeweils mit nativem Zeitfilter, wo die Quelle einen hat
websearch/reddit.py       Reddit (offizielle API, OAuth client_credentials, Rate-Limit-Header)
websearch/engine.py       SearchEngine: TierPolicy (primary -> fallback -> paid), Sucharten web/news/ref/images/videos,
                          Zeitfilter (days), Zeitbudget, Cache je Suchart, Fehlerisolation pro Quelle, search_many, fetch
websearch/fetch.py        Seitenabruf -> Markdown (trafilatura), SSRF-Schutz, robots.txt, Tempo pro Host
websearch/gnews.py        Google-News-Links auflösen (offline bzw. batchexecute), gedrosselt, gecacht, Sperr-Erkennung
websearch/monitor.py      Watches (strenge Prüfung, Hot-Reload), nur neue Treffer, Blind-Warnung, Heartbeat
websearch/notify.py       Benachrichtigung: json | text/ntfy | slack | discord | teams | telegram | email; JSONL-Export
websearch/mcp_server.py   MCP-Tools mit Schemas: web_search, news_search, search_many, image_search, video_search,
                          fetch_url, source_status; Bearer-Token, /healthz, /metrics, DNS-Rebinding-Schutz
websearch/metrics.py      Prometheus-Metriken (hostweit)
websearch/doctor.py       'websearch doctor': Einrichtung prüfen, Lösungshinweise
websearch/wizard.py       'websearch init': .env mit Secret/Kontakt/Sprache, Watches, Docker-Prüfung
websearch/i18n.py         Meldungen Deutsch/Englisch (WEBSEARCH_LANG bzw. Systemsprache)
websearch/__main__.py     CLI: search, many, fetch, monitor, status, sources, doctor, init, metrics, mcp
                          (Exit-Codes 0/1/2/130)
deploy/podman/            Quadlet-Units (rootless Podman, systemd) + install.sh
deploy/bwrap/             websearch-bwrap / searxng-bwrap: Betrieb ohne Container in bubblewrap-Sandboxen
deploy/systemd/           systemd-User-Dienste für den bwrap-Betrieb + install.sh
tests/                    offline: test_core, test_providers, test_monitor, test_notify, test_fetch, test_fetch_pinning,
                          test_gnews, test_reddit, test_mcp, test_cli, test_features, test_setup_cmds, test_i18n,
                          test_deploy; live: mcp_smoke.py (stdio + streamable-HTTP)
watches.example.json      Beispiel-Watchlist · .env.example (englisch) / .env.example.de · .mcp.json für Claude Code
```

Bedienung, JSON-Format, alle Umgebungsvariablen und Fehlersuche: siehe [README.de.md](README.de.md) (deutsch) bzw. [README.md](README.md) (englisch).

**Betriebsarten:** Docker Compose (Standard), Podman (`podman compose` oder rootless als systemd-Dienste per Quadlet,
`deploy/podman/install.sh`) und ganz ohne Container in bubblewrap-Sandboxen (`deploy/bwrap/`, systemd-User-Dienste in
`deploy/systemd/`). Details: README, Abschnitt „Installation“.

**Docker-Hinweise:**
- Für `env_file` mit `required: false` braucht man Docker Compose ≥ 2.24.
- SearXNG läuft als UID 977 (`searxng`) statt root, ohne Capabilities, mit nur lesbarem Dateisystem und nur lesbar eingebundenem `./searxng` (bleibt auf dem Host normal editierbar; `settings.yml` muss für UID 977 lesbar sein, z.B. 0644). Ältere Versionen ließen das Image als root laufen, das `./searxng` an UID 977 übereignete – nach dem Update einmal `sudo chown -R "$USER" searxng`.
- Begrenzungen: `pids_limit` und `mem_limit` je Container; der MCP-Server startet im Container nur mit `MCP_AUTH_TOKEN` (oder bewusst `MCP_ALLOW_NO_AUTH=1`).
- Healthchecks: SearXNG über `/healthz` (mit dem Python im SearXNG-venv, weil das Image kein wget/curl enthält), MCP-Server über eine HTTP-Anfrage, Monitor über eine Heartbeat-Datei, die nach jedem Durchlauf geschrieben wird. MCP-Server und Monitor starten erst, wenn SearXNG gesund ist.

Tests: siehe Abschnitt 10, Teststand.

---

## 10. Teststand

Stand 06.11.2026, Testumgebung mit Rechenzentrums-IP, SearXNG master lokal mit granian wie im offiziellen Image, Konfiguration aus `searxng/settings.yml`. Der Code wurde in mehreren Runden von unabhängigen Reviewern (Architektur, Robustheit/Sicherheit, Bedienung) geprüft; die gefundenen Punkte sind umgesetzt und mit Tests abgesichert.

**Offline-Tests (`python -m pytest`): 677 grün, 2 übersprungen** (echte bwrap-Sandbox – in der Testumgebung fehlen User-Namespaces, siehe unten), unter Python 3.10 und 3.13 jeweils gegen das installierte Wheel. `mypy websearch` (mit `check_untyped_defs`): keine Fehler. Wheel und sdist bauen (`uv build`); der Installationsweg des Dockerfiles (gepinnte Abhängigkeiten aus `requirements.lock`, gepinntes Build-Backend aus `build-requirements.lock`, ohne Build-Isolation) ist in einer frischen Python-3.12-Umgebung geprüft.

| Testdatei | deckt ab |
|---|---|
| `test_core.py` | Normalisierung, RRF und News-Titel-Dedupe; Tempo, Slots, Circuit-Breaker (hostweit); Stufen inkl. Bezahlstufe; Deadline und Abbruch; Fehlerisolation; Lebenszyklus der Engine; Bezahl-Limits |
| `test_providers.py` | alle Quellen gegen nachgebaute Antworten: Sperr-Erkennung, keine Fehlalarme, versteckte `ddgs`-Sperren, Relevanzfilter, Tarifmodi |
| `test_features.py` | Zeitfilter (nativ/nachträglich, Fenster-Cache, undatierte Treffer, Bezahlstufe, alte Provider ohne `days`), Bild-/Videosuche (Mapping, Dedupe am Bild-URL, Felder zusammenführen), Metriken (gepuffert, `deadline`/`cancelled`, gesperrte Datei schaltet Tempo nicht ab), Sprache der CLI |
| `test_i18n.py` | jeder `t()`-Aufruf hat zwei Texte mit gleichen Platzhaltern; keine deutschen Texte außerhalb von `t()` und keine im englischen Text |
| `test_fetch.py`, `test_fetch_pinning.py` | SSRF-Schutz inkl. **DNS-Rebinding** (Verbindung geht nur an die geprüfte IP, auch nach jeder Weiterleitung; Gegenprobe: ohne Fixierung erreicht derselbe Abruf das interne Ziel), SNI/Host-Header bleiben, robots.txt, Zeit-/Größenlimits, begrenzte Extraktions-Threads |
| `test_gnews.py` | Google-News-Links: altes Format offline, neues per `batchexecute`, Cache inkl. Fehlschläge, Sperr-Erkennung, SSRF auf das Ziel |
| `test_reddit.py` | OAuth (Token-Cache, Erneuerung bei 401/403-Sperrseite), Mapping, NSFW, Zeitfenster, Rate-Limit-Header, Relevanzfilter |
| `test_notify.py` | alle 7 Formate, Kürzung, Escaping, Telegram `ok:false`, E-Mail (Fake-SMTP, kein Klartext-Passwort übers Netz), Schwärzen von Tokens, Konfigurationsprüfung |
| `test_monitor.py` | Basislinie, Pool, keine verlorenen Treffer, Warteschlange, Blind-/Recovered-Meldungen, Hot-Reload, Parallelitätsgrenze |
| `test_mcp.py` | alle 7 Tools, Schemas, Bearer-Token, `/healthz`/`/metrics`, Fail-closed-Regel (und dass `doctor` dieselbe Regel anwendet), keine WebSocket-Scopes |
| `test_setup_cmds.py` | `init` (Projektverzeichnis aus Unterordnern, Zeitstempel-Sicherungen, Rechte 0600, Kontakt ohne Shell-Sonderzeichen, Podman/Docker-Erkennung), `doctor` (Platzhalter beider Vorlagen, Zeitlimits gegen „Tröpfel“-Server, geschwärzte URLs, `--offline` ohne Suche) |
| `test_deploy.py` | bwrap-Wrapper: Geheimnisse nie in der Kommandozeile (`--args`-Pipe), `$HOME`/`/run` versteckt, nur das Datenverzeichnis schreibbar, kein Code aus dem Arbeitsverzeichnis, Suchbegriffe nie als Pfade, `.env*`/`.git` verdeckt, seccomp; SearXNG-Wrapper (Quellcode außerhalb des Datenverzeichnisses, gepinnter Commit); Podman-Quadlet (`install.sh`, Mindestversion, SearXNG bekommt nur sein Secret, gehärtete Container); systemd-Units (Escaping); `.env`-Parser identisch in Paket und Skripten; alle Umgebungsvariablen in beiden Vorlagen dokumentiert |
| `test_envfile.py` | `.env` lesen wie docker compose und die Shell (Anführungszeichen, Kommentare, `export`, `$VAR`/`${VAR:-x}`, gegen echtes bash geprüft), nie ausführen; `--env-file` |
| `test_cli.py` | Ausgabe, JSON, Exit-Codes, stdin, Status/Reset, Quellenliste |

**Live-Tests (gegen echte Dienste):**

| Test | Ergebnis |
|---|---|
| Web-, News-, Nachschlage-Suche | ✅ relevante Treffer in ca. 1–4 s; 10 Suchen parallel in < 5 s ohne Ausfälle |
| Bild- und Videosuche | ✅ Bilder mit direkter Bild-URL (Bing, Wikimedia Commons …), Videos mit Laufzeit (YouTube, Brave, DDG, Bing) |
| Zeitfilter `--days` | ✅ News der letzten 2–3 Tage, alle Treffer im Zeitraum |
| Google-News-Link lesen | ✅ in die Herausgeber-URL aufgelöst (u.a. kyivpost.com) und als Markdown gelesen |
| `websearch doctor` | ✅ alle Prüfungen ok (Laufzeit, Konfiguration, Kontakt, SearXNG, Test-Suchen), Hinweis auf Rechenzentrums-IP bei Engine-Fehlern; mit „Tröpfel“-Server nach ca. 10 s beendet |
| MCP stdio + streamable-HTTP | ✅ alle 7 Tools; mit Token: `/healthz` 200, `/metrics` und `/mcp` ohne Token 401, fremder Host 421 |
| Metriken | ✅ `websearch metrics` und `/metrics` zählen Suchen und Quellen hostweit |
| bwrap-Wrapper | ✅ das echte bwrap akzeptiert alle Optionen (scheitert hier erst beim Anlegen der Namespaces); über ein Test-Double laufen websearch und SearXNG (41 Treffer) mit genau dieser Sandbox-Kommandozeile |
| Podman-Quadlet | ✅ `install.sh` erzeugt Units, bereinigte `.env` (0600) und ruft `podman build`/`systemctl` korrekt auf (Podman selbst fehlt hier) |

**Härtung:** Container (compose und Quadlet) ohne Capabilities, mit `no-new-privileges` und nur lesbarem Dateisystem, SearXNG als UID 977 statt root mit nur lesbarer Konfiguration; SearXNG-Image und -Commit sowie alle GitHub-Actions (per SHA) und das Build-Backend sind auf geprüfte Versionen festgelegt.

**Nicht getestet:**
- **Container und Sandboxen wirklich starten:** Die Testumgebung hatte weder Docker noch Podman, und Namespaces waren gesperrt (auch bwrap konnte keine anlegen). Geprüft sind YAML/Units, Befehle, Dateirechte und der Paketbau; die echten Starts sollten im eigenen Betrieb als Erstes passieren.
- **Bezahl-APIs, Reddit, Discord/Teams/Telegram/SMTP live:** Es gibt keine Zugangsdaten. Formate und Fehlerfälle sind gegen nachgebaute Antworten nach Anbieter-Doku getestet.
- **Betrieb von einer Büro- oder Heim-IP.**

---

## 11. Empfohlene erste Schritte im eigenen Betrieb

1. Auf einem Rechner im Büro- oder Heimnetz `docker compose up -d` ausführen und eine Woche Probebetrieb machen. Dabei `/stats` von SearXNG und `websearch status` beobachten.
2. Einen eigenen Marginalia-Key per Mail beantragen und eine Kontaktadresse in `WEBSEARCH_UA` eintragen.
3. Die Takte (`min_interval`) anhand der eigenen Daten feinjustieren. Wird Google oft gesperrt, ggf. `SERPER_API_KEY` als Reserve setzen.
4. Für Reddit eine kostenlose „script“-App anlegen (README, Abschnitt „Quellen“) und prüfen, ob Reddit von der eigenen IP aus antwortet.
5. Prometheus `/metrics` einbinden und Alarme auf `websearch_source_blocked{source="searxng"}` und die Bezahl-Zähler setzen.
6. Wer noch die Google Custom Search JSON API nutzt: vor deren Ende am 01.01.2027 umstellen.

---

### Quellen

- Google CSE-Abschaltung: [Brave – Google API shutdown](https://brave.com/learn/google-api-shutdown/), [Firecrawl – Google Search JSON API alternative](https://www.firecrawl.dev/blog/google-search-json-api-alternative), [Apify – migration guide](https://blog.apify.com/google-custom-search-api-alternative/)
- Bing-API-Abschaltung: [Neowin](https://www.neowin.net/news/microsoft-pulls-plug-on-bing-search-apis/), [PPC Land](https://ppc.land/microsoft-ends-bing-search-apis-on-august-11-alternative-costs-40-483-more/)
- Brave-Preismodell: [Brave Search API](https://brave.com/search/api/), [Implicator – Brave drops free tier](https://www.implicator.ai/brave-drops-free-search-api-tier-puts-all-developers-on-metered-billing/)
- Weitere Bezahl-APIs: [Serper](https://serper.dev/), [DataForSEO SERP API Pricing](https://dataforseo.com/apis/serp-api/pricing), [Mojeek Web Search API](https://www.mojeek.com/services/search/web-search-api/), [Tavily Pricing](https://www.tavily.com/pricing), [Serper-Credits (apiserpent)](https://apiserpent.com/blog/serper-pricing-credits-explained), [Vergleich (DEV Community)](https://dev.to/team_metabees_4b5127db951/7-cheapest-web-search-apis-for-ai-agents-in-2026-ranked-76m), [Kagi API (CostBench)](https://costbench.com/software/ai-search-apis/kagi-search-api/)
- Weitere Such-APIs (Anbieterseiten, abgerufen 05.11.2026): [Perplexity API Pricing](https://docs.perplexity.ai/docs/pricing), [Exa Pricing](https://exa.ai/docs/reference/pricing), [Exa Pricing-Update](https://exa.ai/docs/changelog/pricing-update.md), [Linkup Pricing](https://www.linkup.so/pricing), [Linkup USD-Pricing](https://docs.linkup.so/pages/changelog/usd-pricing.md), [You.com Billing](https://you.com/docs/administration/billing.md), [Firecrawl Pricing](https://www.firecrawl.dev/pricing.md), [Jina Rate Limits](https://jina.ai/api-dashboard/rate-limit), [Kagi API Pricing](https://kagi.com/api/pricing), [SerpApi Pricing](https://serpapi.com/pricing), [Tavily Pricing](https://www.tavily.com/pricing)
- Dritt-Quellen zu Such-APIs: [Perplexity Fast Search (usagepricing)](https://www.usagepricing.com/blueprint/activity/perplexity-ai-2026-09-28-search-api-fast-search), [You.com Free Tier (usagepricing)](https://www.usagepricing.com/blueprint/activity/you-com-2026-07-29-web-search-free-tier), [Jina AI Pricing (markaicode)](https://markaicode.com/pricing/jina-ai-pricing/), [Firecrawl Pricing (tinyfish)](https://www.tinyfish.ai/blog/firecrawl-pricing), [Yandex Search API Pricing](https://yandex.cloud/en/docs/search-api/pricing), [Naver Search API (StarNion)](https://jikime.github.io/starnion/en/integrations/naver-search.html), [NAVER API HUB (Ncloud-Doku)](https://guide.ncloud-docs.com/docs/en/apihub-overview)
- SearXNG: [Settings: search / suspended_times](https://docs.searxng.org/admin/settings/settings_search.html), [Captcha-Fehler beheben (SSD Nodes)](https://www.ssdnodes.com/learn/fix-searxng-engine-captcha-errors), Engine-Status (`inactive`/`disabled`) aller Engines in Abschnitt 2.1 direkt aus `searx/settings.yml` des aktuellen SearXNG-Quellcodes (Commit 3e95f6c, 05.11.2026), [mcp-searxng](https://enterprisedna.co/directories/mcp/ihor-sokoliuk-mcp-searxng)
- Proxys (Anbieterseiten, abgerufen 05.11.2026, ✓): [Webshare Pricing](https://www.webshare.io/pricing), [DataImpulse](https://dataimpulse.com/), [IPRoyal Residential Pricing](https://iproyal.com/pricing/residential-proxies/), [Decodo Residential Pricing](https://decodo.com/proxies/residential-proxies/pricing); SearXNG-Doku zu Proxys pro Engine: `docs/admin/settings/settings_engines.rst` (Commit 3e95f6c)
- Proxys (Dritt-Quellen, (D)): [Cheapest residential proxies (trustmyip, Okt. 2026)](https://trustmyip.com/blog/cheapest-residential-proxies), [Residential proxy cost per GB (send.win)](https://blog.send.win/?p=10576), [Proxy pricing (AIMultiple)](https://aimultiple.com/de/proxy-pricing); Herkunft residential IPs: [The Hacker News – Smart-TV-SDKs als Proxy-Knoten (Juni 2026)](https://thehackernews.com/2026/06/free-apps-are-quietly-turning-smart-tvs.html), [Peakhour – Bandwidth-Sharing-Lieferkette](https://www.peakhour.io/blog/bandwidth-sharing-residential-proxy-supply-chain/), [Google gegen IPIDEA (Pulse)](https://www.pulse.bot/cybersecurity/news/google-targets-ipidea-in-crackdown-on-global-residential-proxy-networks-dc2ad6f2-81fb-47ae-9d64-dec650fd4932/), [Synthient – Popa-SDK](https://synthient.com/blog/popa-from-sourcing-to-distribution)
- Marginalia API: [about.marginalia-search.com/article/api](https://about.marginalia-search.com/article/api)
- GDELT: [Rate-Limiting-Blogpost](https://blog.gdeltproject.org/?p=15416), [py-gdelt REST-Doku](https://rbozydar.github.io/py-gdelt/user-guide/rest-apis)
- ddgs: [ddgs-Projektseite](https://awesome.ecosyste.ms/projects/github.com%2Fdeedy5%2Fddgs), Engine-Liste direkt aus `ddgs.engines.ENGINES` (v9.16.1) ausgelesen
