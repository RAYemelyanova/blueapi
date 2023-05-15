import json
import logging
import uuid
from functools import wraps
from pathlib import Path
from pprint import pprint
from typing import Optional

import click
import requests
from requests.exceptions import ConnectionError

from blueapi import __version__
from blueapi.cli.amq import AmqClient
from blueapi.config import ApplicationConfig, ConfigLoader
from blueapi.messaging.stomptemplate import StompMessagingTemplate
from blueapi.service.handler import get_handler
from blueapi.service.main import start


@click.group(invoke_without_command=True)
@click.version_option(version=__version__, prog_name="blueapi")
@click.option("-c", "--config", type=Path, help="Path to configuration YAML file")
@click.pass_context
def main(ctx: click.Context, config: Optional[Path]) -> None:
    # if no command is supplied, run with the options passed

    config_loader = ConfigLoader(ApplicationConfig)
    if config is not None:
        if config.exists():
            config_loader.use_values_from_yaml(config)
        else:
            raise FileNotFoundError(f"Cannot find file: {config}")

    ctx.ensure_object(dict)
    ctx.obj["config"] = config_loader.load()

    if ctx.invoked_subcommand is None:
        print("Please invoke subcommand!")


@main.command(name="serve")
@click.pass_obj
def start_application(obj: dict):
    start(obj["config"])


@main.command(name="worker", deprecated=True)
@click.pass_obj
def deprecated_start_application(obj: dict):
    print("Please use serve command instead.\n")
    start(obj["config"])


@main.group()
@click.pass_context
def controller(ctx: click.Context) -> None:
    if ctx.invoked_subcommand is None:
        print("Please invoke subcommand!")
        return

    ctx.ensure_object(dict)
    config: ApplicationConfig = ctx.obj["config"]
    logging.basicConfig(level=config.logging.level)


def check_connection(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        try:
            func(*args, **kwargs)
        except ConnectionError:
            print("Failed to establish connection to FastAPI server.")

    return wrapper


@controller.command(name="plans")
@check_connection
@click.pass_obj
def get_plans(obj: dict) -> None:
    config: ApplicationConfig = obj["config"]

    resp = requests.get(f"http://{config.api.host}:{config.api.port}/plans")
    print(f"Response returned with {resp.status_code}: ")
    pprint(resp.json())


@controller.command(name="devices")
@check_connection
@click.pass_obj
def get_devices(obj: dict) -> None:
    config: ApplicationConfig = obj["config"]

    resp = requests.get(f"http://{config.api.host}:{config.api.port}/devices")
    print(f"Response returned with {resp.status_code}: ")
    pprint(resp.json())


@controller.command(name="run")
@click.argument("name", type=str)
@click.option("-p", "--parameters", type=str, help="Parameters as valid JSON")
@click.option(
    "-i", "--id", type=str, help="correlation id of the task to be run", default=None
)
@check_connection
@click.pass_obj
def run_plan(obj: dict, name: str, parameters: str, id: Optional[str] = None) -> None:
    config: ApplicationConfig = obj["config"]

    amq_client = AmqClient(StompMessagingTemplate.autoconfigured(config.stomp))

    with amq_client:
        create_resp = requests.put(
            f"http://{config.api.host}:{config.api.port}/task",
            json={"name": name, "params": json.loads(parameters)},
        )
        task_id = create_resp.json()["taskId"]

        amq_client.subscribe_to_topics(task_id)

        started_resp = requests.put(
            f"http://{config.api.host}:{config.api.port}/started/{task_id}"
        )

        print(f"Response returned with {started_resp.status_code}")

        amq_client.wait_for_complete()
