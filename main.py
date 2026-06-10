from __future__ import annotations

from controller import SubnetController
from models import MessageType
from scenarios import create_disaster_response_scenario


def print_section(title: str) -> None:
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


def main() -> None:
    intent, gateways = create_disaster_response_scenario()
    controller = SubnetController(gateways)

    print_section("场景1：正常建网")
    controller.build_task_subnet(intent)

    print_section("场景2：业务消息转发与白名单隔离")
    ue_gateway = gateways["gw-ue"]
    mec_gateway = gateways["gw-mec"]
    cloud_gateway = gateways["gw-cloud"]

    drone_agent = ue_gateway.agents["agent-drone-capture"]
    legal_message = drone_agent.create_business_message(
        task_id=intent.task_id,
        target="agent-edge-recognition",
        content="灾害现场高清视频片段",
        frame_id="frame-0001",
    )
    legal_result = ue_gateway.deliver(legal_message)
    print(f"[合法业务消息转发] {legal_message.source}->{legal_message.target}: {legal_result.reason}")

    illegal_message = drone_agent.create_business_message(
        task_id=intent.task_id,
        target="agent-cloud-planning",
        content="绕过边缘识别直接请求云端规划",
    )
    illegal_result = ue_gateway.deliver(illegal_message)
    print(
        f"[非法业务消息拦截] {illegal_message.source}->{illegal_message.target}: "
        f"{illegal_result.reason}"
    )
    controller.metrics.blocked_flows = sum(gateway.blocked_count for gateway in gateways.values())

    print_section("场景3：nAgent 承载冲突触发增量调整")
    nagent = mec_gateway.agents["nagent-mec-cloud-path"]
    state_message = nagent.report_path_state(
        task_id=intent.task_id,
        target=controller.controller_id,
        path_state="congested",
        bandwidth_mbps=8,
        congestion_level=0.87,
    )
    delta = controller.handle_state_message(state_message)
    if delta is None:
        print("[增量更新] 未触发调整")
    else:
        print(f"[rule_delta] reason={delta.reason}")
        for rule in delta.update_rules:
            print(
                f"    update {rule.source}->{rule.target} "
                f"policy={rule.policy} priority={rule.priority}"
            )

    print_section("基础实验指标")
    metrics = controller.metrics
    print(f"建网耗时(ms): {metrics.build_time_ms:.2f}")
    print(f"下发规则数量: {metrics.total_rules}")
    print(f"涉及网关数量: {metrics.involved_gateways}")
    print(f"非法流量拦截次数: {metrics.blocked_flows}")
    print(f"增量更新规则数量: {metrics.delta_rules}")
    print(f"增量更新时间(ms): {metrics.delta_update_time_ms:.2f}")

    print_section("网关最终业务规则摘要")
    for gateway_id, gateway in gateways.items():
        business_rules = [
            rule
            for rule in gateway.rules.values()
            if rule.message_type == MessageType.BUSINESS
        ]
        print(f"{gateway_id}: {len(business_rules)} 条业务规则")
        for rule in sorted(business_rules, key=lambda item: (item.source, item.target)):
            print(
                f"    {rule.source}->{rule.target} "
                f"relation={rule.relation} policy={rule.policy} priority={rule.priority}"
            )


if __name__ == "__main__":
    main()

