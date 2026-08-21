"""Replace the BYD models' bad source links (BYD's own site 403s to servers) with
real, fetchable spec-sheet links found via Tavily. Only touches BYD rows whose
source still points at en.byd.com; leaves already-good rows alone.
"""
import csv
import time

from app.ai import tavily_client
from app.database import SessionLocal
from app.models import Forklift

db = SessionLocal()
rows = [f for f in db.query(Forklift).filter_by(manufacturer="BYD").all()
        if (f.source_url or "").find("byd.com") != -1 or not f.source_url]
print(f"Re-fetching links for {len(rows)} BYD models...")

with open("byd_links_report.csv", "w", newline="", encoding="utf-8") as fh:
    w = csv.writer(fh)
    w.writerow(["model", "source_url", "pdf_url"])
    for i, f in enumerate(rows, 1):
        try:
            s = tavily_client.search_spec_sheet(f"BYD {f.model} forklift")
        except Exception as e:  # noqa: BLE001
            print(f"  [{i}/{len(rows)}] {f.model}: search error {e}")
            continue
        results = s.get("results", [])
        f.source_url = results[0].get("url") if results else None
        f.pdf_url = tavily_client.best_pdf_url(s)  # None if no real PDF found
        db.commit()
        w.writerow([f.model, f.source_url, f.pdf_url])
        print(f"  [{i}/{len(rows)}] {f.model}: src={'y' if f.source_url else '-'} pdf={'y' if f.pdf_url else '-'}")
        time.sleep(1)

db.close()
print("Done. Review byd_links_report.csv")
