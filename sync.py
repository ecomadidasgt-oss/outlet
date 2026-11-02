"""Reconcile one manual VTEX collection with storefront discounts."""
import argparse
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class SyncError(Exception):
    pass


class Catalog:
    def __init__(self, account, channel):
        if not re.fullmatch(r"[a-z0-9-]+", account):
            raise SyncError("Nombre de cuenta inválido.")
        self.base = f"https://{account}.vtexcommercestable.com.br"
        self.channel = channel
        self.key = os.environ.get("VTEX_APP_KEY", "")
        self.token = os.environ.get("VTEX_APP_TOKEN", "")
        if not self.key or not self.token:
            raise SyncError("Configura VTEX_APP_KEY y VTEX_APP_TOKEN en el entorno del servidor.")

    def call(self, path, method="GET", body=None, private=True):
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if private:
            headers.update({"X-VTEX-API-AppKey": self.key, "X-VTEX-API-AppToken": self.token})
        data = None if body is None else json.dumps(body).encode()
        for attempt in range(5):
            time.sleep(.15)
            try:
                with urlopen(Request(self.base + path, data=data, headers=headers, method=method), timeout=45) as response:
                    result = response.read()
                    return json.loads(result) if result else None
            except HTTPError as error:
                if error.code in (429, 500, 502, 503, 504) and attempt < 4:
                    try:
                        delay = float(error.headers.get("Retry-After", 2 ** attempt))
                    except ValueError:
                        delay = 2 ** attempt
                    time.sleep(min(max(delay, 1), 60))
                    continue
                raise SyncError(f"VTEX respondió HTTP {error.code} en {method} {path.split('?')[0]}.") from None
            except (URLError, TimeoutError, ValueError):
                if attempt < 4:
                    time.sleep(2 ** attempt)
                    continue
                raise SyncError("No se pudo completar una consulta a VTEX; ejecución interrumpida.") from None

    def products(self):
        products = {}
        total = None
        start = 1
        while True:
            payload = self.call(f"/api/catalog_system/pvt/products/GetProductAndSkuIds?_from={start}&_to={start + 249}")
            if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
                raise SyncError("Formato inesperado del catálogo.")
            reported = int(payload["range"]["total"])
            if total is not None and reported != total:
                raise SyncError("El catálogo cambió durante el escaneo; vuelve a ejecutar.")
            total = reported
            batch = payload["data"]
            for product, skus in batch.items():
                products[int(product)] = {int(sku) for sku in skus}
            if len(products) >= total:
                break
            if not batch:
                raise SyncError("Catálogo incompleto; no se modificarán asociaciones.")
            start += 250
        if len(products) != total or not products:
            raise SyncError("No se obtuvo un catálogo completo y no vacío.")
        return products

    def members(self, collection, group):
        collection_info = self.call(f"/api/catalog/pvt/collection/{collection}")
        if str(collection_info.get("Type", "")).strip().lower() != "manual":
            raise SyncError("La colección no es manual. Revisa sus reglas antes de migrarla.")
        groups = self.call(f"/api/catalog/pvt/collection/{collection}/subcollection")
        if not isinstance(groups, list) or len(groups) != 1:
            raise SyncError("Esta herramienta requiere una colección con un único grupo manual inclusivo. Revisa sus reglas antes de migrarla.")
        info = self.call(f"/api/catalog/pvt/subcollection/{group}")
        if (int(info["CollectionId"]) != collection or info["Type"] != "Inclusive"
                or info.get("PreSale") or info.get("Release")):
            raise SyncError("El grupo no es un grupo manual inclusivo de la colección indicada.")
        if int(groups[0]["Id"]) != group:
            raise SyncError("El grupo indicado no coincide con el de la colección.")
        # VTEX reports automatic/hybrid rules through the collection Type.
        # The brand/category association endpoints do not document GET requests.
        members = set()
        page = 1
        total = None
        rows = 0
        while True:
            result = self.call(f"/api/catalog/pvt/collection/{collection}/products?page={page}&pageSize=100")
            if not isinstance(result, dict) or not isinstance(result.get("Data"), list):
                raise SyncError("Formato inesperado de los miembros de la colección.")
            reported = int(result["TotalRows"])
            if total is not None and reported != total:
                raise SyncError("La colección cambió durante la lectura.")
            total = reported
            for item in result["Data"]:
                if int(item["SubCollectionId"]) != group:
                    raise SyncError("Hay miembros procedentes de otro grupo.")
                members.add(int(item["SkuId"]))
                rows += 1
            if rows == total:
                break
            if rows > total or not result["Data"]:
                raise SyncError("Lectura incompleta de la colección.")
            page += 1
        return members, collection_info.get("Name")

    def discounted(self, product, expected_skus):
        params = urlencode({"fq": f"productId:{product}", "sc": self.channel})
        result = self.call(f"/api/catalog_system/pub/products/search?{params}", private=False)
        if not isinstance(result, list):
            raise SyncError("Formato inesperado de precios de tienda.")
        matching = [item for item in result if int(item["productId"]) == product]
        if len(matching) != 1:
            return None  # Not visible in this channel: preserve current membership.
        known = False
        uncertain = False
        items = matching[0].get("items", [])
        if not expected_skus.issubset({int(sku["itemId"]) for sku in items}):
            uncertain = True
        for sku in items:
            own_sellers = [seller for seller in sku.get("sellers", []) if str(seller.get("sellerId")) == "1"]
            if not own_sellers:
                uncertain = True
            for seller in own_sellers:
                offer = seller.get("commertialOffer", {})
                try:
                    price = Decimal(str(offer.get("Price")))
                    original = Decimal(str(offer.get("ListPrice")))
                    if not price.is_finite() or not original.is_finite():
                        uncertain = True
                        continue
                except InvalidOperation:
                    uncertain = True
                    continue
                if price > 0 and original > 0:
                    known = True
                    if original > price:
                        return True
                else:
                    uncertain = True
        return False if known and not uncertain else None


