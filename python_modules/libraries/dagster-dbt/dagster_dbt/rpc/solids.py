import json
import time
from typing import Callable, Iterator, Optional

import pandas as pd
from dagster_pandas import DataFrame

from dagster import (
    Array,
    AssetMaterialization,
    Bool,
    DagsterInvalidDefinitionError,
    EventMetadataEntry,
    Failure,
    Field,
    InputDefinition,
    Int,
    Noneable,
    Nothing,
    Output,
    OutputDefinition,
    Permissive,
    RetryRequested,
    String,
    check,
    solid,
)
from dagster.core.execution.context.compute import SolidExecutionContext

from ..errors import DagsterDbtRpcUnexpectedPollOutputError
from .types import DbtRpcOutput
from .utils import log_rpc, raise_for_rpc_error


def _generate_materializations(dro: DbtRpcOutput) -> Iterator[AssetMaterialization]:
    """Yields ``AssetMaterializations`` for metadata in the dbt RPC ``DbtRpcOutput``."""
    for node_result in dro.result.results:
        if node_result.node["resource_type"] in ["model", "snapshot"]:
            success = not node_result.fail and not node_result.skip and not node_result.error
            if success:
                entries = [
                    EventMetadataEntry.json(data=node_result.node, label="Node"),
                    EventMetadataEntry.text(text=str(node_result.status), label="Status"),
                    EventMetadataEntry.text(
                        text=str(node_result.execution_time), label="Execution Time"
                    ),
                    EventMetadataEntry.text(
                        text=node_result.node["config"]["materialized"],
                        label="Materialization Strategy",
                    ),
                    EventMetadataEntry.text(text=node_result.node["database"], label="Database"),
                    EventMetadataEntry.text(text=node_result.node["schema"], label="Schema"),
                    EventMetadataEntry.text(text=node_result.node["alias"], label="Alias"),
                    EventMetadataEntry.text(
                        text=node_result.node["description"], label="Description"
                    ),
                ]
                for step_timing in node_result.step_timings:
                    if step_timing.name == "execute":
                        execution_entries = [
                            EventMetadataEntry.text(
                                text=step_timing.started_at.isoformat(timespec="seconds"),
                                label="Execution Started At",
                            ),
                            EventMetadataEntry.text(
                                text=step_timing.completed_at.isoformat(timespec="seconds"),
                                label="Execution Completed At",
                            ),
                            EventMetadataEntry.text(
                                text=str(step_timing.duration), label="Execution Duration"
                            ),
                        ]
                        entries.extend(execution_entries)
                    if step_timing.name == "compile":
                        execution_entries = [
                            EventMetadataEntry.text(
                                text=step_timing.started_at.isoformat(timespec="seconds"),
                                label="Compilation Started At",
                            ),
                            EventMetadataEntry.text(
                                text=step_timing.completed_at.isoformat(timespec="seconds"),
                                label="Compilation Completed At",
                            ),
                            EventMetadataEntry.text(
                                text=str(step_timing.duration), label="Compilation Duration"
                            ),
                        ]
                        entries.extend(execution_entries)

                yield AssetMaterialization(
                    description="A materialized node within the dbt graph.",
                    metadata_entries=entries,
                    asset_key=node_result.node["unique_id"],
                )


