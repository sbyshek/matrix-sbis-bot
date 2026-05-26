# models.py
import uuid
from typing import List, Dict, Optional
from enum import Enum
from pydantic import BaseModel, Field, model_validator
import re

class WeightUnit(str, Enum):
    g = "г"
    ml = "мл"
    pack = "пак"
    piece = "шт"

class Category(str, Enum):
    salad = "Салаты"
    main_courses = "Первые блюда"
    second_courses = "Вторые блюда"
    garnishes = "Гарниры"
    bread = "Хлеб"
    souses = "Соусы"
    desserts = "Десерты"
    drinks = "Напитки"
    unknown = ".."

    @property
    def order_index(self) -> int:
        return {
            'Салаты': 1, 'Первые блюда': 2, 'Вторые блюда': 3,
            'Гарниры': 4, 'Хлеб': 5, 'Соусы': 6,
            'Десерты': 7, 'Напитки': 8, '..': 100
        }.get(self.value, 100)

class MealTime(str, Enum):
    breakfast = "Завтрак"
    lunch = "Обед"

class Dish(BaseModel):
    uuid: str = Field(default_factory=lambda: uuid.uuid4().hex)
    name: str
    price: float = Field(gt=0)
    weight: int = Field(default=1, gt=0)
    weight_unit: WeightUnit = WeightUnit.piece
    category: Category = Category.unknown

class Menu(BaseModel):
    time: MealTime
    dishes: List[Dish] = []

    @model_validator(mode="before")
    @classmethod
    def sort_dishes(cls, data: dict) -> dict:
        if "dishes" in data:
            data["dishes"] = sorted(data["dishes"], key=lambda d: d.category.order_index if hasattr(d, "category") else 100)
        return data

class FullMenu(BaseModel):
    breakfast: Menu = Field(default_factory=lambda: Menu(time=MealTime.breakfast))
    lunch: Menu = Field(default_factory=lambda: Menu(time=MealTime.lunch))
    valid_date: Optional[str] = None

class CartItem(BaseModel):
    dish: Dish
    count: int = 1

class CartState(BaseModel):
    mxid: str
    items: List[CartItem] = []
    confirmed: bool = False
    done: bool = False
    order_uuid: str = Field(default_factory=lambda: uuid.uuid4().hex)

    @property
    def total_price(self) -> float:
        return sum(item.dish.price * item.count for item in self.items)