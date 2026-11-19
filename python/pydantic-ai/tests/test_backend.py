"""`GroundboxSandboxBackend` over `FakeGroundbox`: lifecycle, error mapping, commands and files."""

from __future__ import annotations

import os
from typing import Any

import anyio
import pytest
from groundbox import GroundboxError
from pydantic_ai.workspaces import (
    Workspace,
    WorkspaceError,
    WorkspaceOutputLimitError,
    WorkspaceRef,
    WorkspaceTimeoutError,
    WorkspaceUnavailableError,
)

from pydantic_ai_groundbox import GroundboxSandboxBackend, _backend, delete_sandbox

from .fake_groundbox import FakeGroundbox, _error

pytestmark = [pytest.mark.anyio, pytest.mark.skipif(os.name != 'posix', reason='the fake runs on a POSIX host')]


def make(fake: FakeGroundbox, **kwargs: Any) -> GroundboxSandboxBackend:
    return GroundboxSandboxBackend(client=fake, **kwargs)  # type: ignore[arg-type]


def created(fake: FakeGroundbox) -> str:
    (sandbox_id,) = fake.sandboxes
    return sandbox_id


# -- construction and acquisition ---------------------------------------------------------------


async def test_building_does_no_io(fake: FakeGroundbox) -> None:
    backend = make(fake, create_options={'cpus': 1})
    assert backend.ref is None
    assert fake.calls == []


async def test_first_operation_creates_with_options_env_and_an_idempotency_key(fake: FakeGroundbox) -> None:
    backend = make(fake, env={'A': '1'}, create_options={'cpus': 1, 'metadata': {'k': 'v'}})
    result = await backend.run(['sh', '-c', 'printf "$A$B"'], env={'B': '2'})
    assert result.stdout == '12'
    ((args, kwargs),) = fake.called('create_sandbox')
    assert kwargs['cpus'] == 1 and kwargs['metadata'] == {'k': 'v'} and kwargs['env'] == {'A': '1'}
    assert kwargs['idempotency_key'].startswith('pydantic-ai-')
    assert backend.ref == WorkspaceRef(provider='groundbox', id=created(fake))