def _poll_rpc(
    context: SolidExecutionContext, request_token: str, should_yield_materializations: bool = True
) -> DbtRpcOutput:
    """Polls the dbt RPC server for the status of a request until the state is ``success``."""
    logs_start = 0
    while True:
        # Poll for the dbt RPC request.
        context.log.debug(f"RequestToken: {request_token}")
        resp = context.resources.dbt_rpc.poll(
            request_token=request_token, logs=context.solid_config["logs"], logs_start=logs_start
        )
        raise_for_rpc_error(context, resp)

        # Pass dbt RPC logs into the Dagster/Dagit logger.
        if context.solid_config["logs"]:
            logs = resp.json().get("result").get("logs")
            if len(logs) > 0:
                log_rpc(context, logs)
            logs_start += len(logs)

        # Stop polling if request's state is no longer "running".
        if resp.json().get("result").get("state") != "running":
            break

        # Sleep for the configured time intervale before polling again.
        context.log.debug(
            f"Request {request_token} currently in state '{resp.json().get('result').get('state')}' (elapsed time {resp.json().get('result').get('elapsed', 0)} seconds). Sleeping for {context.solid_config.get('interval')}s.."
        )
        time.sleep(context.solid_config["interval"])

    if resp.json().get("result").get("state") != "success":
        raise Failure(
            description=f"Request {request_token} finished with state '{resp.json().get('result').get('state')}' in {resp.json().get('result').get('elapsed')} seconds",
        )

    context.log.info(
        f"Request {request_token} finished with state '{resp.json().get('result').get('state')}' in {resp.json().get('result').get('elapsed')} seconds"
    )
    context.log.debug(json.dumps(resp.json().get("result"), indent=2))

    polled_run_results = DbtRpcOutput.from_dict(resp.json().get("result"))

    if should_yield_materializations:
        for materialization in _generate_materializations(polled_run_results):
            yield materialization

    yield Output(polled_run_results)


def unwrap_result(poll_rpc_generator) -> DbtRpcOutput:
    """A helper function that extracts the `DbtRpcOutput` value from a generator.

    The parameter `poll_rpc_generator` is expected to be an invocation of `_poll_rpc`.
    """
    output = None
    for x in poll_rpc_generator:
        output = x

    if output is None:
        raise DagsterDbtRpcUnexpectedPollOutputError(
            description="poll_rpc yielded None as its last value. Expected value of type Output containing DbtRpcOutput.",
        )

    if not isinstance(output, Output):
        raise DagsterDbtRpcUnexpectedPollOutputError(
            description=f"poll_rpc yielded value of type {type(output)} as its last value. Expected value of type Output containing DbtRpcOutput.",
        )

    if not isinstance(output.value, DbtRpcOutput):
        raise DagsterDbtRpcUnexpectedPollOutputError(
            description=f"poll_rpc yielded Output containing {type(output.value)}. Expected DbtRpcOutput.",
        )

    return output.value


@solid(
    description="A solid to invoke dbt run over RPC.",
    input_defs=[InputDefinition(name="start_after", dagster_type=Nothing)],
    output_defs=[
        OutputDefinition(
            name="request_token",
            dagster_type=String,
            description="The request token of the invoked dbt run.",
        )
    ],
    config_schema={
        "models": Field(
            config=Noneable(Array(String)),
            default_value=None,
            is_required=False,
            description="The dbt models to run.",
        ),
        "exclude": Field(
            config=Noneable(Array(String)),
            default_value=None,
            is_required=False,
            description="The dbt models to exclude.",
        ),
    },
    required_resource_keys={"dbt_rpc"},
    tags={"kind": "dbt"},
)
def dbt_rpc_run(context: SolidExecutionContext) -> String:
    """This solid sends the ``dbt run`` command to a dbt RPC server and returns the request token.

    This dbt RPC solid is asynchronous. The request token can be used in subsequent RPC requests to
    poll the progress of the running dbt process.
    """
    resp = context.resources.dbt_rpc.run(
        models=context.solid_config["models"], exclude=context.solid_config["exclude"]
    )
    context.log.debug(resp.text)
    raise_for_rpc_error(context, resp)
    return resp.json().get("result").get("request_token")


