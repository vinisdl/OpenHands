def get_runtime_dns_url(container_name: str, port: int = None) -> str:
    host = f"{container_name}.tars.dbserver.com.br"
    return f"https://{host}" if port is None else f"https://{host}:{port}"