from typing import Any, Dict


async def service(config: Dict[str, Any], data: Dict[str, Any]):
    data.update(config=config)
    return data