@solid(
    description="A solid to invoke dbt run over RPC and poll the resulting RPC process until it's complete.",
    input_defs=[InputDefinition(name="start_after", dagster_type=Nothing)],
    output_defs=[OutputDefinition(name="result", dagster_type=DbtRpcOutput)],
    config_schema={
        "models": Field(
            config=Noneable(Array(String)),
            default_value=None,
            is_required=False,
            description="The dbt models to run.",
        ),
        "exclude": Field(
            config=Noneable(Array(String)),
            default_value=None,
            is_required=False,
            description="The dbt models to exclude.",
        ),
        "full_refresh": Field(
            config=Bool,
            description="Whether or not to perform a --full-refresh.",
            is_required=False,
            default_value=False,
        ),
        "fail_fast": Field(
            config=Bool,
            description="Whether or not to --fail-fast.",
            is_required=False,
            default_value=False,
        ),
        "warn_error": Field(
            config=Bool,
            description="Whether or not to --warn-error.",
            is_required=False,
            default_value=False,
        ),
        "interval": Field(
            config=Int,
            is_required=False,
            default_value=10,
            description="The interval (in seconds) at which to poll the dbt rpc process.",
        ),
        "logs": Field(
            config=Bool,
            is_required=False,
            default_value=True,
            description="Whether or not to return logs from the process.",
        ),
        "task_tags": Permissive(),
        "max_retries": Field(config=Int, is_required=False, default_value=5),
        "retry_interval": Field(config=Int, is_required=False, default_value=120),
    },
    required_resource_keys={"dbt_rpc"},
    tags={"kind": "dbt"},
)
def dbt_rpc_run_and_wait(context: SolidExecutionContext) -> DbtRpcOutput:
    """This solid sends the ``dbt run`` command to a dbt RPC server and returns the result of the
    executed dbt process.

    This dbt RPC solid is synchronous, and will periodically poll the dbt RPC server until the dbt
    process is completed.
    """
    if context.solid_config["task_tags"]:
        results = context.resources.dbt_rpc.ps().json()
        for task in results["result"]["rows"]:
            if task["tags"] == context.solid_config["task_tags"]:
                context.log.warning(
                    f"RPC task with tags {json.dumps(task['tags'])} currently running."
                )
                raise RetryRequested(
                    max_retries=context.solid_config["max_retries"],
                    seconds_to_wait=context.solid_config["retry_interval"],
                )

    command = ""

    if context.solid_config["warn_error"]:
        command += " --warn-error"

    command += " run"

    if context.solid_config["models"]:
        models = " ".join(set(context.solid_config["models"]))
        command += f" --models {models}"

    if context.solid_config["exclude"]:
        exclude = " ".join(set(context.solid_config["exclude"]))
        command += f" --exclude {exclude}"

    if context.solid_config["full_refresh"]:
        command += " --full-refresh"

    if context.solid_config["fail_fast"]:
        command += " --fail-fast"

    context.log.debug(f"Running dbt command: dbt {command}")
    resp = context.resources.dbt_rpc.cli(cli=command, **context.solid_config["task_tags"])
    context.log.debug(resp.text)
    raise_for_rpc_error(context, resp)
    request_token = resp.json().get("result").get("request_token")
    return _poll_rpc(context, request_token)


@solid(
    description="A solid to invoke dbt test over RPC.",
    input_defs=[InputDefinition(name="start_after", dagster_type=Nothing)],
    output_defs=[
        OutputDefinition(
            name="request_token",
            dagster_type=String,
            description="The request token of the invoked dbt test.",
        )
    ],
    config_schema={
        "models": Field(
            config=Noneable(Array(String)),
            default_value=None,
            is_required=False,
            description="The dbt models to test.",
        ),
        "exclude": Field(
            config=Noneable(Array(String)),
            default_value=None,
            is_required=False,
            description="The dbt models to exclude.",
        ),
        "data": Field(
            config=Bool,
            default_value=True,
            is_required=False,
            description="Whether or not to run custom data tests.",
        ),
        "schema": Field(
            config=Bool,
            default_value=True,
            is_required=False,
            description="Whether or not to run schema tests.",
        ),
    },
    required_resource_keys={"dbt_rpc"},
    tags={"kind": "dbt"},
)
def dbt_rpc_test(context: SolidExecutionContext) -> String:
    """This solid sends the ``dbt test`` command to a dbt RPC server and returns the request token.

    This dbt RPC solid is asynchronous. The request token can be used in subsequent RPC requests to
    poll the progress of the running dbt process.
    """
    resp = context.resources.dbt_rpc.test(
        models=context.solid_config["models"],
        exclude=context.solid_config["exclude"],
        data=context.solid_config["data"],
        schema=context.solid_config["schema"],
    )
    context.log.debug(resp.text)
    raise_for_rpc_error(context, resp)
    return resp.json().get("result").get("request_token")


