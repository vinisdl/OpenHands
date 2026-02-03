# Usar variável de ambiente DOCKER_REGISTRY se definida, caso contrário usar ghcr.io
# Não sobrescrever se já estiver definida como variável de ambiente
if [[ -z "${DOCKER_REGISTRY:-}" ]]; then
  DOCKER_REGISTRY=ghcr.io
fi
DOCKER_ORG=openhands
DOCKER_IMAGE=openhands
DOCKER_BASE_DIR="."
