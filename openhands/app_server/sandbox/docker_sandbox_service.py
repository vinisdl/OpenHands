import asyncio
import logging
import os
import socket
import typing
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import AsyncGenerator

import base62
import docker
import httpx
from docker.errors import APIError, NotFound
from fastapi import Request
from pydantic import BaseModel, ConfigDict, Field

from openhands.agent_server.utils import utc_now
from openhands.app_server.errors import SandboxError
from openhands.app_server.sandbox.docker_sandbox_spec_service import get_docker_client
from openhands.app_server.sandbox.sandbox_models import (
    AGENT_SERVER,
    VSCODE,
    WORKER_1,
    WORKER_2,
    ExposedUrl,
    SandboxInfo,
    SandboxPage,
    SandboxStatus,
)
from openhands.app_server.sandbox.sandbox_service import (
    ALLOW_CORS_ORIGINS_VARIABLE,
    SESSION_API_KEY_VARIABLE,
    WEBHOOK_CALLBACK_VARIABLE,
    SandboxService,
    SandboxServiceInjector,
)
from openhands.app_server.sandbox.sandbox_spec_service import SandboxSpecService
from openhands.app_server.services.injector import InjectorState
from openhands.app_server.utils.docker_utils import (
    replace_localhost_hostname_for_docker,
)

_logger = logging.getLogger(__name__)
STARTUP_GRACE_SECONDS = 15


class VolumeMount(BaseModel):
    """Mounted volume within the container."""

    host_path: str
    container_path: str
    mode: str = 'rw'

    model_config = ConfigDict(frozen=True)


class ExposedPort(BaseModel):
    """Exposed port within container to be matched to a free port on the host."""

    name: str
    description: str
    container_port: int = 8000

    model_config = ConfigDict(frozen=True)


