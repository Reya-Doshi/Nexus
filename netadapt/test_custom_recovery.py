import pytest
from lab_session import LabSession
from reroute_engine import IncidentState


def lab():
    s = LabSession()
    s.load_reroute_demo(preventive=True)
    s.reroute_controller.ai_client.api_key = ''
    s.add_device('router', 560, 70, name='R7')
    s.add_link('Client', 'R7', latency=5)
    s.add_link('R7', 'R4', latency=5)
    return s


def test_r7_custom_bypass_is_connected_and_verified():
    s = lab()
    s.configure_link('R2', 'R4', {'status':'DOWN'})
    c = s.reroute_controller
    assert c.current_route == ['Client','R7','R4','R6','Server']
    for _ in range(12):
        s.reroute_step()
        assert all(s.simulator.topology.graph[a][b]['status'] == 'UP' for a,b in zip(c.current_route,c.current_route[1:]))
    assert c.incident_state == IncidentState.RECOVERED
    assert c.healing.delivery_restored
    assert c.healing.service_target_met


def test_old_packet_observation_cannot_replace_selected_bypass(monkeypatch):
    s = lab()
    old = list(s.reroute_controller.current_route)
    s.configure_link('R2','R4',{'status':'DOWN'})
    chosen = list(s.reroute_controller.current_route)
    monkeypatch.setattr(s, 'step', lambda: {'last_packet':{'id':9999,'status':'DROPPED','latency':None,'route':old}})
    s.reroute_controller.step()
    assert s.reroute_controller.current_route == chosen
    assert s.reroute_controller.telemetry.packet_history[-1].route == old


def test_second_fault_during_verification_changes_path_safely():
    s = lab()
    s.configure_link('R2','R4',{'status':'DOWN'})
    s.reroute_step()
    s.configure_link('R7','R4',{'status':'DOWN'})
    c = s.reroute_controller
    assert 'R7' not in c.current_route
    assert not c.healing.delivery_restored
    for _ in range(12):s.reroute_step()
    assert c.incident_state == IncidentState.RECOVERED
    assert c.healing.service_target_met


def test_total_outage_revokes_previous_recovery_and_repair_recovers():
    s = lab()
    s.configure_link('R2','R4',{'status':'DOWN'})
    for _ in range(12):s.reroute_step()
    c = s.reroute_controller
    assert c.healing.delivery_restored
    s.configure_link('Client','R1',{'status':'DOWN'})
    s.configure_link('Client','R7',{'status':'DOWN'})
    assert c.incident_state == IncidentState.UNRESOLVED
    assert not c.healing.delivery_restored
    assert not c.healing.service_target_met
    s.configure_link('Client','R7',{'status':'UP'})
    for _ in range(12):s.reroute_step()
    assert c.incident_state == IncidentState.RECOVERED
