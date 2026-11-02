"""Print the VTEX identifiers needed to configure Outlet; never writes to VTEX."""
import json
import sys

from sync import Catalog, SyncError


try:
    catalog = Catalog("adidasgt", channel=1)
    collection = catalog.call("/api/catalog/pvt/collection/138")
    groups = catalog.call("/api/catalog/pvt/collection/138/subcollection")
    channels = catalog.call("/api/catalog_system/pvt/saleschannel/list")
    if not isinstance(groups, list) or not isinstance(channels, list):
        raise SyncError("VTEX devolvió un formato inesperado. No se pudo identificar la configuración.")
    summary = {
        "collection": {key: collection.get(key) for key in ("Id", "Name", "Type", "TotalProducts")},
        "groups": [{key: group.get(key) for key in ("Id", "Name", "CollectionId", "Type", "PreSale", "Release")} for group in groups],
        "salesChannels": [{key: channel.get(key) for key in ("Id", "Name", "IsActive")} for channel in channels],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print("Consulta completada. Elige el canal real de la web; no se ha supuesto el canal 1.")
    print("CollectionId=138 no es el OUTLET_GROUP_ID. Revisa los grupos antes de configurar la sincronización.")
except (SyncError, KeyError, TypeError, ValueError, OSError) as error:
    message = str(error) if isinstance(error, SyncError) else "No se pudo leer la configuración."
    print(f"Error: {message}", file=sys.stderr)
    sys.exit(1)
