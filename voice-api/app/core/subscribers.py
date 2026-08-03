"""
Модуль для работы с абонентами (subscribers).

Формат строки: "телефон[:код[:описание]]"

Примеры:
  - "79001112233"                          -> только номер
  - "4299:it_remote"                       -> номер + код роли
  - "79001112233:boss:Иванов И.И."         -> номер + код + описание
  - "79004445566:security:Охрана поста №1" -> полный формат
"""

from typing import List, Optional
from pydantic import BaseModel, Field


class Subscriber(BaseModel):
    """Представление абонента для оповещения"""
    
    number: str = Field(..., description="Телефонный номер (только цифры)")
    code: str = Field(default="", description="Код роли (boss, security, it_remote)")
    description: str = Field(default="", description="Человекочитаемое описание")
    
    @property
    def display_name(self) -> str:
        """
        Человекочитаемое имя для UI и логов.
        Приоритет: описание > код > номер
        """
        if self.description:
            return f"{self.description} ({self.number})"
        if self.code:
            return f"{self.code} ({self.number})"
        return self.number
    
    @property
    def is_internal(self) -> bool:
        """
        Эвристика: короткий номер (≤5 цифр) считается внутренним.
        Для точного определения нужно сверяться с конфигом узла.
        """
        return len(self.number) <= 5 and self.number.isdigit()
    
    def __str__(self) -> str:
        """Форматирование для логов"""
        return self.display_name
    
    def __repr__(self) -> str:
        return f"Subscriber(number='{self.number}', code='{self.code}', description='{self.description}')"


def parse_subscriber(raw: str) -> Subscriber:
    """
    Парсит строку формата 'number[:code[:description]]' в объект Subscriber.
    
    Args:
        raw: Строка в формате "79001112233:boss:Иванов И.И."
    
    Returns:
        Объект Subscriber
    
    Raises:
        ValueError: Если строка пустая или не содержит цифр
    
    Examples:
        >>> parse_subscriber("79001112233")
        Subscriber(number='79001112233', code='', description='')
        
        >>> parse_subscriber("4299:it_remote")
        Subscriber(number='4299', code='it_remote', description='')
        
        >>> parse_subscriber("79001112233:boss:Иванов И.И.")
        Subscriber(number='79001112233', code='boss', description='Иванов И.И.')
    """
    if not raw or not raw.strip():
        raise ValueError("Empty subscriber string")
    
    # Разделяем по ":", максимум 3 части
    parts = raw.strip().split(":", 2)
    
    # Извлекаем только цифры из первой части (номера)
    number = ''.join(filter(str.isdigit, parts[0]))
    
    if not number:
        raise ValueError(f"No digits found in subscriber string: '{raw}'")
    
    # Код роли (вторая часть, если есть)
    code = parts[1].strip() if len(parts) > 1 else ""
    
    # Описание (третья часть, если есть)
    description = parts[2].strip() if len(parts) > 2 else ""
    
    return Subscriber(
        number=number,
        code=code,
        description=description
    )


def format_subscriber(sub: Subscriber) -> str:
    """
    Обратное преобразование: Subscriber -> строка.
    
    Args:
        sub: Объект Subscriber
    
    Returns:
        Строка в формате "number:code:description"
    
    Example:
        >>> sub = Subscriber(number="79001112233", code="boss", description="Иванов")
        >>> format_subscriber(sub)
        '79001112233:boss:Иванов'
    """
    parts = [sub.number]
    if sub.code:
        parts.append(sub.code)
    if sub.description:
        parts.append(sub.description)
    return ":".join(parts)