@solid(
    description="A solid to invoke dbt test over RPC and poll the resulting RPC process until it's complete.",
    input_defs=[InputDefinition(name="start_after", dagster_type=Nothing)],
    output_defs=[OutputDefinition(name="result", dagster_type=DbtRpcOutput)],
    config_schema={
        "models": Field(
            config=Noneable(Array(String)),
            default_value=None,
            is_required=False,
            description="The dbt models to test.",
        ),
        "exclude": Field(
            config=Noneable(Array(String)),
            default_value=None,
            is_required=False,
            description="The dbt models to exclude.",
        ),
        "data": Field(
            config=Bool,
            default_value=True,
            is_required=False,
            description="Whether or not to run custom data tests.",
        ),
        "schema": Field(
            config=Bool,
            default_value=True,
            is_required=False,
            description="Whether or not to run schema tests.",
        ),
        "interval": Field(
            config=Int,
            is_required=False,
            default_value=10,
            description="The interval (in seconds) at which to poll the dbt rpc process.",
        ),
        "logs": Field(
            config=Bool,
            is_required=False,
            default_value=True,
            description="Whether or not to return logs from the process.",
        ),
    },
    required_resource_keys={"dbt_rpc"},
    tags={"kind": "dbt"},
)
def dbt_rpc_test_and_wait(context: SolidExecutionContext) -> DbtRpcOutput:
    """This solid sends the ``dbt test`` command to a dbt RPC server and returns the result of the
    executed dbt process.

    This dbt RPC solid is synchronous, and will periodically poll the dbt RPC server until the dbt
    process is completed.
    """
    resp = context.resources.dbt_rpc.test(
        models=context.solid_config["models"],
        exclude=context.solid_config["exclude"],
        data=context.solid_config["data"],
        schema=context.solid_config["schema"],
    )
    context.log.debug(resp.text)
    raise_for_rpc_error(context, resp)
    request_token = resp.json().get("result").get("request_token")
    return _poll_rpc(context, request_token)


@solid(
    description="A solid to invoke a dbt run operation over RPC.",
    input_defs=[InputDefinition(name="start_after", dagster_type=Nothing)],
    output_defs=[
        OutputDefinition(
            name="request_token",
            dagster_type=String,
            description="The request token of the invoked dbt run operation.",
        )
    ],
    config_schema={
        "macro": Field(
            config=String,
            is_required=True,
            description="The dbt macro to invoke as a run operation",
        ),
        "args": Field(
            config=Noneable(Permissive()),
            is_required=False,
            default_value=None,
            description="Arguments to supply to the invoked macro.",
        ),
    },
    required_resource_keys={"dbt_rpc"},
    tags={"kind": "dbt"},
)
def dbt_rpc_run_operation(context: SolidExecutionContext) -> String:
    """This solid sends the ``dbt run-operation`` command to a dbt RPC server and returns the
    request token.

    This dbt RPC solid is asynchronous. The request token can be used in subsequent RPC requests to
    poll the progress of the running dbt process.
    """
    resp = context.resources.dbt_rpc.run_operation(
        macro=context.solid_config["macro"], args=context.solid_config["args"]
    )
    context.log.debug(resp.text)
    raise_for_rpc_error(context, resp)
    return resp.json().get("result").get("request_token")


@solid(
    description="A solid to invoke a dbt run operation over RPC and poll the resulting RPC process until it's complete.",
    input_defs=[InputDefinition(name="start_after", dagster_type=Nothing)],
    output_defs=[OutputDefinition(name="result", dagster_type=DbtRpcOutput)],
    config_schema={
        "macro": Field(
            config=String,
            is_required=True,
            description="The dbt macro to invoke as a run operation",
        ),
        "args": Field(
            config=Noneable(Permissive()),
            is_required=False,
            default_value=None,
            description="Arguments to supply to the invoked macro.",
        ),
        "interval": Field(
            config=Int,
            is_required=False,
            default_value=10,
            description="The interval (in seconds) at which to poll the dbt rpc process.",
        ),
        "logs": Field(
            config=Bool,
            is_required=False,
            default_value=True,
            description="Whether or not to return logs from the process.",
        ),
    },
    required_resource_keys={"dbt_rpc"},
    tags={"kind": "dbt"},
)
def dbt_rpc_run_operation_and_wait(context: SolidExecutionContext) -> DbtRpcOutput:
    """This solid sends the ``dbt run-operation`` command to a dbt RPC server and returns the result of the
    executed dbt process.

    This dbt RPC solid is synchronous, and will periodically poll the dbt RPC server until the dbt
    process is completed.
    """
    resp = context.resources.dbt_rpc.run_operation(
        macro=context.solid_config["macro"], args=context.solid_config["args"]
    )
    context.log.debug(resp.text)
    raise_for_rpc_error(context, resp)
    request_token = resp.json().get("result").get("request_token")
    return _poll_rpc(context, request_token)


