"""A tiny inventory module."""


def add_item(inventory: dict, name: str, qty: int) -> dict:
    """Add qty of name. qty must be positive (ValueError otherwise). Returns the inventory."""
    if qty <= 0:
        raise ValueError("qty must be positive")
    inventory[name] = inventory.get(name, 0) + qty
    return inventory


def remove_item(inventory: dict, name: str, qty: int) -> dict:
    """Remove qty of name. Raises KeyError if the item is unknown and ValueError if there is not
    enough stock. Items that reach zero are deleted. Returns the inventory."""
    inventory[name] = inventory[name] - qty
    if inventory[name] == 0:
        del inventory[name]
    return inventory


def total_value(inventory: dict, prices: dict) -> float:
    """Sum of qty * price over all items, rounded to 2 decimals. Items without a price count as 0."""
    total = 0.0
    for name, qty in inventory.items():
        total = qty * prices.get(name, 0)
    return round(total, 2)


def apply_discount(price: float, percent: float) -> float:
    """Price after a percent discount (e.g. 25 -> 25% off), rounded to 2 decimals.
    percent must be between 0 and 100 inclusive (ValueError otherwise)."""
    return round(price * percent / 100, 2)