def parse_subscribers_list(raw_list: List[str]) -> List[Subscriber]:
    """
    Парсит список строк в список объектов Subscriber.
    Невалидные записи пропускаются с предупреждением.
    
    Args:
        raw_list: Список строк в формате ["79001112233:boss:Иванов", "4299:it"]
    
    Returns:
        Список валидных объектов Subscriber
    
    Example:
        >>> parse_subscribers_list(["79001112233", "invalid", "4299:boss"])
        [Subscriber(number='79001112233', ...), Subscriber(number='4299', code='boss', ...)]
    """
    result = []
    for raw in raw_list:
        try:
            subscriber = parse_subscriber(raw)
            result.append(subscriber)
        except ValueError as e:
            # Логируем предупреждение, но не ломаем весь список
            import logging
            logger = logging.getLogger("voice-api.subscribers")
            logger.warning(f"⚠️ Skipping invalid subscriber '{raw}': {e}")
    return result


def filter_by_code(subscribers: List[Subscriber], code: str) -> List[Subscriber]:
    """
    Фильтрует список абонентов по коду роли.
    
    Args:
        subscribers: Список абонентов
        code: Код для фильтрации (например, "boss", "security")
    
    Returns:
        Отфильтрованный список
    
    Example:
        >>> subs = [
        ...     Subscriber(number="79001112233", code="boss"),
        ...     Subscriber(number="4299", code="it"),
        ...     Subscriber(number="79004445566", code="security")
        ... ]
        >>> filter_by_code(subs, "boss")
        [Subscriber(number='79001112233', code='boss', ...)]
    """
    return [sub for sub in subscribers if sub.code == code]


def group_by_code(subscribers: List[Subscriber]) -> dict:
    """
    Группирует абонентов по коду роли.
    
    Args:
        subscribers: Список абонентов
    
    Returns:
        Словарь {код: [список абонентов]}
    
    Example:
        >>> subs = [
        ...     Subscriber(number="79001112233", code="boss"),
        ...     Subscriber(number="4299", code="it"),
        ...     Subscriber(number="79004445566", code="security"),
        ...     Subscriber(number="4102", code="it")
        ... ]
        >>> group_by_code(subs)
        {
            'boss': [Subscriber(...)],
            'it': [Subscriber(...), Subscriber(...)],
            'security': [Subscriber(...)]
        }
    """
    from collections import defaultdict
    groups = defaultdict(list)
    for sub in subscribers:
        key = sub.code if sub.code else "unknown"
        groups[key].append(sub)
    return dict(groups)


# =============================================================================
# Тесты (можно запустить: python -m app.core.subscribers)
# =============================================================================

if __name__ == "__main__":
    # Тест 1: Парсинг разных форматов
    print("=== Тест 1: Парсинг ===")
    test_cases = [
        "79001112233",
        "4299:it_remote",
        "79001112233:boss:Иванов И.И.",
        "79004445566:security:Охрана поста №1",
        "  4102 : warehouse_duty : Дежурный склада  ",  # с пробелами
    ]
    
    for raw in test_cases:
        sub = parse_subscriber(raw)
        print(f"  '{raw}' -> {sub}")
        print(f"    display_name: {sub.display_name}")
        print(f"    is_internal: {sub.is_internal}")
        print()
    
    # Тест 2: Обратное форматирование
    print("=== Тест 2: Форматирование ===")
    sub = Subscriber(number="79001112233", code="boss", description="Иванов И.И.")
    formatted = format_subscriber(sub)
    print(f"  {sub} -> '{formatted}'")
    print()
    
    # Тест 3: Парсинг списка
    print("=== Тест 3: Список ===")
    raw_list = [
        "79001112233:boss:Иванов",
        "4299:it_remote",
        "invalid_string",  # будет пропущено
        "79004445566:security",
    ]
    subscribers = parse_subscribers_list(raw_list)
    print(f" Parsed {len(subscribers)} subscribers:")
    for sub in subscribers:
        print(f"    - {sub}")
    print()
    
    # Тест 4: Группировка
    print("=== Тест 4: Группировка ===")
    groups = group_by_code(subscribers)
    for code, subs in groups.items():
        print(f"  {code}: {len(subs)} subscribers")
        for sub in subs:
            print(f"    - {sub}")