@solid(
    description="A solid to invoke a dbt snapshot over RPC.",
    input_defs=[InputDefinition(name="start_after", dagster_type=Nothing)],
    output_defs=[
        OutputDefinition(
            name="request_token",
            dagster_type=String,
            description="The request token of the invoked dbt snapshot.",
        )
    ],
    config_schema={
        "select": Field(
            config=Noneable(Array(String)),
            default_value=None,
            is_required=False,
            description="The dbt snapshot files to snapshot.",
        ),
        "exclude": Field(
            config=Noneable(Array(String)),
            default_value=None,
            is_required=False,
            description="The dbt snapshot files to exclude from the snapshot.",
        ),
    },
    required_resource_keys={"dbt_rpc"},
    tags={"kind": "dbt"},
)
def dbt_rpc_snapshot(context: SolidExecutionContext) -> String:
    """This solid sends the ``dbt snapshot`` command to a dbt RPC server and returns the
    request token.

    This dbt RPC solid is asynchronous. The request token can be used in subsequent RPC requests to
    poll the progress of the running dbt process.
    """
    resp = context.resources.dbt_rpc.snapshot(
        select=context.solid_config["select"], exclude=context.solid_config["exclude"]
    )
    context.log.debug(resp.text)
    raise_for_rpc_error(context, resp)
    return resp.json().get("result").get("request_token")


@solid(
    description="A solid to invoke a dbt snapshot over RPC and poll the resulting RPC process until it's complete.",
    input_defs=[InputDefinition(name="start_after", dagster_type=Nothing)],
    output_defs=[OutputDefinition(name="result", dagster_type=DbtRpcOutput)],
    config_schema={
        "select": Field(
            config=Noneable(Array(String)),
            default_value=None,
            is_required=False,
            description="The dbt snapshot files to snapshot.",
        ),
        "exclude": Field(
            config=Noneable(Array(String)),
            default_value=None,
            is_required=False,
            description="The dbt snapshot files to exclude from the snapshot.",
        ),
        "interval": Field(
            config=Int,
            is_required=False,
            default_value=10,
            description="The interval (in seconds) at which to poll the dbt rpc process.",
        ),
        "logs": Field(
            config=Bool,
            is_required=False,
            default_value=True,
            description="Whether or not to return logs from the process.",
        ),
        "task_tags": Permissive(),
        "max_retries": Field(config=Int, is_required=False, default_value=5),
        "retry_interval": Field(config=Int, is_required=False, default_value=120),
    },
    required_resource_keys={"dbt_rpc"},
    tags={"kind": "dbt"},
)
def dbt_rpc_snapshot_and_wait(context: SolidExecutionContext) -> DbtRpcOutput:
    """This solid sends the ``dbt snapshot`` command to a dbt RPC server and returns the result of
    the executed dbt process.

    This dbt RPC solid is synchronous, and will periodically poll the dbt RPC server until the dbt
    process is completed.
    """
    if context.solid_config["task_tags"]:
        results = context.resources.dbt_rpc.ps().json()
        for task in results["result"]["rows"]:
            if task["tags"] == context.solid_config["task_tags"]:
                context.log.warning(
                    f"RPC task with tags {json.dumps(task['tags'])} currently running."
                )
                raise RetryRequested(
                    max_retries=context.solid_config["max_retries"],
                    seconds_to_wait=context.solid_config["retry_interval"],
                )

    resp = context.resources.dbt_rpc.snapshot(
        select=context.solid_config["select"], exclude=context.solid_config["exclude"]
    )
    context.log.debug(resp.text)
    raise_for_rpc_error(context, resp)
    request_token = resp.json().get("result").get("request_token")
    return _poll_rpc(context, request_token)


