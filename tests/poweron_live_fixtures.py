from datetime import date

LIVE_EVENT_DATE = date(2026, 4, 10)
LIVE_DATE_GRAPH = "2026-04-10T00:00:00Z"
LIVE_EVENT_ID = 42
LIVE_GROUP = "2.2"


def live_half_hour_times() -> dict[str, str]:
    statuses = ("0", "1", "10")
    return {
        f"{minutes // 60:02}:{minutes % 60:02}": statuses[index % len(statuses)]
        for index, minutes in enumerate(range(0, 24 * 60, 30))
    }


def live_collection() -> dict[str, object]:
    return {
        "@context": "/api/contexts/ActualGpvGraph",
        "@id": "/api/actual_gpv_graphs/custom",
        "@type": "hydra:Collection",
        "hydra:totalItems": 1,
        "hydra:member": [
            {
                "@id": f"/api/actual_gpv_graphs/{LIVE_EVENT_ID}",
                "@type": "ActualGpvGraph",
                "id": LIVE_EVENT_ID,
                "dateGraph": LIVE_DATE_GRAPH,
                "dateCreate": "2026-04-09T16:00:00Z",
                "dataJson": {LIVE_GROUP: {"times": live_half_hour_times()}},
            }
        ],
    }


def live_empty_collection() -> dict[str, object]:
    return {
        "@context": "/api/contexts/ActualGpvGraph",
        "@id": "/api/actual_gpv_graphs/custom",
        "@type": "hydra:Collection",
        "hydra:totalItems": 0,
        "hydra:member": None,
    }
