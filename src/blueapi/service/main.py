from contextlib import asynccontextmanager
from typing import Dict, Set

from fastapi import Body, Depends, FastAPI, HTTPException, Request, Response, status

from blueapi.config import ApplicationConfig
from blueapi.worker import RunPlan, TrackableTask, WorkerState

from .handler import Handler, get_handler, setup_handler, teardown_handler
from .model import (
    DeviceModel,
    DeviceResponse,
    PlanModel,
    PlanResponse,
    StateChangeRequest,
    TaskResponse,
    WorkerTask,
)

REST_API_VERSION = "0.0.3"


@asynccontextmanager
async def lifespan(app: FastAPI):
    config: ApplicationConfig = app.state.config
    setup_handler(config)
    yield
    teardown_handler()


app = FastAPI(
    docs_url="/docs",
    on_shutdown=[teardown_handler],
    title="BlueAPI Control",
    lifespan=lifespan,
    version=REST_API_VERSION,
)


@app.get("/plans", response_model=PlanResponse)
def get_plans(handler: Handler = Depends(get_handler)):
    """Retrieve information about all available plans."""
    return PlanResponse(
        plans=[PlanModel.from_plan(plan) for plan in handler.context.plans.values()]
    )


@app.get(
    "/plans/{name}",
    response_model=PlanModel,
    responses={status.HTTP_404_NOT_FOUND: {"detail": "item not found"}},
)
def get_plan_by_name(name: str, handler: Handler = Depends(get_handler)):
    """Retrieve information about a plan by its (unique) name."""
    try:
        return PlanModel.from_plan(handler.context.plans[name])
    except KeyError:
        raise HTTPException(status_code=404, detail="Item not found")


@app.get("/devices", response_model=DeviceResponse)
def get_devices(handler: Handler = Depends(get_handler)):
    """Retrieve information about all available devices."""
    return DeviceResponse(
        devices=[
            DeviceModel.from_device(device)
            for device in handler.context.devices.values()
        ]
    )


@app.get(
    "/devices/{name}",
    response_model=DeviceModel,
    responses={status.HTTP_404_NOT_FOUND: {"detail": "item not found"}},
)
def get_device_by_name(name: str, handler: Handler = Depends(get_handler)):
    """Retrieve information about a devices by its (unique) name."""
    try:
        return DeviceModel.from_device(handler.context.devices[name])
    except KeyError:
        raise HTTPException(status_code=404, detail="Item not found")


@app.post("/tasks", response_model=TaskResponse, status_code=201)
def submit_task(
    request: Request,
    response: Response,
    task: RunPlan = Body(
        ..., example=RunPlan(name="count", params={"detectors": ["x"]})
    ),
    handler: Handler = Depends(get_handler),
):
    """Submit a task to the worker."""
    task_id: str = handler.worker.submit_task(task)
    response.headers["Location"] = f"{request.url}/{task_id}"
    return TaskResponse(task_id=task_id)


@app.put(
    "/worker/task",
    response_model=WorkerTask,
    responses={status.HTTP_409_CONFLICT: {"worker": "already active"}},
)
def update_task(
    task: WorkerTask,
    handler: Handler = Depends(get_handler),
) -> WorkerTask:
    active_task = handler.worker.get_active_task()
    if active_task is not None and not active_task.is_complete:
        raise HTTPException(status_code=409, detail="Worker already active")
    elif task.task_id is not None:
        handler.worker.begin_task(task.task_id)
    return task


@app.get(
    "/tasks/{task_id}",
    response_model=TrackableTask,
    responses={status.HTTP_404_NOT_FOUND: {"item": "not found"}},
)
def get_task(
    task_id: str,
    handler: Handler = Depends(get_handler),
) -> TrackableTask:
    """Retrieve a task"""

    task = handler.worker.get_pending_task(task_id)
    if task is not None:
        return task
    else:
        raise HTTPException(status_code=404, detail="Item not found")


@app.get("/worker/task")
def get_active_task(handler: Handler = Depends(get_handler)) -> WorkerTask:
    return WorkerTask.of_worker(handler.worker)


@app.get("/worker/state")
def get_state(handler: Handler = Depends(get_handler)) -> WorkerState:
    """Get the State of the Worker"""
    return handler.worker.state


# Map of current_state: allowed new_states
_ALLOWED_TRANSITIONS: Dict[WorkerState, Set[WorkerState]] = {
    WorkerState.RUNNING: {WorkerState.PAUSED},
    WorkerState.PAUSED: {WorkerState.RUNNING},
}


@app.put(
    "/worker/state",
    status_code=status.HTTP_400_BAD_REQUEST,
    responses={
        status.HTTP_400_BAD_REQUEST: {"detail": "Transition not allowed"},
        status.HTTP_202_ACCEPTED: {"detail": "Transition requested"},
    },
)
def set_state(
    state_change_request: StateChangeRequest,
    response: Response,
    handler: Handler = Depends(get_handler),
) -> WorkerState:
    """
    Request that the worker is put into a particular state.
    Returns the state of the worker at the end of the call.
    If the worker is PAUSED, new_state may be RUNNING to resume.
    If the worker is RUNNING, new_state may be PAUSED to pause and
    defer may be True to defer the pause until the new checkpoint.
    All other values of new_state will result in 400 "Bad Request"
    """
    current_state = handler.worker.state
    new_state = state_change_request.new_state
    if (
        current_state in _ALLOWED_TRANSITIONS
        and new_state in _ALLOWED_TRANSITIONS[current_state]
    ):
        response.status_code = status.HTTP_202_ACCEPTED
        if new_state == WorkerState.PAUSED:
            handler.worker.pause(defer=state_change_request.defer)
        elif new_state == WorkerState.RUNNING:
            handler.worker.resume()
    return handler.worker.state


def start(config: ApplicationConfig):
    import uvicorn

    app.state.config = config
    uvicorn.run(app, host=config.api.host, port=config.api.port)


@app.middleware("http")
async def add_api_version_header(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-API-Version"] = REST_API_VERSION
    return response
