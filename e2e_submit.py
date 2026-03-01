#!/usr/bin/env python3
"""E2E test: submit Case #6 and Case #7 to the SNAP-AI backend."""
import json, time, urllib.request, sys

BASE = "http://localhost:8000/api/v1"

def api(method, path, body=None, token=None):
    data = json.dumps(body).encode() if body else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(f"{BASE}{path}", data=data, method=method, headers=headers)
    try:
        resp = urllib.request.urlopen(req, timeout=30)
        return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return json.loads(e.read())

# 1. Login
print("=== Step 1: Login ===")
login = api("POST", "/auth/login", {"username": "admin", "password": "Pancreas123"})
token = login["data"]["token"]
print(f"Token acquired: {token[:40]}...")

# 2. Submit cases
CASES_TEXT = r"""Case #6
Diagnosen:
1. NET G2 des Ileums mit bilobulären Lebermetastasen, pT2, pN1 (3/21), pM1a, L1, V0, Pn0, G2, R0 (lokal) ED 07/2023, UICC Stadium IV
Zufallsbefund bei AFP-Elevation bei Echographie Leberbiopsie vom 23.08.2023: Gut differenzierter NET mit einem Proliferationsindex von 50 % G3. Diskrete Fibrose perizentrilobulär Histologie: Mitoserate: 3/10 HPF, Ki67 Proliferationsindex: 6-7%, SSTR2a: Positiv (100%, 3+)
2. Z. n. Kolonadenomen 2008: Tubuläres Adenom 2020: Tubuläres Adenom mit Dysplasien
3. Nebendiagnosen
Aortenklappenersatz 1998 nach Aortenklappendilatiation (valve carbomedics)
Osteoporose (behandelt mit Alendron und Calcium)
Prednisontherapie wegen Myalgien und Polyarthritis (zuletzt 1,25 mg)
Z. n. Appendektomie bei retrozökaler Appendizitis 2013
Kontrastmittelallergie (Primovist, Cudoviste (Urtikaria)
Operation: DaVinci assistierte erweiterte Ileozoekalresektion, Nachresektion mit erweiterter zentraler Lymphadenektomie, Ileoascendostomie seit-zu-seit, atypische Leberresektion Segment III, Leberbiopsie Seg. IVb, Pfortadertransektion rechts, Cholezystektomie
Histologie: Leber, Segment III (atypische Resektion): Lebermetastase eines neuroendokrinen Tumors. Tumorfreier Resektionsrand. Leber, Segment IV (atypische Resektion): Tumorfreies Leberparenchym. Ileum, Zökum, Lymphknoten (Resektion): Mässig differenzierter neuroendokriner Tumor des Ileums. Maximaler Tumordurchmesser 14 mm. Infiltration bis in die Tunica muscularis propria. Lymphgefässinvasion. Keine Perineuralscheideninfiltration. Tumorfreie Resektionsränder. Mitoserate: 3/10 HPF Ki67 Proliferationsindex: 6-7% SSTR2a: Positiv (100%, 3+) CDX2: Positiv Lymphknotenmetastasen des neuroendokrinen Tumors in 3/17 Lymphknoten (maximaler Durchmesser 5 mm; keine extranodale Ausbreitung). Immunhistochemie - Lymphknotenmetastase: Ki67 Proliferationsindex: 6-7%, SSTR2a: Positiv (100%, 3+) Übrige Dünndarmschleimhaut ohne pathologischen Befund. Tumorfreie Dickdarmschleimhaut ohne pathologischen Befund. Tumorfreie Gallenblase mit geringer chronisch-fibrosierender Cholezystitis. Keine Anhaltspunkte für Malignität. Nachresektat Mesenterium: Tumorfreies Fettgewebe mit vier tumorfreien Lymphknoten (0/4). TNM-Klassifikation: pT2, pN1 (3/21), pM1a, L1, V0, Pn0, G2, R0 (lokal)
Beurteilung und Verlauf:
Herr Novel war elektiv zur Hemihepatektomie rechts und Resektion des Primarius eingetreten. Leider zeigte sich im intraoperativen Ultraschall und anschliessend auch im histologischen Schnellschnitt ein Befall mit zumindest einer kleinen Metastase in Segment III. Aus diesem Grund entschieden wir uns für die Resektion des Primarius und Pfortadertransektion mit dem Ziel einer Zweizeitigen Leberresektion bei ausreichendem Restvolumen und ausbleibendem Befall linksseitig in der postoperativen Kontrolle (n. 6 Wochen). Der peri- und postoperative Verlauf gestaltete sich von chirurgischer Seite ohne Komplikationen. Der Kostaufbau wurde gut toleriert. Die Darmtätigkeit kam in Gang, wobei Herr Novel bei Austritt 3-4/Tag flüssigen Stuhlgang hatte. Die Wundverhältnisse waren allzeit reizlos und trocken. Der Patient klagte jedoch über eine ausgeprägte Schwäche, a.e. im Rahmen der Leberhypertrophie in Kombination mit der Konvaleszenz postoperativ. Bei o.g. Histologie wurde im interdisziplinären Tumorboard eine Nachkontrolle mit MRI in 3 Monaten Januar empfohlen; der Fall wurde zudem am NET-Board am 18.12.2023 vorgestellt, wobei die zusätzliche Therapie mit Sandostatin bis zur Resektion empfohlen wurde. Der Patient wurde ausführlich diesbezüglich informiert. Wir entlassen Herrn Novel in die häusliche Umgebung.
Procedere:
Entfernung des Nahtmaterials entfällt bei resorbierbarer Intrakutannaht
Die MRI Kontrolle der Leber wurde für Ende Januar 2024 am USB terminiert, ambulantes Aufgebot folgt, am Folgetag erfolgt die Besprechung in unserer Sprechstunde.
Bei Diarrhö empfehlen wir die Stuhlregulation durch Einnahme von Immodium.
Bei Resektion der Ileozökalklappe bitten wir um Kontroller der Vitamine (insb. Vitamin B12)/Spurenelement in 3 Monaten in Ihrer Praxis.
NET-Board Vorstellung am 18.12.2023: Eine Sandostatintherapie postoperativ wurde empfohlen (10mg Sandostatin LAR pro Woche, Steigerung bei guter Verträglichkeit).
Sintrom nach dem Austritt wieder starten, therapeutisch

Case #7
Diagnosen:
1. Multifokales mässig differenziertes HCC Rezidiv pT2 (m), L0, V0, G2, R0 (lokal)
- St. n. Anteriorer Sektorektomie, Cholezystektomie, Lymphknotensampling Ligamentum hepatoduodenale am 24.11.2021
- Aktuelle Histologie 11/23: B2023.49292: Zwei Herde eines mässig differenziertes hepatozellulären Karzinoms, teils vom steatohepatitischen Typ mit pseudoglandulärem Wachstumsmuster (Edmondson Grad II). Maximaler TumorDurchmesser: 12 mm.
2. Postoperativ Wundserom der Laparotomiewunde 20.11.2023
3. Aethyltoxische Leberzirrhose Child A ED 09/2021
4. Z. n. Prostata-Karzinom - Radikale Prostataoperation ca. 2005
5. Z. n. Kolonkarzinom ca. 20
Operation: Explorative Laparotomie, Adhäsiolyse (1h), atypische Segment II Resektion
Histologie vom 15.11.2023: 49292: Leber, Segment III (Exzision): Zwei Herde eines mässig differenzierten hepatozellulären Karzinoms, teils vom steatohepatitischen Typ mit pseudoglandulärem oder soliden Wachstumsmuster (Edmondson Grad II). Maximaler Tumor-Durchmesser: 12 mm. Kein Nachweis von Gefässinvasion. Exzision vollständig (minimaler Abstand zum Resektionsrand 5 mm). Übriges Leberparenchym mit porto-portalen Septen und fokal zirrhotischem Umbau. Nachweis von makroregenerativen Knoten mit fokalem Übergang in vollständig exzidierte dysplastische Knoten mit high-grade Dysplasie/frühes HCC. 49298: Omentum majus (Ektomie): Tumorfreies Fettgewebe. 1 tumorfreier Lymphknoten.
Beurteilung und Verlauf:
Es erfolgte der elektive Eintritt des Patienten zur explorativen Laparotomie und Vorgehen nach intraoperativem Befund. Bei dem Patienten wurde 2021 bei HCC eine posteriore Sektorektomie durchgeführt. Bei erhöhten Tumormarken (AFP) wurde nun eine MRI des Abdomens durchgeführt, welche mehrere Läsionen der Leber zeigte. Nach hepatologischer Abklärung zeigte sich ein zum MRI diskrepanter Befund mit einzig einem singulär beschriebenen Rezidiv im Bereich der Resektionsfläche, welches histologisch bestätigt wurde. Gemäss Tumorboardentscheid wurde die Indikation zur Exploration gestellt. Intraoperativ wurde der im MRI geäusserte Verdacht von multiplen Läsionen bestätigt, sodass nach ausgedehnter Adhäsiolyse lediglich zwei Herde in Seg. II atypisch reseziert und zur histologischen Analyse asserviert wurden. Bei multifokalem Geschehen wurde auf die Resektion des Rezidivbefundes im Seg. V verzichtet. Postoperativ wurde der Patient auf die Normalstation verlegt. Die Nahrungeinnahme wurde gut toleriert. Postoperativ kam es zu einer Wundheilungsstörung der Laparotomiewunde mit einem Wundserom, sodass eine Klammer entfernt wurde und ein Easy-Flow Beutel angelegt wurde. Bei erhöhtem Aszites postoperativ haben wir die Dosis des Aldactone erhöht. Wir können den Patienten in gutem Allgemeinzustand in die Rehabilitation Zurzach entlassen.
Procedere:
- Wir bitten um regelmässige Wundkontrollen in der hausärztlichen Sprechstunde. Die Klammerentfernung kann ab dem 14. postoperativen Tag bei gesicherter Wundheilung erfolgen.
- Offene Wundbehandlung und 2x tägliches Spülen im Bereich des Wundseroms
- Engmaschige Kontrolle der Elektrolyte bei erhöhter Dosis des Aldactone, je nach Aszites Evaluation der Dosis im Verlauf
- Bei Auftreten von Fieber, Schüttelfrost, Allgemeinzustandsverschlechterung oder lokalen Entzündungszeichen, ist die sofortige ärztliche Wiedervorstellung indiziert.
- Eine Vorstellung in der hepatologischen Sprechstunde zur Besprechung des onkologischen Procederes bei Prof. Heim ist nach der Rehabilitation geplant. Ein separates Aufgebot folgt.
"""