@pytest.mark.parametrize('bad', [{'idempotency_key': 'x'}, {'env': {'A': '1'}}])
def test_create_options_cannot_override_what_the_backend_sets(bad: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        GroundboxSandboxBackend(create_options=bad)


def test_rejects_another_providers_ref_and_a_relative_working_dir() -> None:
    with pytest.raises(ValueError):
        GroundboxSandboxBackend(ref=WorkspaceRef(provider='e2b', id='x'))
    with pytest.raises(ValueError):
        GroundboxSandboxBackend(working_dir='project')


async def test_waits_for_a_queued_sandbox(fake: FakeGroundbox) -> None:
    fake.create_statuses = ['queued', 'queued', 'starting']
    backend = make(fake)
    assert (await backend.run(['true'])).exit_code == 0
    assert len(fake.called('get_sandbox')) == 3


async def test_a_sandbox_that_fails_to_start_is_unavailable_but_keeps_its_ref(fake: FakeGroundbox) -> None:
    fake.create_statuses = ['queued', 'failed']
    backend = make(fake)
    with pytest.raises(WorkspaceUnavailableError, match='failed'):
        await backend.working_dir()
    assert backend.ref is not None


async def test_start_timeout_is_transient(fake: FakeGroundbox, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_backend, '_STATUS_POLL', 0.02)
    fake.create_statuses = ['queued'] * 1000
    backend = make(fake, start_timeout=0.05)
    with pytest.raises(TimeoutError):
        await backend.working_dir()
    assert backend.ref is not None


async def test_attach_resumes_a_paused_sandbox(fake: FakeGroundbox) -> None:
    first = make(fake)
    await first.working_dir()
    assert first.ref is not None
    fake.pause_sandbox(first.ref.id)
    attached = make(fake, ref=first.ref)
    assert (await attached.run(['true'])).exit_code == 0
    assert len(fake.called('resume_sandbox')) == 1
    assert attached.ref == first.ref


async def test_attach_starts_a_stopped_sandbox(fake: FakeGroundbox) -> None:
    first = make(fake)
    await first.working_dir()
    assert first.ref is not None
    fake.stop_sandbox(first.ref.id)
    attached = make(fake, ref=first.ref)
    assert (await attached.run(['true'])).exit_code == 0
    assert len(fake.called('start_sandbox')) == 1


async def test_attach_to_a_deleted_or_unknown_sandbox_is_unavailable(fake: FakeGroundbox) -> None:
    first = make(fake)
    await first.working_dir()
    assert first.ref is not None
    await delete_sandbox(first.ref.id, client=fake)  # type: ignore[arg-type]
    with pytest.raises(WorkspaceUnavailableError, match='deleted'):
        await make(fake, ref=first.ref).working_dir()
    with pytest.raises(WorkspaceUnavailableError, match='not found'):
        await make(fake, ref=WorkspaceRef(provider='groundbox', id='sbx_missing')).working_dir()
    assert len(fake.called('create_sandbox')) == 1  # attaching never creates a replacement


async def test_a_cancelled_create_still_records_the_ref(fake: FakeGroundbox) -> None:
    fake.create_delay = 0.3
    backend = make(fake)
    with anyio.move_on_after(0.05):
        await backend.working_dir()
    assert backend.ref is not None and backend.ref.id == created(fake)
    # The next operation uses that sandbox; the idempotency key would return it even if re-sent.
    await backend.working_dir()
    assert len(fake.sandboxes) == 1


async def test_working_dir_is_made_on_a_new_sandbox_only(fake: FakeGroundbox) -> None:
    project = str(fake.host_root / 'project')
    backend = make(fake, working_dir=project)
    assert await backend.working_dir() == project
    assert (await backend.run(['pwd'])).stdout == project + '\n'
    other = str(fake.host_root / 'other')
    attached = make(fake, ref=backend.ref, working_dir=other)
    with pytest.raises(WorkspaceError, match='not a folder'):
        await attached.working_dir()


# -- credentials and refusals ---------------------------------------------------------------------


async def test_missing_key_is_unavailable(tmp_path: Any) -> None:
    fake = FakeGroundbox(tmp_path, api_key=None)
    with pytest.raises(WorkspaceUnavailableError, match='GROUNDBOX_API_KEY'):
        await make(fake).working_dir()
    assert fake.calls == []


@pytest.mark.parametrize(
    ('failure', 'expected'),
    [
        (_error(401, 'unauthorized', 'Unknown API key'), 'rejected the API key'),
        (_error(402, 'spend_cap_reached', 'The key reached its monthly spend cap'), 'spend cap'),
        (_error(403, 'missing_scope', 'This key lacks sandboxes:write'), 'sandboxes:write'),
        (_error(404, 'not_found', 'No image python:nope'), 'No image'),
        (_error(400, 'bad_request', 'cpus: Too big'), 'Too big'),
    ],
)
async def test_refused_creates_are_unavailable(fake: FakeGroundbox, failure: GroundboxError, expected: str) -> None:
    fake.fail['create_sandbox'] = [failure]
    with pytest.raises(WorkspaceUnavailableError, match=expected):
        await make(fake).working_dir()


async def test_rate_limits_are_waited_out_then_propagate(fake: FakeGroundbox, monkeypatch: pytest.MonkeyPatch) -> None:
    limited = _error(429, 'rate_limited', 'Too many requests from this API key. Try again in 0.05 seconds')
    fake.fail['create_sandbox'] = [limited, limited]
    backend = make(fake)
    await backend.working_dir()
    assert len(fake.called('create_sandbox')) == 3
    monkeypatch.setattr(_backend, '_RATE_LIMIT_BUDGET', 0.0)
    fake.fail['run_command'] = [limited]
    with pytest.raises(GroundboxError) as caught:
        await make(fake, ref=backend.ref).working_dir()
    assert caught.value is limited


@pytest.mark.parametrize('status', [0, 500, 503])
async def test_transient_failures_propagate_unchanged(fake: FakeGroundbox, status: int) -> None:
    failure = _error(status, 'network' if status == 0 else 'unavailable', 'try again')
    fake.fail['create_sandbox'] = [failure]
    backend = make(fake)
    with pytest.raises(GroundboxError) as caught:
        await backend.working_dir()
    assert caught.value is failure
    # The retry reaches the API again with the same idempotency key.
    await backend.working_dir()
    keys = {kwargs['idempotency_key'] for _, kwargs in fake.called('create_sandbox')}
    assert len(keys) == 1


# -- commands -----------------------------------------------------------------------------------


async def test_command_result_and_undecodable_bytes(fake: FakeGroundbox) -> None:
    backend = make(fake)
    result = await backend.run('printf "out\\377"; printf err >&2; exit 3', shell=True)
    assert (result.exit_code, result.stdout, result.stderr) == (3, 'out�', 'err')


async def test_a_missing_program_exits_127(fake: FakeGroundbox) -> None:
    result = await make(fake).run(['definitely-not-a-program'])
    assert result.exit_code == 127


async def test_timeout_kills_the_command(fake: FakeGroundbox) -> None:
    backend = make(fake)
    with pytest.raises(WorkspaceTimeoutError) as caught:
        await backend.run(['sh', '-c', 'echo before; exec sleep 30'], timeout=1)
    assert caught.value.stdout == 'before\n'
    assert len(fake.called('kill_command')) == 1
    ((_, kwargs),) = fake.called('start_command')
    assert kwargs['timeout_s'] == 1 + _backend._SERVER_TIMEOUT_MARGIN_S


async def test_no_timeout_sends_the_longest_server_timeout(fake: FakeGroundbox) -> None:
    await make(fake).run(['true'])
    ((_, kwargs),) = fake.called('start_command')
    assert kwargs['timeout_s'] == _backend._MAX_COMMAND_TIMEOUT_S


async def test_a_server_timeout_is_a_timeout(fake: FakeGroundbox) -> None:
    backend = make(fake)
    await backend.working_dir()
    original = fake.get_command_output

    def timed_out(*args: Any, **kwargs: Any) -> dict[str, Any]:
        part = original(*args, **kwargs)
        return {**part, 'done': True, 'exit_code': 137, 'timed_out': True}

    fake.get_command_output = timed_out  # type: ignore[method-assign]
    with pytest.raises(WorkspaceTimeoutError):
        await backend.run(['sleep', '1'])


async def test_cancellation_kills_the_command(fake: FakeGroundbox) -> None:
    backend = make(fake)
    await backend.working_dir()
    with anyio.move_on_after(0.5):
        await backend.run(['sleep', '30'])
    assert len(fake.called('kill_command')) == 1


async def test_output_over_the_limit_is_an_error(fake: FakeGroundbox, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_backend, '_MAX_OUTPUT_BYTES', 1000)
    backend = make(fake)
    with pytest.raises(WorkspaceOutputLimitError) as caught:
        await backend.run(['sh', '-c', 'head -c 5000 /dev/zero | tr "\\0" a; exec sleep 30'])
    assert caught.value.stdout.startswith('aaa')
    assert len(fake.called('kill_command')) == 1


async def test_output_the_sandbox_dropped_is_an_error(fake: FakeGroundbox) -> None:
    backend = make(fake)
    await backend.working_dir()
    original = fake.get_command_output

    def skipped(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {**original(*args, **kwargs), 'skipped_bytes': 10}

    fake.get_command_output = skipped  # type: ignore[method-assign]
    with pytest.raises(WorkspaceOutputLimitError):
        await backend.run(['true'])


async def test_a_sandbox_deleted_during_a_command_is_unavailable(fake: FakeGroundbox) -> None:
    backend = make(fake)
    await backend.working_dir()
    assert backend.ref is not None
    sandbox_id = backend.ref.id

    async def delete_soon() -> None:
        await anyio.sleep(0.3)
        fake.delete_sandbox(sandbox_id)

    with pytest.RaisesGroup(pytest.RaisesExc(WorkspaceUnavailableError, match='deleted')):
        async with anyio.create_task_group() as group:
            group.start_soon(delete_soon)
            await backend.run(['sleep', '30'])


async def test_a_sandbox_paused_mid_run_is_unavailable(fake: FakeGroundbox) -> None:
    backend = make(fake)
    await backend.working_dir()
    assert backend.ref is not None
    fake.pause_sandbox(backend.ref.id)
    with pytest.raises(WorkspaceUnavailableError, match='paused, not running'):
        await backend.run(['true'])


async def test_command_argument_checks(fake: FakeGroundbox) -> None:
    backend = make(fake)
    with pytest.raises(TypeError):
        await backend.run('true')
    with pytest.raises(TypeError):
        await backend.run(['true'], shell=True)
    with pytest.raises(ValueError):
        await backend.run(['true'], timeout=0)
    with pytest.raises(TypeError):
        await backend.run(['true'], env={'A': 1})  # type: ignore[dict-item]
    assert fake.calls == []


@pytest.mark.parametrize('anyio_backend', ['trio'])
async def test_works_under_trio(fake: FakeGroundbox, anyio_backend: str) -> None:
    backend = make(fake)
    workspace = Workspace(backend)
    await workspace.write_text('trio.txt', 'hi')
    assert (await backend.run(['cat', 'trio.txt'])).stdout == 'hi'
    with pytest.raises(WorkspaceTimeoutError):
        await backend.run(['sleep', '30'], timeout=0.5)


# -- files --------------------------------------------------------------------------------------


async def test_file_operations(fake: FakeGroundbox) -> None:
    backend = make(fake)
    workspace = Workspace(backend)
    root = await backend.working_dir()
    await workspace.write_bytes('dir/a.bin', b'\x00\x01')
    assert await backend.read_bytes(f'{root}/dir/a.bin') == b'\x00\x01'
    entry = await backend.stat(f'{root}/dir/a.bin')
    assert (entry.name, entry.is_dir, entry.size) == ('a.bin', False, 2)
    assert (await backend.stat(f'{root}/dir')).is_dir
    assert [e.name for e in await backend.list_dir(f'{root}/dir')] == ['a.bin']
    with pytest.raises(IsADirectoryError):
        await backend.read_bytes(f'{root}/dir')
    with pytest.raises(IsADirectoryError):
        await backend.write_bytes(f'{root}/dir', b'x')
    with pytest.raises(NotADirectoryError):
        await backend.write_bytes(f'{root}/dir/a.bin/child', b'x')
    with pytest.raises(NotADirectoryError):
        await backend.list_dir(f'{root}/dir/a.bin')
    with pytest.raises(FileExistsError):
        await backend.make_dir(f'{root}/dir/a.bin')
    with pytest.raises(FileNotFoundError):
        await backend.read_bytes(f'{root}/missing')
    with pytest.raises(FileNotFoundError):
        await backend.remove(f'{root}/missing')
    assert not await backend.exists(f'{root}/missing')
    await backend.remove(f'{root}/dir')
    assert not await backend.exists(f'{root}/dir')


async def test_symlinks_are_followed_and_written_through(fake: FakeGroundbox) -> None:
    backend = make(fake)
    root = await backend.working_dir()
    script = 'echo old > target; mkdir d; ln -s target link; ln -s d dlink; ln -s nowhere dangling; mkfifo fifo'
    assert (await backend.run(['sh', '-c', script])).exit_code == 0
    await backend.write_bytes(f'{root}/link', b'new')
    assert await backend.read_bytes(f'{root}/target') == b'new'
    assert await backend.read_bytes(f'{root}/link') == b'new'
    assert (await backend.stat(f'{root}/dlink')).is_dir
    assert (await backend.stat(f'{root}/link')).size == 3
    with pytest.raises(FileNotFoundError):
        await backend.stat(f'{root}/dangling')
    assert not await backend.exists(f'{root}/dangling')
    entries = {e.name: (e.is_dir, e.size) for e in await backend.list_dir(root)}
    assert entries['dlink'] == (True, None) and entries['link'] == (False, 3) and entries['dangling'] == (False, None)
    with pytest.raises(OSError, match='regular file'):
        await backend.read_bytes(f'{root}/fifo')
    await backend.remove(f'{root}/dangling')
    assert 'dangling' not in {e.name for e in await backend.list_dir(root)}


async def test_unrecognised_file_api_failures_propagate(fake: FakeGroundbox) -> None:
    backend = make(fake)
    root = await backend.working_dir()
    failure = _error(502, 'internal', 'The sandbox could not do that', {'reason': 'socket hang up'})
    fake.fail['write_file'] = [failure]
    with pytest.raises(GroundboxError) as caught:
        await backend.write_bytes(f'{root}/f', b'x')
    assert caught.value is failure


async def test_get_sandbox_and_client(fake: FakeGroundbox) -> None:
    backend = make(fake)
    sandbox = await backend.get_sandbox()
    assert sandbox['status'] == 'running' and backend.client is fake


async def test_delete_sandbox(fake: FakeGroundbox) -> None:
    backend = make(fake)
    await backend.working_dir()
    assert backend.ref is not None
    await delete_sandbox(backend.ref.id, client=fake)  # type: ignore[arg-type]
    await delete_sandbox(backend.ref.id, client=fake)  # type: ignore[arg-type]  # already deleted: quiet
    await delete_sandbox('sbx_unknown', client=fake)  # type: ignore[arg-type]  # never existed: quiet
    fake.fail['delete_sandbox'] = [_error(401, 'unauthorized', 'Unknown API key')]
    with pytest.raises(WorkspaceUnavailableError):
        await delete_sandbox(backend.ref.id, client=fake)  # type: ignore[arg-type]
