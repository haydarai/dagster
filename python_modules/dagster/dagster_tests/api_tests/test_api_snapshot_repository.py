import re
import sys
from contextlib import contextmanager

import grpc
import pytest

from dagster import lambda_solid, pipeline, repository
from dagster.api.snapshot_repository import (
    sync_get_external_repositories,
    sync_get_external_repositories_grpc,
    sync_get_streaming_external_repositories_grpc,
)
from dagster.core.host_representation import ExternalRepository
from dagster.core.host_representation.handle import RepositoryLocationHandle
from dagster.core.types.loadable_target_origin import LoadableTargetOrigin

from .utils import (
    get_bar_repo_grpc_repository_location_handle,
    get_bar_repo_repository_location_handle,
)


def test_external_repositories_api():
    repository_location_handle = get_bar_repo_repository_location_handle()
    external_repos = sync_get_external_repositories(repository_location_handle)

    assert len(external_repos) == 1

    external_repository = external_repos[0]

    assert isinstance(external_repository, ExternalRepository)
    assert external_repository.name == "bar_repo"


def test_external_repositories_api_grpc():
    with get_bar_repo_grpc_repository_location_handle() as repository_location_handle:
        external_repos = sync_get_external_repositories_grpc(
            repository_location_handle.client, repository_location_handle
        )

        assert len(external_repos) == 1

        external_repository = external_repos[0]

        assert isinstance(external_repository, ExternalRepository)
        assert external_repository.name == "bar_repo"


def test_streaming_external_repositories_api_grpc():
    with get_bar_repo_grpc_repository_location_handle() as repository_location_handle:
        external_repos = sync_get_streaming_external_repositories_grpc(
            repository_location_handle.client, repository_location_handle
        )

        assert len(external_repos) == 1

        external_repository = external_repos[0]

        assert isinstance(external_repository, ExternalRepository)
        assert external_repository.name == "bar_repo"


@lambda_solid
def do_something():
    return 1


@pipeline
def giant_pipeline():
    # Pipeline big enough to be larger than the max size limit for a gRPC message in its
    # external repository
    for _i in range(20000):
        do_something()


@repository
def giant_repo():
    return {
        "pipelines": {"giant": giant_pipeline,},
    }


@contextmanager
def get_giant_repo_grpc_repository_location_handle():
    handle = RepositoryLocationHandle.create_process_bound_grpc_server_location(
        loadable_target_origin=LoadableTargetOrigin(
            attribute="giant_repo",
            module_name="dagster_tests.api_tests.test_api_snapshot_repository",
        ),
        location_name="giant_repo_location",
    )
    try:
        yield handle
    finally:
        handle.cleanup()


def test_giant_external_repository():
    repository_location_handle = RepositoryLocationHandle.create_python_env_location(
        loadable_target_origin=LoadableTargetOrigin(
            executable_path=sys.executable,
            module_name="dagster_tests.api_tests.test_api_snapshot_repository",
            attribute="giant_repo",
        ),
        location_name="giant_repo_location",
    )
    external_repos = sync_get_external_repositories(repository_location_handle)

    assert len(external_repos) == 1

    external_repository = external_repos[0]

    assert isinstance(external_repository, ExternalRepository)
    assert external_repository.name == "giant_repo"


def test_giant_external_repository_grpc():
    with get_giant_repo_grpc_repository_location_handle() as repository_location_handle:

        # using default gRPC limit causes the large repo to be unloadable
        with pytest.raises(
            grpc._channel._InactiveRpcError,  # pylint: disable=protected-access
            match=re.escape("Received message larger than max"),
        ):
            sync_get_external_repositories_grpc(
                repository_location_handle.client, repository_location_handle,
            )


def test_giant_external_repository_streaming_grpc():
    with get_giant_repo_grpc_repository_location_handle() as repository_location_handle:
        # Using streaming allows the giant repo to load
        external_repos = sync_get_streaming_external_repositories_grpc(
            repository_location_handle.client, repository_location_handle
        )

        assert len(external_repos) == 1

        external_repository = external_repos[0]

        assert isinstance(external_repository, ExternalRepository)
        assert external_repository.name == "giant_repo"