@dataclass
class DockerSandboxService(SandboxService):
    """Sandbox service built on docker.

    The Docker API does not currently support async operations, so some of these operations will block.
    Given that the docker API is intended for local use on a single machine, this is probably acceptable.
    """

    sandbox_spec_service: SandboxSpecService
    container_name_prefix: str
    host_port: int
    container_url_pattern: str
    mounts: list[VolumeMount]
    exposed_ports: list[ExposedPort]
    health_check_path: str | None
    httpx_client: httpx.AsyncClient
    max_num_sandboxes: int
    web_url: str | None = None
    extra_hosts: dict[str, str] = field(default_factory=dict)
    docker_client: docker.DockerClient = field(default_factory=get_docker_client)
    startup_grace_seconds: int = STARTUP_GRACE_SECONDS
    use_host_network: bool = False

    def _find_unused_port(self) -> int:
        """Find an unused port on the host machine."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(('', 0))
            s.listen(1)
            port = s.getsockname()[1]
        return port

    def _docker_status_to_sandbox_status(self, docker_status: str) -> SandboxStatus:
        """Convert Docker container status to SandboxStatus."""
        status_mapping = {
            'running': SandboxStatus.RUNNING,
            'paused': SandboxStatus.PAUSED,
            # The stop button was pressed in the docker console
            'exited': SandboxStatus.PAUSED,
            'created': SandboxStatus.STARTING,
            'restarting': SandboxStatus.STARTING,
            'removing': SandboxStatus.MISSING,
            'dead': SandboxStatus.ERROR,
        }
        return status_mapping.get(docker_status.lower(), SandboxStatus.ERROR)

    def _get_container_env_vars(self, container, attrs: dict | None = None) -> dict[str, str | None]:
        """Get container environment variables from attrs.

        Args:
            container: Docker container object
            attrs: Optional pre-loaded container attrs to avoid additional API calls
        """
        if attrs is None:
            attrs = container.attrs
        env_vars_list = attrs['Config']['Env']
        result = {}
        for env_var in env_vars_list:
            if '=' in env_var:
                key, value = env_var.split('=', 1)
                result[key] = value
            else:
                # Handle cases where an environment variable might not have a value
                result[env_var] = None
        return result

    async def _container_to_sandbox_info(self, container) -> SandboxInfo | None:
        """Convert Docker container to SandboxInfo."""
        # Reload container once to get fresh data and cache attrs to avoid multiple HTTP requests
        try:
            container.reload()
        except (NotFound, APIError):
            return None

        # Cache attrs to avoid multiple HTTP requests
        attrs = container.attrs

        # Convert Docker status to runtime status
        status = self._docker_status_to_sandbox_status(container.status)

        # Parse creation time
        created_str = attrs.get('Created', '')
        try:
            created_at = datetime.fromisoformat(created_str.replace('Z', '+00:00'))
        except (ValueError, AttributeError):
            created_at = utc_now()

        # Get URL and session key for running containers
        exposed_urls = None
        session_api_key = None

        if status == SandboxStatus.RUNNING:
            # Get session API key first (pass attrs to avoid additional API call)
            env = self._get_container_env_vars(container, attrs)
            session_api_key = env.get(SESSION_API_KEY_VARIABLE)

            # Get the exposed port mappings
            exposed_urls = []

            # Check if container is using host network mode
            network_mode = attrs.get('HostConfig', {}).get('NetworkMode', '')
            is_host_network = network_mode == 'host'

            # Check if we should use Traefik path-based URLs
            # NOTE: With host network, we always use localhost, not Traefik paths
            base_domain = os.environ.get('VITE_BACKEND_BASE_URL', 'localhost')
            use_traefik_paths = base_domain != 'localhost' and not is_host_network

            if is_host_network:
                # Host network mode: container ports are directly accessible on host
                # Always use localhost, not Traefik paths (container is on host network)
                for exposed_port in self.exposed_ports:
                    host_port = exposed_port.container_port

                    # Use port-based URLs for localhost (host network doesn't use Traefik)
                    url = self.container_url_pattern.format(port=host_port)
                    # VSCode URLs require the api_key and working dir
                    if exposed_port.name == VSCODE:
                        url += f'/?tkn={session_api_key}&folder={attrs["Config"]["WorkingDir"]}'

                    exposed_urls.append(
                        ExposedUrl(
                            name=exposed_port.name,
                            url=url,
                            port=host_port,
                        )
                    )
            else:
                # Bridge network mode: use port bindings
                port_bindings = attrs.get('NetworkSettings', {}).get(
                    'Ports', {}
                )
                if port_bindings:
                    for container_port, host_bindings in port_bindings.items():
                        if host_bindings:
                            host_port = int(host_bindings[0]['HostPort'])
                            matching_port = next(
                                (
                                    ep
                                    for ep in self.exposed_ports
                                    if container_port == f'{ep.container_port}/tcp'
                                ),
                                None,
                            )
                            if matching_port:
                                if use_traefik_paths:
                                    # Use Traefik path-based URLs
                                    protocol = 'https' if 'https' in base_domain or not base_domain.startswith('http') else 'http'
                                    base_url = base_domain if base_domain.startswith('http') else f'{protocol}://{base_domain}'

                                    if matching_port.name == AGENT_SERVER:
                                        # Base URL without /api - the /api will be added by the caller
                                        url = f'{base_url}/{container.name}'
                                    elif matching_port.name == VSCODE:
                                        url = f'{base_url}/{container.name}/vscode'
                                    elif matching_port.name == WORKER_1:
                                        url = f'{base_url}/{container.name}/app1'
                                    elif matching_port.name == WORKER_2:
                                        url = f'{base_url}/{container.name}/app2'
                                    else:
                                        url = f'{base_url}/{container.name}/{matching_port.name.lower()}'

                                    # VSCode URLs require the api_key and working dir
                                    if matching_port.name == VSCODE:
                                        url += f'/?tkn={session_api_key}&folder={attrs["Config"]["WorkingDir"]}'
                                else:
                                    # Use port-based URLs for localhost
                                    url = self.container_url_pattern.format(port=host_port)
                                    # VSCode URLs require the api_key and working dir
                                    if matching_port.name == VSCODE:
                                        url += f'/?tkn={session_api_key}&folder={attrs["Config"]["WorkingDir"]}'

                                exposed_urls.append(
                                    ExposedUrl(
                                        name=matching_port.name,
                                        url=url,
                                        port=host_port,
                                    )
                                )

        # Get image from attrs to avoid additional API call (container.image.tags makes HTTP request)
        image_id = attrs.get('Config', {}).get('Image', '')
        if not image_id:
            # Fallback: try to get from Image field or container.image
            image_id = attrs.get('Image', '')
        if not image_id and hasattr(container, 'image') and container.image:
            # Last resort: use container.image.tags (this makes an HTTP request)
            image_id = container.image.tags[0] if container.image.tags else 'unknown'
        elif not image_id:
            image_id = 'unknown'

        return SandboxInfo(
            id=container.name,
            created_by_user_id=None,
            sandbox_spec_id=image_id,
            status=status,
            session_api_key=session_api_key,
            exposed_urls=exposed_urls,
            created_at=created_at,
        )

    async def _container_to_checked_sandbox_info(self, container) -> SandboxInfo | None:
        sandbox_info = await self._container_to_sandbox_info(container)
        if (
            sandbox_info
            and self.health_check_path is not None
            and sandbox_info.exposed_urls
        ):
            app_server_url = next(
                exposed_url.url
                for exposed_url in sandbox_info.exposed_urls
                if exposed_url.name == AGENT_SERVER
            )

            # Build health check URL
            # For Traefik paths, health check should use /api/health
            # For port-based URLs, use the health_check_path directly
            base_domain = os.environ.get('VITE_BACKEND_BASE_URL', 'localhost')

            # Check if container is using host network mode
            # Get attrs (container.attrs is cached after _container_to_sandbox_info, but reload to be safe)
            try:
                container.reload()
            except (NotFound, APIError):
                pass
            attrs = container.attrs
            network_mode = attrs.get('HostConfig', {}).get('NetworkMode', '')
            is_host_network = network_mode == 'host'

            # NOTE: With host network, we always use localhost, not Traefik paths
            use_traefik_paths = base_domain != 'localhost' and not is_host_network

            if use_traefik_paths:
                # Traefik path: health check is at /{container_name}/alive
                # app_server_url is already {base_url}/{container_name}, so we add /alive
                # Note: After Traefik strips /{container_name}, the agent-server receives /alive
                # The agent-server has /alive (not /api/alive) for health checks
                health_url = f'{app_server_url}/alive'
            elif is_host_network:
                # Host network: container ports are directly on host
                # With host network, both OpenHands and agent-server share the host network,
                # so localhost works directly (no need for host.docker.internal)
                # Extract port from app_server_url (format: http://localhost:PORT)
                import re
                port_match = re.search(r':(\d+)(?:/|$)', app_server_url)
                if port_match:
                    port = port_match.group(1)
                    # With host network, use localhost directly (both containers share host network)
                    health_url = f'http://localhost:{port}{self.health_check_path}'
                else:
                    # Fallback: use replace_localhost_hostname_for_docker
                    app_server_url_for_check = replace_localhost_hostname_for_docker(app_server_url)
                    health_url = f'{app_server_url_for_check}{self.health_check_path}'
            else:
                # Bridge network: use replace_localhost_hostname_for_docker and append health_check_path
                app_server_url_for_check = replace_localhost_hostname_for_docker(app_server_url)
                health_url = f'{app_server_url_for_check}{self.health_check_path}'

            # Set timeout based on configuration
            if use_traefik_paths:
                timeout = 15.0  # Longer timeout for Traefik paths (routing can be slower)
            elif is_host_network:
                timeout = 10.0  # Longer timeout for host network
            else:
                timeout = 5.0  # Standard timeout for bridge network

            try:
                _logger.info(
                    f'Checking sandbox health: {health_url} '
                    f'(host_network={is_host_network}, traefik={use_traefik_paths}, timeout={timeout}s, '
                    f'container={container.name}, status={container.status})'
                )
                response = await self.httpx_client.get(health_url, timeout=timeout)
                response.raise_for_status()
                _logger.info(f'Sandbox health check passed: {health_url}')
                # Health check passed - ensure status is RUNNING if container is running
                if container.status == 'running' and sandbox_info.status != SandboxStatus.RUNNING:
                    _logger.info(f'Updating sandbox status to RUNNING after successful health check')
                    sandbox_info.status = SandboxStatus.RUNNING
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Check if we're past the grace period
                grace_period_elapsed = sandbox_info.created_at < utc_now() - timedelta(
                    seconds=self.startup_grace_seconds
                )
                if grace_period_elapsed:
                    _logger.error(
                        f'Sandbox server not running: {health_url} : {exc} '
                        f'(container_status={container.status}, host_network={is_host_network}, '
                        f'traefik={use_traefik_paths}, grace_period_elapsed={grace_period_elapsed}, '
                        f'created_at={sandbox_info.created_at}, now={utc_now()})',
                        exc_info=True,
                    )
                    sandbox_info.status = SandboxStatus.ERROR
                else:
                    _logger.debug(
                        f'Sandbox health check failed (within grace period): {health_url} : {exc}',
                        exc_info=True,
                    )
                    sandbox_info.status = SandboxStatus.STARTING
                sandbox_info.exposed_urls = None
                sandbox_info.session_api_key = None
        return sandbox_info

    async def search_sandboxes(
        self,
        page_id: str | None = None,
        limit: int = 100,
    ) -> SandboxPage:
        """Search for sandboxes."""
        try:
            # Get all containers with our prefix
            all_containers = self.docker_client.containers.list(all=True)
            sandboxes = []

            for container in all_containers:
                if container.name and container.name.startswith(
                    self.container_name_prefix
                ):
                    sandbox_info = await self._container_to_checked_sandbox_info(
                        container
                    )
                    if sandbox_info:
                        sandboxes.append(sandbox_info)

            # Sort by creation time (newest first)
            sandboxes.sort(key=lambda x: x.created_at, reverse=True)

            # Apply pagination
            start_idx = 0
            if page_id:
                try:
                    start_idx = int(page_id)
                except ValueError:
                    start_idx = 0

            end_idx = start_idx + limit
            paginated_containers = sandboxes[start_idx:end_idx]

            # Determine next page ID
            next_page_id = None
            if end_idx < len(sandboxes):
                next_page_id = str(end_idx)

            return SandboxPage(items=paginated_containers, next_page_id=next_page_id)

        except APIError:
            return SandboxPage(items=[], next_page_id=None)

    async def get_sandbox(self, sandbox_id: str) -> SandboxInfo | None:
        """Get a single sandbox info."""
        try:
            if not sandbox_id.startswith(self.container_name_prefix):
                return None
            container = self.docker_client.containers.get(sandbox_id)
            return await self._container_to_checked_sandbox_info(container)
        except (NotFound, APIError):
            return None

    async def get_sandbox_by_session_api_key(
        self, session_api_key: str
    ) -> SandboxInfo | None:
        """Get a single sandbox by session API key."""
        try:
            # Get all containers with our prefix
            all_containers = self.docker_client.containers.list(all=True)

            for container in all_containers:
                if container.name and container.name.startswith(
                    self.container_name_prefix
                ):
                    # Check if this container has the matching session API key
                    env_vars = self._get_container_env_vars(container)
                    container_session_key = env_vars.get(SESSION_API_KEY_VARIABLE)

                    if container_session_key == session_api_key:
                        return await self._container_to_checked_sandbox_info(container)

            return None
        except (NotFound, APIError):
            return None

    async def start_sandbox(
        self, sandbox_spec_id: str | None = None, sandbox_id: str | None = None
    ) -> SandboxInfo:
        """Start a new sandbox."""
        # Warn about port collision risk when using host network mode with multiple sandboxes
        if self.use_host_network and self.max_num_sandboxes > 1:
            _logger.warning(
                'Host network mode is enabled with max_num_sandboxes > 1. '
                'Multiple sandboxes will attempt to bind to the same ports, '
                'which may cause port collision errors. Consider setting '
                'max_num_sandboxes=1 when using host network mode.'
            )

        # Enforce sandbox limits by cleaning up old sandboxes
        await self.pause_old_sandboxes(self.max_num_sandboxes - 1)

        if sandbox_spec_id is None:
            sandbox_spec = await self.sandbox_spec_service.get_default_sandbox_spec()
        else:
            sandbox_spec_maybe = await self.sandbox_spec_service.get_sandbox_spec(
                sandbox_spec_id
            )
            if sandbox_spec_maybe is None:
                raise ValueError('Sandbox Spec not found')
            sandbox_spec = sandbox_spec_maybe

        # Generate a sandbox id if none was provided
        if sandbox_id is None:
            sandbox_id = base62.encodebytes(os.urandom(16))

        # Generate container name and session api key
        container_name = f'{self.container_name_prefix}{sandbox_id}'
        session_api_key = base62.encodebytes(os.urandom(32))

        # Prepare environment variables
        env_vars = sandbox_spec.initial_env.copy()
        # Set both OH_SESSION_API_KEYS_0 (for app server tracking) and SESSION_API_KEY (for agent-server)
        env_vars[SESSION_API_KEY_VARIABLE] = session_api_key
        env_vars['SESSION_API_KEY'] = session_api_key  # Agent-server expects this variable name

        # Determine webhook callback hostname
        # Priority: 1. SANDBOX_API_HOSTNAME env var (for container-to-container communication)
        #          2. host.docker.internal (default for host network access)
        # When containers are in the same Docker network, use the container name (e.g., 'db-tars')
        # instead of 'host.docker.internal' for better reliability
        webhook_hostname = os.getenv('SANDBOX_API_HOSTNAME', 'host.docker.internal')

        env_vars[WEBHOOK_CALLBACK_VARIABLE] = (
            f'http://{webhook_hostname}:{self.host_port}/api/v1/webhooks'
        )

        # Set CORS origins for remote browser access when web_url is configured.
        # This allows the agent-server container to accept requests from the
        # frontend when running OpenHands on a remote machine.
        if self.web_url:
            env_vars[ALLOW_CORS_ORIGINS_VARIABLE] = self.web_url

        # Prepare port mappings and add port environment variables
        # When using host network, container ports are directly accessible on the host
        # so we use the container ports directly instead of mapping to random host ports
        port_mappings: dict[int, int] | None = None
        if self.use_host_network:
            # Host network mode: container ports are directly accessible
            for exposed_port in self.exposed_ports:
                env_vars[exposed_port.name] = str(exposed_port.container_port)
        else:
            # Bridge network mode: map container ports to random host ports
            port_mappings = {}
            for exposed_port in self.exposed_ports:
                host_port = self._find_unused_port()
                port_mappings[exposed_port.container_port] = host_port
                env_vars[exposed_port.name] = str(host_port)

        # Prepare labels with Traefik configuration
        labels = {
            'sandbox_spec_id': sandbox_spec.id,
        }
        traefik_labels = self.generate_traefik_labels(container_name)
        labels.update(traefik_labels)

        # Prepare volumes
        volumes = {
            mount.host_path: {
                'bind': mount.container_path,
                'mode': mount.mode,
            }
            for mount in self.mounts
        }

        # Determine network mode and network name
        network_name = None
        if not self.use_host_network:
            network_name = self.get_traefik_network_name()
            if network_name:
                _logger.info(f'Creating sandbox {container_name} in Traefik network: {network_name}')

        if self.use_host_network:
            _logger.info(f'Starting sandbox {container_name} with host network mode')

        # Prepare run kwargs for network configuration
        # Use the same approach as docker_runtime.py: set network_mode or network directly
        run_kwargs: dict[str, typing.Any] = {}
        if self.use_host_network:
            run_kwargs['network_mode'] = 'host'
        elif network_name:
            # Create container directly in Traefik network (like docker_runtime.py does)
            run_kwargs['network'] = network_name

        try:
            # Prepare container run arguments
            # When using host network, don't pass ports parameter (Docker requirement)
            container_run_kwargs: dict[str, typing.Any] = {
                'image': sandbox_spec.id,
                'command': sandbox_spec.command,  # Use default command from image
                'remove': False,
                'name': container_name,
                'environment': env_vars,
                'volumes': volumes,
                'working_dir': sandbox_spec.working_dir,
                'labels': labels,
                'detach': True,
                # Use Docker's tini init process to ensure proper signal handling and reaping of
                # zombie child processes.
                'init': True,
            }

            # Only add ports if not using host network (host network doesn't support port mappings)
            if not self.use_host_network and port_mappings:
                container_run_kwargs['ports'] = port_mappings

            # Allow agent-server containers to resolve host.docker.internal
            # and other custom hostnames for LAN deployments
            # Note: extra_hosts is not needed with host network mode
            if self.extra_hosts and not self.use_host_network:
                container_run_kwargs['extra_hosts'] = self.extra_hosts

            # Add network configuration (network_mode='host' or network=network_name)
            container_run_kwargs.update(run_kwargs)

            # Create and start the container
            # Container is created directly in the Traefik network (if network_name is set)
            # or in host network mode (if use_host_network is True)
            container = self.docker_client.containers.run(  # type: ignore[call-overload]
                **container_run_kwargs,
            )

            # Log network configuration for debugging
            try:
                container.reload()
                actual_network_mode = container.attrs.get('HostConfig', {}).get('NetworkMode', 'default')
                _logger.info(
                    f'Container {container_name} created with network_mode: {actual_network_mode}, '
                    f'expected: {"host" if self.use_host_network else network_name or "bridge"}'
                )
            except Exception as e:
                _logger.warning(f'Failed to reload container {container_name} after creation: {e}')

            # Reload container to get updated status
            try:
                container.reload()
                _logger.info(
                    f'Container {container_name} created with status: {container.status}, '
                    f'network_mode: {container.attrs.get("HostConfig", {}).get("NetworkMode", "default")}'
                )
            except Exception as e:
                _logger.warning(f'Failed to reload container {container_name}: {e}')

            sandbox_info = await self._container_to_sandbox_info(container)
            assert sandbox_info is not None
            _logger.info(
                f'Sandbox {container_name} info: status={sandbox_info.status}, '
                f'exposed_urls={[url.url for url in sandbox_info.exposed_urls] if sandbox_info.exposed_urls else None}'
            )
            return sandbox_info

        except APIError as e:
            error_msg = str(e)
            # Include more details about the error
            if hasattr(e, 'response') and e.response is not None:
                try:
                    error_body = e.response.text if hasattr(e.response, 'text') else str(e.response)
                    error_msg = f'{error_msg} - Response: {error_body[:500]}'
                except Exception:
                    pass
            _logger.error(
                f'Failed to start container {container_name}: {error_msg}',
                exc_info=True,
            )
            raise SandboxError(f'Failed to start container: {error_msg}')

    def get_traefik_network_name(self, use_host_network: bool | None = None) -> str | None:
        """Get the Traefik network name if available.

        Args:
            use_host_network: Whether host network mode is enabled. If None, uses self.use_host_network.

        Returns:
            Network name if found, None otherwise.
        """
        if use_host_network is None:
            use_host_network = self.use_host_network

        if use_host_network:
            return None

        # Check if Traefik network exists and connect container to it
        # Try common Traefik network names, including docker-compose network names
        traefik_networks = [
            'traefik',
            'traefik_default',
            'traefik-traefik',
            'azure-db_default',
            'openhands-default',
            'openhands_default',
        ]
        for network_name in traefik_networks:
            try:
                traefik_network = self.docker_client.networks.get(network_name)
                _logger.info(f'Found Traefik network: {traefik_network.name}')
                return traefik_network.name
            except NotFound:
                continue

        # Try to find Traefik network by inspecting containers with traefik labels
        try:
            all_networks = self.docker_client.networks.list()
            for network in all_networks:
                # Check if this network has containers with traefik labels
                if network.containers:
                    for container_id in network.containers:
                        try:
                            container = self.docker_client.containers.get(container_id)
                            if container.labels and any(
                                key.startswith('traefik.') for key in container.labels.keys()
                            ):
                                _logger.info(f'Found Traefik network via container inspection: {network.name}')
                                return network.name
                        except (NotFound, APIError):
                            continue
        except Exception as e:
            _logger.debug(f'Error searching for Traefik network: {e}')

        _logger.warning('No Traefik network found, container will use default bridge network. Traefik may not be able to access the container.')
        return None

    def generate_traefik_labels(self, container_name: str) -> dict[str, str]:
        """Generate Traefik labels for the container.

        Args:
            container_name: Name of the container.

        Returns:
            Dictionary of Traefik labels.
        """
        base_domain = os.environ.get('VITE_BACKEND_BASE_URL', 'localhost')

        # Get exposed ports from the container configuration
        # We need to determine which ports are exposed
        agent_server_port = 8000
        vscode_port = 8001
        worker_1_port = 8011
        worker_2_port = 8012

        # Find the actual ports from exposed_ports
        for exposed_port in self.exposed_ports:
            if exposed_port.name == AGENT_SERVER:
                agent_server_port = exposed_port.container_port
            elif exposed_port.name == VSCODE:
                vscode_port = exposed_port.container_port
            elif exposed_port.name == WORKER_1:
                worker_1_port = exposed_port.container_port
            elif exposed_port.name == WORKER_2:
                worker_2_port = exposed_port.container_port

        labels = {
            "traefik.enable": "true",
        }

        # Middleware genérico - remove apenas o prefixo do container
        labels[f"traefik.http.middlewares.{container_name}-stripprefix.stripprefix.prefixes"] = f"/{container_name}"

        # Criar rotas baseadas nas portas expostas
        port_configs = [
            (AGENT_SERVER, agent_server_port, container_name, f"/{container_name}/", f"/{container_name}/api/", False),
            (VSCODE, vscode_port, f"{container_name}-vscode", f"/{container_name}/vscode", None, True),
            (WORKER_1, worker_1_port, f"{container_name}-app1", f"/{container_name}/app1", None, False),
            (WORKER_2, worker_2_port, f"{container_name}-app2", f"/{container_name}/app2", None, False),
        ]

        for port_name, port, service_name, path_prefix, additional_route, is_vscode in port_configs:
            # Criar middleware específico para serviços que precisam remover o path completo
            if path_prefix != f"/{container_name}/":
                middleware_name = f"{container_name}-{service_name.split('-')[-1]}-stripprefix"
                labels[f"traefik.http.middlewares.{middleware_name}.stripprefix.prefixes"] = path_prefix
            else:
                middleware_name = f"{container_name}-stripprefix"

            # Router principal
            labels[f"traefik.http.routers.{service_name}.rule"] = f"Host(`{base_domain}`) && PathPrefix(`{path_prefix}`)"
            labels[f"traefik.http.routers.{service_name}.entrypoints"] = "websecure"
            labels[f"traefik.http.routers.{service_name}.tls"] = "true"
            labels[f"traefik.http.routers.{service_name}.tls.certresolver"] = "tlsresolver"
            labels[f"traefik.http.routers.{service_name}.service"] = service_name
            labels[f"traefik.http.routers.{service_name}.middlewares"] = middleware_name

            # Rota adicional (apenas para AGENT_SERVER)
            if additional_route:
                labels[f"traefik.http.routers.{service_name}-api.rule"] = f"Host(`{base_domain}`) && PathPrefix(`{additional_route}`)"
                labels[f"traefik.http.routers.{service_name}-api.entrypoints"] = "websecure"
                labels[f"traefik.http.routers.{service_name}-api.tls"] = "true"
                labels[f"traefik.http.routers.{service_name}-api.tls.certresolver"] = "tlsresolver"
                labels[f"traefik.http.routers.{service_name}-api.service"] = service_name
                labels[f"traefik.http.routers.{service_name}-api.middlewares"] = f"{container_name}-stripprefix"
                labels[f"traefik.http.routers.{service_name}-api.priority"] = "10"

            # Service
            labels[f"traefik.http.services.{service_name}.loadbalancer.server.port"] = str(port)
            labels[f"traefik.http.services.{service_name}.loadbalancer.server.scheme"] = "http"

            # Configurações específicas para VSCode
            if is_vscode:
                labels[f"traefik.http.services.{service_name}.loadbalancer.passHostHeader"] = "true"
                labels[f"traefik.http.services.{service_name}.loadbalancer.responseForwarding.flushInterval"] = "1ms"

        return labels

    async def resume_sandbox(self, sandbox_id: str) -> bool:
        """Resume a paused sandbox."""
        # Enforce sandbox limits by cleaning up old sandboxes
        await self.pause_old_sandboxes(self.max_num_sandboxes - 1)

        try:
            if not sandbox_id.startswith(self.container_name_prefix):
                return False
            container = self.docker_client.containers.get(sandbox_id)

            if container.status == 'paused':
                container.unpause()
            elif container.status == 'exited':
                container.start()

            return True
        except (NotFound, APIError):
            return False

    async def pause_sandbox(self, sandbox_id: str) -> bool:
        """Pause a running sandbox."""
        try:
            if not sandbox_id.startswith(self.container_name_prefix):
                return False
            container = self.docker_client.containers.get(sandbox_id)

            if container.status == 'running':
                container.pause()

            return True
        except (NotFound, APIError):
            return False

    async def delete_sandbox(self, sandbox_id: str) -> bool:
        """Delete a sandbox."""
        try:
            if not sandbox_id.startswith(self.container_name_prefix):
                return False
            container = self.docker_client.containers.get(sandbox_id)

            # Stop the container if it's running
            if container.status in ['running', 'paused']:
                container.stop(timeout=10)

            # Remove the container
            container.remove()

            # Remove associated volume
            try:
                volume_name = f'openhands-workspace-{sandbox_id}'
                volume = self.docker_client.volumes.get(volume_name)
                volume.remove()
            except (NotFound, APIError):
                # Volume might not exist or already removed
                pass

            return True
        except (NotFound, APIError):
            return False


class DockerSandboxServiceInjector(SandboxServiceInjector):
    """Dependency injector for docker sandbox services."""

    container_url_pattern: str = Field(
        default='http://localhost:{port}',
        description=(
            'URL pattern for exposed sandbox ports. Use {port} as placeholder. '
            'For remote access, set to your server IP (e.g., http://192.168.1.100:{port}). '
            'Configure via OH_SANDBOX_CONTAINER_URL_PATTERN environment variable.'
        ),
    )
    host_port: int = Field(
        default=3000,
        description=(
            'The port on which the main OpenHands app server is running. '
            'Used for webhook callbacks from agent-server containers. '
            'If running OpenHands on a non-default port, set this to match. '
            'Configure via OH_SANDBOX_HOST_PORT environment variable.'
        ),
    )
    container_name_prefix: str = 'oh-agent-server-'
    max_num_sandboxes: int = Field(
        default=5,
        description='Maximum number of sandboxes allowed to run simultaneously',
    )
    mounts: list[VolumeMount] = Field(default_factory=list)
    exposed_ports: list[ExposedPort] = Field(
        default_factory=lambda: [
            ExposedPort(
                name=AGENT_SERVER,
                description=(
                    'The port on which the agent server runs within the container'
                ),
                container_port=8000,
            ),
            ExposedPort(
                name=VSCODE,
                description=(
                    'The port on which the VSCode server runs within the container'
                ),
                container_port=8001,
            ),
            ExposedPort(
                name=WORKER_1,
                description=(
                    'The first port on which the agent should start application servers.'
                ),
                container_port=8011,
            ),
            ExposedPort(
                name=WORKER_2,
                description=(
                    'The first port on which the agent should start application servers.'
                ),
                container_port=8012,
            ),
        ]
    )
    health_check_path: str | None = Field(
        default='/health',
        description=(
            'The url path in the sandbox agent server to check to '
            'determine whether the server is running'
        ),
    )
    extra_hosts: dict[str, str] = Field(
        default_factory=lambda: {'host.docker.internal': 'host-gateway'},
        description=(
            'Extra hostname mappings to add to agent-server containers. '
            'This allows containers to resolve hostnames like host.docker.internal '
            'for LAN deployments and MCP connections. '
            'Format: {"hostname": "ip_or_gateway"}'
        ),
    )
    startup_grace_seconds: int = Field(
        default=STARTUP_GRACE_SECONDS,
        description=(
            'Number of seconds were no response from the agent server is acceptable'
            'before it is considered an error'
        ),
    )
    use_host_network: bool = Field(
        default=os.getenv('SANDBOX_USE_HOST_NETWORK', '').lower()
        in (
            'true',
            '1',
            'yes',
        ),
        description=(
            'Whether to use host networking mode for sandbox containers. '
            'When enabled, containers share the host network namespace, '
            'making all container ports directly accessible on the host. '
            'This is useful for reverse proxy setups where dynamic port mapping '
            'is problematic. Configure via OH_SANDBOX_USE_HOST_NETWORK environment variable.'
        ),
    )

    async def inject(
        self, state: InjectorState, request: Request | None = None
    ) -> AsyncGenerator[SandboxService, None]:
        # Define inline to prevent circular lookup
        from openhands.app_server.config import (
            get_global_config,
            get_httpx_client,
            get_sandbox_spec_service,
        )

        # Get web_url from global config for CORS support
        config = get_global_config()
        web_url = config.web_url

        async with (
            get_httpx_client(state) as httpx_client,
            get_sandbox_spec_service(state) as sandbox_spec_service,
        ):
            yield DockerSandboxService(
                sandbox_spec_service=sandbox_spec_service,
                container_name_prefix=self.container_name_prefix,
                host_port=self.host_port,
                container_url_pattern=self.container_url_pattern,
                mounts=self.mounts,
                exposed_ports=self.exposed_ports,
                health_check_path=self.health_check_path,
                httpx_client=httpx_client,
                max_num_sandboxes=self.max_num_sandboxes,
                web_url=web_url,
                extra_hosts=self.extra_hosts,
                startup_grace_seconds=self.startup_grace_seconds,
                use_host_network=self.use_host_network,
            )
