"""Capability that supplies a Groundbox sandbox to an agent run."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.exceptions import UserError
from pydantic_ai.messages import Authority
from pydantic_ai.tools import AgentDepsT, RunContext
from pydantic_ai.workspaces import AuthorityWorkspace, Workspace, WorkspaceBackend, WorkspaceRef

from ._backend import DEFAULT_START_TIMEOUT, PROVIDER, GroundboxSandboxBackend, delete_sandbox
from ._util import check_env_types, check_path_authority, check_positive_int

if TYPE_CHECKING:
    from groundbox import Groundbox


@dataclass(kw_only=True)
class GroundboxSandbox(AbstractCapability[AgentDepsT]):
    """Supply an isolated [Groundbox](https://groundbox.dev) sandbox as the run's workspace.

    A run with no workspace ref creates a fresh sandbox; a run given a `WorkspaceRef` (from
    `workspace=` or the message history) attaches to that sandbox, resuming it if it is paused and
    starting it if it was stopped.

    This capability supplies execution only. Compose it with tools or capabilities that use the
    workspace, such as `Coder`, `Shell` or `FileSystem` from pydantic-ai-harness.
    """

    client: Groundbox | None = None
    """A `groundbox.Groundbox` to create, attach and delete with; one reading `GROUNDBOX_API_KEY` from the
    environment when `None`. A spec cannot set it."""

    cpus: int | None = None
    """vCPUs for a new sandbox (1 to 16); Groundbox's default (2) when `None`."""

    memory_gb: int | None = None
    """Memory for a new sandbox, up to 4 GB per vCPU; 2 GB per vCPU when `None`."""

    disk_gb: int | None = None
    """The smallest disk for a new sandbox; 20 GB, or the image's size, when `None`."""

    timeout_minutes: int | None = None
    """Lifetime of a new sandbox in minutes, from its start (up to 43,200); 60 when `None`.

    What happens then is `on_timeout`.
    """

    on_timeout: Literal['delete', 'stop', 'pause'] | None = None
    """What a new sandbox's timeout does: `'delete'` (Groundbox's default) ends it, `'stop'` keeps its disk,
    `'pause'` keeps memory and disk. `'stop'` and `'pause'` need the paid plan; a later run attaching by ref
    starts or resumes the sandbox."""

    idle_timeout_seconds: int | None = None
    """Pause a new sandbox after this many idle seconds (30 at least); the next command or file call wakes it."""

    template: str | None = None
    """A template (id, name or `name:version`) for a new sandbox; the team's default, else Groundbox's image."""

    image: str | None = None
    """A public Docker image (or one in your Groundbox registry) to start a new sandbox from; not with `template`.

    Commands then run as root in the image's home directory. The first sandbox from an image waits while the
    image is turned into a disk.
    """

    allow_hosts: Sequence[str] | None = None
    """Egress allowlist for a new sandbox: it reaches these hosts (names, `*.wildcards`, IPv4 addresses or
    networks) and nothing else."""

    deny_hosts: Sequence[str] | None = None
    """Egress deny list for a new sandbox: it reaches everything but these."""

    secrets: Sequence[str] | None = None
    """Team secrets (see `Groundbox.set_secret`) given to every command in a new sandbox as variables of the same name.

    The values never pass through your application or the model.
    """

    metadata: Mapping[str, str] | None = None
    """Labels for a new sandbox, for `list_sandboxes(metadata=...)`."""

    create_options: Mapping[str, Any] | None = field(default=None, repr=False)
    """Any other `Groundbox.create_sandbox` arguments for a new sandbox (`snapshot_id`, `flex`, `volumes`,
    `env_from_secrets`, `desktop`, ...); the named fields above win."""

    working_dir: str | None = None
    """Absolute directory commands start in and relative paths resolve against, made on a new sandbox;
    the sandbox's home directory (`/home/runner`) when `None`."""

    env: Mapping[str, str] | None = field(default=None, repr=False)
    """Variables every command gets, also on an attached sandbox; nothing is read from the host."""

    start_timeout: float = DEFAULT_START_TIMEOUT
    """Seconds a new or attached sandbox may take to reach `running` (an image's first build can take minutes)."""

    authority: Authority = 'operator'
    """The [authority][pydantic_ai.messages.Authority] of the sandbox's files: `'none'` for files nobody on the
    application's side wrote, such as a cloned third-party repository, and `'user'` for the user's own files.
    Anything below `'operator'` wraps the workspace in an `AuthorityWorkspace`."""

    path_authority: Mapping[str, Authority] | None = None
    """Levels for some directories and files, overriding `authority` for the paths under them."""

    def __post_init__(self) -> None:
        if getattr(self, 'defer_loading', False):
            raise UserError(
                '`GroundboxSandbox` does not support `defer_loading=True`: '
                'the workspace is selected before deferred capabilities load.'
            )
        for name in ('cpus', 'memory_gb', 'disk_gb', 'timeout_minutes', 'idle_timeout_seconds'):
            check_positive_int(name, getattr(self, name))
        if self.template is not None and self.image is not None:
            raise UserError('Pass either `template` or `image`, not both.')
        if self.working_dir is not None and not self.working_dir.startswith('/'):
            raise UserError(f'working_dir must be an absolute path in the sandbox or None, got {self.working_dir!r}.')
        check_path_authority(self.path_authority)
        try:
            check_env_types(self.env)
        except TypeError:
            raise UserError('`env` keys and values must be strings.') from None
        if self.create_options is not None and ({'env', 'idempotency_key'} & set(self.create_options)):
            raise UserError('Set `env` on the capability; `idempotency_key` is set by the backend.')

    @classmethod
    def from_spec(
        cls,
        *,
        id: str | None = None,
        description: str | None = None,
        cpus: int | None = None,
        memory_gb: int | None = None,
        disk_gb: int | None = None,
        timeout_minutes: int | None = None,
        on_timeout: Literal['delete', 'stop', 'pause'] | None = None,
        idle_timeout_seconds: int | None = None,
        template: str | None = None,
        image: str | None = None,
        allow_hosts: Sequence[str] | None = None,
        deny_hosts: Sequence[str] | None = None,
        secrets: Sequence[str] | None = None,
        metadata: Mapping[str, str] | None = None,
        working_dir: str | None = None,
        env: Mapping[str, str] | None = None,
        start_timeout: float = DEFAULT_START_TIMEOUT,
        authority: Authority = 'operator',
        path_authority: Mapping[str, Authority] | None = None,
    ) -> GroundboxSandbox[Any]:
        """Build from an agent spec, naming the fields a spec can express (not `client` or `create_options`)."""
        extra: dict[str, Any] = {}
        if id is not None:
            extra['id'] = id
        if description is not None:
            extra['description'] = description
        return cls(
            cpus=cpus,
            memory_gb=memory_gb,
            disk_gb=disk_gb,
            timeout_minutes=timeout_minutes,
            on_timeout=on_timeout,
            idle_timeout_seconds=idle_timeout_seconds,
            template=template,
            image=image,
            allow_hosts=allow_hosts,
            deny_hosts=deny_hosts,
            secrets=secrets,
            metadata=metadata,
            working_dir=working_dir,
            env=env,
            start_timeout=start_timeout,
            authority=authority,
            path_authority=path_authority,
            **extra,
        )

    def _create_options(self) -> dict[str, Any]:
        options: dict[str, Any] = dict(self.create_options or {})
        named = {
            'cpus': self.cpus,
            'memory_gb': self.memory_gb,
            'disk_gb': self.disk_gb,
            'timeout_minutes': self.timeout_minutes,
            'on_timeout': self.on_timeout,
            'idle_timeout_seconds': self.idle_timeout_seconds,
            'template': self.template,
            'image': self.image,
            'allow_hosts': list(self.allow_hosts) if self.allow_hosts is not None else None,
            'deny_hosts': list(self.deny_hosts) if self.deny_hosts is not None else None,
            'secrets': list(self.secrets) if self.secrets is not None else None,
            'metadata': dict(self.metadata) if self.metadata is not None else None,
        }
        options.update({name: value for name, value in named.items() if value is not None})
        return options

    def backend(self, ref: WorkspaceRef) -> GroundboxSandboxBackend:
        """A backend that attaches lazily to the existing sandbox `ref` names."""
        if ref.provider != PROVIDER:
            raise ValueError(f'unsupported workspace provider {ref.provider!r}; expected {PROVIDER!r}')
        return GroundboxSandboxBackend(
            client=self.client,
            ref=ref,
            working_dir=self.working_dir,
            env=self.env,
            start_timeout=self.start_timeout,
        )

    async def destroy(self, ref: WorkspaceRef) -> None:
        """Delete the sandbox `ref` names: billing stops and its disk is gone. Already deleted is fine."""
        if ref.provider != PROVIDER:
            raise ValueError(f'unsupported workspace provider {ref.provider!r}; expected {PROVIDER!r}')
        await delete_sandbox(ref.id, client=self.client)

    def get_workspace(self, ctx: RunContext[AgentDepsT], *, ref: WorkspaceRef | None) -> WorkspaceBackend | None:
        """The backend for this run, built without I/O: it creates or attaches on first use.

        Returns `None` for a ref of another provider, leaving it to another capability.
        """
        del ctx
        if ref is not None and ref.provider != PROVIDER:
            return None
        backend = GroundboxSandboxBackend(
            client=self.client,
            ref=ref,
            working_dir=self.working_dir,
            env=self.env,
            start_timeout=self.start_timeout,
            create_options=self._create_options(),
        )
        if self.authority == 'operator' and not self.path_authority:
            return backend
        return AuthorityWorkspace(Workspace(backend), authority=self.authority, path_authority=self.path_authority)