@solid(
    description="A solid to invoke dbt source snapshot-freshness over RPC.",
    input_defs=[InputDefinition(name="start_after", dagster_type=Nothing)],
    output_defs=[
        OutputDefinition(
            name="request_token",
            dagster_type=String,
            description="The request token of the invoked dbt snapshot.",
        )
    ],
    config_schema={
        "select": Field(
            config=Noneable(Array(String)),
            default_value=None,
            is_required=False,
            description="The dbt sources to snapshot-freshness for.",
        ),
        "warn_error": Field(
            config=Bool,
            description="Whether or not to --warn-error.",
            is_required=False,
            default_value=False,
        ),
    },
    required_resource_keys={"dbt_rpc"},
    tags={"kind": "dbt"},
)
def dbt_rpc_snapshot_freshness(context: SolidExecutionContext) -> String:
    """This solid sends the ``dbt source snapshot-freshness`` command to a dbt RPC server and
    returns the request token.

    This dbt RPC solid is asynchronous. The request token can be used in subsequent RPC requests to
    poll the progress of the running dbt process.
    """
    command = ""

    if context.solid_config["warn_error"]:
        command += " --warn-error"

    command += " source snapshot-freshness"

    if context.solid_config["select"]:
        select = " ".join(set(context.solid_config["select"]))
        command += f" --select {select}"

    context.log.debug(f"Running dbt command: dbt {command}")
    resp = context.resources.dbt_rpc.cli(cli=command)
    context.log.debug(resp.text)
    raise_for_rpc_error(context, resp)
    return resp.json().get("result").get("request_token")


@solid(
    description="A solid to invoke dbt source snapshot-freshness over RPC and poll the resulting RPC process until it's complete.",
    input_defs=[InputDefinition(name="start_after", dagster_type=Nothing)],
    output_defs=[OutputDefinition(name="result", dagster_type=DbtRpcOutput)],
    config_schema={
        "select": Field(
            config=Noneable(Array(String)),
            default_value=None,
            is_required=False,
            description="The dbt sources to snapshot-freshness for.",
        ),
        "warn_error": Field(
            config=Bool,
            description="Whether or not to --warn-error.",
            is_required=False,
            default_value=False,
        ),
        "interval": Field(
            config=Int,
            is_required=False,
            default_value=10,
            description="The interval (in seconds) at which to poll the dbt rpc process.",
        ),
        "logs": Field(
            config=Bool,
            is_required=False,
            default_value=True,
            description="Whether or not to return logs from the process.",
        ),
    },
    required_resource_keys={"dbt_rpc"},
    tags={"kind": "dbt"},
)
def dbt_rpc_snapshot_freshness_and_wait(context: SolidExecutionContext) -> DbtRpcOutput:
    """This solid sends the ``dbt source snapshot`` command to a dbt RPC server and returns the
    result of the executed dbt process.

    This dbt RPC solid is synchronous, and will periodically poll the dbt RPC server until the dbt
    process is completed.
    """
    command = ""

    if context.solid_config["warn_error"]:
        command += " --warn-error"

    command += " source snapshot-freshness"

    if context.solid_config["select"]:
        select = " ".join(set(context.solid_config["select"]))
        command += f" --select {select}"

    context.log.debug(f"Running dbt command: dbt {command}")
    resp = context.resources.dbt_rpc.cli(cli=command)
    context.log.debug(resp.text)
    raise_for_rpc_error(context, resp)
    request_token = resp.json().get("result").get("request_token")
    return _poll_rpc(context, request_token)