print("\n=== Step 2: Submit cases ===")
upload = api("POST", "/upload/text", {"text": CASES_TEXT}, token=token)
print(json.dumps(upload, indent=2))

if not upload.get("success"):
    print("UPLOAD FAILED — aborting.")
    sys.exit(1)

job_id = upload["data"]["job_id"]
case_count = upload["data"]["case_count"]
print(f"\nJob ID: {job_id}")
print(f"Case count: {case_count}")

# 3. Poll job status
print("\n=== Step 3: Polling job status ===")
for i in range(120):  # up to 20 minutes
    time.sleep(10)
    status = api("GET", f"/jobs/{job_id}", token=token)
    job_status = status["data"]["status"]
    cases = status["data"].get("cases", [])
    completed = sum(1 for c in cases if c["status"] == "completed")
    failed = sum(1 for c in cases if c["status"] == "failed")
    print(f"  [{i*10:>4}s] status={job_status}  completed={completed}  failed={failed}")
    if job_status in ("completed", "failed"):
        break
else:
    print("TIMEOUT after 20 minutes")
    sys.exit(1)

# 4. Fetch full results
print(f"\n=== Step 4: Full results (job_status={job_status}) ===")
results = api("GET", f"/jobs/{job_id}/results", token=token)
for case in results["data"]["results"]:
    print(f"\n--- {case.get('case_label', 'Case')} (#{case['case_number']}) ---")
    print(f"  Status: {case['status']}")
    print(f"  Verdict: {case.get('final_verdict')}")
    print(f"  CCI: {case.get('final_cci')}")
    print(f"  Duration: {case.get('total_duration_ms')}ms")
    print(f"  Error: {case.get('error_message')}")
    if case.get("layer1_output"):
        l1 = case["layer1_output"]
        print(f"  Layer1 keys: {list(l1.keys()) if isinstance(l1, dict) else 'raw'}")
    if case.get("layer2_output"):
        l2 = case["layer2_output"]
        print(f"  Layer2 keys: {list(l2.keys()) if isinstance(l2, dict) else 'raw'}")
    if case.get("layer3_output"):
        l3 = case["layer3_output"]
        print(f"  Layer3 keys: {list(l3.keys()) if isinstance(l3, dict) else 'raw'}")

print("\n=== E2E Test Complete ===")
