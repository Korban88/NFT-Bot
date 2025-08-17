# ... всё то же самое сверху без изменений ...

async def _fetch_from_tonapi() -> List[Dict[str, Any]]:
    if not TON_COLLECTIONS:
        return []
    headers = {}
    if getattr(settings, "TONAPI_KEY", None):
        headers["Authorization"] = f"Bearer {settings.TONAPI_KEY}"

    rows: List[Dict[str, Any]] = []
    try:
        async with httpx.AsyncClient(timeout=20.0, headers=headers) as client:
            total_cap = 200
            for coll_addr in TON_COLLECTIONS:
                if total_cap <= 0:
                    break
                items = await _fetch_collection_items(client, coll_addr)
                for it in items:
                    sale = it.get("sale") or it.get("marketplace")
                    if not isinstance(sale, dict):
                        continue

                    addr = it.get("address") or ""
                    if not addr:
                        continue

                    price_ton = _extract_sale_price_ton(sale)

                    img = None
                    previews = it.get("previews") or []
                    if isinstance(previews, list) and previews:
                        img = previews[-1].get("url") or previews[0].get("url")

                    name = (it.get("metadata") or {}).get("name") or addr
                    ts = _parse_iso_ts(sale.get("created_at")) or _parse_iso_ts(it.get("created_at")) \
                         or int(_now_utc().timestamp())

                    url_view = _tonviewer_url(addr)
                    gg_url = None
                    market_name = (sale.get("marketplace") or {}).get("name") if isinstance(sale.get("marketplace"), dict) else sale.get("market") or sale.get("name")
                    if isinstance(market_name, str) and "getgems" in market_name.lower():
                        gg_url = _getgems_url(addr)

                    rows.append({
                        "name": name,
                        "collection": coll_addr,
                        "nft_address": addr,
                        "price_ton": price_ton,
                        "floor_ton": None,
                        "median_ton": None,
                        "discount_floor": None,
                        "discount_med": None,
                        "timestamp": ts,
                        "url": url_view,
                        "gg_url": gg_url,
                        "image": img,
                    })
                    total_cap -= 1
                    if total_cap <= 0:
                        break
    except Exception:
        return []

    # 🔥 фикс: считаем floor и median правильно
    by_coll: Dict[str, List[float]] = {}
    for x in rows:
        p = _safe_decimal(x.get("price_ton"))
        if p and p > 0:
            by_coll.setdefault(x["collection"], []).append(float(p))

    floors: Dict[str, float] = {c: min(v) for c, v in by_coll.items() if v}
    medians: Dict[str, float] = {c: _median(v) for c, v in by_coll.items() if v}

    for x in rows:
        coll = x["collection"]
        floor = floors.get(coll)
        med = medians.get(coll)
        if floor:
            x["floor_ton"] = floor
        if med:
            x["median_ton"] = med

        price = x.get("price_ton")
        if price and floor:
            try:
                x["discount_floor"] = _pct(float(price), float(floor))
            except Exception:
                pass
        if price and med:
            try:
                x["discount_med"] = _pct(float(price), float(med))
            except Exception:
                pass

    return rows


# ======== Рендер карточки ========
def _item_caption(it: Dict[str, Any]) -> str:
    name = it.get("name") or "—"
    coll = it.get("collection") or "—"
    price = it.get("price_ton")
    floor = it.get("floor_ton")
    median = it.get("median_ton")
    disc_floor = it.get("discount_floor")
    disc_med = it.get("discount_med")

    lines = [f"🔥 {name}", f"Коллекция: {coll}"]

    if price is not None:
        lines.append(f"Цена: {float(price):.3f} TON")
    if floor is not None and floor > 0:
        lines.append(f"Floor: {float(floor):.3f} TON")
    if median is not None and median > 0:
        lines.append(f"Медиана: {float(median):.3f} TON")

    if disc_floor is not None:
        lines.append(f"Скидка к floor: {float(disc_floor):.1f}%")
    if disc_med is not None:
        lines.append(f"Скидка к медиане: {float(disc_med):.1f}%")

    return "\n".join(lines)