@solid(
    description="A solid to compile a SQL query in context of a dbt project over RPC.",
    input_defs=[
        InputDefinition(name="start_after", dagster_type=Nothing),
        InputDefinition(
            name="sql", description="The SQL query to be compiled.", dagster_type=String
        ),
    ],
    output_defs=[
        OutputDefinition(name="sql", description="The compiled SQL query.", dagster_type=String)
    ],
    config_schema={
        "name": Field(config=String),
        "interval": Field(
            config=Int,
            is_required=False,
            default_value=10,
            description="The interval (in seconds) at which to poll the dbt rpc process.",
        ),
        "logs": Field(
            config=Bool,
            is_required=False,
            default_value=True,
            description="Whether or not to return logs from the process.",
        ),
    },
    required_resource_keys={"dbt_rpc"},
    tags={"kind": "dbt"},
)
def dbt_rpc_compile_sql(context: SolidExecutionContext, sql: String) -> String:
    """This solid sends the ``dbt compile`` command to a dbt RPC server and returns the request
    token.

    This dbt RPC solid is asynchronous. The request token can be used in subsequent RPC requests to
    poll the progress of the running dbt process.
    """
    resp = context.resources.dbt_rpc.compile_sql(sql=sql, name=context.solid_config["name"])
    context.log.debug(resp.text)
    raise_for_rpc_error(context, resp)
    request_token = resp.json().get("result").get("request_token")
    result = unwrap_result(_poll_rpc(context, request_token))
    return result.results[0].node["compiled_sql"]


def create_dbt_rpc_run_sql_solid(
    name: str, output_def: Optional[OutputDefinition] = None, **kwargs
) -> Callable:
    """This function is a factory which constructs a solid that will copy the results of a SQL query
    run within the context of a dbt project to a pandas ``DataFrame``.

    Any kwargs passed to this function will be passed along to the underlying :func:`@solid
    <dagster.solid>` decorator. However, note that overriding ``config_schema``, ``input_defs``, and
    ``required_resource_keys`` is not allowed and will throw a :class:`DagsterInvalidDefinitionError
    <dagster.DagsterInvalidDefinitionError>`.

    If you would like to configure this solid with different config fields, you could consider using
    :func:`@composite_solid <dagster.composite_solid>` to wrap this solid.

    Args:
        name (str): The name of this solid.
        output_def (OutputDefinition, optional): The :class:`OutputDefinition
            <dagster.OutputDefinition>` for the solid. This value should always be a representation
            of a pandas ``DataFrame``. If not specified, the solid will default to an
            :class:`OutputDefinition <dagster.OutputDefinition>` named "df" with a ``DataFrame``
            dagster type.

    Returns:
        SolidDefinition: Returns the constructed solid definition.
    """
    check.str_param(obj=name, param_name="name")
    check.opt_inst_param(obj=output_def, param_name="output_def", ttype=OutputDefinition)

    if "config_schema" in kwargs:
        raise DagsterInvalidDefinitionError("Overriding config_schema is not supported.")

    if "input_defs" in kwargs:
        raise DagsterInvalidDefinitionError("Overriding input_defs is not supported.")

    if "required_resource_keys" in kwargs:
        raise DagsterInvalidDefinitionError("Overriding required_resource_keys is not supported.")

    @solid(
        name=name,
        description=kwargs.pop(
            "description",
            "A solid to run a SQL query in context of a dbt project over RPC and return the results in a pandas DataFrame.",
        ),
        input_defs=[
            InputDefinition(name="start_after", dagster_type=Nothing),
            InputDefinition(
                name="sql", description="The SQL query to be run.", dagster_type=String
            ),
        ],
        output_defs=[
            output_def
            or OutputDefinition(
                name="df", description="The results of the SQL query.", dagster_type=DataFrame
            )
        ],
        config_schema={
            "name": Field(config=String),
            "interval": Field(
                config=Int,
                is_required=False,
                default_value=10,
                description="The interval (in seconds) at which to poll the dbt rpc process.",
            ),
            "logs": Field(
                config=Bool,
                is_required=False,
                default_value=True,
                description="Whether or not to return logs from the process.",
            ),
        },
        required_resource_keys={"dbt_rpc"},
        tags={"kind": "dbt"},
        **kwargs,
    )
    def _dbt_rpc_run_sql(context: SolidExecutionContext, sql: String) -> DataFrame:
        resp = context.resources.dbt_rpc.run_sql(sql=sql, name=context.solid_config["name"])
        context.log.debug(resp.text)
        raise_for_rpc_error(context, resp)
        request_token = resp.json().get("result").get("request_token")
        result = unwrap_result(_poll_rpc(context, request_token))
        table = result.results[0].table
        return pd.DataFrame.from_records(data=table["rows"], columns=table["column_names"])

    return _dbt_rpc_run_sql
