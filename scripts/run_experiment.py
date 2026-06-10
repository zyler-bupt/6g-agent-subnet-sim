from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from models import Message, MessageType, to_jsonable
from scenarios import create_disaster_response_scenario


CONTROLLER_URL = "http://127.0.0.1:18000"
GATEWAY_URLS = {
    "gw-ue": "http://127.0.0.1:18001",
    "gw-mec": "http://127.0.0.1:18002",
    "gw-cloud": "http://127.0.0.1:18003",
}


def http_json(method: str, url: str, payload: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    request = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=10) as response:
            raw = response.read().decode("utf-8")
            return response.status, json.loads(raw) if raw else {}
    except HTTPError as exc:
        raw = exc.read().decode("utf-8")
        return exc.code, json.loads(raw) if raw else {}
    except URLError as exc:
        return 599, {"error": str(exc.reason)}


def wait_for_services(timeout_s: int = 60) -> None:
    urls = [f"{CONTROLLER_URL}/health", *(f"{url}/health" for url in GATEWAY_URLS.values())]
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if all(http_json("GET", url)[0] == 200 for url in urls):
            return
        time.sleep(1)
    raise RuntimeError("services did not become ready; run docker compose up --build first")


def create_task() -> dict[str, Any]:
    intent, _ = create_disaster_response_scenario()
    status, body = http_json("POST", f"{CONTROLLER_URL}/tasks", {"intent": to_jsonable(intent)})
    if status != 200 or body.get("status") != "READY":
        raise RuntimeError(f"task build failed: {status} {body}")
    return body


def send_business(source: str, target: str, content: str, size_kbytes: int = 256) -> dict[str, Any]:
    message = Message(
        task_id="task-disaster-001",
        message_type=MessageType.BUSINESS,
        source=source,
        target=target,
        payload={"content": content, "size_kbytes": size_kbytes},
    )
    status, body = http_json(
        "POST",
        f"{GATEWAY_URLS['gw-ue']}/messages/business",
        {"message": to_jsonable(message)},
    )
    body["http_status"] = status
    return body


def trigger_network_congestion() -> dict[str, Any]:
    message = Message(
        task_id="task-disaster-001",
        message_type=MessageType.STATE,
        source="nagent-mec-cloud-path",
        target="subnet-controller",
        payload={
            "layer": "network",
            "path_id": "path-ue-mec-cloud",
            "path_state": "congested",
            "bandwidth_mbps": 8,
            "congestion_level": 0.87,
        },
    )
    status, body = http_json("POST", f"{CONTROLLER_URL}/messages/state", {"message": to_jsonable(message)})
    body["http_status"] = status
    return body


def trigger_physical_degradation() -> dict[str, Any]:
    message = Message(
        task_id="task-disaster-001",
        message_type=MessageType.STATE,
        source="pagent-ue-link",
        target="subnet-controller",
        payload={
            "layer": "physical",
            "link_id": "wireless-link-drone-gnb",
            "link_quality": "poor",
            "channel_available": True,
            "signal_score": 35,
        },
    )
    status, body = http_json("POST", f"{CONTROLLER_URL}/messages/state", {"message": to_jsonable(message)})
    body["http_status"] = status
    return body


def trigger_agent_failure() -> dict[str, Any]:
    status, body = http_json(
        "POST",
        f"{CONTROLLER_URL}/events/agent-failure",
        {"task_id": "task-disaster-001", "agent_id": "agent-edge-recognition"},
    )
    body["http_status"] = status
    return body


def collect_metrics() -> dict[str, Any]:
    status, controller_metrics = http_json("GET", f"{CONTROLLER_URL}/metrics")
    if status != 200:
        raise RuntimeError(controller_metrics)
    gateway_metrics = {}
    for gateway_id, url in GATEWAY_URLS.items():
        _, gateway_metrics[gateway_id] = http_json("GET", f"{url}/metrics")
    return {
        "experiment_mode": "docker-http-simulation",
        "controller": controller_metrics,
        "gateways": gateway_metrics,
    }


def run_scenario(scenario: str) -> dict[str, Any]:
    wait_for_services()
    result: dict[str, Any] = {"scenario": scenario, "steps": []}
    result["steps"].append({"name": "normal-build", "result": create_task()})

    if scenario in {"whitelist", "all"}:
        result["steps"].append({
            "name": "legal-business-flow",
            "result": send_business("agent-drone-capture", "agent-edge-recognition", "高清灾害现场视频", 512),
        })
        result["steps"].append({
            "name": "illegal-business-flow",
            "result": send_business("agent-drone-capture", "agent-cloud-planning", "绕过边缘识别的非法业务流", 128),
        })
    if scenario in {"network-congestion", "all"}:
        result["steps"].append({"name": "network-congestion", "result": trigger_network_congestion()})
    if scenario in {"physical-degradation", "all"}:
        before = send_business("agent-drone-capture", "agent-edge-recognition", "链路下降前视频流", 512)
        result["steps"].append({"name": "physical-before-business-flow", "result": before})
        result["steps"].append({"name": "physical-degradation", "result": trigger_physical_degradation()})
        after = send_business("agent-drone-capture", "agent-edge-recognition", "链路下降后视频流", 512)
        result["steps"].append({"name": "physical-after-business-flow", "result": after})
    if scenario in {"agent-failure", "all"}:
        result["steps"].append({"name": "agent-failure", "result": trigger_agent_failure()})
    if scenario in {"full-rebuild", "all"}:
        start = time.perf_counter()
        rebuild = create_task()
        rebuild["full_rebuild_time_ms"] = (time.perf_counter() - start) * 1000
        result["steps"].append({"name": "full-rebuild", "result": rebuild})

    result["metrics"] = collect_metrics()
    return result


def write_result(result: dict[str, Any]) -> Path:
    results_dir = Path("results")
    results_dir.mkdir(exist_ok=True)
    path = results_dir / f"{result['scenario']}-{int(time.time())}.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--scenario",
        choices=[
            "normal-build",
            "whitelist",
            "network-congestion",
            "physical-degradation",
            "agent-failure",
            "full-rebuild",
            "all",
        ],
        default="all",
    )
    args = parser.parse_args()
    result = run_scenario(args.scenario)
    path = write_result(result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"\nresult_file={path}")


if __name__ == "__main__":
    main()
