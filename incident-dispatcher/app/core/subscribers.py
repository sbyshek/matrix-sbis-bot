def subscriber_key(raw: str) -> str:
    """
    Возвращает ключ уникальности абонента.

    Примеры:
        "89234743710:ACX:BCX" -> "89234743710"
        "4299:IT:Zavod"       -> "4299"
    """
    first_part = str(raw).split(":", 1)[0].strip()
    digits = "".join(ch for ch in first_part if ch.isdigit())

    return digits or str(raw).strip()


def unique_subscribers(subscribers: list[str]) -> list[str]:
    """
    Возвращает уникальных абонентов, сохраняя порядок.
    """
    seen = {}

    for raw in subscribers:
        raw = str(raw).strip()

        if not raw:
            continue

        key = subscriber_key(raw)

        if key not in seen:
            seen[key] = raw

    return list(seen.values())