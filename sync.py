"""Reconcile one manual VTEX collection with storefront discounts."""
import argparse
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import threading
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
        self._request_lock = threading.Lock()
        self._next_request = 0
        self.key = os.environ.get("VTEX_APP_KEY", "")
        self.token = os.environ.get("VTEX_APP_TOKEN", "")
        if not self.key or not self.token:
            raise SyncError("Configura VTEX_APP_KEY y VTEX_APP_TOKEN en el entorno del servidor.")

    def call(self, path, method="GET", body=None, private=True, missing_ok=False):
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if private:
            headers.update({"X-VTEX-API-AppKey": self.key, "X-VTEX-API-AppToken": self.token})
        data = None if body is None else json.dumps(body).encode()
        for attempt in range(5):
            with self._request_lock:
                delay = self._next_request - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                self._next_request = time.monotonic() + .15
            try:
                with urlopen(Request(self.base + path, data=data, headers=headers, method=method), timeout=45) as response:
                    result = response.read()
                    return json.loads(result) if result else None
            except HTTPError as error:
                if missing_ok and method == "DELETE" and error.code == 404:
                    return None
                if error.code in (429, 500, 502, 503, 504) and attempt < 4:
                    try:
                        delay = float(error.headers.get("Retry-After", 2 ** attempt))
                    except ValueError:
                        delay = 2 ** attempt
                    with self._request_lock:
                        self._next_request = max(self._next_request, time.monotonic() + min(max(delay, 1), 60))
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

    def discounted(self, product):
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
        for sku in items:
            own_sellers = [seller for seller in sku.get("sellers", []) if str(seller.get("sellerId")) == "1"]
            if not own_sellers:
                uncertain = True
            for seller in own_sellers:
                offer = seller.get("commertialOffer", {})
                if offer.get("IsAvailable") is False:
                    known = True
                    continue
                try:
                    quantity = Decimal(str(offer.get("AvailableQuantity")))
                    if not quantity.is_finite() or quantity < 0:
                        uncertain = True
                        continue
                except InvalidOperation:
                    uncertain = True
                    continue
                if quantity == 0:
                    known = True
                    continue
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


def scan(catalog, products, args, output):
    identity = {
        "version": 4, "account": args.account, "collection": args.collection,
        "group": args.group, "channel": args.channel,
        "catalogHash": hashlib.sha256(json.dumps(
            [(product, sorted(skus)) for product, skus in sorted(products.items())]
        ).encode()).hexdigest(),
    }
    checkpoint_path = output / "checkpoint.json"
    started = time.time()
    states = {}
    if not args.apply and checkpoint_path.exists():
        try:
            previous = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            candidate = previous["states"]
            age = time.time() - previous["startedAt"]
            valid = (previous["identity"] == identity and 0 <= age < 7200
                     and isinstance(candidate, dict)
                     and all(key.isdigit() and int(key) in products and
                             (type(value) is bool or value is None)
                             for key, value in candidate.items()))
            if valid:
                states = candidate
                started = previous["startedAt"]
                print(f"Retomando {len(states)}/{len(products)} productos del informe reciente.", flush=True)
            else:
                print("El punto de avance venció o pertenece a otro catálogo; se consultará de nuevo.", flush=True)
        except (OSError, ValueError, KeyError, TypeError):
            print("Punto de avance no válido; se consultará de nuevo.", flush=True)
    elif args.apply:
        print("Modo aplicación: se consultarán todos los precios de nuevo, sin reutilizar el informe.", flush=True)

    def persist():
        save(checkpoint_path, {
            "identity": identity, "startedAt": started,
            "updatedAt": time.time(), "states": states,
            "complete": len(states) == len(products),
        })
        save(output / "progress.json", {
            "mode": "apply-scan" if args.apply else "report-only",
            "productsScanned": len(states), "productsTotal": len(products),
            "complete": len(states) == len(products),
            "unknownProducts": sum(value is None for value in states.values()),
            "checkpointContainsObservationsOnly": True,
        })

    persist()
    remaining = iter(product for product in sorted(products) if str(product) not in states)
    with ThreadPoolExecutor(max_workers=4) as executor:
        pending = {}

        def submit_next():
            item = next(remaining, None)
            if item is not None:
                product = item
                pending[executor.submit(catalog.discounted, product)] = product

        for _ in range(4):
            submit_next()
        try:
            while pending:
                done, _ = wait(pending, return_when=FIRST_COMPLETED)
                for future in done:
                    product = pending.pop(future)
                    states[str(product)] = future.result()
                    if len(states) % 25 == 0:
                        persist()
                        print(f"Consultados {len(states)}/{len(products)} productos.", flush=True)
                    submit_next()
        finally:
            for future in pending:
                future.cancel()
            persist()
    print(f"Revisión completa: {len(states)}/{len(products)} productos.", flush=True)
    return states, started


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
        print("Leyendo la colección y sus miembros...", flush=True)
        current, name = catalog.members(args.collection, args.group)
        print("Leyendo el catálogo completo...", flush=True)
        products = catalog.products()
        print(f"Revisando {len(products)} productos con hasta cuatro consultas simultáneas y límite global de peticiones.", flush=True)
        states, scan_started = scan(catalog, products, args, output)
        product_by_sku = {sku: product for product, skus in products.items() for sku in skus}
        def collection_products(members):
            return {product_by_sku[sku] for sku in members if sku in product_by_sku}

        current_products = collection_products(current)
        wanted = set(current_products)
        unknown = []
        for product, skus in sorted(products.items()):
            discount = states[str(product)]
            if discount is True:
                if skus:
                    wanted.add(product)
            elif discount is False:
                wanted.discard(product)
            else:
                unknown.append(product)
        add_products = wanted - current_products
        remove_products = current_products - wanted
        additions = sorted(min(products[product]) for product in add_products)
        removals = sorted(sku for product in remove_products for sku in products[product])
        report = {
            "createdAt": datetime.now(timezone.utc).isoformat(),
            "account": args.account, "collectionId": args.collection,
            "collectionName": name, "subCollectionId": args.group,
            "salesChannel": args.channel,
            "criterion": "any own-seller SKU with AvailableQuantity > 0, IsAvailable != false and 0 < Price < ListPrice",
            "mode": "apply" if args.apply else "report-only",
            "scanStartedAt": datetime.fromtimestamp(scan_started, timezone.utc).isoformat(),
            "complete": True,
            "productsScanned": len(products), "unknownProductsPreserved": unknown,
            "addSkuIds": additions, "removeSkuIds": removals, "completed": [],
            "addProductIds": sorted(add_products), "removeProductIds": sorted(remove_products),
        }
        report_path = output / f"plan-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}.json"
        save(report_path, report)
        print(f"Propuesta: añadir {len(additions)} SKU y retirar {len(removals)} SKU. Informe: {report_path}")
        if not args.apply:
            return
        if current_products and len(remove_products) / len(current_products) > .25 and not args.allow_large_removal:
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
                    catalog.call(f"{path}/{sku}", "DELETE", missing_ok=True)
                report["completed"].append({"action": action, "skuId": sku})
                save(report_path, report)
        verified_skus, _ = catalog.members(args.collection, args.group)
        verified = collection_products(verified_skus)
        report["unverifiedAddProductIds"] = sorted(wanted - verified)
        report["unverifiedRemoveProductIds"] = sorted(verified - wanted)
        if verified != wanted:
            save(report_path, report)
            raise SyncError("La colección aún difiere del plan aplicado. Revisa las asociaciones pendientes en el informe.")
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
