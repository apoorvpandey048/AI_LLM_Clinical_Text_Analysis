#!/usr/bin/env python3
"""E2E test: submit only Case #7 (HCC recurrence) to the SNAP-AI backend."""
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

# 2. Submit Case #7 only
CASE7_TEXT = r"""Case #7
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

print("\n=== Step 2: Submit Case #7 only ===")
upload = api("POST", "/upload/text", {"text": CASE7_TEXT}, token=token)
print(json.dumps(upload, indent=2))

if not upload.get("success"):
    print("UPLOAD FAILED — aborting.")
    sys.exit(1)

job_id = upload["data"]["job_id"]
case_count = upload["data"]["case_count"]
print(f"\nJob ID: {job_id}")
print(f"Case count: {case_count}")

# 3. Poll job status — 30 min max for single case
print("\n=== Step 3: Polling job status ===")
for i in range(180):
    time.sleep(10)
    status = api("GET", f"/jobs/{job_id}", token=token)
    job_status = status["data"]["status"]
    cases = status["data"].get("cases", [])
    completed = sum(1 for c in cases if c["status"] == "completed")
    failed = sum(1 for c in cases if c["status"] == "failed")
    print(f"  [{i*10+10:>4}s] status={job_status}  completed={completed}  failed={failed}")
    if job_status in ("completed", "failed"):
        break
else:
    print("TIMEOUT after 30 minutes")
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
