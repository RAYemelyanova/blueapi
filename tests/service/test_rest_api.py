from dataclasses import dataclass
from typing import Optional
from unittest.mock import MagicMock

import pytest
from bluesky.run_engine import RunEngineStateMachine
from fastapi import status
from fastapi.testclient import TestClient
from pydantic import BaseModel

from blueapi.core.bluesky_types import Plan
from blueapi.service.handler import Handler
from blueapi.worker.task import RunPlan
from src.blueapi.worker import WorkerState

_TASK = RunPlan(name="count", params={"detectors": ["x"]})


def test_get_plans(handler: Handler, client: TestClient) -> None:
    class MyModel(BaseModel):
        id: str

    plan = Plan(name="my-plan", model=MyModel)

    handler.context.plans = {"my-plan": plan}
    response = client.get("/plans")

    assert response.status_code == 200
    assert response.json() == {"plans": [{"name": "my-plan"}]}


def test_get_plan_by_name(handler: Handler, client: TestClient) -> None:
    class MyModel(BaseModel):
        id: str

    plan = Plan(name="my-plan", model=MyModel)

    handler.context.plans = {"my-plan": plan}
    response = client.get("/plans/my-plan")

    assert response.status_code == 200
    assert response.json() == {"name": "my-plan"}


def test_get_devices(handler: Handler, client: TestClient) -> None:
    @dataclass
    class MyDevice:
        name: str

    device = MyDevice("my-device")

    handler.context.devices = {"my-device": device}
    response = client.get("/devices")

    assert response.status_code == 200
    assert response.json() == {
        "devices": [
            {
                "name": "my-device",
                "protocols": ["HasName"],
            }
        ]
    }


def test_get_device_by_name(handler: Handler, client: TestClient) -> None:
    @dataclass
    class MyDevice:
        name: str

    device = MyDevice("my-device")

    handler.context.devices = {"my-device": device}
    response = client.get("/devices/my-device")

    assert response.status_code == 200
    assert response.json() == {
        "name": "my-device",
        "protocols": ["HasName"],
    }


def test_create_task(handler: Handler, client: TestClient) -> None:
    response = client.post("/tasks", json=_TASK.dict())
    task_id = response.json()["task_id"]

    pending = handler.worker.get_pending_task(task_id)
    assert pending is not None
    assert pending.task == _TASK


def test_put_plan_begins_task(handler: Handler, client: TestClient) -> None:
    handler.worker.start()
    response = client.post("/tasks", json=_TASK.dict())
    task_id = response.json()["task_id"]

    task_json = {"task_id": task_id}
    client.put("/worker/task", json=task_json)

    active_task = handler.worker.get_active_task()
    assert active_task is not None
    assert active_task.task_id == task_id
    handler.worker.stop()


def test_get_state_updates(handler: Handler, client: TestClient) -> None:
    assert client.get("/worker/state").text == f'"{WorkerState.IDLE.name}"'
    handler.worker._on_state_change(  # type: ignore
        RunEngineStateMachine.States.RUNNING
    )
    assert client.get("/worker/state").text == f'"{WorkerState.RUNNING.name}"'


@pytest.fixture
def mockable_state_machine(handler: Handler):
    def set_state(state: RunEngineStateMachine.States):
        handler.context.run_engine.state = state  # type: ignore
        handler.worker._on_state_change(state)  # type: ignore

    def pause(_: bool):
        set_state(RunEngineStateMachine.States.PAUSED)

    def run():
        set_state(RunEngineStateMachine.States.RUNNING)

    mock_pause = handler.context.run_engine.request_pause = MagicMock()  # type: ignore
    mock_pause.side_effect = pause
    mock_resume = handler.context.run_engine.resume = MagicMock()  # type: ignore
    mock_resume.side_effect = run
    yield handler


def test_running_while_idle_denied(
    mockable_state_machine: Handler, client: TestClient
) -> None:
    re = mockable_state_machine.context.run_engine

    assert client.get("/worker/state").text == f'"{WorkerState.IDLE.name}"'
    response = client.put("/worker/state", json={"new_state": WorkerState.RUNNING.name})
    assert response.status_code is status.HTTP_400_BAD_REQUEST
    assert response.text == f'"{WorkerState.IDLE.name}"'
    assert not re.request_pause.called  # type: ignore
    assert not re.resume.called  # type: ignore
    assert client.get("/worker/state").text == f'"{WorkerState.IDLE.name}"'


def test_pausing_while_idle_denied(
    mockable_state_machine: Handler, client: TestClient
) -> None:
    re = mockable_state_machine.context.run_engine

    assert client.get("/worker/state").text == f'"{WorkerState.IDLE.name}"'
    response = client.put("/worker/state", json={"new_state": WorkerState.PAUSED.name})
    assert response.status_code is status.HTTP_400_BAD_REQUEST
    assert response.text == f'"{WorkerState.IDLE.name}"'
    assert not re.request_pause.called  # type: ignore
    assert not re.resume.called  # type: ignore
    assert client.get("/worker/state").text == f'"{WorkerState.IDLE.name}"'


@pytest.mark.parametrize("defer", [True, False, None])
def test_calls_pause_if_running(
    mockable_state_machine: Handler, client: TestClient, defer: Optional[bool]
) -> None:
    re = mockable_state_machine.context.run_engine
    mockable_state_machine.worker._on_state_change(  # type: ignore
        RunEngineStateMachine.States.RUNNING
    )

    assert client.get("/worker/state").text == f'"{WorkerState.RUNNING.name}"'
    response = client.put(
        "/worker/state", json={"new_state": WorkerState.PAUSED.name, "defer": defer}
    )
    assert response.status_code is status.HTTP_202_ACCEPTED
    assert response.text == f'"{WorkerState.PAUSED.name}"'
    assert re.request_pause.called  # type: ignore
    re.request_pause.assert_called_with(defer)  # type: ignore
    assert not re.resume.called  # type: ignore
    assert client.get("/worker/state").text == f'"{WorkerState.PAUSED.name}"'


def test_pause_and_resume(mockable_state_machine: Handler, client: TestClient) -> None:
    re = mockable_state_machine.context.run_engine
    mockable_state_machine.worker._on_state_change(  # type: ignore
        RunEngineStateMachine.States.RUNNING
    )

    assert client.get("/worker/state").text == f'"{WorkerState.RUNNING.name}"'
    response = client.put("/worker/state", json={"new_state": WorkerState.PAUSED.name})
    assert response.status_code is status.HTTP_202_ACCEPTED
    assert response.text == f'"{WorkerState.PAUSED.name}"'
    assert re.request_pause.call_count == 1  # type: ignore
    assert not re.resume.called  # type: ignore
    assert client.get("/worker/state").text == f'"{WorkerState.PAUSED.name}"'

    response = client.put("/worker/state", json={"new_state": WorkerState.RUNNING.name})
    assert response.status_code is status.HTTP_202_ACCEPTED
    assert response.text == f'"{WorkerState.RUNNING.name}"'
    assert re.request_pause.call_count == 1  # type: ignore
    assert re.resume.call_count == 1  # type: ignore
    assert client.get("/worker/state").text == f'"{WorkerState.RUNNING.name}"'
