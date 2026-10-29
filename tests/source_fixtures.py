"""Clearly synthetic source rows for import failure and revision checks."""

import json

from ingestion.snapshots import FetchResult, SnapshotStore
from ingestion.sources import load_sources


def complaint(identifier=12345678, summary="SYNTHETIC COMPLAINT", event="01/02/2020"):
    return {
        "odiNumber": identifier,
        "summary": summary,
        "dateOfIncident": event,
        "dateComplaintFiled": "02/20/2020",
        "products": [{"productMake": "FORD", "productModel": "F-150", "productYear": "2016"}],
    }


def recall():
    return {
        "NHTSACampaignNumber": "10V039000",
        "Make": "SYNTHETIC",
        "Model": "TEST",
        "ModelYear": "2010",
        "Component": "TEST COMPONENT",
        "Summary": "SYNTHETIC RECALL",
        "ReportReceivedDate": "09/02/2010",
    }


def capture_rows(root, rows, kind="complaint_api", *, campaign=None):
    sources = load_sources()
    source = next(s for s in sources.values() if s.kind == kind)
    if campaign is not None:
        from ingestion.contracts import SourceSpec

        source = SourceSpec.model_validate(
            source.model_dump(mode="json")
            | {
                "source_id": "SYNTHETIC-CAMPAIGN-" + campaign,
                "selection": {"campaign_number": campaign},
                "url": "https://api.nhtsa.gov/recalls/campaignNumber?campaignNumber=" + campaign,
            }
        )
    count_key = "Count" if kind == "recall_api" else "count"
    body = json.dumps({count_key: len(rows), "results": rows}).encode()
    return SnapshotStore(root).capture(source, lambda _: FetchResult(200, {}, body))