def save(path, data):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def run(args):
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    lock = output / "sync.lock"
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise SyncError("Otra ejecución está activa o dejó sync.lock tras interrumpirse; revisa el proceso antes de quitarlo.") from None
    os.close(descriptor)
    try:
        catalog = Catalog(args.account, args.channel)
        current, name = catalog.members(args.collection, args.group)
        products = catalog.products()
        wanted = set(current)
        unknown = []
        for index, (product, skus) in enumerate(sorted(products.items()), 1):
            discount = catalog.discounted(product, skus)
            if discount is True:
                wanted.update(skus)
            elif discount is False:
                wanted.difference_update(skus)
            else:
                unknown.append(product)
            if index % 100 == 0:
                print(f"Consultados {index}/{len(products)} productos.", flush=True)
        additions = sorted(wanted - current)
        removals = sorted(current - wanted)
        report = {
            "createdAt": datetime.now(timezone.utc).isoformat(),
            "account": args.account, "collectionId": args.collection,
            "collectionName": name, "subCollectionId": args.group,
            "salesChannel": args.channel, "criterion": "any own-seller SKU with 0 < Price < ListPrice",
            "mode": "apply" if args.apply else "report-only",
            "productsScanned": len(products), "unknownProductsPreserved": unknown,
            "addSkuIds": additions, "removeSkuIds": removals, "completed": [],
        }
        report_path = output / f"plan-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}.json"
        save(report_path, report)
        print(f"Propuesta: añadir {len(additions)} SKU y retirar {len(removals)} SKU. Informe: {report_path}")
        if not args.apply:
            return
        if current and len(removals) / len(current) > .25 and not args.allow_large_removal:
            raise SyncError("La baja supera el 25% de la colección. Revisa el informe antes de usar --allow-large-removal.")
        latest, _ = catalog.members(args.collection, args.group)
        if latest != current:
            raise SyncError("Los miembros cambiaron durante el escaneo; no se aplicará el plan.")
        for action, skus in (("add", additions), ("remove", removals)):
            for sku in skus:
                path = f"/api/catalog/pvt/subcollection/{args.group}/stockkeepingunit"
                if action == "add":
                    catalog.call(path, "POST", {"SkuId": sku})
                else:
                    catalog.call(f"{path}/{sku}", "DELETE")
                report["completed"].append({"action": action, "skuId": sku})
                save(report_path, report)
        report["finishedAt"] = datetime.now(timezone.utc).isoformat()
        save(report_path, report)
        print("Sincronización completada. La tienda puede tardar en reflejar la indexación.")
    finally:
        lock.unlink(missing_ok=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", default="adidasgt")
    parser.add_argument("--collection", type=int, default=138)
    parser.add_argument("--group", type=int, required=True)
    parser.add_argument("--channel", type=int, required=True)
    parser.add_argument("--output", default=str(Path(__file__).parent / "reports"))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--allow-large-removal", action="store_true")
    try:
        run(parser.parse_args())
    except (SyncError, KeyError, TypeError, ValueError, OSError) as error:
        message = str(error) if isinstance(error, SyncError) else "Formato o estado local inesperado; revisa el informe."
        print(f"Error: {message}", file=sys.stderr)
        sys.exit(1)
