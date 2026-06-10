from __future__ import annotations

import os
from urllib.parse import urlparse

from models import AgentCard, AgentRole, LayerType, MessageType, to_jsonable
from services.common import JsonHandler, run_server


class AgentService:
    def __init__(self) -> None:
        self.card = AgentCard(
            agent_id=os.environ["AGENT_ID"],
            name=os.environ.get("AGENT_NAME", os.environ["AGENT_ID"]),
            capabilities=tuple(os.environ.get("CAPABILITIES", "").split(",")) if os.environ.get("CAPABILITIES") else (),
            endpoint=os.environ.get("ENDPOINT", f"http://{os.environ['AGENT_ID']}:8000"),
            node=os.environ["NODE"],
            gateway_id=os.environ["GATEWAY_ID"],
            protocol="http-json",
            status=os.environ.get("STATUS", "available"),
            agent_role=AgentRole(os.environ["AGENT_ROLE"]),
            layer_type=LayerType(os.environ["LAYER_TYPE"]),
            state_items=tuple(os.environ.get("STATE_ITEMS", "").split(",")) if os.environ.get("STATE_ITEMS") else (),
            link_id=os.environ.get("LINK_ID"),
            path_id=os.environ.get("PATH_ID"),
        )
        self.received_business_messages = 0

    def handle(self, method: str, path: str, payload: dict) -> tuple[int, dict]:
        parsed = urlparse(path)
        if method == "GET" and parsed.path == "/health":
            return 200, {"status": "ok", "agent_id": self.card.agent_id}
        if method == "GET" and parsed.path == "/card":
            return 200, {"agent_card": to_jsonable(self.card)}
        if method == "POST" and parsed.path == "/messages/business":
            self.received_business_messages += 1
            return 200, {
                "accepted": True,
                "agent_id": self.card.agent_id,
                "message_type": MessageType.BUSINESS.value,
                "received_business_messages": self.received_business_messages,
                "payload": payload.get("payload", {}),
            }
        if method == "POST" and parsed.path == "/status":
            self.card = AgentCard(
                agent_id=self.card.agent_id,
                name=self.card.name,
                capabilities=self.card.capabilities,
                endpoint=self.card.endpoint,
                node=self.card.node,
                gateway_id=self.card.gateway_id,
                protocol=self.card.protocol,
                status=payload["status"],
                agent_role=self.card.agent_role,
                layer_type=self.card.layer_type,
                state_items=self.card.state_items,
                link_id=self.card.link_id,
                path_id=self.card.path_id,
            )
            return 200, {"status": self.card.status}
        if method == "GET" and parsed.path == "/metrics":
            return 200, {"received_business_messages": self.received_business_messages}
        return 404, {"error": "not_found", "path": parsed.path}


def main() -> None:
    service = AgentService()
    JsonHandler.routes_owner = service
    run_server(JsonHandler, int(os.environ.get("PORT", "8000")))


if __name__ == "__main__":
    main()

